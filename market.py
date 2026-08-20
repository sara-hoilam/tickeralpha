"""
market.py -- market data from Financial Modeling Prep.

This is the second source, and it answers a different question from the SEC.
EDGAR knows what a company earned last quarter; it knows nothing about what
the stock did today. FMP supplies prices, market capitalisation, the day's
movers and sector performance -- and nothing here touches the financial
statements, which stay with the filings.

The API key is a paid secret, so every call happens in the worker. The browser
reads the results out of Supabase like everything else.

Standard library only.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://financialmodelingprep.com/stable"
# A couple of endpoints never made it onto /stable; those calls pass this in.
V3_BASE = "https://financialmodelingprep.com/api/v3"
KEY = os.environ.get("FMP_API_KEY", "")

# FMP bills per request and rate-limits per minute. Nothing here is urgent, so
# pace it rather than risk a 429 mid-refresh.
_MIN_INTERVAL = 0.25
_last = [0.0]


class MarketError(RuntimeError):
    pass


def configured() -> bool:
    return bool(KEY)


def _get(path: str, _base: str | None = None, **params):
    """One FMP call. ``_base`` overrides the stable API root for the few
    endpoints that only exist on the older versioned one."""
    if not KEY:
        raise MarketError("FMP_API_KEY is not set; market data is unavailable.")

    wait = _MIN_INTERVAL - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()

    params["apikey"] = KEY
    url = f"{_base or BASE}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:200]
        if exc.code == 402:
            raise MarketError(f"{path}: not included in this FMP plan") from exc
        raise MarketError(f"{path}: HTTP {exc.code} {body}") from exc
    except Exception as exc:
        raise MarketError(f"{path}: {exc}") from exc


def _is_page_limit_error(exc: BaseException) -> bool:
    """True when FMP rejected a page past its hard maximum (currently 100)."""
    msg = str(exc).lower()
    # FMP's own message misspells "Maximum" as "Maxmium".
    return "maximum page" in msg or "maxmium" in msg


# ---------------------------------------------------------------------------
# The day's movers
# ---------------------------------------------------------------------------

# FMP's movers lists are dominated by sub-dollar stocks that doubled on no
# volume. A "top stock of the day" that is a $0.09 shell up 200% is noise, not
# information, so anything below this price is left out.
MIN_MOVER_PRICE = float(os.environ.get("MIN_MOVER_PRICE", "5"))
MIN_MOVER_CAP = float(os.environ.get("MIN_MOVER_CAP", "1e9"))

# The screener returns the whole large-cap universe in one call, which is what
# makes the market-cap filter affordable: the alternative is a quote request
# per candidate. Cached for the length of a refresh so gainers, losers and
# actives share the one call.
#
# 5,000 was not the whole universe: above $1B there are ~5,800 listings, and a
# truncated list silently drops everything from the smallest returned cap
# downwards -- which is a filter nobody asked for.
SCREENER_LIMIT = 10000

_caps_cache: dict[str, object] = {"at": 0.0, "min": None, "rows": [],
                                  "symbols": set(), "caps": {}}


def _screener(min_cap: float | None = None, ttl: int = 900) -> list[dict]:
    """The raw screener rows, cached. Carries market cap and company name."""
    want = float(min_cap or MIN_MOVER_CAP)
    if (time.time() - float(_caps_cache["at"]) < ttl
            and _caps_cache["rows"] and _caps_cache["min"] == want):
        return _caps_cache["rows"]             # type: ignore[return-value]
    rows = _get("company-screener", marketCapMoreThan=int(want),
                isActivelyTrading="true", limit=SCREENER_LIMIT) or []
    if rows:
        _caps_cache.update(
            at=time.time(), min=want, rows=rows,
            symbols={r["symbol"] for r in rows if r.get("symbol")},
            caps={r["symbol"]: r for r in rows if r.get("symbol")})
    return rows


def large_caps(min_cap: float | None = None, ttl: int = 900) -> set[str]:
    """Symbols above `min_cap`, as a set for cheap membership tests."""
    _screener(min_cap, ttl)
    return _caps_cache["symbols"]              # type: ignore[return-value]


def cap_universe(min_cap: float | None = None, ttl: int = 900) -> dict[str, dict]:
    """Symbol -> {market_cap, name} for every company above `min_cap`.

    The earnings calendar names ~1,100 symbols on a busy day and only a few
    hundred of them are companies anyone follows; this is how the rest are
    told apart, for one request rather than one per symbol.

    ETFs and funds are left out the same way `top_by_cap` leaves them out: a
    fund does not report earnings, and one of them ranks above ConocoPhillips
    on the day's calendar if you let it.
    """
    _screener(min_cap, ttl)
    return {sym: {"market_cap": r.get("marketCap"),
                  "name": r.get("companyName")}
            for sym, r in (_caps_cache["caps"] or {}).items()   # type: ignore[union-attr]
            if r.get("marketCap") is not None
            and not r.get("isEtf") and not r.get("isFund")}


def _movers(path: str, kind: str, limit: int, allowed: set[str] | None) -> list[dict]:
    rows = _get(path) or []
    out = []
    for r in rows:
        sym, price = r.get("symbol"), r.get("price")
        if not sym or price is None or price < MIN_MOVER_PRICE:
            continue
        if allowed is not None and sym not in allowed:
            continue
        out.append({
            "kind": kind,
            "rank": len(out) + 1,
            "symbol": sym,
            "name": r.get("name"),
            "price": price,
            "change": r.get("change"),
            "change_pct": r.get("changesPercentage"),
            "exchange": r.get("exchange"),
        })
        if len(out) >= limit:
            break
    return out


def gainers(limit: int = 25, min_cap: float | None = None) -> list[dict]:
    return _movers("biggest-gainers", "gainer", limit, large_caps(min_cap))


def losers(limit: int = 25, min_cap: float | None = None) -> list[dict]:
    return _movers("biggest-losers", "loser", limit, large_caps(min_cap))


def actives(limit: int = 25, min_cap: float | None = None) -> list[dict]:
    return _movers("most-actives", "active", limit, large_caps(min_cap))


# ---------------------------------------------------------------------------
# Sectors
# ---------------------------------------------------------------------------

def sectors(day: dt.date | None = None, look_back: int = 6) -> tuple[list[dict], str | None]:
    """Average move per sector on the last trading day.

    Asked on a weekend or a holiday the snapshot is empty, so walk back until
    a session turns up. Returns the rows and the date they belong to, because
    "the last trading day" is something the page should be able to say out
    loud rather than imply.

    FMP reports one row per sector per exchange, so the exchanges are averaged
    into a single figure per sector.
    """
    start = day or dt.date.today()
    for back in range(look_back):
        d = start - dt.timedelta(days=back)
        rows = _get("sector-performance-snapshot", date=d.isoformat()) or []
        agg: dict[str, list[float]] = {}
        for r in rows:
            s, v = r.get("sector"), r.get("averageChange")
            if s and v is not None:
                agg.setdefault(s, []).append(float(v))
        if agg:
            out = sorted(
                ({"sector": s, "change_pct": sum(v) / len(v)} for s, v in agg.items()),
                key=lambda r: -r["change_pct"])
            return out, d.isoformat()
    return [], None


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------

def quote(symbol: str) -> dict | None:
    rows = _get("quote", symbol=symbol) or []
    if not rows:
        return None
    r = rows[0]
    return {
        "symbol": r.get("symbol"),
        "name": r.get("name"),
        "price": r.get("price"),
        "change": r.get("change"),
        "change_pct": r.get("changePercentage"),
        "volume": r.get("volume"),
        "market_cap": r.get("marketCap"),
        "day_low": r.get("dayLow"),
        "day_high": r.get("dayHigh"),
        "exchange": r.get("exchange"),
    }


# Major indexes.
#
# Prices / history: FMP caret symbols (^GSPC / ^IXIC / ^DJI) — same paid feed
# as the rest of Markets Today. Verified on the stable quote + EOD endpoints.
#
# Holdings: prefer Slickcharts, which publishes methodology-correct portfolio
# weights that sum to ~100% (float market-cap for S&P 500 / Nasdaq-100,
# price-weighted for the Dow). FMP's *-constituent endpoints are preferred
# when the plan includes them (often 402 on starter tiers). Wikipedia /
# datasets CSV are membership fallbacks only; weights are then recomputed.
#
# NASDAQ card price is the Composite (^IXIC). Holdings for IXIC use the
# Nasdaq-100 — the Composite has ~3,000+ members and is not a useful table.
INDEXES = {
    "SPX": {
        "fmp": "^GSPC",
        "name": "S&P 500",
        "exchange": "INDEX",
        "constituents": "sp500",
        "weighting": "market_cap",
        "fmp_constituent": "sp500-constituent",
        "slickcharts": "https://www.slickcharts.com/sp500",
        "wikipedia": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        # Forward P/E history (History of Market public JSON).
        "pe_api": "https://historyofmarket.com/api/sp500/forward-pe.json",
        "pe_label": "S&P 500",
    },
    "IXIC": {
        "fmp": "^IXIC",
        "name": "NASDAQ Composite",
        "holdings_name": "Nasdaq-100",
        "exchange": "INDEX",
        "constituents": "nasdaq100",
        "weighting": "market_cap",
        "fmp_constituent": "nasdaq-constituent",
        "slickcharts": "https://www.slickcharts.com/nasdaq100",
        # Composite price; Nasdaq-100 forward P/E is the useful valuation series.
        "pe_api": "https://historyofmarket.com/api/ndx/forward-pe.json",
        "pe_label": "NASDAQ",
    },
    "DJI": {
        "fmp": "^DJI",
        "name": "Dow Jones Industrial Average",
        "exchange": "INDEX",
        "constituents": "dowjones",
        "weighting": "price",  # DJIA is price-weighted, not market-cap
        "fmp_constituent": "dowjones-constituent",
        "slickcharts": "https://www.slickcharts.com/dowjones",
        "wikipedia": "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average",
        "pe_label": "Dow",
    },
}

# Actively maintained S&P 500 membership mirror (Wikipedia → GitHub).
_SP500_DATASETS_CSV = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies"
    "/master/data/constituents.csv")

_UA = "TickerAlpha/1.0 (index-holdings; +https://github.com/sara-hoilam/stock-dashboard)"


def index_quote(alias: str) -> dict | None:
    """Quote one major index, returned under its searchable alias (SPX/…)."""
    meta = INDEXES.get((alias or "").upper())
    if not meta:
        return None
    q = quote(meta["fmp"])
    if not q:
        return None
    q["symbol"] = alias.upper()
    q["name"] = meta["name"]
    q["exchange"] = meta["exchange"]
    return q


def index_quotes() -> list[dict]:
    out = []
    for alias in INDEXES:
        try:
            q = index_quote(alias)
        except MarketError:
            q = None
        if q:
            out.append(q)
    return out


def index_history(alias: str, limit: int = 420) -> list[dict]:
    """Daily OHLCV for an index alias, as ``{d, o, h, l, c, v}`` bars."""
    meta = INDEXES.get((alias or "").upper())
    if not meta:
        return []
    try:
        rows = _get("historical-price-eod/full", symbol=meta["fmp"]) or []
    except MarketError:
        return []
    bars = []
    for r in rows[: max(1, int(limit or 420))]:
        d = r.get("date")
        c = r.get("close") if r.get("close") is not None else r.get("price")
        if not d or c is None:
            continue
        bars.append({
            "d": d,
            "o": r.get("open"),
            "h": r.get("high"),
            "l": r.get("low"),
            "c": c,
            "v": r.get("volume"),
        })
    bars.sort(key=lambda b: b["d"])
    return bars


def _http_get_text(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "text/html,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _clean_index_symbol(raw: str) -> str | None:
    sym = (raw or "").upper().strip().strip('"').replace("-", ".")
    if not sym or not re.match(r"^[A-Z][A-Z0-9.\-]{0,9}$", sym):
        return None
    return sym


def _fmp_constituent_rows(path: str) -> list[dict]:
    """FMP index constituents when the plan includes them (else [])."""
    if not path or not KEY:
        return []
    try:
        rows = _get(path) or []
    except MarketError:
        return []
    out = []
    for r in rows:
        sym = _clean_index_symbol(r.get("symbol") or "")
        if not sym:
            continue
        out.append({
            "symbol": sym,
            "name": (r.get("name") or r.get("companyName") or "").strip() or None,
            "industry": (r.get("subSector") or r.get("industry") or "").strip() or None,
            "sector": (r.get("sector") or "").strip() or None,
            "weightPct": None,
            "source": "fmp",
        })
    return out


def _slickcharts_holdings(url: str) -> list[dict]:
    """Parse Slickcharts index table: Symbol + Portfolio % (methodology weights)."""
    if not url:
        return []
    try:
        text = _http_get_text(url)
    except Exception:
        return []
    table = re.search(r"<table[^>]*>(.*?)</table>", text, re.I | re.S)
    if not table:
        return []
    out, seen = [], set()
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table.group(1), re.I | re.S)[1:]:
        cells = [re.sub(r"<[^>]+>", "", c) for c in
                 re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.I | re.S)]
        cells = [" ".join(c.replace("\xa0", " ").split()) for c in cells]
        if len(cells) < 4:
            continue
        # Columns: #, Company, Symbol, Portfolio%, Price, …
        sym = _clean_index_symbol(cells[2])
        if not sym or sym in seen:
            continue
        try:
            weight = float(cells[3].replace("%", "").replace(",", "").strip())
        except ValueError:
            weight = None
        if weight is not None and not (0 < weight <= 100):
            weight = None
        name = cells[1].strip() or None
        seen.add(sym)
        out.append({
            "symbol": sym,
            "name": name,
            "industry": None,
            "weightPct": weight,
            "source": "slickcharts",
        })
    # Sanity: expect a near-complete published book (Dow 30, NDX ~100, SPX ~500).
    if len(out) < 20:
        return []
    return out


def _wikipedia_sp500() -> list[dict]:
    """Current S&P 500 membership from Wikipedia's constituent table."""
    try:
        text = _http_get_text(INDEXES["SPX"]["wikipedia"])
    except Exception:
        return []
    table = re.search(
        r"<table[^>]*class=\"[^\"]*wikitable[^\"]*\"[^>]*>(.*?)</table>",
        text, re.I | re.S)
    if not table:
        return []
    out, seen = [], set()
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table.group(1), re.I | re.S)[1:]:
        cells = [re.sub(r"<[^>]+>", "", c) for c in
                 re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.I | re.S)]
        cells = [" ".join(c.replace("\xa0", " ").split()) for c in cells]
        if len(cells) < 4:
            continue
        sym = _clean_index_symbol(cells[0])
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append({
            "symbol": sym,
            "name": cells[1] or None,
            "industry": cells[3] or None,   # GICS Sub-Industry
            "sector": cells[2] or None,
            "weightPct": None,
            "source": "wikipedia",
        })
    return out if len(out) >= 400 else []


