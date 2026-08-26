"""
worker.py -- the only process allowed to talk to the SEC.

    python worker.py sync-directory     refresh the ticker list
    python worker.py seed [N]           ingest the largest N companies
    python worker.py ingest TICKER ...  ingest specific companies
    python worker.py backfill           drain the request queue once
    python worker.py sweep [YYYY-MM-DD] ingest that day's new 10-Q/10-K
    python worker.py market             refresh prices, movers and sectors
    python worker.py warm               pre-fetch the front tables' tickers
    python worker.py indexes            refresh SPX / IXIC / DJI quotes + holdings
    python worker.py sections           refresh heatmap, rotation and trades
    python worker.py news               refresh market news
    python worker.py earnings           refresh earnings calendar
    python worker.py economics          refresh US economic calendar
    python worker.py logos [N]          cache Logo.dev images (S&P 500 + crypto)
    python worker.py prices [TICKER...] fill price requests, or named symbols
    python worker.py analyst [TICKER...] fill coverage requests, or named symbols
    python worker.py intraday TICKER    refresh one chart series
    python worker.py funds             refresh the mutual / money market fund list
    python worker.py long-closes [T...] 10y closes for the correlation heatmap
    python worker.py stats              coverage summary
    python worker.py run                the long-running loop (this is what
                                        Render runs)

Why one process: the SEC's rate limit is per IP, so the whole service shares a
single budget. `edgar.py` already serialises and paces its own requests, and
keeping every SEC call inside this one program is what makes that limit hold.
Do not run two copies against the same deployment.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sys
import time
import traceback

import envload  # noqa: F401  -- must precede edgar/store

import edgar
import market
import store

POLL = int(os.environ.get("BACKFILL_POLL_SECONDS", "20"))
RECHECK_HOURS = int(os.environ.get("COMPANY_RECHECK_HOURS", "6"))


def log(msg: str) -> None:
    print(f"{dt.datetime.now():%H:%M:%S} {msg}", flush=True)


# ---------------------------------------------------------------------------
# Ticker directory
# ---------------------------------------------------------------------------

def sync_directory() -> int:
    rows = edgar.ticker_directory()
    n = store.upsert_directory(rows)
    log(f"directory: {len(rows):,} tickers upserted ({n:,} rows written)")
    # ETF + crypto lists power portfolio / nav search for symbols the SEC
    # directory does not carry (VOO, BTCUSD, …).
    try:
        sync_market_symbols()
    except Exception as exc:
        log(f"  market symbols: {exc}")
    # Members of Congress, for the Insider & Congress page: party, district
    # and the bioguide id its photos are keyed by. Daily is generous — the
    # file changes when membership does. Before migration 0058 the RPC does
    # not exist yet, which lands here as an error worth logging, not a stop.
    try:
        pols = market.legislators()
        store.replace_politicians(pols)
        log(f"  politicians: {len(pols):,} members synced")
    except Exception as exc:
        log(f"  politicians: {exc}")
    return len(rows)


def sync_market_symbols() -> int:
    """Refresh FMP ETF and cryptocurrency symbol directories."""
    if not market.configured():
        log("market symbols: FMP_API_KEY not set — skipped")
        return 0
    total = 0
    try:
        etfs = market.etf_list()
        n = store.upsert_market_symbols(etfs)
        total += n
        log(f"market symbols: {len(etfs):,} ETFs ({n:,} rows written)")
    except market.MarketError as exc:
        log(f"  etf-list: {exc}")
    except store.StoreError as exc:
        log(f"  etf-list write: {exc}")
    try:
        coins = market.crypto_list()
        n = store.upsert_market_symbols(coins)
        total += n
        log(f"market symbols: {len(coins):,} crypto ({n:,} rows written)")
    except market.MarketError as exc:
        log(f"  cryptocurrency-list: {exc}")
    except store.StoreError as exc:
        log(f"  cryptocurrency-list write: {exc}")
    try:
        funds = market.fund_list()
        n = store.upsert_market_symbols(funds)
        total += n
        mm = sum(1 for f in funds if f.get("kind") == "money_market")
        log(f"market symbols: {len(funds):,} funds, {mm} money market "
            f"({n:,} rows written)")
    except market.MarketError as exc:
        log(f"  fund screen: {exc}")
    except store.StoreError as exc:
        # 0057 not applied yet: the kind check still refuses 'fund'. Everything
        # else in this sync has already been written.
        log(f"  fund write (apply 0057_funds.sql): {exc}")
    return total


# ---------------------------------------------------------------------------
# Ingesting one company
# ---------------------------------------------------------------------------

def ingest(ticker: str, quarters: int = 24) -> dict:
    """Build a company from SEC filings and write it to Supabase.

    Breakdowns are fetched for the most recent quarters only. Older ones are
    filled in on demand -- each costs several requests to the filing's own
    tables, and almost nobody scrolls back four years.
    """
    started = time.time()
    company = edgar.build_company(ticker, max_quarters=quarters)

    breakdowns: list[dict] = []
    for q in company["quarters"][:8]:
        try:
            for cut in edgar.attach_segments(company, q["end"]):
                breakdowns.append({
                    "period_end": q["end"], "name": cut["name"],
                    "source": cut.get("source"), "basis": cut.get("basis"),
                    "rows": cut["rows"],
                })
        except Exception as exc:
            # A breakdown that will not parse is not a reason to lose the
            # income statement. Record it and move on.
            log(f"  {ticker} {q['label']}: breakdown failed ({exc})")

    written = store.ingest_company(company, breakdowns)
    log(f"{ticker:<6} {written['quarters']:>2} quarters, "
        f"{written['breakdowns']:>2} breakdowns, {time.time()-started:.1f}s")
    return written


def ingest_safely(ticker: str) -> str | None:
    """Returns an error string, or None on success."""
    try:
        ingest(ticker)
        return None
    except edgar.FetchError as exc:
        return str(exc)
    except store.StoreError:
        raise                      # configuration problems should stop the run
    except Exception as exc:
        traceback.print_exc()
        return f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def seed(limit: int = 500) -> None:
    """Ingest a starting universe so the site is useful on day one.

    Ordered by CIK as a rough proxy for "long-listed and therefore likely to
    be searched"; anything missed arrives through the backfill queue.
    """
    rows = edgar.ticker_directory()
    seen, universe = set(), []
    for r in sorted(rows, key=lambda r: r["cik"]):
        if r["ticker"] in seen:
            continue
        seen.add(r["ticker"])
        universe.append(r["ticker"])
        if len(universe) >= limit:
            break

    log(f"seeding {len(universe)} companies")
    ok = failed = 0
    for i, t in enumerate(universe, 1):
        err = ingest_safely(t)
        if err:
            failed += 1
            log(f"  [{i}/{len(universe)}] {t}: {err[:90]}")
        else:
            ok += 1
        if i % 25 == 0:
            log(f"  progress {i}/{len(universe)} — {ok} ok, {failed} failed")
    log(f"seed complete: {ok} ok, {failed} failed")


# ---------------------------------------------------------------------------
# Backfill queue
# ---------------------------------------------------------------------------

def drain_backfill(max_items: int = 25) -> int:
    done = 0
    while done < max_items:
        job = store.claim_backfill()
        if not job:
            break
        err = ingest_safely(job["ticker"])
        store.finish_backfill(job["id"], err)
        if err:
            log(f"backfill {job['ticker']}: {err[:90]}")
        else:
            # A company being read for the first time is one whose report is
            # about to be opened, so fetch its prices in the same pass rather
            # than making the visitor wait for a second round trip.
            fetch_prices(job["ticker"])
        done += 1
    return done


# ---------------------------------------------------------------------------
# Daily index sweep
# ---------------------------------------------------------------------------

_IDX_FORMS = ("10-Q", "10-K", "20-F", "40-F")


def sweep(day: dt.date | None = None) -> int:
    """Ingest companies that filed a periodic report on `day`.

    The daily index is the SEC's own complete record of what was disseminated,
    so this is the mechanism that keeps the database ahead of visitors rather
    than behind them.
    """
    day = day or dt.date.today()
    qtr = (day.month - 1) // 3 + 1
    url = (f"https://www.sec.gov/Archives/edgar/daily-index/"
           f"{day.year}/QTR{qtr}/form.{day:%Y%m%d}.idx")
    try:
        raw = edgar.fetch(url, ttl=6 * 3600)
    except edgar.FetchError as exc:
        log(f"sweep {day}: no index ({exc})")
        return 0

    lines = raw.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.startswith("---")) + 1
    except StopIteration:
        return 0

    ciks: dict[int, str] = {}
    for line in lines[start:]:
        form = line[:12].strip()
        if form not in _IDX_FORMS:
            continue
        m = re.search(r"edgar/data/(\d+)/([\d-]+)\.txt", line)
        if not m:
            continue
        ciks.setdefault(int(m.group(1)), m.group(2))

    if not ciks:
        log(f"sweep {day}: no periodic filings")
        return 0

    # Only companies we already track. New listings arrive on demand.
    directory = {r["cik"]: r["ticker"] for r in edgar.ticker_directory()}
    todo = [(cik, directory[cik], acc) for cik, acc in ciks.items() if cik in directory]
    log(f"sweep {day}: {len(ciks)} periodic filings, {len(todo)} with tickers")

    done = 0
    for cik, ticker, accession in todo:
        if store.filing_seen(accession):
            continue
        err = ingest_safely(ticker)
        if err:
            store.record_failure(accession, cik, "10-Q/K", day.isoformat(), None, err)
        done += 1
    log(f"sweep {day}: {done} companies refreshed")
    return done


# ---------------------------------------------------------------------------
# Market data (FMP)
# ---------------------------------------------------------------------------

# The watchlist behind Market Summary. Broad-market ETFs first so the page
# always has an index to call the day's leader.
WATCHLIST = os.environ.get("WATCHLIST", "").split(",") if os.environ.get("WATCHLIST") else [
    "SPY", "QQQ", "IWM", "DIA",
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD",
    "AVGO", "JPM", "UBER", "COIN", "MU", "NFLX",
]

# How many followed companies get a chart series on every market pass. One
# FMP request each, so this is the cost of the feature: at the default
# fifteen-minute cadence, forty names is 160 requests an hour. Raise it when
# the plan allows; anything past the cap still gets a series the moment
# someone opens it, through the request queue.
FOLLOWED_CHARTS = int(os.environ.get("FOLLOWED_CHART_LIMIT", "40"))


def refresh_market() -> bool:
    """Pull the day's market picture into Supabase.

    Runs on a timer, not on a visitor's request, because the FMP key is a paid
    secret that must never reach a browser.
    """
    if not market.configured():
        return False
    started = time.time()

    sectors, as_of = market.sectors()
    if sectors:
        store.replace_sectors(sectors, as_of)

    store_movers: dict[str, list[dict]] = {}
    for kind, fetch in (("gainer", market.gainers),
                        ("loser", market.losers),
                        ("active", market.actives)):
        try:
            rows = fetch(25)
            store_movers[kind] = rows
            store.replace_movers(kind, rows, as_of)
        except market.MarketError as exc:
            log(f"  movers {kind}: {exc}")

    # Remember the mover names for the warm pool: this is the one place that
    # already holds them, so warming them costs no extra discovery request.
    global _mover_symbols
    _mover_symbols = [r["symbol"] for k in ("gainer", "loser")
                      for r in (store_movers.get(k) or [])[:WARM_MOVERS]
                      if r.get("symbol")]

    # Chart series for the watchlist and for the top few movers, so every
    # view of the summary list has something to draw when it is opened --
    # and for what people actually follow, which is the only part of this
    # list that is not known in advance.
    followed: list[str] = []
    try:
        followed = store.watchlisted_symbols(FOLLOWED_CHARTS)
    except store.StoreError as exc:
        # 0020 not applied yet. Followed companies still get a series from the
        # request queue when someone opens one; they just are not pre-fetched.
        log(f"  followed symbols unavailable (apply 0020_intraday_requests.sql): {exc}")

    pending: list[str] = []
    try:
        pending = store.pending_prices(FOLLOWED_CHARTS)
    except store.StoreError as exc:
        log(f"  pending prices unavailable: {exc}")

    # Quotes feed the watchlist price column and the tape. The hardcoded
    # WATCHLIST alone left followed names (AZN, HOOD, …) with a chart but
    # "—" for the price, so anything people follow or have asked about is
    # quoted here too.
    quote_syms = list(dict.fromkeys(WATCHLIST + followed + pending))
    quotes = market.quotes(quote_syms)
    if quotes:
        store.upsert_quotes(quotes)

    chart_syms = list(dict.fromkeys(
        WATCHLIST
        + [r["symbol"] for r in (store_movers.get("gainer") or [])[:6]]
        + [r["symbol"] for r in (store_movers.get("loser") or [])[:6]]
        + followed))
    for sym in chart_syms:
        try:
            pts = market.intraday(sym, days=2)
            if pts:
                store.upsert_intraday(sym, pts, pts[-1]["t"][:10])
        except market.MarketError:
            continue

    # Fear & Greed is not an FMP field — see market.fear_greed.
    try:
        fg = market.fear_greed()
        store.upsert_sentiment("fear_greed", fg["score"], fg.get("rating"),
                               fg.get("previous"), fg.get("source"))
        log(f"  fear&greed: {fg['score']:.0f} ({fg.get('rating')}) "
            f"via {fg.get('source')}"
            + (f" — {fg['note']}" if fg.get("note") else ""))
    except (market.MarketError, store.StoreError) as exc:
        log(f"  fear&greed: {exc}")

    try:
        refresh_indexes(holdings=False)
    except Exception as exc:
        log(f"  indexes: {exc}")

    log(f"market: {len(sectors)} sectors, {len(quotes)} quotes, "
        f"{len(chart_syms)} charts, as of {as_of}, {time.time()-started:.1f}s")
    return True


def refresh_indexes(holdings: bool = True) -> bool:
    """Quotes, daily bars, search rows, and optional holdings for SPX/IXIC/DJI.

    FMP serves caret symbols (^GSPC…); we store searchable aliases. Holdings
    hit public constituent CSVs + the screener, so they run on the slower
    sections cadence unless ``holdings`` is forced.
    """
    if not market.configured():
        return False
    sym_rows = [{
        "symbol": alias,
        "name": meta["name"],
        "kind": "index",
        "exchange": meta.get("exchange") or "INDEX",
    } for alias, meta in market.INDEXES.items()]
    try:
        store.upsert_market_symbols(sym_rows)
    except store.StoreError as exc:
        log(f"  index symbols: {exc}")

    quotes = market.index_quotes()
    if quotes:
        store.upsert_quotes([{
            "symbol": q["symbol"],
            "name": q.get("name"),
            "price": q.get("price"),
            "change": q.get("change"),
            "change_pct": q.get("change_pct"),
            "volume": q.get("volume"),
            "market_cap": q.get("market_cap"),
            "exchange": q.get("exchange"),
        } for q in quotes])

    for alias in market.INDEXES:
        try:
            fetch_index_prices(alias)
        except Exception as exc:
            log(f"  index prices {alias}: {exc}")

    if holdings:
        for alias in market.INDEXES:
            try:
                rows = market.index_holdings(alias)
                if rows:
                    n = store.replace_index_holdings(alias, rows)
                    log(f"  index holdings {alias}: {n} rows")
                    # Warm quotes so the holdings table can show today's % move.
                    members = [r["symbol"] for r in rows if r.get("symbol")]
                    for i in range(0, len(members), 50):
                        try:
                            batch = market.quotes(members[i:i + 50]) or []
                            if batch:
                                store.upsert_quotes([{
                                    "symbol": x["symbol"],
                                    "name": x.get("name"),
                                    "price": x.get("price"),
                                    "change": x.get("change"),
                                    "change_pct": x.get("change_pct"),
                                    "volume": x.get("volume"),
                                    "market_cap": x.get("market_cap"),
                                    "exchange": x.get("exchange"),
                                } for x in batch if x.get("symbol")])
                        except market.MarketError as exc:
                            log(f"  index holding quotes {alias}: {exc}")
                            break
            except Exception as exc:
                log(f"  index holdings {alias}: {exc}")

    log(f"indexes: {len(quotes)} quotes"
        + (" + holdings" if holdings else ""))
    return bool(quotes)


def fetch_index_prices(alias: str) -> bool:
    """Daily bars, quote, monthly closes, and PE history for one index alias."""
    sym = (alias or "").upper()
    if sym not in market.INDEXES:
        return False
    meta = market.INDEXES[sym]
    q = market.index_quote(sym)
    # ~25 years of daily bars for all-time-high drawdown + historical
    # probability guides on the index drawdown chart.
    bars = market.index_history(sym, 6500)
    quote_detail = None
    if q:
        quote_detail = {
            "name": q.get("name"),
            "price": q.get("price"),
            "change": q.get("change"),
            "change_pct": q.get("change_pct"),
            "day_low": q.get("day_low"),
            "day_high": q.get("day_high"),
            "volume": q.get("volume"),
            "market_cap": q.get("market_cap"),
            "exchange": q.get("exchange"),
        }
    store.upsert_prices(sym, bars, quote_detail, bars[-1]["d"] if bars else None)

    # Seasonality + PE comparison chart on the index ticker page.
    try:
        monthly = market.monthly_closes(meta["fmp"], 11)
    except Exception as exc:
        log(f"  index monthly {sym}: {exc}")
        monthly = []
    try:
        pe_hist = market.index_forward_pe_history(sym, years=10)
    except Exception as exc:
        log(f"  index pe {sym}: {exc}")
        pe_hist = []
    if monthly or pe_hist:
        try:
            store.upsert_company_extras(
                sym, monthly or None, None, None, pe_hist or None)
        except store.StoreError as exc:
            log(f"  index extras {sym}: {exc}")

    return bool(bars)


def refresh_news() -> bool:
    """Latest market news, on the same cadence as prices.

    Two requests for the whole page, so it can run often without costing much.
    """
    if not market.configured():
        return False
    rows, keywords = market.news(120)
    if not rows:
        return False
    n = store.upsert_news(rows, keywords)
    log(f"news: {len(rows)} articles ({n} written), {len(keywords)} keywords")

    # The feed pastes generic stock photos on press releases, so for those
    # stories (and any with no image at all) read the article page's own
    # og:image instead. A bounded batch per cycle; each URL is attempted
    # exactly once, and a page with nothing to give is not asked again.
    try:
        queue = store.news_image_queue(20)
    except store.StoreError as exc:
        log(f"news image queue failed (continuing): {exc}")
        queue = []
    if queue:
        results = [{"url": q.get("url"),
                    "image": market.article_image(q.get("url"))}
                   for q in queue if q.get("url")]
        try:
            store.set_news_images(results)
            found = sum(1 for r in results if r["image"])
            log(f"news images: {found}/{len(results)} taken from article pages")
        except store.StoreError as exc:
            log(f"news images failed (continuing): {exc}")
    return True


_FMP_PAUSED_UNTIL = 0.0


def _fmp_limited(exc) -> bool:
    """An account-level refusal (rate or bandwidth cap), not a bad symbol."""
    m = str(exc).lower()
    return "429" in m or "limit" in m or "bandwidth" in m


def _pause_fmp(reason: str, seconds: int = 600) -> None:
    """Stop draining request queues for a while.

    During the August bandwidth outage every queued request was attempted,
    failed with a 429, and was marked done anyway -- the mark that stops a bad
    symbol from spinning the queue also locked every good symbol out for the
    request table's 12-hour re-ask window. When the plan recovered, the queue
    stayed silent for hours while the timed jobs worked fine. A limit error
    now leaves the request pending and pauses the drains instead: the retry
    happens when the pause lapses, not twelve hours later.
    """
    global _FMP_PAUSED_UNTIL
    _FMP_PAUSED_UNTIL = time.time() + seconds
    log(f"FMP limit hit ({reason}); pausing request drains {seconds}s")


def fetch_prices(symbol: str) -> bool:
    """Daily bars and a quote for one company. Two FMP requests."""
    if not market.configured():
        return False
    sym = symbol.upper()
    # Indexes are quoted under caret symbols at FMP; route by alias.
    if sym in market.INDEXES:
        try:
            return fetch_index_prices(sym)
        except Exception as exc:
            log(f"  prices {sym}: {exc}")
            store.upsert_prices(sym, [], None, None)
            return False
    try:
        bars = market.daily(sym, 300)
        q = market.quote_detail(sym)
    except market.MarketError as exc:
        log(f"  prices {sym}: {exc}")
        if _fmp_limited(exc):
            # The account is refused, not the symbol. Leave the request
            # pending and back off; marking it done here is what silenced the
            # queue for twelve hours after the bandwidth cap lifted.
            _pause_fmp(f"prices {sym}")
            return False
        # Mark it finished anyway, or the queue spins on a bad symbol.
        store.upsert_prices(sym, [], None, None)
        return False
    store.upsert_prices(sym, bars, q, bars[-1]["d"] if bars else None)

    # Keep the summary quote in sync too. fetch_prices writes quote_detail for
    # the company page; without this, a watchlisted symbol that was never on
    # the hardcoded WATCHLIST still shows "—" on Markets Today.
    if q and q.get("price") is not None:
        try:
            store.upsert_quotes([{
                "symbol": q.get("symbol") or sym,
                "name": q.get("name"),
                "price": q.get("price"),
                "change": q.get("change"),
                "change_pct": q.get("change_pct"),
                "volume": q.get("volume"),
                "market_cap": q.get("market_cap"),
                "exchange": q.get("exchange"),
            }])
        except store.StoreError as exc:
            log(f"  quote {sym}: {exc}")

    # The report also plots this company against its sector and over ten years
    # of months. Both come off the same visit, so they are fetched here rather
    # than through a second queue.
    extras = ""
    try:
        prof = market.profile(sym) or {}
        monthly = market.monthly_closes(sym, 11)
        pe_hist: list[dict] = []
        emp_hist: list[dict] = []
        try:
            pe_hist = market.pe_history(sym)
        except market.MarketError as exc:
            log(f"  pe history {sym}: {exc}")
        try:
            emp_hist = market.employee_history(sym)
        except market.MarketError as exc:
            log(f"  employee history {sym}: {exc}")
        try:
            store.upsert_company_extras(
                sym, monthly, prof.get("sector"), prof.get("industry"),
                pe_hist or None, prof or None, emp_hist if emp_hist is not None else [])
        except store.StoreError as exc:
            # Older migrations: peel off the newest optional args one by one.
            log(f"  extras write failed ({exc}); falling back")
            try:
                store.upsert_company_extras(
                    sym, monthly, prof.get("sector"), prof.get("industry"),
                    pe_hist or None, prof or None)
            except store.StoreError:
                try:
                    store.upsert_company_extras(
                        sym, monthly, prof.get("sector"), prof.get("industry"),
                        pe_hist or None)
                except store.StoreError:
                    store.upsert_company_extras(
                        sym, monthly, prof.get("sector"), prof.get("industry"))
        extras = f", {len(monthly)} months, {prof.get('sector') or 'no sector'}"
        if pe_hist:
            extras += f", {len(pe_hist)} pe"
        if emp_hist:
            extras += f", {len(emp_hist)} headcount"
        if prof.get("ceo") or prof.get("description"):
            extras += ", profile"
        etf = market.SECTOR_ETF.get(prof.get("sector") or "")
        if etf:
            fetch_benchmark(etf)
    except market.MarketError as exc:
        log(f"  extras {sym}: {exc}")
    except store.StoreError as exc:
        log(f"  extras {sym}: {exc}")

    # Analyst coverage rides along with the same visit.
    if fetch_analyst(sym):
        extras += ", analysts"

    # Dividend history for the portfolio Dividend column (ex-date + amount).
    try:
        divs = market.dividends(sym)
        n_div = store.replace_symbol_dividends(sym, divs)
        if divs:
            extras += f", {n_div} dividends"
    except market.MarketError as exc:
        log(f"  dividends {sym}: {exc}")
    except store.StoreError as exc:
        log(f"  dividends {sym}: {exc}")

    # Beat/miss history for the ticker page's Earnings tab. The market-wide
    # earnings calendar is rewritten to a rolling window every run, so the
    # per-symbol record has to be fetched and kept separately.
    try:
        eps = market.earnings_history(sym)
        n_eps = store.replace_symbol_earnings(sym, eps)
        if eps:
            extras += f", {n_eps} earnings"
    except market.MarketError as exc:
        log(f"  earnings {sym}: {exc}")
    except store.StoreError as exc:
        log(f"  earnings {sym}: {exc}")

    # 13F holders for the Ownership tab. The most likely failure here is a plan
    # that does not carry institutional ownership at all, which is why it is
    # last and why it only logs: everything above it has already been written.
    try:
        held = market.institutional_holders(sym)
        n_held = store.replace_symbol_holders(sym, held)
        if held:
            extras += f", {n_held} holders"
    except market.MarketError as exc:
        log(f"  holders {sym}: {exc}")
    except store.StoreError as exc:
        log(f"  holders {sym}: {exc}")

    log(f"prices {sym}: {len(bars)} bars{', quote' if q else ', no quote'}{extras}")
    return True


def fill_company_profile(symbol: str) -> bool:
    """Write FMP /profile (+ headcount history) onto an existing price_daily row."""
    if not market.configured():
        return False
    sym = symbol.upper()
    try:
        prof = market.profile(sym)
    except market.MarketError as exc:
        log(f"  profile {sym}: {exc}")
        return False
    if not prof:
        return False
    emp_hist: list[dict] = []
    try:
        emp_hist = market.employee_history(sym)
    except market.MarketError as exc:
        log(f"  employee history {sym}: {exc}")
    try:
        store.upsert_company_extras(sym, None, prof.get("sector"),
                                    prof.get("industry"), None, prof,
                                    emp_hist)
    except store.StoreError:
        try:
            store.upsert_company_extras(sym, None, prof.get("sector"),
                                        prof.get("industry"), None, prof)
        except store.StoreError as exc:
            log(f"  profile write {sym}: {exc}")
            return False
    log(f"profile {sym}: {prof.get('name') or sym}"
        f"{', ' + prof['ceo'] if prof.get('ceo') else ''}"
        f"{', ' + str(len(emp_hist)) + ' headcount' if emp_hist else ''}")
    return True


def fill_employee_history(symbol: str) -> bool:
    """Write FMP headcount history only (for the revenue / employee line)."""
    if not market.configured():
        return False
    sym = symbol.upper()
    try:
        emp_hist = market.employee_history(sym)
    except market.MarketError as exc:
        log(f"  employee history {sym}: {exc}")
        return False
    try:
        store.upsert_company_extras(sym, None, None, None, None, None, emp_hist)
    except store.StoreError as exc:
        log(f"  employee history write {sym}: {exc}")
        return False
    log(f"employee history {sym}: {len(emp_hist)} year(s)")
    return True


def drain_profiles(max_items: int = 15) -> int:
    """Fill About-card profiles for symbols that already have prices."""
    try:
        pending = store.pending_profiles(max_items)
    except store.StoreError as exc:
        log(f"  profile queue unavailable (apply 0031_pending_profiles.sql): {exc}")
        return 0
    return sum(1 for sym in pending if fill_company_profile(sym))


def drain_employee_history(max_items: int = 15) -> int:
    """Fill headcount history for symbols missing the revenue / employee series."""
    try:
        pending = store.pending_employee_history(max_items)
    except store.StoreError as exc:
        log(f"  employee-history queue unavailable "
            f"(apply 0033_pending_employee_history.sql): {exc}")
        return 0
    return sum(1 for sym in pending if fill_employee_history(sym))


def fetch_analyst(symbol: str) -> bool:
    """Targets, the rating tally and recent house actions for one company.

    Written even when FMP returns nothing, because "no house covers this
    company" is an answer the report can show, and an absent row is
    indistinguishable from one that has not been fetched yet. Fields that came
    back empty keep whatever they held, so a single failing endpoint cannot
    blank a section that was complete a minute ago.
    """
    if not market.configured():
        return False
    sym = symbol.upper()
    try:
        view = market.analyst_view(sym)
    except market.MarketError as exc:
        log(f"  analyst {sym}: {exc}")
        return False
    store.upsert_analyst(sym, view)
    c = view.get("consensus") or {}
    log(f"analyst {sym}: {c.get('rating') or 'no consensus'}, "
        f"{len(view.get('grades') or [])} houses, "
        f"{len(view.get('news') or [])} stories")
    return True


# One series per sector rather than per company, refreshed at most daily.
_benchmark_seen: dict[str, float] = {}


def fetch_benchmark(etf: str, ttl: int = 20 * 3600) -> bool:
    """The sector SPDR a company is plotted against."""
    if time.time() - _benchmark_seen.get(etf, 0) < ttl:
        return False
    rows = market.closes(etf, 11)
    if not rows:
        return False
    store.upsert_benchmark(etf, rows)
    _benchmark_seen[etf] = time.time()
    log(f"benchmark {etf}: {len(rows)} closes")
    return True


def refresh_industry_pe() -> bool:
    """Price/earnings by industry and sector, for the whole market at once."""
    if not market.configured():
        return False
    rows, as_of = market.industry_pe()
    if not rows:
        return False
    n = store.replace_industry_pe(rows, as_of)
    log(f"industry PE: {n} rows as of {as_of}")
    return True


def drain_prices(max_items: int = 5) -> int:
    """Fetch prices for the companies whose report pages have been opened.

    Markets Today tracks a fixed sixty-odd names and can be pre-fetched. The
    company report can be opened for any listed company, so prices are pulled
    on demand -- the same shape as the filings backfill, and for the same
    reason. Two requests per company: the daily bars and the quote.
    """
    if not market.configured():
        return 0
    if time.time() < _FMP_PAUSED_UNTIL:
        return 0
    done = 0
    for sym in store.pending_prices(max_items):
        if fetch_prices(sym):
            done += 1
        if time.time() < _FMP_PAUSED_UNTIL:
            break                       # the account just got refused; stop
    return done


def drain_long_closes(max_items: int = 3) -> int:
    """Fetch a decade of daily closes for symbols the portfolio wants.

    One request each, and the payload is large, so this drains fewer per pass
    than the other queues. `closes` uses FMP's light series -- date and close
    only -- which is all the correlation heatmap reads and a third the size of
    the OHLCV the candlestick chart needs.
    """
    if not market.configured():
        return 0
    if time.time() < _FMP_PAUSED_UNTIL:
        return 0
    try:
        pending = store.pending_long_closes(max_items)
    except store.StoreError as exc:
        log(f"  long-close queue unavailable (apply 0054_long_closes.sql): {exc}")
        return 0
    done = 0
    for sym in pending:
        try:
            rows = market.closes(sym, years=10)
        except market.MarketError as exc:
            log(f"  long closes {sym}: {exc}")
            if _fmp_limited(exc):
                # Same rule as prices: an account refusal must not consume
                # the request. Stop this pass; retry after the pause.
                _pause_fmp(f"long closes {sym}")
                break
            rows = []
        try:
            n = store.upsert_long_closes(sym, rows)
            if n:
                log(f"  long closes {sym}: {n} closes")
                done += 1
        except store.StoreError as exc:
            log(f"  long closes {sym} write: {exc}")
    return done


def drain_analyst(max_items: int = 5) -> int:
    """Fetch coverage for the companies whose report pages have asked for it.

    Coverage used to be a passenger on the price fetch, which only runs when
    the page finds prices missing or half a day old -- so a company with fresh
    prices never got any, and its report sat on a spinner forever. It has its
    own queue now, drained the same way prices are.
    """
    if not market.configured():
        return 0
    try:
        pending = store.pending_analyst(max_items)
    except store.StoreError as exc:
        # 0017 not applied yet. Coverage still arrives with the price fetch,
        # so this degrades rather than taking the loop down with it.
        log(f"  analyst queue unavailable (apply 0017_analyst_requests.sql): {exc}")
        return 0
    return sum(1 for sym in pending if fetch_analyst(sym))


# ---------------------------------------------------------------------------
# Pre-warming the front tables
# ---------------------------------------------------------------------------
# The Markets Today tables (market cap, gainers, losers) are the front door:
# most first clicks land on one of their tickers, and left to the request
# queue alone, the first visitor of the day waits out a full on-demand fetch.
# So the worker keeps those names warm itself. Every market cycle rebuilds the
# candidate pool -- the largest companies by market cap plus both mover
# lists -- and between visitor requests a small batch of whichever names have
# gone stale is fetched through the same fetch_prices path a visit would use.
# The staleness threshold matches the page's own (get_prices marks 12 hours),
# so a warmed name never shows the slow path.

WARM_TOP_CAP = int(os.environ.get("WARM_TOP_CAP", "50"))
WARM_MOVERS = int(os.environ.get("WARM_MOVERS", "25"))
WARM_BATCH = int(os.environ.get("WARM_BATCH", "4"))
WARM_STALE_HOURS = int(os.environ.get("WARM_STALE_HOURS", "12"))

_mover_symbols: list[str] = []      # written by refresh_market
_warm_pool: list[str] = []          # rebuilt each market cycle
_warm_idle_until = 0.0              # everything fresh: stop asking for a while


def rebuild_warm_pool() -> None:
    """The names worth keeping warm, largest caps first.

    top_by_cap rides the screener call the mover filter already makes (and
    caches), so rebuilding the pool costs nothing extra. Order matters: the
    due-list preserves it, and the batch fetched first is the front of it --
    a stale NVDA beats a stale 48th-largest name.
    """
    global _warm_pool
    pool: list[str] = []
    try:
        pool += [t for t, _name in market.top_by_cap(WARM_TOP_CAP)]
    except market.MarketError as exc:
        log(f"  warm pool: screener unavailable ({exc})")
    pool += _mover_symbols
    _warm_pool = list(dict.fromkeys(s.upper() for s in pool if s))


def warm_reports(batch: int = WARM_BATCH) -> int:
    """Fetch report data for stale front-table names, a few per pass.

    Visitor requests always drain first; this fills the quiet between them.
    reports_due is the resumable memory: the database says which names still
    need today's fetch, so a worker restart resumes rather than starting the
    whole pool over.
    """
    global _warm_idle_until
    if not _warm_pool or not market.configured():
        return 0
    if time.time() < _FMP_PAUSED_UNTIL or time.time() < _warm_idle_until:
        return 0
    try:
        due = store.reports_due(_warm_pool, WARM_STALE_HOURS)
    except store.StoreError as exc:
        log(f"  warm queue unavailable (apply 0069_report_warm.sql): {exc}")
        return 0
    if not due:
        # Names go stale one at a time over hours; once everything is fresh
        # there is no reason to re-ask every poll. The pool rebuild cadence
        # is the natural recheck.
        _warm_idle_until = time.time() + 900
        return 0
    done = 0
    for sym in due[:max(0, batch)]:
        if fetch_prices(sym):
            done += 1
        if time.time() < _FMP_PAUSED_UNTIL:
            break                   # the account just got refused; stop
    if done:
        log(f"warm: {done} fetched, {len(due) - done} still due "
            f"of {len(_warm_pool)} front-table names")
    return done


def refresh_sections(do_trades: bool = True) -> bool:
    """Heatmap, sector rotation, insider and congressional trades.

    Slower and less time-critical than prices, so it runs on its own cadence:
    the treemap costs one request per constituent.

    ``do_trades`` gates the insider/congress pulls onto a slower cadence
    still. Covering ninety days of the Form 4 feed costs up to a hundred
    1,000-row pages -- roughly 60MB -- and doing that every hour plus the
    congress feeds burned ~1.8GB a day, which is how the FMP Starter plan's
    20GB trailing-30-day bandwidth cap died eleven days into the month and
    took every price refresh down with it. Disclosures move daily at most;
    the heatmap does not need to pay for them hourly.
    """
    if not market.configured():
        return False
    started = time.time()
    _, as_of = market.sectors()

    try:
        rows = market.heatmap()
        if rows:
            store.replace_heatmap(rows, as_of)
    except market.MarketError as exc:
        log(f"  heatmap: {exc}")
        rows = []

    try:
        # 120 days so the chart's longest time filter has data behind it.
        hist = market.sector_history(120)
        if hist:
            store.replace_sector_history(hist)
    except market.MarketError as exc:
        log(f"  sector history: {exc}")
        hist = []

    # Risk and return per sector. Eleven *full* price histories -- each ETF
    # from 1990 -- is ~4.5MB a run, ~108MB a day on the hourly cycle, second
    # only to the trade feeds in what ate the bandwidth cap. The underlying
    # numbers are years of closes; they do not move hourly, so they ride the
    # trades cadence (six-hourly) rather than the hourly one.
    if do_trades:
        try:
            risk = market.sector_risk_return()
            if risk:
                store.replace_sector_risk(risk)
                log(f"  sector risk: {len(risk)} sectors")
        except market.MarketError as exc:
            log(f"  sector risk: {exc}")
        except store.StoreError as exc:
            # 0053 not applied yet. The panel shows its empty state; nothing
            # else in this refresh should be lost over it.
            log(f"  sector risk write (apply 0053_sector_risk.sql): {exc}")

    if not do_trades:
        # The trades and their derived flow ride the slower trades cadence.
        log(f"sections: {len(rows)} heatmap, {len(hist)} sector series, "
            f"trades skipped (own cadence), {time.time()-started:.1f}s")
        return True

    try:
        # Pull sixty days once. FMP caps page at 100, so the worker uses
        # limit=1000 (see market._trade_list_page_size) — limit=100 only
        # reached ~10 days and left the inflow/outflow charts nearly empty.
        # 90 days: the widest window get_trades can serve, and what the
        # Insider & Congress page reads. The flow chart still requests its
        # own 60 through get_trade_flow.
        flow_days = 90
        ins_all = market.insider_trades(
            days=flow_days, store_cap=15000, collapse=False)
        con_all = market.congress_trades(days=flow_days, store_cap=8000)

        # Persist the full 90-day material pulls. Tables still request 7 / 14
        # days via get_trades; flow charts and fallbacks need the longer set.
        # No congress amount floor (MIN_CONGRESS_AMOUNT = 0).
        ins_store = ins_all[:8000]
        con_store = con_all[:3500]
        store.replace_trades(ins_store, con_store)
        log(f"  trades pulled: {len(ins_all)} insider / {len(con_all)} congress "
            f"over {flow_days}d; stored {len(ins_store)} / {len(con_store)}")
    except market.MarketError as exc:
        log(f"  trades: {exc}")
        ins = con = []
        ins_all = con_all = []
        flow_days = 90
    except Exception as exc:
        log(f"  trades: {exc}")
        ins = con = []
        ins_all = con_all = []
        flow_days = 90
    else:
        ins = ins_store
        con = con_store

    try:
        flow = market.trade_flow_daily(ins_all, con_all, days=flow_days)
        store.replace_trade_flow(flow)
        nonzero = sum(1 for r in flow
                      if (r.get("inflow") or 0) > 0 or (r.get("outflow") or 0) > 0)
        log(f"  trade flow: {len(flow)} day-rows, {nonzero} with volume")
    except Exception as exc:
        # Flow RPC may not be applied yet; keep the tables writing.
        log(f"  trade flow: {exc}")
        flow = []

    log(f"sections: {len(rows)} heatmap, {len(hist)} sector series, "
        f"{len(ins)} insider rows, {len(con)} congress rows, "
        f"{len(flow)} flow days, "
        f"{time.time()-started:.1f}s")
    return True


# The calendar lists every filer that reports, shells and OTC stubs included.
# The page hides anything below this unless the visitor watchlists it, and the
# same figure orders the day, so it has to be stored per event.
EARNINGS_MIN_CAP = float(os.environ.get("EARNINGS_MIN_CAP", "1e9"))


def refresh_earnings() -> bool:
    """FMP earnings calendar for the window the Earnings page reads."""
    if not market.configured():
        return False
    start = dt.date.today() - dt.timedelta(days=7)
    # FMP allows ~90 days; keep enough runway for "next earning" on mega-caps
    # that just reported (e.g. AAPL → late October).
    end = dt.date.today() + dt.timedelta(days=90)
    try:
        rows = market.earnings_calendar(start, end)
    except market.MarketError as exc:
        log(f"  earnings: {exc}")
        return False
    if not rows:
        log(f"earnings: FMP returned no rows for "
            f"{start.isoformat()} → {end.isoformat()}")
        return False

    # One screener call covers the whole above-$1B universe. Losing it costs
    # ordering and the size filter, not the calendar, so it must not abort.
    caps = {}
    try:
        caps = market.cap_universe(EARNINGS_MIN_CAP)
    except market.MarketError as exc:
        log(f"  earnings: market caps unavailable ({exc})")
    stamped = 0
    for r in rows:
        known = caps.get(r["symbol"])
        if not known:
            continue
        r["market_cap"] = known.get("market_cap")
        r["name"] = known.get("name")
        stamped += 1

    try:
        n = store.replace_earnings(rows)
    except store.StoreError as exc:
        # Missing migration or RPC must not take down the whole worker loop.
        log(f"  earnings write failed: {exc}")
        return False
    log(f"earnings: {len(rows)} events ({n} written, {stamped} above "
        f"${EARNINGS_MIN_CAP/1e9:g}B), {start.isoformat()} → {end.isoformat()}")
    return True


def refresh_logos(limit: int = 40) -> int:
    """Fetch Logo.dev images for index + common stocks + crypto; cache in Supabase.

    Priority set: S&P 500, Nasdaq-100, Dow, Russell 1000, common stocks, and
    top crypto. ``logos_due`` skips symbols already cached within 30 days.
    Returns rows upserted.
    """
    crypto_n = int(os.environ.get("LOGOS_CRYPTO_N", "80"))
    common_n = int(os.environ.get("LOGOS_COMMON_N", "2000"))
    targets = market.logo_priority_targets(crypto_n=crypto_n, common_n=common_n)
    due = store.logos_due(targets, limit=limit)
    if not due:
        log("logos: nothing due (priority indexes + crypto already cached)")
        return 0
    rows = []
    ok = miss = err = 0
    for t in due:
        sym = (t.get("symbol") or "").upper()
        kind = t.get("kind") or "stock"
        if not sym:
            continue
        row = market.download_logo(sym, kind)
        rows.append(row)
        st = row.get("status")
        if st == "ok":
            ok += 1
        elif st == "missing":
            miss += 1
        else:
            err += 1
    n = store.upsert_symbol_logos(rows) if rows else 0
    log(f"logos: upserted {n} (ok={ok} missing={miss} error={err})")
    return n


def refresh_economic_calendar() -> bool:
    """FMP US economic releases for the portfolio Calendar panel."""
    if not market.configured():
        return False
    start = dt.date.today() - dt.timedelta(days=14)
    end = dt.date.today() + dt.timedelta(days=60)
    try:
        rows = market.economic_calendar(start, end, country="US")
    except market.MarketError as exc:
        log(f"  economic calendar: {exc}")
        return False
    if not rows:
        log(f"economic calendar: FMP returned no US rows for "
            f"{start.isoformat()} → {end.isoformat()}")
        return False
    try:
        n = store.replace_economic_calendar(rows)
    except store.StoreError as exc:
        log(f"  economic calendar write failed: {exc}")
        return False
    log(f"economic calendar: {len(rows)} US events ({n} written), "
        f"{start.isoformat()} → {end.isoformat()}")
    return True


def refresh_intraday(symbol: str) -> bool:
    """Fetch one symbol's chart series on demand."""
    if not market.configured():
        return False
    pts = market.intraday(symbol, days=2)
    if not pts:
        # Close the request anyway. A symbol FMP has no series for -- a
        # delisting, a ticker that never traded -- would otherwise sit at the
        # head of the queue and be drawn again on every pass.
        try:
            store.skip_intraday(symbol)
        except store.StoreError:
            pass
        return False
    store.upsert_intraday(symbol, pts, pts[-1]["t"][:10])
    q = market.quote(symbol)
    if q:
        store.upsert_quotes([q])
    return True


