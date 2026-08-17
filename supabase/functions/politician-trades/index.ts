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

/* ---- SEC EDGAR ownership filings -------------------------------------------
 * Some people this page lists file no STOCK Act report at all -- they are not
 * in Congress, so the house/senate feeds return nothing and the profile came
 * up empty. Where such a person is an officer, director or >10% holder of a
 * listed company, their position and its changes are public on EDGAR as Forms
 * 3, 4 and 5, and EDGAR is free, official, structured, and carries no
 * restriction on use. That is what this reads.
 *
 * Matched by CIK, never by name. A name pattern is not safe here: Donald
 * Trump Jr. is a sitting DJT director and 10% owner filing under his own CIK,
 * so /trump/i would have printed the son's transactions on the father's
 * profile. A CIK is the identity SEC assigns and it does not collide.
 *
 * Worth being clear about what this is not. quiverquant and unusualwhales
 * show Trump's executive-branch OGE Form 278-T periodic transaction reports
 * -- hundreds of equity and municipal-bond positions filed by the White House
 * Office. EDGAR carries none of that. What it carries is his Trump Media
 * (DJT) ownership, which is a real and complete record of one holding.
 */
const SEC = "https://www.sec.gov";
const SEC_DATA = "https://data.sec.gov";
const SEC_YEARS = 5;          // far enough back to cover a whole position

/* SEC asks that automated callers identify themselves with a contact address
   and holds them to 10 requests a second. Set SEC_USER_AGENT to a real one:
   supabase secrets set SEC_USER_AGENT="Ticker Alpha you@yourdomain.com" */
const secHeaders = () => ({
  "User-Agent": Deno.env.get("SEC_USER_AGENT")
    || "Ticker Alpha (tickeralpha.com)",
  "Accept-Encoding": "gzip, deflate",
});

const SEC_FILERS: Record<string, { ciks: string[]; note: string }> = {
  "donald trump": {
    // Himself, and the revocable trust he moved the whole stake into in
    // December 2024 -- the trust is where the position lives now, so leaving
    // it out would show a holder of nothing.
    ciks: ["0000947033", "0002050191"],
    note: "Donald J. Trump and the Donald J. Trump Revocable Trust",
  },
};

const nameKey = (s: string) =>
  s.toLowerCase().replace(/[^a-z ]/g, "").replace(/\s+/g, " ").trim();

/* Form 4 transaction codes. Only P and S are open-market trades; the rest are
   compensation and estate plumbing. They are all reported and all shown, but
   labelled for what they are -- calling a gift a sale would be a lie, and
   dropping it would leave a 114.75M-share disposal off the record entirely. */
const CODE: Record<string, { label: string; side?: "buy" | "sell" }> = {
  P: { label: "Buy", side: "buy" },
  S: { label: "Sell", side: "sell" },
  A: { label: "Award" },
  G: { label: "Gift" },
  M: { label: "Option exercise" },
  X: { label: "Option exercise" },
  F: { label: "Withheld for tax" },
  C: { label: "Conversion" },
  D: { label: "Returned to issuer" },
  J: { label: "Other" },
  K: { label: "Equity swap" },
  U: { label: "Tender" },
};

/* These documents are a fixed SEC schema: no namespaces, no attributes on the
   fields read here, no mixed content, one value per tag. String extraction is
   sound against that and keeps the function dependency-free -- Deno has no
   DOMParser, and pulling a wasm XML parser in for four tag names would cost
   more than it is worth. */
/** XML entities back to text. Issuer names really do carry them -- Trump
    Media & Technology Group arrives as "Trump Media &amp;amp; Technology", and
    printing that raw put a literal "&amp;amp;" in the table. &amp;amp; is decoded
    last so an escaped entity like &amp;amp;lt; does not turn into a bare "<". */