def _wikipedia_dow() -> list[dict]:
    """DJIA components + published index weightings from Wikipedia."""
    try:
        text = _http_get_text(INDEXES["DJI"]["wikipedia"])
    except Exception:
        return []
    # First wikitable with a Symbol column.
    for block in re.findall(
            r"<table[^>]*class=\"[^\"]*wikitable[^\"]*\"[^>]*>(.*?)</table>",
            text, re.I | re.S):
        header = re.findall(r"<th[^>]*>(.*?)</th>", block, re.I | re.S)
        headers = [" ".join(re.sub(r"<[^>]+>", "", h).split()).lower() for h in header]
        if not any("symbol" == h for h in headers):
            continue
        out, seen = [], set()
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", block, re.I | re.S)[1:]:
            cells = [re.sub(r"<[^>]+>", "", c) for c in
                     re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.I | re.S)]
            cells = [" ".join(c.replace("\xa0", " ").split()) for c in cells]
            if len(cells) < 4:
                continue
            # Company, Exchange, Symbol, Sector, …, Index weighting
            sym = _clean_index_symbol(cells[2] if len(cells) > 2 else "")
            if not sym or sym in seen:
                continue
            weight = None
            for c in reversed(cells):
                m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%", c)
                if m:
                    try:
                        weight = float(m.group(1))
                    except ValueError:
                        weight = None
                    break
            seen.add(sym)
            out.append({
                "symbol": sym,
                "name": cells[0] or None,
                "industry": cells[3] if len(cells) > 3 else None,
                "weightPct": weight,
                "source": "wikipedia",
            })
        if len(out) >= 25:
            return out
    return []


def _datasets_sp500() -> list[dict]:
    """S&P 500 membership from the datasets/s-and-p-500-companies CSV mirror."""
    try:
        text = _http_get_text(_SP500_DATASETS_CSV)
    except Exception:
        return []
    out, seen = [], set()
    for i, line in enumerate(text.splitlines()):
        if i == 0 or not line.strip():
            continue
        # Symbol,Security,GICS Sector,GICS Sub-Industry,… (quoted fields OK).
        cols, cur, in_q = [], [], False
        for ch in line:
            if ch == '"':
                in_q = not in_q
            elif ch == "," and not in_q:
                cols.append("".join(cur))
                cur = []
            else:
                cur.append(ch)
        cols.append("".join(cur))
        if len(cols) < 4:
            continue
        sym = _clean_index_symbol(cols[0])
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append({
            "symbol": sym,
            "name": cols[1].strip() or None,
            "sector": cols[2].strip() or None,
            "industry": cols[3].strip() or None,
            "weightPct": None,
            "source": "datasets",
        })
    return out if len(out) >= 400 else []


def _enrich_holdings_meta(rows: list[dict], weighting: str) -> list[dict]:
    """Attach sector/industry (and fill missing weights) from FMP screener / quotes.

    Slickcharts (preferred weight source) does not publish industry. Without
    this pass the index pie collapses to a single \"Other / unlisted\" slice.
    """
    if not rows:
        return []
    member_set = {r["symbol"] for r in rows}
    caps: dict[str, float] = {}
    prices: dict[str, float] = {}
    names: dict[str, str] = {}
    industries: dict[str, str] = {}
    sectors: dict[str, str] = {}

    try:
        for r in _screener(1e8, ttl=3600) or []:
            sym = (r.get("symbol") or "").upper()
            if sym not in member_set:
                continue
            try:
                cap = float(r.get("marketCap") or 0)
            except (TypeError, ValueError):
                cap = 0.0
            try:
                px = float(r.get("price") or 0)
            except (TypeError, ValueError):
                px = 0.0
            if cap > 0:
                caps[sym] = cap
            if px > 0:
                prices[sym] = px
            if r.get("companyName"):
                names[sym] = r["companyName"]
            if r.get("industry"):
                industries[sym] = str(r["industry"]).strip()
            if r.get("sector"):
                sectors[sym] = str(r["sector"]).strip()
    except MarketError:
        pass

    need = [s for s in member_set
            if s not in caps or (weighting == "price" and s not in prices)][:60]
    if need:
        try:
            for q in quotes(need) or []:
                sym = (q.get("symbol") or "").upper()
                if not sym:
                    continue
                try:
                    cap = float(q.get("market_cap") or 0)
                except (TypeError, ValueError):
                    cap = 0.0
                try:
                    px = float(q.get("price") or 0)
                except (TypeError, ValueError):
                    px = 0.0
                if cap > 0:
                    caps[sym] = cap
                if px > 0:
                    prices[sym] = px
                if q.get("name"):
                    names[sym] = q["name"]
        except MarketError:
            pass

    have_weights = any(r.get("weightPct") is not None for r in rows)
    if not have_weights:
        if weighting == "price":
            total = sum(prices.get(r["symbol"], 0) for r in rows) or 0.0
            if total:
                for r in rows:
                    px = prices.get(r["symbol"], 0)
                    r["weightPct"] = round(100.0 * px / total, 4) if px else None
        else:
            total = sum(caps.get(r["symbol"], 0) for r in rows) or 0.0
            if total:
                for r in rows:
                    cap = caps.get(r["symbol"], 0)
                    r["weightPct"] = round(100.0 * cap / total, 4) if cap else None

    for r in rows:
        if not r.get("name") and names.get(r["symbol"]):
            r["name"] = names[r["symbol"]]
        if not r.get("industry") and industries.get(r["symbol"]):
            r["industry"] = industries[r["symbol"]]
        if not r.get("sector") and sectors.get(r["symbol"]):
            r["sector"] = sectors[r["symbol"]]
        if caps.get(r["symbol"]):
            r["marketCap"] = caps[r["symbol"]]
    return rows


def index_forward_pe_history(alias: str, years: int = 10) -> list[dict]:
    """Forward P/E history for an index, as ``[{d, pe}, …]`` oldest first.

    Prefers History of Market's published forward series (SPX / NDX). For the
    Dow (no public forward series on that feed), builds a price-scaled path
    from today's price-weighted constituent TTM P/E so the comparison chart
    still has a Dow line.
    """
    meta = INDEXES.get((alias or "").upper())
    if not meta:
        return []
    api = meta.get("pe_api")
    if api:
        try:
            raw = _http_get_text(api, timeout=30)
            data = json.loads(raw)
        except Exception:
            data = None
        pts = []
        if isinstance(data, dict):
            series = data.get("forward") or data.get("trailing") or []
            cutoff = (dt.date.today() - dt.timedelta(days=int(years) * 365)).isoformat()
            for row in series:
                d = (row.get("date") or "")[:10]
                try:
                    pe = float(row.get("value"))
                except (TypeError, ValueError):
                    continue
                if d and pe > 0 and d >= cutoff:
                    pts.append({"d": d, "pe": round(pe, 4)})
            pts.sort(key=lambda p: p["d"])
            if len(pts) >= 2:
                return pts

    # Dow (and any index without a public forward series): spot from holdings,
    # then scale along the index price path.
    return _index_pe_from_price_path(alias.upper(), years=years)


def _index_pe_spot(alias: str) -> float | None:
    """Weighted TTM P/E from current constituents + FMP quote P/Es.

    Uses each holding's published weight (Slickcharts methodology), so the Dow
    stays price-weighted and the S&P / NDX stay float market-cap weighted.
    """
    if (alias or "").upper() not in INDEXES:
        return None
    rows = index_holdings(alias)
    if not rows:
        return None
    # Need P/E per name — quote_detail carries ratios-ttm pe when available.
    num = den = 0.0  # Σ w / Σ(w/PE) ≡ weight-weighted harmonic mean of P/Es
    for r in rows:
        sym = r.get("symbol")
        w = r.get("weightPct")
        if not sym or not w:
            continue
        try:
            q = quote_detail(sym)
        except MarketError:
            q = None
        pe = (q or {}).get("pe")
        try:
            pe_f = float(pe) if pe is not None else 0.0
        except (TypeError, ValueError):
            pe_f = 0.0
        if pe_f <= 0:
            continue
        # weightPct already encodes methodology (Slickcharts).
        num += float(w)
        den += float(w) / pe_f
    if den <= 0:
        return None
    return num / den


def _index_pe_from_price_path(alias: str, years: int = 10) -> list[dict]:
    """Approximate PE history: spot_pe × price(t) / price_now."""
    spot = _index_pe_spot(alias)
    if not spot or spot <= 0:
        return []
    meta = INDEXES[alias]
    try:
        bars = closes(meta["fmp"], years) or []
    except MarketError:
        bars = []
    if len(bars) < 2:
        try:
            bars = [{"d": b["d"], "c": b["c"]} for b in index_history(alias, 420)]
        except Exception:
            bars = []
    bars = [b for b in bars if b.get("d") and b.get("c")]
    if len(bars) < 2:
        return [{"d": dt.date.today().isoformat(), "pe": round(spot, 4)}]
    last = float(bars[-1]["c"])
    if last <= 0:
        return []
    # Monthly points keep the series light and aligned with seasonality.
    by_month: dict[str, dict] = {}
    for b in bars:
        by_month[b["d"][:7]] = b
    out = []
    for k in sorted(by_month):
        b = by_month[k]
        pe = spot * (float(b["c"]) / last)
        if pe > 0:
            out.append({"d": b["d"], "pe": round(pe, 4)})
    return out


