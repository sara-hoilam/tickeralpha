/**
 * politician-trades — one member's disclosed trades over the last 365 days.
 *
 * Why this exists: the stored ledger.congress_trade table is a 90-day full
 * replace (replace_congress_trades deletes everything each run), so a year of
 * history is not in the database and cannot be. FMP's *-trades-by-name feeds
 * carry roughly eight years in a single call, but reaching them needs
 * FMP_API_KEY, which must never be shipped to a browser. So the page calls
 * this, and this calls FMP.
 *
 * Deploy:
 *   supabase functions deploy politician-trades
 * The key is read from the function's own secret store, set separately with
 *   supabase secrets set FMP_API_KEY=...
 * (Render's copy is a different runtime and is not visible here.)
 *
 * This endpoint is public, so it is also a door to the FMP quota. It is kept
 * narrow deliberately: the only input is a member name, the window and the
 * upstream paths are fixed here rather than passed in, and responses carry a
 * cache header so repeat views of the same member are served without a second
 * upstream call. Disclosures move weekly at most, so an hour is generous.
 */

const FMP = "https://financialmodelingprep.com/stable";
const DAYS = 365;
const CACHE_SECONDS = 3600;

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
};

const json = (body: unknown, status = 200, extra: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS, "Content-Type": "application/json", ...extra },
  });

/** Purchase / Sale (Partial) / Exchange -> the buy|sell|other the page reads.
    paintProfile tests side.startsWith("buy"|"sell"), so the words matter. */
function side(type: string): string {
  const t = (type || "").toLowerCase();
  if (t.includes("purchase") || t.includes("buy")) return "buy";
  if (t.includes("sale") || t.includes("sold") || t.includes("sell")) return "sell";
  return type || "";
}

/** Calendar days between the trade and its disclosure -- the "Filed after"
    column. The STOCK Act allows 45; the gap is the interesting part. */
function filedAfter(traded: string, disclosed: string): number | null {
  if (!traded || !disclosed) return null;
  const a = Date.parse(traded), b = Date.parse(disclosed);
  if (!Number.isFinite(a) || !Number.isFinite(b)) return null;
  return Math.max(0, Math.round((b - a) / 86400000));
}

/* ---- SEC Form 4 -----------------------------------------------------------
 * Some people the page lists are not members of Congress and so file no STOCK
 * Act report at all -- the house/senate feeds return nothing for them, and the
 * profile came up empty. Where such a person is instead an officer, director
 * or >10% holder of a listed company, their trades in that company are public
 * as SEC Form 4, and that is a feed we can reach.
 *
 * Donald Trump is the case in hand: not in Congress, but a >10% holder of
 * Trump Media & Technology Group (DJT). Note the shares sit in the Donald J.
 * Trump Revocable Trust, so the reporting name on a filing may be the trust
 * rather than the man -- hence a pattern rather than an equality test.
 *
 * This is deliberately NOT what quiverquant and unusualwhales show for him.
 * Those come from his executive-branch OGE Form 278-T periodic transaction
 * reports (filed by the White House Office, hundreds of positions across
 * equities and municipal bonds). Form 4 is a narrower, different thing: only
 * DJT, only his own filings. See the note in the page for what it covers.
 */
const SEC_INSIDER: Record<string, { symbol: string; company: string; match: RegExp }> = {
  "donald trump": {
    symbol: "DJT",
    company: "Trump Media & Technology Group Corp.",
    match: /trump/i,
  },
};

/** Reduce a name to the key SEC_INSIDER is looked up by. */
const nameKey = (s: string) =>
  s.toLowerCase().replace(/[^a-z ]/g, "").replace(/\s+/g, " ").trim();

/** typeOfOwner -> a short role label, the same shorthand the tables use. */
function ownerLabel(raw: string): string {
  const t = (raw || "").toLowerCase();
  if (t.includes("tenpercent") || t.includes("10 percent") || t.includes("10%")) return "10% Owner";
  if (t.includes("officer")) {
    const m = raw.match(/officer[:\s]+(.+)$/i);
    return m ? m[1].trim().slice(0, 40) : "Officer";
  }
  if (t.includes("director")) return "Director";
  return "Insider";
}

/** One person's Form 4 transactions in one company, over the same window.
 *
 * Open-market only: P is a purchase and S a sale, and every other Form 4 code
 * is compensation plumbing -- M exercises, F tax withholding, A awards, G
 * gifts. The worker applies exactly this rule for the insider tables, and
 * counting an award as a purchase is how a vesting schedule turns into
 * "insider buying" that nobody chose to do. */