const unesc = (s: string): string => s
  .replace(/&lt;/g, "<")
  .replace(/&gt;/g, ">")
  .replace(/&quot;/g, '"')
  .replace(/&apos;/g, "'")
  .replace(/&#(\d+);/g, (_, n) => String.fromCharCode(Number(n)))
  .replace(/&amp;/g, "&");

const xtag = (xml: string, name: string): string => {
  const m = xml.match(new RegExp(`<${name}>\\s*(?:<value>)?\\s*([^<]*)`, "i"));
  return m ? unesc(m[1].trim()) : "";
};
const xblocks = (xml: string, name: string): string[] =>
  xml.split(`<${name}>`).slice(1).map((s) => s.split(`</${name}>`)[0]);

async function secJson(url: string): Promise<Record<string, unknown> | null> {
  try {
    const r = await fetch(url, { headers: secHeaders() });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

/** One ownership document -> its transactions, plus the position it leaves. */
function parseOwnership(xml: string, filed: string, link: string) {
  const symbol = xtag(xml, "issuerTradingSymbol").toUpperCase();
  const company = xtag(xml, "issuerName");
  const who = xtag(xml, "rptOwnerName");
  const rel = xblocks(xml, "reportingOwnerRelationship")[0] || "";
  const owner = xtag(rel, "isTenPercentOwner") === "1" ? "10% Owner"
    : xtag(rel, "isDirector") === "1" ? "Director"
    : xtag(rel, "isOfficer") === "1" ? (xtag(rel, "officerTitle") || "Officer")
    : "Insider";

  const trades: Record<string, unknown>[] = [];
  for (const table of ["nonDerivativeTransaction", "derivativeTransaction"]) {
    for (const b of xblocks(xml, table)) {
      const code = xtag(b, "transactionCode").toUpperCase();
      const meta = CODE[code] || { label: code || "Reported" };
      const shares = Number(xtag(b, "transactionShares") || 0);
      const price = Number(xtag(b, "transactionPricePerShare") || 0);
      const disposed = xtag(b, "transactionAcquiredDisposedCode").toUpperCase() === "D";
      const traded = xtag(b, "transactionDate").slice(0, 10);
      const value = shares * price;
      trades.push({
        symbol,
        name: company,
        assetType: table === "derivativeTransaction" ? "derivative" : "stock",
        // side drives the page's green/red chip, so only a genuine open-market
        // trade gets one. Everything else carries its own neutral label.
        side: meta.side || "",
        code,
        codeLabel: meta.label,
        open: !!meta.side,
        acquired: !disposed,
        security: xtag(b, "securityTitle"),
        // A gift is priced at zero, and "$0" would read as a free purchase.
        // Amount is left empty there and the share count carries the size.
        amount: value > 0 ? `$${Math.round(value).toLocaleString("en-US")}` : "",
        shares,
        price: price > 0 ? price : null,
        owner,
        reportedBy: who,
        traded,
        disclosed: filed,
        filedAfter: filedAfter(traded, filed),
        office: "",
        district: "",
        link,
        source: "sec",
      });
    }
  }

  // What the filer says they hold afterwards -- the answer to "and holdings".
  let position: Record<string, unknown> | null = null;
  const holds = [...xblocks(xml, "nonDerivativeHolding"),
                 ...xblocks(xml, "nonDerivativeTransaction")];
  for (const h of holds) {
    const n = Number(xtag(h, "sharesOwnedFollowingTransaction") || 0);
    if (!(n > 0)) continue;
    const indirect = xtag(h, "directOrIndirectOwnership").toUpperCase() === "I";
    position = {
      symbol, company, shares: n, who, asOf: filed,
      indirect, through: indirect ? xtag(h, "natureOfOwnership") : "",
    };
    break;
  }
  return { trades, position };
}

/** Every Form 3/4/5 the configured CIKs filed inside the window. */
async function secFilings(person: string) {
  const cfg = SEC_FILERS[nameKey(person)];
  if (!cfg) return { trades: [], positions: [], since: null as string | null };

  const since = new Date(Date.now() - SEC_YEARS * 365 * 86400000)
    .toISOString().slice(0, 10);
  const trades: Record<string, unknown>[] = [];
  const positions: Record<string, unknown>[] = [];

  for (const cik of cfg.ciks) {
    const sub = await secJson(`${SEC_DATA}/submissions/CIK${cik}.json`);
    const recent = (sub?.filings as Record<string, unknown> | undefined)
      ?.recent as Record<string, string[]> | undefined;
    if (!recent) continue;

    const bare = String(Number(cik));       // Archives paths drop leading zeros
    for (let i = 0; i < (recent.form || []).length; i++) {
      if (!["3", "4", "5"].includes(recent.form[i])) continue;
      if ((recent.filingDate[i] || "") < since) continue;
      // primaryDocument points at the XSL-rendered view; the raw XML sits
      // beside it, and only the modern XML filings are machine-readable.
      const doc = (recent.primaryDocument[i] || "").split("/").pop() || "";
      if (!doc.endsWith(".xml")) continue;
      const acc = (recent.accessionNumber[i] || "").replace(/-/g, "");
      const base = `${SEC}/Archives/edgar/data/${bare}/${acc}`;
      try {
        const r = await fetch(`${base}/${doc}`, { headers: secHeaders() });
        if (!r.ok) continue;
        const parsed = parseOwnership(
          await r.text(),
          recent.filingDate[i],
          `${base}/${recent.accessionNumber[i]}-index.htm`,
        );
        trades.push(...parsed.trades);
        if (parsed.position) positions.push(parsed.position);
      } catch { /* one unreadable filing should not lose the rest */ }
    }
  }

  trades.sort((a, b) => String(b.traded || "").localeCompare(String(a.traded || "")));
  // Newest statement of each holding wins.
  positions.sort((a, b) => String(b.asOf || "").localeCompare(String(a.asOf || "")));
  // Keyed on the symbol alone, not on symbol and filer. The same 114.75M DJT
  // shares are stated twice -- once by the trust that holds them and once by
  // Trump reporting them indirectly through it -- and listing both read as two
  // separate stakes worth 229M shares.
  const seenPos = new Set<string>();
  const latest = positions.filter((p) => {
    const k = String(p.symbol);
    if (seenPos.has(k)) return false;
    seenPos.add(k);
    return true;
  });
  return { trades, positions: latest, since };
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

  // ?debug=1 surfaces what FMP itself answered -- status and the first bit of
  // the body, never the URL, which carries the key. When FMP refuses a key it
  // says why in the body (rate limit, bandwidth, plan), and with the errors
  // swallowed here that message was invisible: an account limit looked
  // identical to a member with no trades.
  const debug = new URL(req.url).searchParams.get("debug") === "1";
  const upstream: Record<string, unknown>[] = [];

  // Both chambers: a name is in one or the other, and asking both costs one
  // extra request rather than making the caller know which.
  const paths = ["house-trades-by-name", "senate-trades-by-name"];
  const settled = await Promise.all(paths.map(async (p) => {
    const u = `${FMP}/${p}?name=${encodeURIComponent(name)}&apikey=${encodeURIComponent(key)}`;
    try {
      const r = await fetch(u);
      if (!r.ok) {
        if (debug) upstream.push({ path: p, status: r.status,
          body: (await r.text().catch(() => "")).slice(0, 300) });
        return [] as unknown[];
      }
      const b = await r.json();
      if (debug) upstream.push({ path: p, status: r.status,
        rows: Array.isArray(b) ? b.length : null,
        body: Array.isArray(b) ? undefined : JSON.stringify(b).slice(0, 300) });
      return Array.isArray(b) ? b : [];
    } catch (e) {
      if (debug) upstream.push({ path: p, error: String(e).slice(0, 200) });
      return [] as unknown[];
    }
  }));

  // EDGAR ownership filings, for a person who files no STOCK Act report but is
  // an insider of a listed company. Runs alongside the chamber feeds rather
  // than only on their failure: a person could in principle have both.
  const sec = await secFilings(name);

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

  const all = [...trades, ...sec.trades]
    .sort((a, b) => String(b.traded || "").localeCompare(String(a.traded || "")));

  // Which feeds actually produced something, so the page can say where a
  // profile's rows came from instead of implying every name is a legislator.
  const sources: string[] = [];
  if (trades.length) sources.push("congress");
  if (sec.trades.length) sources.push("sec");

  return json(
    {
      person: name, days: DAYS, since: cutoff,
      count: all.length, sources, trades: all,
      ...(debug ? { upstream } : {}),
      congressCount: trades.length,
      // The EDGAR half reaches further back than the chamber feeds and says so
      // separately, rather than letting one window label two spans.
      sec: {
        count: sec.trades.length,
        since: sec.since,
        positions: sec.positions,
        tracked: !!SEC_FILERS[nameKey(name)],
        note: SEC_FILERS[nameKey(name)]?.note || null,
      },
    },
    200,
    { "Cache-Control": `public, max-age=${CACHE_SECONDS}` },
  );
});