def index_holdings(alias: str) -> list[dict]:
    """Constituents + % weights for an index page / industry pie.

    Source order:
      1. Slickcharts published portfolio weights (best free methodology match)
      2. FMP ``*-constituent`` when the API plan allows
      3. Wikipedia / datasets membership, with weights recomputed
         (market-cap for SPX/NDX, price for DJI)
    """
    meta = INDEXES.get((alias or "").upper())
    if not meta:
        return []
    kind = meta["constituents"]
    weighting = meta.get("weighting") or "market_cap"
    rows: list[dict] = []
    source = None

    slick = _slickcharts_holdings(meta.get("slickcharts") or "")
    if slick:
        rows, source = slick, "slickcharts"

    if not rows:
        fmp_rows = _fmp_constituent_rows(meta.get("fmp_constituent") or "")
        if fmp_rows:
            rows, source = fmp_rows, "fmp"

    if not rows and kind == "sp500":
        rows = _wikipedia_sp500() or _datasets_sp500()
        source = rows[0]["source"] if rows else None
    elif not rows and kind == "dowjones":
        rows = _wikipedia_dow()
        source = "wikipedia" if rows else None

    if not rows:
        return []

    rows = _enrich_holdings_meta(rows, weighting)
    rows.sort(key=lambda r: (r.get("weightPct") is None,
                             -(r.get("weightPct") or 0), r["symbol"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
        r["source"] = source or r.get("source")
    return rows


def quote_detail(symbol: str) -> dict | None:
    """Everything the company page's price panel shows, in one call.

    `quote` already carries the session's range, the 52-week range and the
    moving averages, so the panel costs a single request. The ratios are a
    second, optional one -- a company with no earnings has no P/E, and that is
    a fact about the company rather than a failure to fetch.
    """
    rows = _get("quote", symbol=symbol) or []
    if not rows:
        return None
    r = rows[0]
    out = {
        "symbol": r.get("symbol"),
        "name": r.get("name"),
        "price": r.get("price"),
        "change": r.get("change"),
        "change_pct": r.get("changePercentage"),
        "open": r.get("open"),
        "previous_close": r.get("previousClose"),
        "day_low": r.get("dayLow"),
        "day_high": r.get("dayHigh"),
        "year_low": r.get("yearLow"),
        "year_high": r.get("yearHigh"),
        "volume": r.get("volume"),
        "market_cap": r.get("marketCap"),
        "avg_50": r.get("priceAvg50"),
        "avg_200": r.get("priceAvg200"),
        "exchange": r.get("exchange"),
    }
    try:
        rr = (_get("ratios-ttm", symbol=symbol) or [{}])[0]
        out["pe"] = rr.get("priceToEarningsRatioTTM")
        out["pb"] = rr.get("priceToBookRatioTTM")
        out["dividend_yield"] = rr.get("dividendYieldTTM")
    except MarketError:
        pass
    if out["market_cap"] and out["price"]:
        out["shares"] = out["market_cap"] / out["price"]
    return out


def daily(symbol: str, days: int = 300) -> list[dict]:
    """Daily bars, oldest first, for the candlestick chart."""
    end = dt.date.today()
    start = end - dt.timedelta(days=max(30, days))
    rows = _get("historical-price-eod/full", symbol=symbol,
                **{"from": start.isoformat(), "to": end.isoformat()}) or []
    bars = [{"d": r["date"], "o": r.get("open"), "h": r.get("high"),
             "l": r.get("low"), "c": r.get("close"), "v": r.get("volume")}
            for r in rows
            if r.get("date") and r.get("close") is not None]
    bars.sort(key=lambda b: b["d"])
    return bars


_SYMBOL_RE = re.compile(r"^[A-Z][A-Z.\-]{0,9}$")


def _clean_market_symbol(raw: str) -> str | None:
    sym = (raw or "").upper().strip()
    if not _SYMBOL_RE.match(sym):
        return None
    return sym


# Money market funds sit at $1.00 by design, which is exactly why the fund
# screen misses them: it ranks by market capitalisation and they have none worth
# ranking. They are also among the most commonly held, so the well-known ones
# are named here and typed separately from an ordinary mutual fund. The
# portfolio uses that distinction to treat them as cash-like rather than
# reporting a holding that never moves.
MONEY_MARKET_FUNDS = {
    "SPAXX": "Fidelity Government Money Market Fund",
    "FDRXX": "Fidelity Cash Reserves",
    "SPRXX": "Fidelity Money Market Fund",
    "FZFXX": "Fidelity Treasury Money Market Fund",
    "VMFXX": "Vanguard Federal Money Market Fund",
    "VMRXX": "Vanguard Cash Reserves Federal Money Market",
    "VUSXX": "Vanguard Treasury Money Market Fund",
    "SWVXX": "Schwab Value Advantage Money Fund",
    "SNAXX": "Schwab Value Advantage Money Fund Ultra",
    "SNSXX": "Schwab U.S. Treasury Money Fund",
}


def fund_list(pages: int = 8) -> list[dict]:
    """Mutual funds, from the screener rather than a list endpoint.

    FMP publishes no ``mutual-fund-list``; ``company-screener`` with
    ``isFund=true`` is the only bulk source, and it returns at most 1,000 rows
    per call ordered by market capitalisation. Walking a descending cap ceiling
    pages through it: each request asks for funds smaller than the smallest seen
    so far, so the largest funds arrive first and the walk stops when a page
    comes back short.
    """
    out: dict[str, dict] = {}
    ceiling: float | None = None
    for _ in range(max(1, pages)):
        params = {"isFund": "true", "limit": 1000}
        if ceiling is not None:
            params["marketCapLowerThan"] = int(ceiling)
        try:
            rows = _get("company-screener", **params) or []
        except MarketError:
            break
        if not rows:
            break
        caps = []
        for r in rows:
            sym = _clean_market_symbol(r.get("symbol") or "")
            name = (r.get("companyName") or "").strip()
            cap = r.get("marketCap")
            if cap is not None:
                caps.append(float(cap))
            if not sym or not name or sym in out:
                continue
            out[sym] = {
                "symbol": sym,
                "name": name,
                "kind": "money_market" if sym in MONEY_MARKET_FUNDS else "fund",
                "exchange": (r.get("exchangeShortName") or r.get("exchange") or "") or None,
            }
        if len(rows) < 1000 or not caps:
            break
        low = min(caps)
        # A page that cannot lower the ceiling would repeat for ever.
        if ceiling is not None and low >= ceiling:
            break
        ceiling = low

    # The named money market funds, whether or not the screen reached them.
    for sym, name in MONEY_MARKET_FUNDS.items():
        out.setdefault(sym, {"symbol": sym, "name": name,
                             "kind": "money_market", "exchange": None})
        out[sym]["kind"] = "money_market"

    return sorted(out.values(), key=lambda x: x["symbol"])


def etf_list() -> list[dict]:
    """All ETFs from FMP ``/etf-list`` (symbol + name)."""
    rows = _get("etf-list") or []
    out = []
    seen = set()
    for r in rows:
        sym = _clean_market_symbol(r.get("symbol") or "")
        name = (r.get("name") or "").strip()
        if not sym or not name or sym in seen:
            continue
        seen.add(sym)
        out.append({
            "symbol": sym,
            "name": name,
            "kind": "etf",
            "exchange": (r.get("exchange") or r.get("exchangeShortName") or "") or None,
        })
    out.sort(key=lambda x: x["symbol"])
    return out


def crypto_list() -> list[dict]:
    """Cryptocurrencies from FMP ``/cryptocurrency-list`` (e.g. BTCUSD).

    FMP sometimes attaches garbage names to long *USD tickers — e.g.
    ``BITCOINUSD`` → "HarryPotterObamaSonic10Inu…". Real Bitcoin is
    ``BTCUSD``. Drop pairs whose long stem does not appear in the name.
    """
    rows = _get("cryptocurrency-list") or []
    out = []
    seen = set()
    for r in rows:
        sym = _clean_market_symbol(r.get("symbol") or "")
        name = (r.get("name") or "").strip()
        if not sym or not name or sym in seen:
            continue
        if sym.endswith("USD") and len(sym) > 3:
            stem = sym[:-3]
            # Long stems like BITCOIN must appear in the name; short ones
            # (BTC, ETH, SOL) are allowed without that check.
            if len(stem) >= 6:
                compact = re.sub(r"[^A-Z0-9]", "", name.upper())
                if stem not in compact:
                    continue
        seen.add(sym)
        out.append({
            "symbol": sym,
            "name": name,
            "kind": "crypto",
            "exchange": (r.get("exchange") or r.get("exchangeShortName") or "CRYPTO") or None,
        })
    out.sort(key=lambda x: x["symbol"])
    return out


def dividends(symbol: str) -> list[dict]:
    """Per-share dividend history for one ticker (ex-date + amount).

    FMP ``/dividends?symbol=`` returns past and declared upcoming payouts.
    ``date`` is the ex-dividend date. Used by the portfolio page to credit
    cash on/after ex-date for holdings logged before that date.
    """
    rows = _get("dividends", symbol=symbol) or []
    out = []
    for r in rows:
        ex = (r.get("date") or r.get("exDividendDate") or "")[:10]
        amt = r.get("adjDividend")
        if amt is None:
            amt = r.get("dividend")
        if not ex or amt is None:
            continue
        try:
            amount = float(amt)
        except (TypeError, ValueError):
            continue
        if amount < 0:
            continue
        out.append({
            "exDate": ex,
            "amount": amount,
            "adjAmount": r.get("adjDividend"),
            "yield": r.get("yield"),
            "frequency": r.get("frequency"),
            "declarationDate": (r.get("declarationDate") or "")[:10] or None,
            "recordDate": (r.get("recordDate") or "")[:10] or None,
            "paymentDate": (r.get("paymentDate") or "")[:10] or None,
        })
    out.sort(key=lambda x: x["exDate"])
    return out


def quotes(symbols: list[str]) -> list[dict]:
    """Batch quoting is not in every FMP plan, so fetch one at a time.

    Slower, but it is the worker doing it on a timer, not a visitor waiting.
    """
    out = []
    for s in symbols:
        try:
            q = quote(s)
            if q and q.get("price") is not None:
                out.append(q)
        except MarketError:
            continue
    return out


# ---------------------------------------------------------------------------
# Intraday series for the chart
# ---------------------------------------------------------------------------

def intraday(symbol: str, days: int = 2) -> list[dict]:
    """Five-minute bars over the last couple of sessions, oldest first."""
    end = dt.date.today()
    start = end - dt.timedelta(days=max(1, days) + 3)   # pad for weekends
    rows = _get("historical-chart/5min", symbol=symbol,
                **{"from": start.isoformat(), "to": end.isoformat()}) or []
    pts = [{"t": r["date"], "c": r.get("close")} for r in rows
           if r.get("date") and r.get("close") is not None]
    pts.sort(key=lambda p: p["t"])
    # Keep the most recent two sessions' worth without needing a calendar.
    sessions = sorted({p["t"][:10] for p in pts})[-days:]
    return [p for p in pts if p["t"][:10] in sessions]


# ---------------------------------------------------------------------------
# Heatmap constituents
# ---------------------------------------------------------------------------
# FMP's index-constituent endpoint is not in every plan, so the heatmap runs
# off a curated large-cap list instead. Sector membership barely moves, and a
# treemap of ~55 names reads better than one of 500 anyway -- the small tiles
# in a full-index map are unreadable.
HEATMAP_UNIVERSE = [
    # Technology
    "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "AMD", "ADBE", "CSCO", "ACN",
    "TXN", "QCOM", "INTU", "IBM",
    # Communication services
    "GOOGL", "META", "NFLX", "DIS", "TMUS", "VZ", "T",
    # Consumer cyclical
    "AMZN", "TSLA", "HD", "MCD", "NKE", "SBUX", "LOW", "BKNG",
    # Consumer defensive
    "WMT", "PG", "COST", "KO", "PEP", "PM",
    # Healthcare
    "LLY", "UNH", "JNJ", "ABBV", "MRK", "TMO", "ABT", "PFE",
    # Financials
    "BRK-B", "JPM", "V", "MA", "BAC", "WFC", "GS",
    # Industrials, energy, utilities, materials, real estate
    "CAT", "GE", "RTX", "UNP", "XOM", "CVX", "COP", "NEE", "DUK",
    "LIN", "SHW", "AMT", "PLD",
]


def heatmap(symbols: list[str] | None = None) -> list[dict]:
    """Sector, market cap and day change per symbol, for the treemap.

    `profile` returns all three in one call, so this costs one request per
    name rather than three.
    """
    out = []
    for sym in (symbols or HEATMAP_UNIVERSE):
        try:
            rows = _get("profile", symbol=sym) or []
        except MarketError:
            continue
        if not rows:
            continue
        r = rows[0]
        if r.get("marketCap") and r.get("sector"):
            out.append({
                "symbol": r.get("symbol"),
                "name": r.get("companyName"),
                "sector": r.get("sector"),
                "industry": r.get("industry"),
                "market_cap": r.get("marketCap"),
                "price": r.get("price"),
                "change_pct": r.get("changePercentage"),
            })
    return out


# ---------------------------------------------------------------------------
# Sector movement over time
# ---------------------------------------------------------------------------

SECTOR_NAMES = [
    "Technology", "Communication Services", "Consumer Cyclical",
    "Consumer Defensive", "Healthcare", "Financial Services",
    "Industrials", "Energy", "Utilities", "Real Estate", "Basic Materials",
]


def sector_history(days: int = 45) -> list[dict]:
    """Daily average change per sector, for the rotation chart."""
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    out = []
    for sector in SECTOR_NAMES:
        try:
            rows = _get("historical-sector-performance", sector=sector,
                        **{"from": start.isoformat(), "to": end.isoformat()}) or []
        except MarketError:
            continue
        # One row per exchange per day; average them into a single series.
        by_day: dict[str, list[float]] = {}
        for r in rows:
            d, v = r.get("date"), r.get("averageChange")
            if d and v is not None:
                by_day.setdefault(d, []).append(float(v))
        series = [{"d": d, "v": sum(v) / len(v)} for d, v in sorted(by_day.items())]
        if series:
            out.append({"sector": sector, "series": series})
    return out


# ---------------------------------------------------------------------------
# Risk and return per sector, for the bubble chart
# ---------------------------------------------------------------------------
# Every combination the page offers is computed here rather than in the
# browser. There are only twenty-eight of them per sector and each is three
# numbers, so the whole matrix is a few kilobytes -- far less than the twenty
# years of daily closes it is derived from, and it means changing a filter is
# a redraw rather than a round trip.

RISK_PERIODS = {                      # label -> days back, or None for special
    "1W": 7, "1M": 30, "YTD": None, "1Y": 365,
    "5Y": 1826, "10Y": 3653, "ALL": None,
}
RISK_INTERVALS = ("daily", "weekly", "monthly", "annual")
PERIODS_PER_YEAR = {"daily": 252, "weekly": 52, "monthly": 12, "annual": 1}

# Below this many returns a standard deviation is noise dressed as a number.
MIN_RETURNS = 3


def _resample(series: list[tuple[str, float]], interval: str) -> list[tuple[str, float]]:
    """Last close of each bucket, oldest first."""
    if interval == "daily":
        return series
    if interval == "weekly":
        key = lambda d: dt.date.fromisoformat(d).isocalendar()[:2]   # noqa: E731
    elif interval == "monthly":
        key = lambda d: d[:7]                                        # noqa: E731
    else:
        key = lambda d: d[:4]                                        # noqa: E731
    out: list[tuple[str, float]] = []
    cur = last = None
    for d, c in series:
        k = key(d)
        if cur is not None and k != cur:
            out.append(last)
        cur, last = k, (d, c)
    if last:
        out.append(last)
    return out


def _risk_stats(series: list[tuple[str, float]], interval: str) -> dict | None:
    """Average return and volatility over one interval, or None if too short."""
    pts = _resample(series, interval)
    if len(pts) < MIN_RETURNS + 1:
        return None
    rets = [pts[i][1] / pts[i - 1][1] - 1
            for i in range(1, len(pts)) if pts[i - 1][1]]
    if len(rets) < MIN_RETURNS:
        return None
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)      # sample, not population
    vol = var ** 0.5
    return {
        "ret": mean,
        "vol": vol,
        # The annualised figure is the one people compare across intervals, so
        # it rides along even though the axes show the raw interval.
        "annVol": vol * (PERIODS_PER_YEAR[interval] ** 0.5),
        "n": n,
        "from": pts[0][0],
        "to": pts[-1][0],
    }


def _sector_caps() -> dict[str, float]:
    """Combined market capitalisation of each sector's US-listed companies.

    Used only to size the bubbles, which is why the billion-dollar floor is
    acceptable: it drops thousands of rows and about 0.3% of the total. The
    figure counts ADRs at their whole global capitalisation and both classes
    of dual-class names, so it overstates the US market and is labelled for
    what it is rather than as "the sector's market cap".
    """
    caps: dict[str, float] = {}
    for sector in SECTOR_NAMES:
        try:
            rows = _get("company-screener", sector=sector,
                        marketCapMoreThan=1_000_000_000,
                        isEtf="false", isFund="false",
                        exchange="NASDAQ,NYSE,AMEX", limit=1000) or []
        except MarketError:
            continue
        caps[sector] = sum(float(r.get("marketCap") or 0) for r in rows)
    return caps


def sector_risk_return() -> list[dict]:
    """Return, volatility and size per sector, for every period and interval.

    Prices come from the sector SPDRs, the same eleven funds the company
    report already benchmarks against. Two of them are younger than the
    longest window on offer -- XLRE launched in 2015 and XLC in 2018 -- so a
    ten-year view of those is really their whole life. Each carries the first
    date it actually has, and the page says so rather than quietly comparing
    an eight-year record with a twenty-year one.
    """
    caps = _sector_caps()
    today = dt.date.today()
    out = []

    for sector, etf in SECTOR_ETF.items():
        try:
            rows = _get("historical-price-eod/light", symbol=etf,
                        **{"from": "1990-01-01", "to": today.isoformat()}) or []
        except MarketError:
            continue
        series = sorted(((r["date"], float(r["price"])) for r in rows
                         if r.get("date") and r.get("price")), key=lambda x: x[0])
        if len(series) < 30:
            continue

        stats: dict[str, dict] = {}
        for period, days in RISK_PERIODS.items():
            if period == "ALL":
                window = series
            elif period == "YTD":
                cut = f"{today.year}-01-01"
                window = [p for p in series if p[0] >= cut]
            else:
                cut = (today - dt.timedelta(days=days)).isoformat()
                window = [p for p in series if p[0] >= cut]
            if len(window) < 2:
                continue
            by_interval = {i: _risk_stats(window, i) for i in RISK_INTERVALS}
            if any(by_interval.values()):
                stats[period] = by_interval

        out.append({
            "sector": sector,
            "etf": etf,
            "cap": caps.get(sector),
            "inception": series[0][0],
            "stats": stats,
        })
    return out


# ---------------------------------------------------------------------------
# Insider and congressional trades
# ---------------------------------------------------------------------------

def _insider_title(type_of_owner: str | None) -> str:
    """Collapse FMP's typeOfOwner string into a short role label.

    Filings say things like ``officer: Sr. VP, Chief Acct Officer`` or plain
    ``director``. The Markets Today panel only has room for a generic title
    (CEO, CFO, Director, …) above the person's name.
    """
    raw = " ".join(str(type_of_owner or "").split())
    if not raw:
        return "Insider"
    # Lowercase and split camelCase (tenPercentOwner → ten percent owner) so
    # the same patterns work on both FMP styles.
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", raw)
    s = spaced.lower()
    body = s.split(":", 1)[-1].strip() if ":" in s else s
    hay = f"{s} {body}"

    # VP family before President: "vice president" contains "president".
    rules: list[tuple[str, str]] = [
        ("CEO", r"chief\s+executive|\bceo\b"),
        ("CFO", r"chief\s+financial|chief\s+acct|chief\s+account|\bcontroller\b|\bcfo\b"),
        ("CTO", r"chief\s+technolog|chief\s+information|\bcto\b|\bcio\b"),
        ("COO", r"chief\s+operating|\bcoo\b"),
        ("CMO", r"chief\s+marketing|\bcmo\b"),
        ("CLO", r"chief\s+legal|general\s+counsel|\bclo\b"),
        ("CHRO", r"chief\s+human|chief\s+people|\bchro\b"),
        ("SVP", r"senior\s+vice\s+president|sr\.?\s*vice\s+president|sr\.?\s*vp\b|\bsvp\b"),
        ("EVP", r"executive\s+vice\s+president|exec(?:utive)?\s+vp\b|\bevp\b"),
        ("VP", r"vice\s+president|\bv\.?p\.?\b"),
        ("President", r"\bpresident\b"),
        ("Director", r"\bdirector\b"),
        ("10% Owner", r"ten\s*percent|10\s*percent|10\s*%|beneficial\s+owner"),
        ("Officer", r"\bofficer\b"),
    ]
    for label, pattern in rules:
        if re.search(pattern, hay):
            return label
    return "Insider"


def _insider_person_name(name: str | None) -> str | None:
    """Light cleanup so ALL-CAPS Form 4 names read as ordinary names."""
    if not name:
        return None
    text = " ".join(str(name).split())
    if text.isupper() and any(c.isalpha() for c in text):
        return text.title()
    return text


def _as_day(value) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


# Markets Today only surfaces material trades.
MIN_INSIDER_AMOUNT = 1_000_000       # $1M absolute Form 4 value
MIN_INSIDER_SHARES_PCT = 0.01        # or ≥1% of shares outstanding

# FMP's Form 4 feed carries some unusable `price` values -- WHLR has come
# through at $14,515,200,000 a share, HOVR at $250,000,000, FINS at
# $40,000,000. Left alone they produce single days of hundreds of trillions,
# which is not merely wrong: on a linear axis one such day flattens every
# real bar in a sixty-day chart below half a pixel, so the panel looks empty
# rather than looking broken.
#
# Two guards, because one is not enough. The ceiling catches the absurd
# outright: BRK.A is the highest-priced US listed share at a few hundred
# thousand dollars, so a million is well clear of anything genuine. The
# ratio test catches what the ceiling cannot -- REBN priced at $180,000 while
# the stock trades near a dollar is under any fixed cap, and only a
# comparison with the real price reveals it.
MAX_PLAUSIBLE_SHARE_PRICE = 1_000_000
MAX_PRICE_RATIO = 20                 # reported vs traded price
# Congress: store every disclosure (no $0.5M floor). UI + get_trades match.
MIN_CONGRESS_AMOUNT = 0


def _congress_amount_vals(raw) -> list[float]:
    """Dollar figures mentioned in a disclosure amount band."""
    vals: list[float] = []
    for n in re.findall(r"[\d,]+", str(raw or "")):
        try:
            vals.append(float(n.replace(",", "")))
        except ValueError:
            continue
    return vals


def _congress_amount_high(raw) -> float:
    """Largest dollar figure mentioned in a disclosure amount band."""
    vals = _congress_amount_vals(raw)
    return max(vals) if vals else 0.0


def _congress_amount_mid(raw) -> float:
    """Midpoint of a disclosure band (or the sole figure when there is one)."""
    vals = _congress_amount_vals(raw)
    if not vals:
        return 0.0
    return (min(vals) + max(vals)) / 2.0


def _shares_outstanding_map(symbols: list[str], cap: int = 100) -> dict[str, float]:
    """Approx shares outstanding from FMP quote marketCap / price.

    Capped so a noisy Form 4 dump cannot turn into hundreds of quote calls.
    """
    out: dict[str, float] = {}
    for sym in symbols[:max(0, cap)]:
        if not sym or sym in out:
            continue
        try:
            q = quote(sym)
        except MarketError:
            continue
        if not q:
            continue
        px = q.get("price")
        cap_v = q.get("market_cap")
        if px and cap_v and float(px) > 0:
            out[sym] = float(cap_v) / float(px)
    return out


def _trade_list_page_size(days: int) -> int:
    """Rows per FMP page for trade feeds.

    FMP hard-caps ``page`` at 100. With ``limit=100`` the Form 4 feed only
    reaches ~10 calendar days, which left the 60-day inflow/outflow chart
    empty for most of its window. ``limit=1000`` covers 60+ days inside that
    page cap.
    """
    d = max(1, int(days or 1))
    return 1000 if d > 14 else 100


def _trade_list_max_pages(days: int) -> int:
    """How far to page FMP 'latest' trade feeds to cover ``days``.

    Never ask past page 100 — FMP returns HTTP 400 beyond that, which used to
    abort the whole trades refresh and leave stale sparse charts.
    """
    d = max(1, int(days or 1))
    if d <= 7:
        return 24
    if d <= 14:
        return 48
    return 100


def insider_trades(days: int = 7, store_cap: int = 400,
                   collapse: bool = True) -> list[dict]:
    """Form 4 filings from the last `days`.

    A single company often files a dozen Form 4s on the same day -- each
    officer separately, or one sale split across several lots. For the
    Markets Today table (`collapse=True`) only the largest transaction per
    company in the window is kept. For the 60-day inflow/outflow chart
    (`collapse=False`) every material filing is kept so daily sums are honest.

    A row is kept when abs(dollar amount) > $1M, or when shares transacted are
    at least 1% of estimated shares outstanding (market cap / price).
    """
    cutoff = dt.date.today() - dt.timedelta(days=max(1, days))
    raw_rows: list[dict] = []
    page_size = _trade_list_page_size(days)
    max_pages = _trade_list_max_pages(days)
    for page in range(max_pages):
        try:
            rows = _get("insider-trading/latest", page=page, limit=page_size) or []
        except MarketError as exc:
            if page > 0 and _is_page_limit_error(exc):
                break
            raise
        if not rows:
            break
        page_days = [_as_day(r.get("filingDate")) for r in rows]
        page_days = [d for d in page_days if d]
        if page_days and max(page_days) < cutoff:
            break
        for r in rows:
            filed = _as_day(r.get("filingDate"))
            if filed is not None and filed < cutoff:
                continue
            shares = float(r.get("securitiesTransacted") or 0)
            price = float(r.get("price") or 0)
            if price > MAX_PLAUSIBLE_SHARE_PRICE:
                continue                  # see MAX_PLAUSIBLE_SHARE_PRICE
            code = str(r.get("transactionType")
                       or r.get("acquisitionOrDisposition") or "").upper()
            # Open-market conviction only: P is a purchase, S a sale. Every
            # other Form 4 code is compensation plumbing -- M exercises, F tax
            # withholding, A awards, G gifts -- and treating it as trading is
            # how Musk's 2018-package exercise became "$21B of insider selling":
            # the exercise rows priced 304M shares at the $23.34 strike, and
            # the 17.5M shares Tesla withheld to cover it counted as a sale,
            # when no share was sold on the open market at all.
            if code.startswith("P"):
                buy = True
            elif code.startswith("S"):
                buy = False
            else:
                continue
            amount = (shares * price) * (1 if buy else -1)
            sym = r.get("symbol")
            if not sym:
                continue
            raw_rows.append({
                "filed": (r.get("filingDate") or "")[:10],
                "symbol": sym,
                "side": "Buy" if buy else "Sell",
                "shares": shares,
                "price": price,
                "amount": amount,
                "person": _insider_person_name(r.get("reportingName")),
                "title": _insider_title(r.get("typeOfOwner")),
                "shares_out": None,
            })
        if len(rows) < page_size:
            break

    # The feed repeats filings verbatim from time to time -- the same Musk
    # exercise arrived twice, doubling a $7B row. One copy of each is enough.
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for r in raw_rows:
        k = (r["filed"], r["symbol"], r["person"], r["side"],
             round(float(r["shares"] or 0), 4), round(float(r["price"] or 0), 4))
        if k in seen:
            continue
        seen.add(k)
        deduped.append(r)
    raw_rows = deduped

    # Quote only names that need the 1%-of-float test (amount alone is not enough).
    need_out: dict[str, float] = {}
    for r in raw_rows:
        if abs(r["amount"] or 0) > MIN_INSIDER_AMOUNT:
            continue
        sh = float(r["shares"] or 0)
        if sh <= 0:
            continue
        sym = r["symbol"]
        need_out[sym] = max(need_out.get(sym, 0.0), sh)
    ranked_need = sorted(need_out, key=lambda s: -need_out[s])
    shares_out = _shares_outstanding_map(ranked_need, cap=150)

    # A reported price far above the traded price is a bad field, not a large
    # trade. Only the rows big enough to distort a chart are worth a quote --
    # a handful per refresh -- so this checks those and leaves the rest alone.
    suspect = {r["symbol"] for r in raw_rows if abs(r["amount"] or 0) > 1_000_000_000}
    traded: dict[str, float] = {}
    caps: dict[str, float] = {}
    for sym in sorted(suspect)[:60]:
        try:
            q = quote(sym)
        except MarketError:
            continue
        if not q:
            continue
        px, mc = q.get("price"), q.get("market_cap")
        if px and float(px) > 0:
            traded[sym] = float(px)
        if mc and float(mc) > 0:
            caps[sym] = float(mc)

    out: list[dict] = []
    for r in raw_rows:
        px = traded.get(r["symbol"])
        if px and float(r["price"] or 0) > px * MAX_PRICE_RATIO:
            continue                      # see MAX_PRICE_RATIO
        # Nobody can trade more of a company than the company is worth. This
        # is the guard for the other corrupt field: SVRE has come through at
        # $115bn on a plausible $6.93 price, which means 16.6 billion shares
        # for a micro-cap -- `securitiesTransacted` is wrong, and no test on
        # price alone can see it.
        mc = caps.get(r["symbol"])
        if mc and abs(r["amount"] or 0) > mc:
            continue
        so = shares_out.get(r["symbol"])
        if so:
            r["shares_out"] = so
        big_dollars = abs(r["amount"] or 0) > MIN_INSIDER_AMOUNT
        big_float = bool(
            so and so > 0 and (float(r["shares"] or 0) / so) >= MIN_INSIDER_SHARES_PCT)
        if big_dollars or big_float:
            out.append(r)

    if not collapse:
        ranked = sorted(out,
                        key=lambda r: (r["filed"] or "", abs(r["amount"] or 0)),
                        reverse=True)
        return ranked[:max(1, store_cap)]

    best: dict[str, dict] = {}
    for r in out:
        prev = best.get(r["symbol"])
        if prev is None or abs(r["amount"] or 0) > abs(prev["amount"] or 0):
            best[r["symbol"]] = r
    ranked = sorted(best.values(),
                    key=lambda r: (r["filed"] or "", abs(r["amount"] or 0)),
                    reverse=True)
    return ranked[:max(1, store_cap)]


_LEGISLATORS_JSON = (
    "https://unitedstates.github.io/congress-legislators/legislators-current.json")


def legislators() -> list[dict]:
    """Every sitting member of Congress, from the @unitedstates project.

    This is what connects a disclosure feed's bare "Nancy Pelosi" to a party,
    a district and — through the bioguide id — a photo. The file is the
    canonical open dataset the civic-tech world maintains; it changes when
    membership does, so a daily read is already generous.
    """
    req = urllib.request.Request(
        _LEGISLATORS_JSON, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        raise MarketError(f"legislators: {exc}") from exc

    rows: list[dict] = []
    for m in data or []:
        ids = m.get("id") or {}
        name = m.get("name") or {}
        terms = m.get("terms") or []
        if not ids.get("bioguide") or not terms:
            continue
        cur = terms[-1]                      # the term they are serving now
        chamber = "Senate" if cur.get("type") == "sen" else "House"
        district = cur.get("district")
        rows.append({
            "bioguide": ids["bioguide"],
            "full_name": name.get("official_full")
                or " ".join(x for x in (name.get("first"), name.get("last")) if x),
            "first_name": name.get("first"),
            "last_name": name.get("last"),
            # 'Democrat' → 'D': the page shows the single-letter chip the way
            # every trades site does.
            "party": (cur.get("party") or "")[:1].upper() or None,
            "chamber": chamber,
            "state": cur.get("state"),
            "district": (f"{cur.get('state')}-{district}"
                         if chamber == "House" and district is not None
                         else cur.get("state")),
        })
    return rows


def _congress_side(raw: str | None) -> str | None:
    """Map FMP disclosure types onto the same Buy/Sell labels as Form 4s."""
    if not raw:
        return None
    s = str(raw).strip().lower()
    if s.startswith(("buy", "purchase", "receive")) or "purchase" in s or "receive" in s:
        return "Buy"
    if (s.startswith(("sell", "sale")) or "sale" in s or "sell" in s
            or "exchange" in s):
        return "Sell"
    return str(raw).strip()


def congress_trades(days: int = 14, store_cap: int = 400) -> list[dict]:
    """Disclosures from both chambers filed in the last `days`, newest first.

    All amount bands are kept (no $0.5M floor) so the Congress table and
    inflow/outflow chart stay populated.
    """
    cutoff = dt.date.today() - dt.timedelta(days=max(1, days))
    rows: list[dict] = []
    page_size = _trade_list_page_size(days)
    max_pages = _trade_list_max_pages(days)
    min_amt = float(MIN_CONGRESS_AMOUNT or 0)
    for path, chamber in (("senate-latest", "Senate"), ("house-latest", "House")):
        try:
            for page in range(max_pages):
                try:
                    batch = _get(path, page=page, limit=page_size) or []
                except MarketError as exc:
                    if page > 0 and _is_page_limit_error(exc):
                        break
                    raise
                if not batch:
                    break
                page_days = [_as_day(r.get("disclosureDate")) for r in batch]
                page_days = [d for d in page_days if d]
                if page_days and max(page_days) < cutoff:
                    break
                for r in batch:
                    disclosed = _as_day(r.get("disclosureDate"))
                    if disclosed is not None and disclosed < cutoff:
                        continue
                    amount = r.get("amount")
                    if min_amt > 0 and _congress_amount_high(amount) <= min_amt:
                        continue
                    rows.append({
                        "disclosed": (r.get("disclosureDate") or "")[:10],
                        "traded": (r.get("transactionDate") or "")[:10],
                        "symbol": r.get("symbol"),
                        "person": " ".join(
                            x for x in (r.get("firstName"), r.get("lastName")) if x),
                        "chamber": chamber,
                        "side": _congress_side(r.get("type")),
                        "amount": amount,
                        # Who actually traded (Self / Spouse / Child), the
                        # member's district, and the disclosure document
                        # itself. FMP always sent these; dropping them was
                        # a loss for no saving.
                        "owner": (r.get("owner") or "").strip() or None,
                        "district": (r.get("district") or "").strip() or None,
                        "link": (r.get("link") or "").strip() or None,
                    })
                if len(batch) < page_size:
                    break
        except MarketError:
            continue
    rows = [r for r in rows if r["symbol"]]
    rows.sort(key=lambda r: r["disclosed"] or "", reverse=True)
    return rows[:max(1, store_cap)]


def trade_flow_daily(insiders: list[dict], congress: list[dict],
                     days: int = 60) -> list[dict]:
    """Sum buy (inflow) and sell (outflow) dollars per calendar day.

    Insider amounts are already signed (+ buy / − sell). Congress disclosures
    are bands; the midpoint of the band is used so a $1M–$5M sale does not
    count as a $5M day.
    """
    from collections import defaultdict

    window = max(1, days)
    start = dt.date.today() - dt.timedelta(days=window - 1)
    buckets: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])

    for r in insiders or []:
        day = (r.get("filed") or "")[:10]
        if not day or day < start.isoformat():
            continue
        amt = float(r.get("amount") or 0)
        if amt > 0:
            buckets[("insider", day)][0] += amt
        elif amt < 0:
            buckets[("insider", day)][1] += abs(amt)

    for r in congress or []:
        day = (r.get("disclosed") or "")[:10]
        if not day or day < start.isoformat():
            continue
        mid = _congress_amount_mid(r.get("amount"))
        if mid <= 0:
            continue
        side = str(r.get("side") or "").lower()
        if side.startswith("buy") or "purchase" in side or "receive" in side:
            buckets[("congress", day)][0] += mid
        elif side.startswith("sell") or "sale" in side or "sell" in side:
            buckets[("congress", day)][1] += mid

    # Emit every calendar day in the window so the chart has a bar slot even
    # on quiet days (drawn as zero-height).
    out: list[dict] = []
    for i in range(window):
        day = (start + dt.timedelta(days=i)).isoformat()
        for kind in ("insider", "congress"):
            inflow, outflow = buckets.get((kind, day), [0.0, 0.0])
            out.append({
                "kind": kind,
                "day": day,
                "inflow": inflow,
                "outflow": outflow,
            })
    return out


# ---------------------------------------------------------------------------
# Earnings calendar
# ---------------------------------------------------------------------------

# FMP's earnings-calendar returns at most ~4000 rows and prefers the back of
# a wide window, so a single 60-day pull silently drops the current week.
# Pull week-sized chunks and merge.
_EARNINGS_CHUNK_DAYS = 7


def earnings_calendar(start: dt.date | None = None,
                      end: dt.date | None = None) -> list[dict]:
    """Upcoming and recent earnings announcements from FMP.

    ``from``/``to`` are capped by FMP (about three months). The worker asks for
    a short window around today so the Earnings page can scroll a week at a
    time without storing the whole universe forever.
    """
    start = start or (dt.date.today() - dt.timedelta(days=7))
    end = end or (dt.date.today() + dt.timedelta(days=90))
    if end < start:
        start, end = end, start

    by_key: dict[tuple[str, str], dict] = {}
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + dt.timedelta(days=_EARNINGS_CHUNK_DAYS - 1), end)
        rows = _get("earnings-calendar",
                    **{"from": cursor.isoformat(), "to": chunk_end.isoformat()}) or []
        for r in rows:
            sym = (r.get("symbol") or "").upper().strip()
            day = (r.get("date") or "")[:10]
            if not sym or not day:
                continue
            by_key[(day, sym)] = {
                "date": day,
                "symbol": sym,
                "eps_actual": r.get("epsActual"),
                "eps_estimated": r.get("epsEstimated"),
                "revenue_actual": r.get("revenueActual"),
                "revenue_estimated": r.get("revenueEstimated"),
                # Stable calendar often omits timing / fiscal-end; keep both
                # spellings so a plan that still sends them is not dropped.
                "time": (r.get("time") or r.get("when") or "")[:16] or None,
                "fiscal_date": (r.get("fiscalDateEnding") or r.get("fiscalDate")
                                or "")[:10] or None,
            }
        cursor = chunk_end + dt.timedelta(days=1)

    out = list(by_key.values())
    out.sort(key=lambda x: (x["date"], x["symbol"]))
    return out