async function form4(
  person: string, apiKey: string, cutoff: string,
): Promise<Record<string, unknown>[]> {
  const cfg = SEC_INSIDER[nameKey(person)];
  if (!cfg) return [];
  const u = `${FMP}/insider-trading/search?symbol=${encodeURIComponent(cfg.symbol)}`
          + `&limit=200&apikey=${encodeURIComponent(apiKey)}`;
  let rows: Record<string, unknown>[] = [];
  try {
    const r = await fetch(u);
    if (!r.ok) return [];
    const b = await r.json();
    rows = Array.isArray(b) ? b : [];
  } catch {
    return [];
  }

  return rows
    .filter((r) => cfg.match.test(String(r.reportingName || "")))
    .map((r) => {
      const code = String(r.transactionType || r.acquisitionOrDisposition || "").toUpperCase();
      const buy = code.startsWith("P");
      const sell = code.startsWith("S");
      if (!buy && !sell) return null;
      const shares = Number(r.securitiesTransacted || 0);
      const price = Number(r.price || 0);
      if (!(shares > 0) || !(price > 0)) return null;
      const value = shares * price;
      const traded = String(r.transactionDate || "").slice(0, 10);
      const disclosed = String(r.filingDate || "").slice(0, 10);
      return {
        symbol: cfg.symbol,
        name: cfg.company,
        assetType: "stock",
        side: buy ? "buy" : "sell",
        rawType: code,
        // Form 4 reports an exact figure rather than a band. Handed over as a
        // single dollar string so the page's band() renders and ranks it the
        // same way it does a disclosure bracket, with no second code path.
        amount: `$${Math.round(value).toLocaleString("en-US")}`,
        shares,
        price,
        owner: ownerLabel(String(r.typeOfOwner || "")),
        reportedBy: String(r.reportingName || ""),
        traded,
        disclosed,
        filedAfter: filedAfter(traded, disclosed),
        office: "",
        district: "",
        link: String(r.url || r.link || ""),
        source: "form4",
      } as Record<string, unknown>;
    })
    .filter((t): t is Record<string, unknown> => !!t)
    .filter((t) => String(t.traded || "") >= cutoff);
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });

  const key = Deno.env.get("FMP_API_KEY");
  if (!key) return json({ error: "FMP_API_KEY is not set on this function" }, 500);

  // Accept the name from the query string or a JSON body, so the page can use
  // whichever is convenient.
  let name = new URL(req.url).searchParams.get("name") || "";
  if (!name && req.method === "POST") {
    try { name = ((await req.json()) || {}).name || ""; } catch { /* ignore */ }
  }
  name = String(name).trim();

  // A member's surname, not a free-form passthrough: letters, spaces, hyphens,
  // apostrophes and full stops only. Anything else is refused rather than
  // forwarded, so this cannot be used to probe other FMP endpoints.
  if (!name || name.length > 60 || !/^[A-Za-z][A-Za-z .'\-]*$/.test(name)) {
    return json({ error: "name must be a member name, 1-60 letters" }, 400);
  }

  const cutoff = new Date(Date.now() - DAYS * 86400000).toISOString().slice(0, 10);

  // Both chambers: a name is in one or the other, and asking both costs one
  // extra request rather than making the caller know which.
  const paths = ["house-trades-by-name", "senate-trades-by-name"];
  const settled = await Promise.all(paths.map(async (p) => {
    const u = `${FMP}/${p}?name=${encodeURIComponent(name)}&apikey=${encodeURIComponent(key)}`;
    try {
      const r = await fetch(u);
      if (!r.ok) return [] as unknown[];
      const b = await r.json();
      return Array.isArray(b) ? b : [];
    } catch {
      return [] as unknown[];
    }
  }));

  // SEC Form 4, for a person who files no STOCK Act report but is an insider
  // of a listed company. Runs alongside the chamber feeds rather than only on
  // their failure: a person could in principle have both.
  const sec = await form4(name, key, cutoff);

  const seen = new Set<string>();
  const trades = settled.flat()
    .map((r: Record<string, unknown>) => {
      const traded = String(r.transactionDate || "");
      const disclosed = String(r.disclosureDate || "");
      return {
        symbol: String(r.symbol || "").toUpperCase(),
        name: String(r.assetDescription || ""),
        assetType: String(r.assetType || ""),
        side: side(String(r.type || "")),
        rawType: String(r.type || ""),
        amount: String(r.amount || ""),          // a bracket, e.g. "$1,001 - $15,000"
        owner: String(r.owner || ""),
        traded,
        disclosed,
        filedAfter: filedAfter(traded, disclosed),
        office: String(r.office || ""),
        district: String(r.district || ""),
        link: String(r.link || ""),
      };
    })
    // Only rows inside the window, and only rows naming a ticker -- the feeds
    // carry municipal bonds and private funds with no symbol, which the table
    // cannot link anywhere.
    .filter((t) => t.symbol && t.traded && t.traded >= cutoff)
    .filter((t) => {
      const k = `${t.symbol}|${t.traded}|${t.amount}|${t.owner}|${t.rawType}`;
      if (seen.has(k)) return false;            // the same filing in both feeds
      seen.add(k);
      return true;
    });

  const all = [...trades, ...sec]
    .sort((a, b) => String(b.traded || "").localeCompare(String(a.traded || "")));

  // Which feeds actually produced something, so the page can say where a
  // profile's rows came from instead of implying every name is a legislator.
  const sources: string[] = [];
  if (trades.length) sources.push("congress");
  if (sec.length) sources.push("form4");

  return json(
    {
      person: name, days: DAYS, since: cutoff,
      count: all.length, sources, trades: all,
      congressCount: trades.length, form4Count: sec.length,
      form4Symbol: SEC_INSIDER[nameKey(name)]?.symbol || null,
    },
    200,
    { "Cache-Control": `public, max-age=${CACHE_SECONDS}` },
  );
});