def drain_intraday(max_items: int = 5) -> int:
    """Fetch chart series for companies someone has just opened or followed.

    The market refresh pre-fetches a fixed set. Anything outside it -- a
    company added to a personal watchlist -- is pulled here, so it has a chart
    within a minute of being followed rather than at the next refresh.
    """
    if not market.configured():
        return 0
    try:
        pending = store.pending_intraday(max_items)
    except store.StoreError as exc:
        # 0020 not applied yet. The page falls back to daily closes, so this
        # degrades rather than taking the loop down with it.
        log(f"  intraday queue unavailable (apply 0020_intraday_requests.sql): {exc}")
        return 0
    return sum(1 for sym in pending if refresh_intraday(sym))


# ---------------------------------------------------------------------------
# Scheduled refresh of companies we already hold
# ---------------------------------------------------------------------------

def refresh_stale(limit: int = 20) -> int:
    due = store.companies_due(RECHECK_HOURS, limit)
    for c in due:
        ingest_safely(c["ticker"])
    if due:
        log(f"refreshed {len(due)} stale companies")
    return len(due)


# ---------------------------------------------------------------------------
# The loop Render runs
# ---------------------------------------------------------------------------

def run() -> None:
    log("worker up — the only process talking to the SEC and to FMP")
    last_directory = 0.0
    last_market = 0.0
    last_sections = 0.0
    last_logos = 0.0
    last_sweep_day: dt.date | None = None
    market_every = int(os.environ.get("MARKET_REFRESH_SECONDS", "900"))
    sections_every = int(os.environ.get("SECTIONS_REFRESH_SECONDS", "3600"))
    # The insider/congress pulls cost ~65MB of FMP bandwidth per run (up to a
    # hundred 1,000-row pages to cover ninety days). Hourly, that was ~1.8GB a
    # day and exhausted the Starter plan's 20GB/30-day cap mid-month. Every
    # six hours is ~260MB/day -- inside the cap with room for everything else
    # -- and still four refreshes through each trading day.
    last_trades = 0.0
    trades_every = int(os.environ.get("TRADES_REFRESH_SECONDS", "21600"))
    # Pace Logo.dev: a small batch each cycle until the priority set is warm.
    logos_every = int(os.environ.get("LOGOS_REFRESH_SECONDS", "3600"))
    # Larger priority set (indexes + common stocks + crypto) — 50/hour warms
    # ~1.2k logos/day and stays well under Logo.dev's free monthly cap.
    logos_batch = int(os.environ.get("LOGOS_BATCH", "50"))

    while True:
        try:
            now = time.time()

            if now - last_directory > 24 * 3600:
                sync_directory()
                last_directory = now

            if now - last_market > market_every:
                try:
                    refresh_market()
                except market.MarketError as exc:
                    log(f"market refresh failed (continuing): {exc}")
                try:
                    rebuild_warm_pool()
                except Exception as exc:
                    log(f"warm pool rebuild failed (continuing): {exc}")
                try:
                    refresh_news()
                except market.MarketError as exc:
                    log(f"news refresh failed (continuing): {exc}")
                # Keep the Earnings page warm with the market cycle (one FMP
                # call) so a deploy does not wait an hour for the first fill.
                try:
                    refresh_earnings()
                except market.MarketError as exc:
                    log(f"earnings refresh failed (continuing): {exc}")
                try:
                    refresh_economic_calendar()
                except market.MarketError as exc:
                    log(f"economic calendar refresh failed (continuing): {exc}")
                last_market = now

            if now - last_sections > sections_every:
                try:
                    refresh_industry_pe()
                except market.MarketError as exc:
                    log(f"industry PE failed (continuing): {exc}")
                try:
                    trades_due = now - last_trades > trades_every
                    refresh_sections(do_trades=trades_due)
                    if trades_due:
                        last_trades = now
                except market.MarketError as exc:
                    log(f"sections refresh failed (continuing): {exc}")
                try:
                    refresh_indexes(holdings=True)
                except Exception as exc:
                    log(f"index holdings refresh failed (continuing): {exc}")
                last_sections = now

            if now - last_logos > logos_every:
                try:
                    refresh_logos(logos_batch)
                except store.StoreError as exc:
                    log(f"logos refresh failed (continuing): {exc}")
                except Exception as exc:
                    log(f"logos refresh failed (continuing): {type(exc).__name__}: {exc}")
                last_logos = now

            # Visitors first: a queued company should appear within a minute.
            if drain_backfill():
                continue
            if drain_prices():
                continue
            if drain_profiles():
                continue
            if drain_employee_history():
                continue
            if drain_intraday():
                continue
            if drain_long_closes():
                continue
            if drain_analyst():
                continue

            # Nobody waiting: spend the quiet keeping the front tables warm,
            # so the first click of the day lands on data already there.
            try:
                warm_reports()
            except store.StoreError as exc:
                log(f"warm pass failed (continuing): {exc}")

            today = dt.date.today()
            if last_sweep_day != today and dt.datetime.now().hour >= 22:
                sweep(today)
                last_sweep_day = today

            refresh_stale(5)

        except store.StoreError as exc:
            log(f"store error, stopping: {exc}")
            raise
        except Exception as exc:
            log(f"loop error (continuing): {type(exc).__name__}: {exc}")
            traceback.print_exc()

        time.sleep(POLL)


# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "run"

    if cmd != "stats":
        log(f"SEC contact: {edgar.CONTACT}")
        if edgar.CONTACT == "dashboard-user@example.com":
            log("  warning: set SEC_CONTACT to a real address")

    if cmd == "sync-directory":
        sync_directory()
    elif cmd == "sync-market-symbols":
        sync_market_symbols()
    elif cmd == "seed":
        seed(int(argv[1]) if len(argv) > 1 else 500)
    elif cmd == "ingest":
        if len(argv) < 2:
            print("usage: python worker.py ingest TICKER [TICKER ...]")
            return 2
        for t in argv[1:]:
            err = ingest_safely(t)
            if err:
                log(f"{t}: {err}")
    elif cmd == "backfill":
        log(f"drained {drain_backfill()} request(s)")
    elif cmd == "sweep":
        sweep(dt.date.fromisoformat(argv[1]) if len(argv) > 1 else None)
    elif cmd == "prices":
        if len(argv) > 1:
            # An explicit symbol skips the queue, for checking one by hand.
            for sym in argv[1:]:
                fetch_prices(sym)
        else:
            log(f"filled {drain_prices(25)} price request(s)")

    elif cmd == "analyst":
        if len(argv) > 1:
            for sym in argv[1:]:
                fetch_analyst(sym)
        else:
            log(f"filled {drain_analyst(25)} analyst request(s)")

    elif cmd == "industry-pe":
        log("industry PE refreshed" if refresh_industry_pe()
            else "industry PE unavailable")

    elif cmd == "market":
        log("market refreshed" if refresh_market()
            else "FMP_API_KEY not set; nothing to do")
    elif cmd == "warm":
        # One full pass by hand: everything due, not just a loop batch.
        if not refresh_market():
            log("FMP_API_KEY not set; nothing to do")
        else:
            rebuild_warm_pool()
            n = warm_reports(len(_warm_pool))
            log(f"warm: {n} name(s) fetched"
                if n else "warm: everything already fresh")
    elif cmd == "indexes":
        log("indexes refreshed" if refresh_indexes(holdings=True)
            else "FMP_API_KEY not set; nothing to do")
    elif cmd == "news":
        log("news refreshed" if refresh_news()
            else "FMP_API_KEY not set; nothing to do")
    elif cmd == "sections":
        log("sections refreshed" if refresh_sections()
            else "FMP_API_KEY not set; nothing to do")
    elif cmd == "earnings":
        log("earnings refreshed" if refresh_earnings()
            else "earnings unavailable (check FMP_API_KEY / plan / migration)")
    elif cmd == "economics" or cmd == "economic-calendar":
        log("economic calendar refreshed" if refresh_economic_calendar()
            else "economic calendar unavailable (check FMP_API_KEY / plan / migration)")
    elif cmd == "logos":
        n = refresh_logos(int(argv[1]) if len(argv) > 1 else 40)
        log(f"logos done ({n} row(s))" if n else "logos: nothing due or upsert failed")
    elif cmd == "profiles":
        if len(argv) > 1:
            for sym in argv[1:]:
                log(f"{sym}: {'ok' if fill_company_profile(sym.upper()) else 'failed'}")
        else:
            n = drain_profiles(40)
            log(f"filled {n} company profile(s)" if n
                else "no profiles pending (apply 0031, or none missing)")
    elif cmd == "employees":
        if len(argv) > 1:
            for sym in argv[1:]:
                log(f"{sym}: {'ok' if fill_employee_history(sym.upper()) else 'failed'}")
        else:
            n = drain_employee_history(40)
            log(f"filled {n} employee history series" if n
                else "no employee history pending (apply 0033, or none missing)")
    elif cmd == "intraday":
        if len(argv) < 2:
            print("usage: python worker.py intraday TICKER")
            return 2
        for t in argv[1:]:
            log(f"{t}: {'ok' if refresh_intraday(t.upper()) else 'no data'}")
    elif cmd == "funds":
        try:
            funds = market.fund_list()
        except market.MarketError as exc:
            print(f"fund screen failed: {exc}")
            return 1
        n = store.upsert_market_symbols(funds)
        mm = sum(1 for f in funds if f.get("kind") == "money_market")
        log(f"funds: {len(funds):,} fetched ({mm} money market), {n:,} rows written")
    elif cmd == "long-closes":
        # Named symbols, or drain whatever the portfolio pages have queued.
        if len(argv) > 1:
            for t in argv[1:]:
                sym = t.upper()
                try:
                    rows = market.closes(sym, years=10)
                except market.MarketError as exc:
                    log(f"{sym}: {exc}")
                    continue
                n = store.upsert_long_closes(sym, rows)
                log(f"{sym}: {n} closes"
                    + (f" ({rows[0]['d']} -> {rows[-1]['d']})" if rows else ""))
        else:
            total = 0
            while True:
                n = drain_long_closes(5)
                total += n
                if not n:
                    break
            log(f"long closes: {total} symbols filled")
    elif cmd == "stats":
        for k, v in (store.stats() or {}).items():
            print(f"  {k:<24} {v}")
    elif cmd == "run":
        run()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except store.StoreError as exc:
        print(f"\nconfiguration problem:\n  {exc}\n", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