# ---------------------------------------------------------------------------
# Institutional ownership (13F holders)
# ---------------------------------------------------------------------------

# The one thing on this page FMP has moved more than once. The stable API and
# the older versioned one disagree on both the path and the row keys, and which
# of them an account can reach depends on its plan, so the fetch tries the
# spellings in turn and keeps the first that answers with rows rather than
# betting the feature on one guess. A 402 here is ordinary: 13F data sits above
# the entry tiers.
_HOLDER_ENDPOINTS = (
    ("institutional-ownership/symbol-ownership", None),
    ("institutional-ownership/extract-analytics/holder", None),
    ("institutional-holder", V3_BASE),
)

# FMP has used every one of these for the same column.
_HOLDER_KEYS = {
    "holder": ("holderName", "investorName", "holder", "name"),
    "shares": ("sharesNumber", "shares", "currentShares", "sharesHeld"),
    "value": ("marketValue", "value", "currentMarketValue"),
    "change": ("changeInSharesNumber", "change", "sharesChange"),
    "date": ("date", "dateReported", "filingDate", "reportDate"),
    "cik": ("cik", "investorCik", "holderCik"),
}


def _first(row: dict, keys) -> object:
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return v
    return None


def _as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def institutional_holders(symbol: str, limit: int = 60) -> list[dict]:
    """Institutions holding one ticker, largest position first.

    Rows are whatever the reachable endpoint returns, normalised to one shape.
    Raises MarketError when no spelling answers -- the caller logs it and
    carries on, because a holders list is an addition to a company page rather
    than a precondition for one.
    """
    last_error = None
    for path, base in _HOLDER_ENDPOINTS:
        try:
            rows = _get(path, _base=base, symbol=symbol, limit=limit) or []
        except MarketError as exc:
            last_error = exc
            continue
        if not isinstance(rows, list) or not rows:
            continue

        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            name = _first(r, _HOLDER_KEYS["holder"])
            shares = _as_float(_first(r, _HOLDER_KEYS["shares"]))
            if not name or shares is None:
                continue
            out.append({
                "holder": str(name).strip()[:120],
                "cik": (str(_first(r, _HOLDER_KEYS["cik"]) or "") or None),
                "shares": shares,
                "value": _as_float(_first(r, _HOLDER_KEYS["value"])),
                "change": _as_float(_first(r, _HOLDER_KEYS["change"])),
                "date": (str(_first(r, _HOLDER_KEYS["date"]) or "")[:10] or None),
            })
        if out:
            out.sort(key=lambda x: x["shares"], reverse=True)
            return out[:limit]

    raise MarketError(
        f"institutional holders for {symbol}: no endpoint answered"
        + (f" ({last_error})" if last_error else ""))


