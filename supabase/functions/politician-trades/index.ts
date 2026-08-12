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
    })
    .sort((a, b) => (b.traded || "").localeCompare(a.traded || ""));

  return json(
    { person: name, days: DAYS, since: cutoff, count: trades.length, trades },
    200,
    { "Cache-Control": `public, max-age=${CACHE_SECONDS}` },
  );
});