def earnings_history(symbol: str, limit: int = 40) -> list[dict]:
    """Reported and scheduled earnings for one ticker, newest first.

    The same figures as ``earnings_calendar`` read along the other axis: that
    one asks "who reports this week" across the market, this one asks "what has
    this company reported" across its history. The ticker page's beat/miss
    record needs the second, and the calendar cannot answer it — the worker
    rewrites it to a rolling ±90-day window on every run.

    Raises MarketError when the endpoint is not in the account's plan, which
    the caller is expected to log and carry on from: an earnings history is an
    addition to a company page, not a precondition for one.
    """
    rows = _get("earnings", symbol=symbol, limit=limit) or []
    out = []
    for r in rows:
        day = (r.get("date") or "")[:10]
        if not day:
            continue
        out.append({
            "date": day,
            "epsActual": r.get("epsActual"),
            "epsEstimated": r.get("epsEstimated"),
            "revenueActual": r.get("revenueActual"),
            "revenueEstimated": r.get("revenueEstimated"),
            "fiscalDate": (r.get("fiscalDateEnding") or r.get("fiscalDate")
                           or "")[:10] or None,
        })
    # Newest first is the order the page reads them in, and the order FMP
    # already returns -- sorted here so a change at their end cannot flip it.
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Economic calendar (US macro releases — jobs, CPI, FOMC, etc.)
# ---------------------------------------------------------------------------

def economic_calendar(start: dt.date | None = None,
                      end: dt.date | None = None,
                      country: str = "US") -> list[dict]:
    """Scheduled economic data releases from FMP ``economic-calendar``.

    Same shape as MarketWatch's economy calendar: unemployment, CPI, Fed
    decisions, GDP, and other high-impact prints. FMP caps the window at
    about 90 days; the worker keeps a month behind and ~two months ahead.
    """
    start = start or (dt.date.today() - dt.timedelta(days=14))
    end = end or (dt.date.today() + dt.timedelta(days=60))
    if end < start:
        start, end = end, start

    rows = _get("economic-calendar",
                **{"from": start.isoformat(), "to": end.isoformat()}) or []
    want = (country or "US").upper()
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for r in rows:
        ctry = (r.get("country") or "").upper().strip() or "US"
        if want and ctry != want:
            continue
        raw = (r.get("date") or "").strip()
        day = raw[:10]
        if not day or len(day) < 10:
            continue
        time_part = raw[11:16] if len(raw) >= 16 else None
        event = (r.get("event") or "").strip()
        if not event:
            continue
        key = (day, ctry, event)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "date": day,
            "time": time_part,
            "country": ctry,
            "currency": (r.get("currency") or "")[:8] or None,
            "event": event[:200],
            "impact": (r.get("impact") or "")[:16] or None,
            "previous": r.get("previous"),
            "estimate": r.get("estimate"),
            "actual": r.get("actual"),
            "change": r.get("change"),
            "changePct": r.get("changePercentage"),
        })
    out.sort(key=lambda x: (x["date"], x["event"]))
    return out


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------

# Counting words in headlines produced chips like "here's" and "august" --
# technically frequent, useless to click. These are the subjects a market
# reader actually navigates by, each paired with the text the filter searches
# for. Label and query are separate so a chip can read "The Fed" while
# matching the word that appears in the copy.
_TOPICS: list[tuple[str, str]] = [
    ("S&P 500",        "S&P 500"),
    ("Nasdaq",         "Nasdaq"),
    ("Dow Jones",      "Dow Jones"),
    ("The Fed",        "Fed"),
    ("Inflation",      "inflation"),
    ("Interest Rates", "interest rate"),
    ("Earnings",       "earnings"),
    ("Guidance",       "guidance"),
    ("Dividends",      "dividend"),
    ("Buybacks",       "buyback"),
    ("Tariffs",        "tariff"),
    ("Jobs",           "jobs report"),
    ("Recession",      "recession"),
    ("Oil",            "oil price"),
    ("Gold",           "gold"),
    ("Bitcoin",        "bitcoin"),
    ("AI",             "artificial intelligence"),
    ("Semiconductors", "semiconductor"),
    ("IPO",            "IPO"),
    ("M&A",            "acquisition"),
    ("Layoffs",        "layoff"),
    ("Upgrades",       "upgrade"),
]


def top_by_cap(n: int = 8) -> list[tuple[str, str]]:
    """The largest listed companies, as (ticker, company name).

    Comes off the same screener call the mover filter already makes, so this
    costs nothing extra. Names are trimmed of their suffixes because a chip
    should read "NVIDIA", not "NVIDIA Corporation".

    Funds are excluded -- the screener ranks Vanguard's total-market fund
    among the largest listings, which is true and not a company -- and dual
    listings collapse to one entry, so Alphabet appears once rather than as
    both GOOG and GOOGL.
    """
    ranked = sorted(
        (r for r in _screener()
         if r.get("symbol") and r.get("marketCap")
         and not r.get("isEtf") and not r.get("isFund")),
        key=lambda r: -float(r["marketCap"]))
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for r in ranked:
        name = _trim_company(r.get("companyName") or r["symbol"])
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append((r["symbol"], name))
        if len(out) >= n:
            break
    return out


_SUFFIXES = re.compile(
    r",?\s+(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|"
    r"holdings?|group|sa|nv|ag|lp|llc|class [a-c]|& co)\.?$", re.I)


def _trim_company(name: str) -> str:
    prev = None
    while prev != name:
        prev = name
        name = _SUFFIXES.sub("", name).strip()
    return name


# Policy / geopolitics subjects the Discover rail on Markets Today also
# surfaces. Appended to the chip catalogue so the News page can filter them.
_POLICY_TOPICS: list[tuple[str, str]] = [
    ("War",            "war"),
    ("Sanctions",      "sanction"),
    ("Geopolitics",    "geopolit"),
]


def _topics(companies: list[tuple[str, str]]) -> list[dict]:
    """The catalogue of chips: what each one says, and what it searches for.

    Deliberately uncounted here. Counting the batch just fetched disagreed
    with the seven-day filter badly enough that "Nasdaq" read 2 and opened
    on 21 stories. upsert_news counts each chip against the stored corpus
    with the same match rules as the filter; get_news only reads those
    numbers so the page cannot time out recounting them.

    `ord` is the position: for companies that is market-cap rank, which is the
    order they are shown in and the reason they are on the list at all.
    """
    topics = list(_TOPICS) + _POLICY_TOPICS
    out = [{"word": label, "query": query, "kind": "topic", "ord": i}
           for i, (label, query) in enumerate(topics)]
    out += [{"word": name, "query": ticker, "kind": "company", "ord": i}
            for i, (ticker, name) in enumerate(companies)]
    return out


def news(limit: int = 120) -> tuple[list[dict], list[dict]]:
    """Latest market news, plus the topics that describe it.

    Company news and general market news are merged: the first names a ticker
    and the second sets the backdrop, and a reader wants both in one stream.
    """
    rows: list[dict] = []
    for path, kind in (("news/stock-latest", "stock"),
                       ("news/general-latest", "general")):
        try:
            for r in _get(path, page=0, limit=limit) or []:
                title = (r.get("title") or "").strip()
                if not title:
                    continue
                rows.append({
                    "url": r.get("url"),
                    "title": title,
                    "summary": (r.get("text") or "").strip(),
                    "image": r.get("image"),
                    "publisher": r.get("publisher") or r.get("site"),
                    "symbol": (r.get("symbol") or "").upper() or None,
                    "published": r.get("publishedDate"),
                    "kind": kind,
                })
        except MarketError:
            continue

    seen, uniq = set(), []
    for r in rows:
        key = r["url"] or r["title"]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    uniq.sort(key=lambda r: r["published"] or "", reverse=True)

    try:
        # Discover on Markets Today covers the twenty largest names; keep the
        # chip catalogue aligned so those tickers are tagged in the corpus.
        companies = top_by_cap(20)
    except MarketError:
        companies = []
    return uniq[:limit * 2], _topics(companies)


# ---------------------------------------------------------------------------
# Company report: benchmark, seasonality, industry valuation
# ---------------------------------------------------------------------------

# FMP sells no per-industry price index, so the benchmark is the sector SPDR.
# Eleven ETFs cover every US listing, and because they are shared by every
# company in the sector they cost one fetch each rather than one per company.
SECTOR_ETF = {
    "Technology": "XLK",
    "Financial Services": "XLF",
    "Healthcare": "XLV",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Industrials": "XLI",
    "Energy": "XLE",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Basic Materials": "XLB",
    "Communication Services": "XLC",
}


def profile(symbol: str) -> dict | None:
    """Company identity from FMP /profile.

    Sector and industry decide the benchmark and peer PE. The rest (CEO, IPO,
    employees, description, …) fills the flip side of the price metrics card.
    """
    rows = _get("profile", symbol=symbol) or []
    if not rows:
        return None
    r = rows[0]
    employees = r.get("fullTimeEmployees")
    try:
        employees = int(employees) if employees not in (None, "") else None
    except (TypeError, ValueError):
        employees = None
    return {
        "symbol": r.get("symbol"),
        "name": r.get("companyName"),
        "sector": r.get("sector"),
        "industry": r.get("industry"),
        "exchange": r.get("exchangeShortName") or r.get("exchange"),
        "exchangeFull": r.get("exchangeFullName") or r.get("exchange"),
        "ceo": r.get("ceo") or None,
        "ipoDate": (r.get("ipoDate") or "")[:10] or None,
        "employees": employees,
        "country": r.get("country") or None,
        "description": r.get("description") or None,
        "website": r.get("website") or None,
        "city": r.get("city") or None,
        "state": r.get("state") or None,
    }


def closes(symbol: str, years: int = 11) -> list[dict]:
    """Daily closes over several years, oldest first.

    The `light` series carries date, close and volume only, which is all the
    seasonality and benchmark charts need and a fraction of the payload of the
    full OHLC series.

    Indexes are quoted under caret symbols at FMP, exactly as in
    fetch_prices. This fetch skipped the alias, so a request for "SPX"
    fetched whatever instrument FMP lists under that literal ticker -- a
    penny-priced stray -- and a decade of near-zero closes was stored under
    the index's name and drawn under its chart.
    """
    sym = str(symbol or "").upper()
    fmp_sym = INDEXES[sym]["fmp"] if sym in INDEXES else sym
    end = dt.date.today()
    start = end - dt.timedelta(days=int(365.25 * max(1, years)))
    rows = _get("historical-price-eod/light", symbol=fmp_sym,
                **{"from": start.isoformat(), "to": end.isoformat()}) or []
    out = [{"d": r["date"], "c": r.get("price")}
           for r in rows if r.get("date") and r.get("price") is not None]
    out.sort(key=lambda b: b["d"])
    return out


def monthly_closes(symbol: str, years: int = 11) -> list[dict]:
    """The last close of each month, which is what monthly returns are built
    from. Ten years of months is 120 numbers; ten years of days is 2,600."""
    last: dict[str, dict] = {}
    for b in closes(symbol, years):
        last[b["d"][:7]] = b            # ascending, so the last write wins
    return [last[k] for k in sorted(last)]


def pe_history(symbol: str, limit: int = 40) -> list[dict]:
    """Historical price/earnings from FMP key-metrics, oldest first.

    The company page's P/E chart uses this rather than reconstructing TTM from
    sparse filing EPS (which misleads on FPIs that only tag some quarters).
    The price panel's current P/E still comes from ratios-ttm via quote_detail.
    """
    rows = _get("key-metrics", symbol=symbol, period="quarter", limit=limit) or []
    out = []
    for r in rows:
        pe = r.get("peRatio")
        if pe is None:
            pe = r.get("priceToEarningsRatio")
        d = r.get("date")
        if d and pe is not None:
            try:
                pe_f = float(pe)
            except (TypeError, ValueError):
                continue
            if pe_f > 0:
                out.append({"d": d, "pe": pe_f})
    out.sort(key=lambda p: p["d"])
    return out


def employee_history(symbol: str) -> list[dict]:
    """Annual headcount from FMP historical-employee-count, oldest first.

    Counts come from 10-K filings (periodOfReport). The revenue chart joins
    each quarter to the latest count on or before that quarter's period end.
    """
    rows = _get("historical-employee-count", symbol=symbol) or []
    out: list[dict] = []
    for r in rows:
        d = (r.get("periodOfReport") or r.get("filingDate") or "")[:10]
        n = r.get("employeeCount")
        if not d or n in (None, ""):
            continue
        try:
            count = int(n)
        except (TypeError, ValueError):
            continue
        if count > 0:
            out.append({"d": d, "n": count})
    # Prefer the later filing when the same period appears twice.
    by_day: dict[str, int] = {}
    for row in sorted(out, key=lambda p: p["d"]):
        by_day[row["d"]] = row["n"]
    return [{"d": d, "n": by_day[d]} for d in sorted(by_day)]


def industry_pe(day: dt.date | None = None, look_back: int = 6) -> tuple[list[dict], str | None]:
    """Price/earnings by industry and by sector, for the most recent day FMP
    has. Only recent dates are served on this plan, so there is no history to
    draw -- these are reference values, not a series."""
    start = day or dt.date.today()
    for back in range(look_back):
        d = (start - dt.timedelta(days=back)).isoformat()
        rows: list[dict] = []
        for path, field in (("industry-pe-snapshot", "industry"),
                            ("sector-pe-snapshot", "sector")):
            try:
                got = _get(path, date=d) or []
            except MarketError:
                continue
            # One row per exchange; a company is not tied to an exchange's
            # valuation, so the exchanges are averaged.
            agg: dict[str, list[float]] = {}
            for r in got:
                name, pe = r.get(field), r.get("pe")
                if name and pe is not None:
                    agg.setdefault(name, []).append(float(pe))
            rows += [{"kind": field, "name": n, "pe": sum(v) / len(v)}
                     for n, v in agg.items()]
        if rows:
            return rows, d
    return [], None


# ---------------------------------------------------------------------------
# Fear & Greed — not an FMP endpoint
# ---------------------------------------------------------------------------

# CNN publishes a 0–100 equity Fear & Greed index. FMP has no equivalent.
# The worker reads CNN's public dataviz JSON (the same feed their page uses)
# and caches the score in Supabase so the browser never talks to CNN.
_CNN_FNG = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
_CNN_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (compatible; TickerAlphaBot/1.0; "
                   "+https://github.com/sara-hoilam/stock-dashboard)"),
    "Accept": "application/json",
    "Referer": "https://www.cnn.com/markets/fear-and-greed",
    "Origin": "https://www.cnn.com",
}


def _series(symbol: str, days: int = 200) -> list[float]:
    end = dt.date.today()
    rows = _get("historical-price-eod/light", symbol=symbol,
                **{"from": (end - dt.timedelta(days=days)).isoformat(),
                   "to": end.isoformat()}) or []
    rows = [r for r in rows if r.get("price") is not None]
    rows.sort(key=lambda r: r["date"])
    return [float(r["price"]) for r in rows]


def _scale(v: float, lo: float, hi: float) -> float:
    """Put a raw reading on 0-100, clamped."""
    if hi == lo:
        return 50.0
    return max(0.0, min(100.0, (v - lo) / (hi - lo) * 100.0))


def fear_greed_composite() -> dict:
    """A composite in the spirit of CNN's index, from the parts FMP serves.

    Five of CNN's seven components are reproducible here; their put/call ratio
    and 52-week high/low breadth are not, so this is our own reading rather
    than theirs, and it is labelled that way. Each component is scored 0-100,
    where 100 is greed, and the score is their mean.
    """
    parts: dict[str, float] = {}

    spy = _series("SPY", 220)
    if len(spy) > 130:
        ma125 = sum(spy[-125:]) / 125
        # +/-8% either side of the mean spans the range in practice.
        parts["momentum"] = _scale((spy[-1] / ma125 - 1) * 100, -8, 8)

    vix = _series("^VIX", 120)
    if len(vix) > 55:
        ma50 = sum(vix[-50:]) / 50
        # Inverted: a VIX above its own average is fear.
        parts["volatility"] = _scale((ma50 / vix[-1] - 1) * 100, -35, 35)

    tlt = _series("TLT", 60)
    if len(spy) > 21 and len(tlt) > 21:
        stocks = spy[-1] / spy[-21] - 1
        bonds = tlt[-1] / tlt[-21] - 1
        parts["safeHaven"] = _scale((stocks - bonds) * 100, -8, 8)

    hyg, lqd = _series("HYG", 60), _series("LQD", 60)
    if len(hyg) > 21 and len(lqd) > 21:
        junk = hyg[-1] / hyg[-21] - 1
        safe = lqd[-1] / lqd[-21] - 1
        parts["junkBonds"] = _scale((junk - safe) * 100, -3, 3)

    if not parts:
        return {}
    score = round(sum(parts.values()) / len(parts))
    label = ("Extreme fear" if score < 25 else "Fear" if score < 45 else
             "Neutral" if score < 55 else "Greed" if score < 75 else "Extreme greed")
    return {"score": float(score), "rating": label, "previous": None,
            "source": "composite",
            "components": {k: round(v) for k, v in parts.items()}}


def fear_greed() -> dict:
    """Current Fear & Greed score.

    CNN first, because theirs is the index people mean by the name. It is an
    undocumented endpoint on someone else's site, though, so a failure there
    falls back to our own composite rather than leaving the card blank -- and
    the row records which one it was, so the page never passes one off as the
    other.
    """
    try:
        return _fear_greed_cnn()
    except MarketError as exc:
        own = fear_greed_composite()
        if not own:
            raise
        own["note"] = f"CNN unavailable ({exc})"
        return own


def _fear_greed_cnn() -> dict:
    """Current CNN Fear & Greed score. Raises MarketError on failure."""
    req = urllib.request.Request(_CNN_FNG, headers=_CNN_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        raise MarketError(f"fear_greed: {exc}") from exc

    fg = data.get("fear_and_greed") or {}
    score = fg.get("score")
    if score is None:
        raise MarketError("fear_greed: response missing score")
    return {
        "score": float(score),
        "rating": fg.get("rating"),
        "previous": fg.get("previous_close"),
        "source": "cnn",
    }


# ---------------------------------------------------------------------------
# Analyst coverage
# ---------------------------------------------------------------------------
# All of this is FMP's own: the consensus targets, the buy/hold/sell tally,
# the individual house ratings, and the wire stories behind each target change.
# Nothing here comes from a scraped or undocumented source.

def analyst_view(symbol: str, houses: int = 14, stories: int = 6) -> dict:
    """Targets, the rating tally, recent house actions and their sources."""
    out: dict = {"symbol": symbol.upper()}

    try:
        rows = _get("price-target-consensus", symbol=symbol) or []
        if rows:
            r = rows[0]
            out["target"] = {"high": r.get("targetHigh"), "low": r.get("targetLow"),
                             "consensus": r.get("targetConsensus"),
                             "median": r.get("targetMedian")}
    except MarketError:
        pass

    try:
        rows = _get("price-target-summary", symbol=symbol) or []
        if rows:
            r = rows[0]
            out["targetCounts"] = {
                "month": r.get("lastMonthCount"), "monthAvg": r.get("lastMonthAvgPriceTarget"),
                "quarter": r.get("lastQuarterCount"), "quarterAvg": r.get("lastQuarterAvgPriceTarget"),
                "year": r.get("lastYearCount"), "yearAvg": r.get("lastYearAvgPriceTarget"),
            }
    except MarketError:
        pass

    try:
        rows = _get("grades-consensus", symbol=symbol) or []
        if rows:
            r = rows[0]
            out["consensus"] = {
                "strongBuy": r.get("strongBuy") or 0, "buy": r.get("buy") or 0,
                "hold": r.get("hold") or 0, "sell": r.get("sell") or 0,
                "strongSell": r.get("strongSell") or 0,
                "rating": r.get("consensus"),
            }
    except MarketError:
        pass

    # One row per house per action, newest first. Keep the most recent action
    # from each house rather than the same bank five times.
    try:
        seen: set[str] = set()
        grades = []
        for g in sorted(_get("grades", symbol=symbol, limit=200) or [],
                        key=lambda g: g.get("date") or "", reverse=True):
            house = g.get("gradingCompany")
            if not house or house in seen:
                continue
            seen.add(house)
            grades.append({"date": g.get("date"), "house": house,
                           "from": g.get("previousGrade"), "to": g.get("newGrade"),
                           "action": g.get("action")})
            if len(grades) >= houses:
                break
        if grades:
            out["grades"] = grades
    except MarketError:
        pass

    # The wire story behind each target change, so a figure on the page can be
    # traced back to the note it came from.
    try:
        news = []
        for n in _get("price-target-news", symbol=symbol, limit=stories * 3) or []:
            if not n.get("newsURL"):
                continue
            news.append({
                "published": n.get("publishedDate"), "title": n.get("newsTitle"),
                "url": n.get("newsURL"), "publisher": n.get("newsPublisher"),
                "house": n.get("analystCompany"), "analyst": n.get("analystName"),
                "target": n.get("priceTarget"), "priceWhenPosted": n.get("priceWhenPosted"),
            })
            if len(news) >= stories:
                break
        if news:
            out["news"] = news
    except MarketError:
        pass

    return out


# ---------------------------------------------------------------------------
# Logo.dev — company / crypto logos (publishable key; worker caches bytes)
# ---------------------------------------------------------------------------

# Safe to ship client-side; override via env if the key rotates.
LOGO_DEV_KEY = os.environ.get(
    "LOGO_DEV_PUBLISHABLE_KEY", "pk_RQkedGufR1uCvTvlztKeQg")
LOGO_DEV_BASE = "https://img.logo.dev"
_LOGO_MIN_INTERVAL = 0.2
_logo_last = [0.0]

# Well-known large-cap cryptos when FMP quote ranking is unavailable.
_TOP_CRYPTO_FALLBACK = [
    "BTCUSD", "ETHUSD", "BNBUSD", "XRPUSD", "SOLUSD", "ADAUSD", "DOGEUSD",
    "TRXUSD", "TONUSD", "AVAXUSD", "LINKUSD", "DOTUSD", "MATICUSD", "SHIBUSD",
    "LTCUSD", "BCHUSD", "UNIUSD", "ATOMUSD", "XLMUSD", "NEARUSD", "APTUSD",
    "ICPUSD", "FILUSD", "ARBUSD", "OPUSD", "VETUSD", "HBARUSD", "AAVEUSD",
    "MKRUSD", "GRTUSD", "SANDUSD", "MANAUSD", "AXSUSD", "EGLDUSD", "FTMUSD",
    "ALGOUSD", "XTZUSD", "EOSUSD", "FLOWUSD", "THETAUSD", "SUIUSD", "SEIUSD",
    "INJUSD", "IMXUSD", "RNDRUSD", "PEPEUSD", "WIFUSD", "BONKUSD", "FLRUSD",
    "STXUSD", "TIAUSD", "RUNEUSD", "KASUSD", "CFXUSD", "GALAUSD", "ENSUSD",
    "LDOUSD", "CRVUSD", "SNXUSD", "COMPUSD", "1INCHUSD", "ZRXUSD", "BATUSD",
    "CHZUSD", "ENJUSD", "ROSEUSD", "KAVAUSD", "ZILUSD", "IOTAUSD", "QTUMUSD",
    "DASHUSD", "ZECUSD", "XMRUSD", "NEOUSD", "WAVESUSD", "CAKEUSD", "DYDXUSD",
    "GMTUSD", "APEUSD", "BLURUSD",
]


def crypto_base_symbol(symbol: str) -> str:
    """Map FMP pair tickers (BTCUSD) to Logo.dev crypto ids (BTC)."""
    s = (symbol or "").upper().strip()
    for suffix in ("USD", "USDT", "USDC", "EUR", "GBP"):
        if s.endswith(suffix) and len(s) > len(suffix):
            return s[: -len(suffix)]
    return s


def logo_dev_url(symbol: str, kind: str = "stock", *, size: int = 128) -> str:
    """CDN URL for one logo. ``fallback=404`` so we can detect misses."""
    sym = (symbol or "").upper().strip()
    if kind == "crypto":
        path = f"crypto/{crypto_base_symbol(sym)}"
    else:
        path = f"ticker/{sym}"
    q = urllib.parse.urlencode({
        "token": LOGO_DEV_KEY,
        "size": str(size),
        "format": "png",
        "fallback": "404",
        "retina": "true",
    })
    return f"{LOGO_DEV_BASE}/{path}?{q}"


def download_logo(symbol: str, kind: str = "stock", *, size: int = 128
                  ) -> dict:
    """Fetch one logo image. Returns a row ready for ``upsert_symbol_logos``."""
    url = logo_dev_url(symbol, kind, size=size)
    wait = _LOGO_MIN_INTERVAL - (time.time() - _logo_last[0])
    if wait > 0:
        time.sleep(wait)
    _logo_last[0] = time.time()

    row = {
        "symbol": (symbol or "").upper().strip(),
        "kind": "crypto" if kind == "crypto" else "stock",
        "logoUrl": url,
        "imageMime": None,
        "imageB64": None,
        "status": "missing",
    }
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "TickerAlpha/1.0 (logo-cache)",
            "Accept": "image/png,image/webp,image/jpeg,*/*",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            ctype = (resp.headers.get("Content-Type") or "image/png").split(";")[0].strip()
            if not raw or len(raw) < 32:
                row["status"] = "missing"
                return row
            row["imageMime"] = ctype if ctype.startswith("image/") else "image/png"
            row["imageB64"] = base64.b64encode(raw).decode("ascii")
            row["status"] = "ok"
            return row
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            row["status"] = "missing"
        else:
            row["status"] = "error"
        return row
    except Exception:
        row["status"] = "error"
        return row


_SYM_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

# Public constituent CSVs used when FMP's index endpoints are not on the plan.
_INDEX_CSV = {
    "sp500": (
        "https://raw.githubusercontent.com/datasets/s-and-p-500-companies"
        "/master/data/constituents.csv"),
    "nasdaq100": (
        "https://yfiua.github.io/index-constituents/constituents-nasdaq100.csv"),
    "dowjones": (
        "https://yfiua.github.io/index-constituents/constituents-dowjones.csv"),
}


def _normalize_symbol(raw: str) -> str | None:
    sym = (raw or "").upper().strip().replace("/", ".")
    # Wikipedia / some CSVs use BRK-B; Logo.dev and FMP prefer BRK.B.
    if "-" in sym and re.match(r"^[A-Z]+-[A-Z]$", sym):
        sym = sym.replace("-", ".")
    if not sym or not _SYM_RE.match(sym):
        return None
    return sym


def _add_symbol(raw: str, out: list[str], seen: set[str]) -> None:
    sym = _normalize_symbol(raw)
    if not sym or sym in seen:
        return
    seen.add(sym)
    out.append(sym)


def _symbols_from_fmp(path: str) -> list[str]:
    if not KEY:
        return []
    try:
        rows = _get(path) or []
    except MarketError:
        return []
    out, seen = [], set()
    for r in rows:
        if isinstance(r, dict):
            _add_symbol(r.get("symbol") or "", out, seen)
    return out


def _symbols_from_csv(url: str) -> list[str]:
    """First-column ticker CSV (header row skipped)."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "TickerAlpha/1.0 (logo-priority)"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", "replace")
    except Exception:
        return []
    out, seen = [], set()
    for i, line in enumerate(text.splitlines()):
        if i == 0 or not line.strip():
            continue
        _add_symbol(line.split(",", 1)[0].strip().strip('"'), out, seen)
    return out


def _symbols_from_wikipedia(title: str) -> list[str]:
    """Tickers from the first Symbol/Ticker column of a Wikipedia wikitable."""
    url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "TickerAlpha/1.0 (logo-priority)"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception:
        return []
    tables = re.findall(
        r'<table[^>]*class="[^"]*wikitable[^"]*"[^>]*>(.*?)</table>',
        html, re.S | re.I)
    out, seen = [], set()
    for table in tables:
        header = re.search(r"<tr[^>]*>(.*?)</tr>", table, re.S | re.I)
        if not header:
            continue
        cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", header.group(1), re.S | re.I)
        col = -1
        for i, cell in enumerate(cells):
            plain = re.sub(r"<[^>]+>", "", cell).strip().lower()
            if plain in ("symbol", "ticker", "ticker symbol"):
                col = i
                break
        if col < 0:
            continue
        for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S | re.I)[1:]:
            row_cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row_html, re.S | re.I)
            if col >= len(row_cells):
                continue
            plain = re.sub(r"<[^>]+>", " ", row_cells[col])
            plain = re.sub(r"\s+", " ", plain).strip()
            # Prefer the first token that looks like a ticker.
            for tok in re.split(r"[\s,;/]+", plain):
                if _normalize_symbol(tok):
                    _add_symbol(tok, out, seen)
                    break
        if out:
            break
    return out


def sp500_symbols() -> list[str]:
    """S&P 500 tickers for logo priority (FMP, else public CSV)."""
    out = _symbols_from_fmp("sp500-constituent")
    if len(out) < 100:
        for sym in _symbols_from_csv(_INDEX_CSV["sp500"]):
            if sym not in out:
                out.append(sym)
    if len(out) < 40:
        for sym in HEATMAP_UNIVERSE:
            if _normalize_symbol(sym) and sym not in out:
                out.append(sym)
    out.sort()
    return out


def nasdaq100_symbols() -> list[str]:
    """Nasdaq-100 tickers for logo priority."""
    out = _symbols_from_fmp("nasdaq-constituent")
    if len(out) < 50:
        for sym in _symbols_from_csv(_INDEX_CSV["nasdaq100"]):
            if sym not in out:
                out.append(sym)
    if len(out) < 50:
        for sym in _symbols_from_wikipedia("Nasdaq-100"):
            if sym not in out:
                out.append(sym)
    out.sort()
    return out


def dowjones_symbols() -> list[str]:
    """Dow Jones Industrial Average tickers for logo priority."""
    out = _symbols_from_fmp("dowjones-constituent")
    if len(out) < 20:
        for sym in _symbols_from_csv(_INDEX_CSV["dowjones"]):
            if sym not in out:
                out.append(sym)
    if len(out) < 20:
        for sym in _symbols_from_wikipedia("Dow_Jones_Industrial_Average"):
            if sym not in out:
                out.append(sym)
    out.sort()
    return out


def russell1000_symbols() -> list[str]:
    """Russell 1000 tickers for logo priority (Wikipedia; FMP has no stable list)."""
    out = _symbols_from_wikipedia("Russell_1000_Index")
    out.sort()
    return out


def common_stock_symbols(limit: int = 2000) -> list[str]:
    """Actively traded common stocks (ex-ETF/fund), ranked by market cap."""
    limit = max(1, min(int(limit or 2000), 5000))
    if not KEY:
        return []
    try:
        rows = _get(
            "company-screener",
            marketCapMoreThan=int(5e8),
            isActivelyTrading="true",
            isEtf="false",
            isFund="false",
            limit=limit,
        ) or []
    except MarketError:
        return []
    scored: list[tuple[float, str]] = []
    seen = set()
    for r in rows:
        sym = _normalize_symbol(r.get("symbol") or "")
        if not sym or sym in seen:
            continue
        if r.get("isEtf") or r.get("isFund"):
            continue
        try:
            cap = float(r.get("marketCap") or 0)
        except (TypeError, ValueError):
            cap = 0.0
        seen.add(sym)
        scored.append((cap, sym))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:limit]]


def top_crypto_by_cap(n: int = 80) -> list[str]:
    """Top cryptocurrencies by market cap (FMP quotes), for logo priority."""
    n = max(1, min(int(n or 80), 120))
    if KEY:
        try:
            listed = crypto_list()
            caps: list[tuple[float, str]] = []
            chunk = 50
            syms = [r["symbol"] for r in listed]
            for i in range(0, min(len(syms), 600), chunk):
                batch = syms[i:i + chunk]
                for q in quotes(batch) or []:
                    sym = (q.get("symbol") or "").upper().strip()
                    cap = q.get("marketCap")
                    try:
                        cap_f = float(cap) if cap is not None else 0.0
                    except (TypeError, ValueError):
                        cap_f = 0.0
                    if sym and cap_f > 0:
                        caps.append((cap_f, sym))
            if caps:
                caps.sort(key=lambda x: x[0], reverse=True)
                return [s for _, s in caps[:n]]
        except MarketError:
            pass
    # Extend the hardcoded fallback if more than its length is requested.
    base = list(_TOP_CRYPTO_FALLBACK)
    return base[:n] if n <= len(base) else base


def logo_priority_targets(crypto_n: int = 80,
                          common_n: int = 2000) -> list[dict]:
    """Index equities + common stocks + top crypto for Logo.dev backfill.

    Sources (unioned, de-duplicated):
      S&P 500, Nasdaq-100, Dow Jones, Russell 1000, common stocks (screener),
      and the top ``crypto_n`` cryptocurrencies by market cap.
    """
    out, seen = [], set()

    def add_stock(sym: str) -> None:
        if sym in seen:
            return
        seen.add(sym)
        out.append({"symbol": sym, "kind": "stock"})

    for sym in sp500_symbols():
        add_stock(sym)
    for sym in nasdaq100_symbols():
        add_stock(sym)
    for sym in dowjones_symbols():
        add_stock(sym)
    for sym in russell1000_symbols():
        add_stock(sym)
    for sym in common_stock_symbols(common_n):
        add_stock(sym)

    for sym in top_crypto_by_cap(crypto_n):
        if sym in seen:
            continue
        seen.add(sym)
        out.append({"symbol": sym, "kind": "crypto"})

    if not any(t["kind"] == "stock" for t in out):
        for sym in ("AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "BRK.B",
                    "TSLA", "JPM", "V", "UNH", "XOM", "JNJ", "WMT", "MA"):
            add_stock(sym)
    return out
