"""alpha.py -- the nightly Alpha of the Day scan.

Every trading morning, before the open, the worker scores the S&P 500 +
Nasdaq-100 universe over six families of evidence -- price vs its own
history, valuation vs peers, smart money, fundamentals, the Street, and
momentum -- and keeps a short list: one headline pick and the runner-up
buy and sell candidates, written to ledger.alpha_pick (migration 0073)
for the Alpha of the Day page to read.

The scoring rules were tuned on a synthetic dry run before they were
trusted with real data, and three of its lessons are load-bearing here:

  * News gates at three negative stories in a week, not one, and
    otherwise only drags the score -- a binary gate killed every deep
    drawdown candidate, because cheap stocks always have one bad story.
  * A sell needs an anchor (a P/E in its own high percentiles, an
    insider selling cluster, or a news pile-up). Without one, mediocre
    names win sell days on blandness alone.
  * An event outranks a state. A congress purchase disclosed this week
    is news; "still expensive" will be true again tomorrow. So a sell
    must clear an event-driven buy by 15 points, a stateful buy by 5.

Data comes from migration 0074's bulk read (one paged call for the
universe) plus a fresh FMP quote sweep; the decade of daily closes that
the drawdown-rarity number needs is fetched only for the ~50 shortlisted
names. Missing data never invents a score: a family without evidence
scores neutral and cannot count toward conviction.

Standard library only, like the rest of the project.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import time

import market
import store


def log(msg: str) -> None:
    print(f"[alpha {dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Trading calendar (mirrors insights.py; a missed holiday fails safe --
# the scan just re-reads yesterday's closes and the cooldown holds)
# ---------------------------------------------------------------------------

US_MARKET_HOLIDAYS = {
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}


def is_trading_day(day: dt.date) -> bool:
    return day.weekday() < 5 and day.isoformat() not in US_MARKET_HOLIDAYS


# ---------------------------------------------------------------------------
# Scoring constants
# ---------------------------------------------------------------------------

W = {"hist": 25, "val": 15, "smart": 20, "fund": 15, "street": 15, "mom": 10}

FAMILY_LABEL = {
    "hist": "Price vs own history", "val": "Valuation vs peers",
    "smart": "Smart money", "fund": "Fundamentals",
    "street": "The Street", "mom": "Momentum & season",
}

# Headline tone words. NEG drags and gates at >=3 in a week; SEVERE
# hard-gates a buy at any count -- fraud is not a dip.
NEG_WORDS = (
    "lawsuit", "sues ", "sued", "probe", "investigat", "recall",
    "downgrade", "misses", "miss estimates", "falls", "plunge", "slump",
    "layoff", "job cuts", "cuts forecast", "cuts guidance", "warns",
    "warning", "halts", "slides", "drops", "tumbles", "sinks", "weak",
    "disappoint", "short seller", "outage", "breach", "strike",
)
SEVERE_WORDS = (
    "fraud", "restatement", "sec charges", "criminal", "subpoena",
    "delisting", "default", "bankruptcy", "chapter 11", "accounting probe",
)

MIN_CAP = 10e9              # small caps move on air; below $10B stay out
CG_EVENT_DAYS = 21          # a congress disclosure this old is still an event
COOLDOWN_DAYS = 14
SHORTLIST = 30              # names per side that get the decade-history pass
LONG_FETCH_BUDGET = 25      # FMP decade-closes fetches per night
RATIOS_FETCH_BUDGET = 80    # FMP ratios-ttm fetches per night (P/E backfill)
QUOTE_STALE_HOURS = 20
# 'cheaper on only 3% of days' is only a fact worth stating when there are
# enough days behind it. GE Vernova listed in 2024: over its two years the
# same sentence described a pullback from an all-time high.
RARITY_MIN_YEARS = 5
MA_TOUCH_BAND = 0.06        # within 6% of the 200-day counts as 'at' it


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _pct(sorted_vals: list[float], v: float) -> float:
    """Percentile rank of v within vals (0..100, higher = larger)."""
    if not sorted_vals:
        return 50.0
    below = 0
    for x in sorted_vals:
        if x < v:
            below += 1
        else:
            break
    return 100.0 * below / max(1, len(sorted_vals) - 1)


def _amount_low(text: str | None) -> float:
    """Lower bound of a congress amount range, in dollars.
    '$1,000,001 - $5,000,000' -> 1000001."""
    if not text:
        return 0.0
    nums = re.findall(r"[\d,]+", str(text))
    if not nums:
        return 0.0
    try:
        return float(nums[0].replace(",", ""))
    except ValueError:
        return 0.0


def _news_tone(titles: list) -> tuple[int, int]:
    """(negative stories, severe stories) in the 7-day window."""
    neg = severe = 0
    for t in titles or []:
        low = f" {str(t).lower()} "
        if any(w in low for w in SEVERE_WORDS):
            severe += 1
            neg += 1
        elif any(w in low for w in NEG_WORDS):
            neg += 1
    return neg, severe


def _rev_trend(quarters: list) -> tuple[int | None, int]:
    """(quarters of last 8 with revenue up on a year ago, comparisons made).
    None when fewer than 4 year-over-year comparisons exist."""
    rows = [(q.get("e"), q.get("r")) for q in (quarters or [])
            if q.get("e") and q.get("r")]
    rows.sort()
    if len(rows) < 5:
        return None, 0
    up = comps = 0
    revs = [r for _, r in rows]
    for i in range(max(4, len(revs) - 8), len(revs)):
        base = revs[i - 4]
        if base and base > 0:
            comps += 1
            if revs[i] > base:
                up += 1
    if comps < 4:
        return None, comps
    return up, comps


def _season_pct(monthly: list, month: int) -> float | None:
    """Average return of calendar `month` across the stored years, in %."""
    closes = [(m.get("d"), m.get("c")) for m in (monthly or [])
              if m.get("d") and m.get("c")]
    closes.sort()
    rets = []
    for i in range(1, len(closes)):
        d = closes[i][0]
        try:
            if int(d[5:7]) == month and closes[i - 1][1]:
                rets.append(closes[i][1] / closes[i - 1][1] - 1)
        except (ValueError, TypeError, ZeroDivisionError):
            continue
    if len(rets) < 4:
        return None
    return 100.0 * sum(rets) / len(rets)


def _pe_own_pct(pe_history: list, pe: float | None) -> float | None:
    """Where today's P/E sits in the company's own stored record (0..100)."""
    if not pe or pe <= 0:
        return None
    vals = sorted(p.get("pe") for p in (pe_history or [])
                  if p.get("pe") and p.get("pe") > 0)
    if len(vals) < 12:            # three years of quarters, minimum
        return None
    return _pct(vals, pe)


def _drawdown_stats(closes: list, price: float | None) -> dict | None:
    """Decade drawdown facts: today's drawdown, and how rare it is.

    rarity = share of stored days whose drawdown from the running high was
    shallower than today's -- 99 means the stock has been this cheap on ~1%
    of days, the McDonald's chart in one number.
    """
    pts = [(c.get("d"), c.get("c")) for c in (closes or [])
           if c.get("d") and c.get("c")]
    pts.sort()
    if len(pts) < 250 or not price or price <= 0:
        return None
    dds = []
    high = 0.0
    for _, c in pts:
        high = max(high, c)
        dds.append(1.0 - c / high)
    high = max(high, price)
    dd_now = 1.0 - price / high
    shallower = sum(1 for d in dds if d < dd_now)
    return {
        "dd": dd_now,
        "rarity": 100.0 * shallower / len(dds),
        "high": high,
        "years": round((dt.date.fromisoformat(pts[-1][0])
                        - dt.date.fromisoformat(pts[0][0])).days / 365.25, 1),
    }


def _weekly(closes: list, years: float = 3.0) -> list[dict]:
    """Downsample daily closes to one point a week over the last `years`."""
    pts = [(c.get("d"), c.get("c")) for c in (closes or [])
           if c.get("d") and c.get("c")]
    pts.sort()
    cut = (dt.date.today() - dt.timedelta(days=int(365.25 * years))).isoformat()
    pts = [p for p in pts if p[0] >= cut]
    by_week: dict[str, tuple] = {}
    for d, c in pts:                       # ascending: last close of each ISO week wins
        y, w, _ = dt.date.fromisoformat(d).isocalendar()
        by_week[f"{y}-{w:02d}"] = (d, c)
    return [{"d": d, "c": round(c, 2)} for d, c in
            (by_week[k] for k in sorted(by_week))]


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def load_inputs() -> list[dict]:
    """The whole universe from 0074, paged."""
    rows: list[dict] = []
    offset = 0
    while True:
        page = store.rpc("alpha_scan_inputs",
                         {"p_offset": offset, "p_limit": 120}) or []
        rows.extend(page)
        if len(page) < 120:
            break
        offset += 120
    log(f"universe: {len(rows)} names from index holdings")
    return rows


def refresh_quotes(rows: list[dict]) -> None:
    """Freshen price/cap/ranges with one FMP quote per stale name.

    Stored P/E rides along, rescaled to the fresh price (earnings do not
    move overnight); names with no stored P/E get a ratios-ttm call from a
    bounded budget so coverage converges over the first nights. A run of
    consecutive FMP failures stops the sweep -- scoring proceeds on stored
    quotes rather than hammering a limited plan.
    """
    if not market.configured():
        log("FMP not configured; scoring on stored quotes only")
        return
    stale_cut = (dt.datetime.now(dt.timezone.utc)
                 - dt.timedelta(hours=QUOTE_STALE_HOURS)).isoformat()
    fails = 0
    fetched = ratio_budget = 0
    ratios_left = RATIOS_FETCH_BUDGET
    for r in rows:
        q = r.get("quote") or {}
        if q.get("updatedAt") and str(q["updatedAt"]) > stale_cut:
            continue
        try:
            fresh = market.quote(r["symbol"])
        except market.MarketError:
            fails += 1
            if fails >= 5:
                log(f"quote sweep stopped after {fails} straight failures "
                    f"({fetched} refreshed)")
                return
            continue
        fails = 0
        if not fresh or fresh.get("price") is None:
            continue
        fetched += 1
        old_price, old_pe = q.get("price"), q.get("pe")
        q.update({
            "price": fresh.get("price"), "marketCap": fresh.get("market_cap"),
        })
        if old_pe and old_price:
            q["pe"] = old_pe * fresh["price"] / old_price
        elif ratios_left > 0:
            ratios_left -= 1
            ratio_budget += 1
            try:
                rr = (market._get("ratios-ttm", symbol=r["symbol"]) or [{}])[0]
                pe = rr.get("priceToEarningsRatioTTM")
                if pe:
                    q["pe"] = float(pe)
            except (market.MarketError, TypeError, ValueError, IndexError):
                pass
        r["quote"] = q
    log(f"quotes: {fetched} refreshed, {ratio_budget} P/E backfills")


# ---------------------------------------------------------------------------
# Per-name digest
# ---------------------------------------------------------------------------

def digest(r: dict, today: dt.date) -> dict | None:
    """Everything scoring needs about one name, or None when unquotable."""
    q = r.get("quote") or {}
    price, cap = q.get("price"), q.get("marketCap")
    if not price or price <= 0:
        return None

    ins = r.get("insider") or {}
    cg_recent_amt = 0.0
    cg_people: set[str] = set()
    cg_sell_amt = 0.0
    cg_marks = []
    event_cut = (today - dt.timedelta(days=CG_EVENT_DAYS)).isoformat()
    for t in r.get("congress") or []:
        when = t.get("disclosed") or t.get("traded") or ""
        low = _amount_low(t.get("amount"))
        if t.get("side") == "Buy" and when >= event_cut:
            cg_recent_amt += low
            if t.get("person"):
                cg_people.add(t["person"])
            cg_marks.append({"d": t.get("traded") or when,
                             "person": t.get("person"), "amt": low})
        elif t.get("side") == "Sell" and when >= event_cut:
            cg_sell_amt += low

    news = r.get("news") or {}
    neg7, severe7 = _news_tone(news.get("titles") or [])

    an = r.get("analyst") or {}
    target = (an.get("target") or {})
    tgt = target.get("median") or target.get("consensus")
    upside = (tgt / price - 1.0) if tgt else None
    updown30 = None
    if an:
        updown30 = int(an.get("upgrades30") or 0) - int(an.get("downgrades30") or 0)

    rev_up, rev_comps = _rev_trend(r.get("revenue"))

    next_earn = r.get("nextEarnings")
    earn_days = None
    if next_earn:
        try:
            earn_days = (dt.date.fromisoformat(next_earn) - today).days
        except ValueError:
            pass

    avg200 = q.get("avg200")
    ext_pct = (price / avg200 - 1.0) if avg200 else None

    buyers = int(ins.get("buyers") or 0)
    buy_amt = float(ins.get("buyAmt") or 0)
    big_sellers = int(ins.get("bigSellers") or 0)
    sell_amt = float(ins.get("sellAmt") or 0)

    return {
        "symbol": r["symbol"], "name": r.get("name") or r["symbol"],
        "industry": r.get("industry") or r.get("sector") or "—",
        "price": price, "cap": cap, "pe": q.get("pe"),
        "year_high": q.get("yearHigh"), "avg200": avg200,
        "dd_1y": (1.0 - price / q["yearHigh"]) if q.get("yearHigh") else None,
        "ext_pct": ext_pct,
        "pe_own_pct": _pe_own_pct(r.get("peHistory"), q.get("pe")),
        "pe_hist": r.get("peHistory") or [],
        "ins_trades": r.get("insiderTrades") or [],
        "street_rows": r.get("analystTargets") or [],
        "target_range": (r.get("analyst") or {}).get("target"),
        "monthly_raw": r.get("monthly") or [],
        "rev_q": r.get("revenue") or [],
        "cg_amt": cg_recent_amt, "cg_members": len(cg_people),
        "cg_sell_amt": cg_sell_amt, "cg_marks": cg_marks[:4],
        "ins_cluster_buy": buyers >= 3 or (buyers >= 2 and buy_amt >= 2e6),
        "ins_cluster_sell": big_sellers >= 3,
        "ins_buyers": buyers, "ins_buy_amt": buy_amt,
        "ins_sellers": big_sellers, "ins_sell_amt": sell_amt,
        "neg7": neg7, "severe7": severe7,
        "stories7": int(news.get("stories") or 0),
        "upside": upside, "updown30": updown30,
        "rev_up": rev_up, "rev_comps": rev_comps,
        "earn_days": earn_days,
        "season": _season_pct(r.get("monthly"), today.month),
        "has_long": bool(r.get("longCloses")),
        "long_to": (r.get("longCloses") or {}).get("to"),
        "dd_stats": None,          # stage 2 fills this for the shortlist
    }


# ---------------------------------------------------------------------------
# Family scores
# ---------------------------------------------------------------------------

def score(d: dict, ctx: dict) -> dict:
    """Buy and sell composites for one digest, against universe context.

    A family with no evidence behind it scores exactly 50 and is listed in
    `thin`, so it can never be one of the two >=80 families conviction needs.
    """
    fams: dict[str, float] = {}
    thin: list[str] = []

    # Price vs own history. Stage 1 ranks the 1-year drawdown against the
    # universe; stage 2 replaces that with the decade rarity number.
    if d.get("dd_stats") and d["dd_stats"]["years"] >= RARITY_MIN_YEARS:
        hist = 0.75 * d["dd_stats"]["rarity"] + 0.25 * (
            100 - d["pe_own_pct"] if d.get("pe_own_pct") is not None else 50)
    elif d.get("dd_1y") is not None:
        hist = 0.7 * _pct(ctx["dds"], d["dd_1y"]) + 0.3 * (
            100 - d["pe_own_pct"] if d.get("pe_own_pct") is not None else 50)
    else:
        hist = 50.0
        thin.append("hist")
    fams["hist"] = max(0.0, min(100.0, hist))

    peer_pe = ctx["peer_pe"].get(d["industry"]) or ctx["median_pe"]
    if d.get("pe") and d["pe"] > 0 and peer_pe:
        gap = d["pe"] / peer_pe
        val = 0.6 * (100 - _pct(ctx["gaps"], gap)) + 0.4 * (
            100 - d["pe_own_pct"] if d.get("pe_own_pct") is not None else 50)
    else:
        val = 50.0
        thin.append("val")
    fams["val"] = max(0.0, min(100.0, val))
    d["peer_pe"] = peer_pe

    smart = 50.0
    had_smart = False
    if d["cg_amt"] > 0:
        had_smart = True
        smart += min(35.0, 8.0 * math.log1p(d["cg_amt"] / 1e6))
        smart += 6.0 * max(0, d["cg_members"] - 1)
    if d["cg_sell_amt"] > d["cg_amt"]:
        had_smart = True
        smart -= 20.0
    if d["ins_cluster_buy"]:
        had_smart = True
        smart += 22.0
    if d["ins_cluster_sell"]:
        had_smart = True
        smart -= 18.0
    if not had_smart:
        thin.append("smart")
    fams["smart"] = max(0.0, min(100.0, smart))

    if d["rev_up"] is not None:
        fams["fund"] = 100.0 * d["rev_up"] / d["rev_comps"]
    else:
        fams["fund"] = 50.0
        thin.append("fund")

    if d["upside"] is not None:
        street = 0.75 * _pct(ctx["ups"], d["upside"]) + 25.0 * math.tanh(
            max(0, d["updown30"] or 0) / 2.0)
    else:
        street = 50.0
        thin.append("street")
    fams["street"] = max(0.0, min(100.0, street))

    mom = 50.0
    had_mom = False
    if d["season"] is not None:
        had_mom = True
        mom += 18.0 * math.tanh(d["season"] / 1.5)
    if d["ext_pct"] is not None:
        had_mom = True
        mom -= 25.0 * math.tanh(max(0.0, d["ext_pct"]) / 0.2)
    if not had_mom:
        thin.append("mom")
    fams["mom"] = max(0.0, min(100.0, mom))

    total = sum(W.values())
    buy = sum(W[f] * fams[f] for f in W) / total
    buy -= 4.0 * min(2, d["neg7"])          # light news drag, not a wall

    sell_f = {
        "hist": d["pe_own_pct"] if d.get("pe_own_pct") is not None else 50.0,
        "val": (_pct(ctx["gaps"], d["pe"] / peer_pe)
                if d.get("pe") and peer_pe else 50.0),
        "smart": max(0.0, min(100.0, 50.0
                    + (25.0 if d["ins_cluster_sell"] else 0.0)
                    + (15.0 if d["cg_sell_amt"] > 0 else 0.0)
                    - (30.0 if d["cg_amt"] > 0 else 0.0))),
        "fund": 100.0 - fams["fund"],
        "street": 100.0 - fams["street"],
        "mom": 50.0 + (25.0 * math.tanh(max(0.0, d["ext_pct"]) / 0.2)
                       if d["ext_pct"] is not None else 0.0),
    }
    sell = sum(W[f] * sell_f[f] for f in W) / total
    sell += 3.0 * min(2, d["neg7"])

    sell_anchor = ((d.get("pe_own_pct") or 0) >= 88
                   or d["ins_cluster_sell"] or d["neg7"] >= 3)
    sell_thin = [f for f in ("hist", "val", "fund", "street")
                 if f in thin] + (["smart"] if not had_smart else []) \
                + (["mom"] if d["ext_pct"] is None else [])

    return {"buy": buy, "sell": sell, "fams": fams, "sell_f": sell_f,
            "thin": thin, "sell_thin": sell_thin, "sell_anchor": sell_anchor}


def buy_gates(d: dict) -> list[str]:
    """Reasons a name cannot be a buy today. Empty means clear."""
    why = []
    if d["severe7"]:
        why.append("severe headline")
    if d["neg7"] >= 3:
        why.append(f"negative news ×{d['neg7']}")
    if d["earn_days"] is not None and 0 <= d["earn_days"] <= 2:
        why.append("earnings <2d")
    if not d["cap"] or d["cap"] < MIN_CAP:
        why.append("size")
    if d["rev_up"] is not None and d["rev_up"] < d["rev_comps"] / 2:
        why.append("revenue shrinking")
    return why


def _strong(fams: dict, thin: list[str]) -> list[str]:
    return [f for f, v in fams.items() if v >= 80 and f not in thin]


def is_event(d: dict) -> bool:
    return d["cg_amt"] >= 1e6 or d["ins_cluster_buy"]


# ---------------------------------------------------------------------------
# Context: universe-relative distributions
# ---------------------------------------------------------------------------

def build_ctx(digests: list[dict]) -> dict:
    by_ind: dict[str, list[float]] = {}
    pes = []
    for d in digests:
        if d.get("pe") and 0 < d["pe"] < 400:
            by_ind.setdefault(d["industry"], []).append(d["pe"])
            pes.append(d["pe"])
    median_pe = sorted(pes)[len(pes) // 2] if pes else 22.0
    peer_pe = {}
    for ind, vals in by_ind.items():
        if len(vals) >= 4:
            vals.sort()
            peer_pe[ind] = vals[len(vals) // 2]
    dds = sorted(d["dd_1y"] for d in digests if d.get("dd_1y") is not None)
    ups = sorted(d["upside"] for d in digests if d.get("upside") is not None)
    gaps = sorted(d["pe"] / (peer_pe.get(d["industry"]) or median_pe)
                  for d in digests if d.get("pe") and d["pe"] > 0)
    return {"peer_pe": peer_pe, "median_pe": median_pe,
            "dds": dds, "ups": ups, "gaps": gaps}


# ---------------------------------------------------------------------------
# Stage 2: the decade of closes for the shortlist
# ---------------------------------------------------------------------------

def fill_history(shortlist: list[dict]) -> dict[str, list]:
    """Decade closes for the shortlisted names: stored ones in one call,
    the rest from FMP inside a nightly budget (and stored for next time)."""
    syms = [d["symbol"] for d in shortlist]
    have = store.rpc("alpha_long_history", {"p_symbols": syms}) or {}
    missing = [s for s in syms if not have.get(s)]
    budget = LONG_FETCH_BUDGET
    fails = 0
    for sym in missing:
        if budget <= 0 or not market.configured():
            break
        try:
            closes = market.closes(sym, years=10)
        except market.MarketError:
            fails += 1
            if fails >= 4:
                log("long-close fetches stopped (FMP refusing)")
                break
            continue
        budget -= 1
        if closes:
            have[sym] = closes
            try:
                store.upsert_long_closes(sym, closes)
            except store.StoreError as exc:
                log(f"  long closes {sym} store: {exc}")
    log(f"history: {len([s for s in syms if have.get(s)])}/{len(syms)} "
        f"shortlisted names have a decade of closes")
    return have



def _dd_series(closes: list, years: float = 3.0) -> list[dict] | None:
    """Weekly drawdown-from-the-decade-high over the last `years`, in percent
    (negative). Computed against the running max of the FULL stored series, so
    the endpoint agrees with the "off its high" chip rather than with whatever
    high happens to fall inside the chart window."""
    pts = [(c.get("d"), c.get("c")) for c in (closes or [])
           if c.get("d") and c.get("c")]
    pts.sort()
    if len(pts) < 250:
        return None
    high = 0.0
    dd = []
    for d, c in pts:
        high = max(high, c)
        dd.append((d, -100.0 * (1.0 - c / high)))
    cut = (dt.date.today() - dt.timedelta(days=int(365.25 * years))).isoformat()
    by_week: dict[str, tuple] = {}
    for d, v in dd:
        if d < cut:
            continue
        y, w, _ = dt.date.fromisoformat(d).isocalendar()
        by_week[f"{y}-{w:02d}"] = (d, v)
    out = [{"d": d, "v": round(v, 1)} for d, v in
           (by_week[k] for k in sorted(by_week))]
    return out if len(out) > 20 else None


def _seasonality(monthly: list) -> list[dict] | None:
    """Average calendar-month return across the stored years, one row per
    month, in percent. None when there are not ~3 years of months to average."""
    closes = [(m.get("d"), m.get("c")) for m in (monthly or [])
              if m.get("d") and m.get("c")]
    closes.sort()
    if len(closes) < 36:
        return None
    by_month: dict[int, list[float]] = {m: [] for m in range(1, 13)}
    for i in range(1, len(closes)):
        try:
            m = int(closes[i][0][5:7])
            if closes[i - 1][1]:
                by_month[m].append(closes[i][1] / closes[i - 1][1] - 1)
        except (ValueError, TypeError, ZeroDivisionError):
            continue
    out = []
    for m in range(1, 13):
        rets = by_month[m]
        out.append({"m": m,
                    "r": round(100.0 * sum(rets) / len(rets), 2) if rets else None})
    return out


def _ma_series(closes: list, years: float = 3.0) -> list[dict] | None:
    """Weekly closes with their 50- and 200-day averages, over `years`.

    The averages are rolled over the *daily* series and then sampled weekly,
    so a 200-day line means 200 sessions rather than 200 of the sampled
    points. Needs a year of daily closes before the 200-day has any value.
    """
    pts = [(c.get("d"), c.get("c")) for c in (closes or [])
           if c.get("d") and c.get("c")]
    pts.sort()
    if len(pts) < 260:
        return None
    out_daily, run50, run200 = [], 0.0, 0.0
    vals = [c for _, c in pts]
    for i, (d, c) in enumerate(pts):
        run50 += c - (vals[i - 50] if i >= 50 else 0.0)
        run200 += c - (vals[i - 200] if i >= 200 else 0.0)
        out_daily.append((d, c,
                          run50 / 50 if i >= 49 else None,
                          run200 / 200 if i >= 199 else None))
    cut = (dt.date.today() - dt.timedelta(days=int(365.25 * years))).isoformat()
    by_week: dict[str, tuple] = {}
    for row in out_daily:
        if row[0] < cut:
            continue
        y, w, _ = dt.date.fromisoformat(row[0]).isocalendar()
        by_week[f"{y}-{w:02d}"] = row
    rows = [by_week[k] for k in sorted(by_week)]
    if len(rows) < 20:
        return None
    return [{"d": d, "c": round(c, 2),
             "m50": round(m50, 2) if m50 else None,
             "m200": round(m200, 2) if m200 else None}
            for d, c, m50, m200 in rows]


# ---------------------------------------------------------------------------
# The story: chips, headline, gates for the page
# ---------------------------------------------------------------------------

def _fmt_musd(v: float) -> str:
    if v >= 1e9:
        return f"${v / 1e9:.1f}B"
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    return f"${max(v, 0) / 1e3:.0f}K"


def build_idea(d: dict, s: dict, side: str, rank: int, is_pick: bool,
               closes: list | None) -> dict:
    sell = side == "SELL"
    fams = s["sell_f"] if sell else s["fams"]
    chips = []
    dds = d.get("dd_stats")

    if d["cg_amt"] > 0:
        chips.append({"t": "up", "l": "Congress purchase",
                      "v": f"~{_fmt_musd(d['cg_amt'])}"})
    if d["ins_cluster_buy"]:
        chips.append({"t": "up", "l": "Insider cluster",
                      "v": f"{d['ins_buyers']} buyers"})
    if d["ins_cluster_sell"]:
        chips.append({"t": "down", "l": "Insider selling",
                      "v": f"{d['ins_sellers']} sellers"})
    if dds:
        chips.append({"t": "up" if not sell else "mut", "l": "Off its high",
                      "v": f"−{dds['dd'] * 100:.0f}%"})
        if not sell and dds["rarity"] >= 80:
            pct = 100 - dds["rarity"]
            chips.append({"t": "up", "l": "Cheaper only",
                          "v": f"{pct:.0f}% of days" if pct >= 1 else "<1% of days"})
    if d.get("pe") and d.get("peer_pe"):
        chips.append({"t": "down" if sell and d["pe"] > d["peer_pe"] else
                      ("up" if d["pe"] < d["peer_pe"] else "mut"),
                      "l": f"P/E {d['pe']:.1f} vs peers",
                      "v": f"{d['peer_pe']:.1f}"})
    if d.get("pe_own_pct") is not None and (sell or d["pe_own_pct"] <= 25):
        chips.append({"t": "down" if sell else "up", "l": "P/E own history",
                      "v": f"{d['pe_own_pct']:.0f}th pct"})
    if d["rev_up"] is not None:
        chips.append({"t": "up" if d["rev_up"] >= d["rev_comps"] - 2 else "mut",
                      "l": "Revenue up",
                      "v": f"{d['rev_up']}/{d['rev_comps']} quarters"})
    if d["upside"] is not None and (d["upside"] > 0.12 or sell and d["upside"] < 0):
        chips.append({"t": "up" if d["upside"] > 0 else "down",
                      "l": "Consensus target",
                      "v": f"{d['upside'] * 100:+.0f}%"})
    if d["neg7"]:
        chips.append({"t": "down", "l": "Bearish stories 7d",
                      "v": str(d["neg7"])})

    ma_series = _ma_series(closes) if closes else None
    headline, lead_chart = _headline(d, s, side, has_ma=bool(ma_series))
    # A drawdown chart with two years behind it argues nothing; when the
    # sentence could not use the rarity, the chart should not imply it.
    dd_ok = bool(dds and dds["years"] >= RARITY_MIN_YEARS)

    gates = ([
        {"l": (f"{d['neg7']} bearish stor{'y' if d['neg7'] == 1 else 'ies'} "
               "in 7 days — under the gate") if d["neg7"]
         else "No negative story in 7 days", "ok": True},
        {"l": (f"Next earnings {d['earn_days']} days out"
               if d["earn_days"] is not None else "No earnings imminent"),
         "ok": True},
        {"l": "Not picked in the last 14 days", "ok": True},
    ] if is_pick else [])

    # Flags for the price chart: congress purchases first (rarer news), then
    # the idea's own side of the insider tape, largest trades first. Form 4
    # names come surname-first, so the first token is the readable handle.
    marks = [{"d": m["d"], "s": "buy",
              "l": f"{(m['person'] or 'Congress').split()[-1]} "
                   f"~{_fmt_musd(m['amt'])}"}
             for m in d["cg_marks"][:2] if m.get("d")]
    ins_rows = sorted((t for t in d["ins_trades"]
                       if t.get("filed") and t.get("amount")),
                      key=lambda t: -abs(t["amount"]))
    ins_rows.sort(key=lambda t: (t.get("side") == "Sell") != sell)
    for t in ins_rows[:3 - len(marks)]:
        selling = t.get("side") == "Sell"
        who = (t.get("person") or "Insider").split()[0].title()
        marks.append({"d": t["filed"], "s": "sell" if selling else "buy",
                      "l": f"{who} {'−' if selling else '+'}"
                           f"{_fmt_musd(abs(t['amount']))}"})

    evidence = {
        "chips": chips[:6],
        "marks": marks[:3],
        "ddSeries": (_dd_series(closes) if closes and dd_ok else None),
        "seasonality": _seasonality(d.get("monthly_raw")),
        "revSeries": [{"e": q["e"], "r": q["r"]}
                      for q in (d.get("rev_q") or [])
                      if q.get("e") and q.get("r")][-8:] or None,
        "streetTargets": [{"house": t.get("house"), "analyst": t.get("analyst"),
                           "target": t.get("target"), "d": t.get("published")}
                          for t in d.get("street_rows", [])
                          if t.get("target")][:5] or None,
        "targetRange": d.get("target_range"),
        "industry": d["industry"],
        "peerPe": round(d["peer_pe"], 1) if d.get("peer_pe") else None,
        "ownPe": round(d["pe"], 1) if d.get("pe") else None,
        "peOwnPct": round(d["pe_own_pct"]) if d.get("pe_own_pct") is not None else None,
        "ddPct": round(dds["dd"] * 100, 1) if dds else None,
        "ddRarity": round(dds["rarity"], 1) if dds else None,
        "ddYears": dds["years"] if dds else None,
        "revUp": d["rev_up"], "revComps": d["rev_comps"],
        "upsidePct": round(d["upside"] * 100) if d["upside"] is not None else None,
        "congress": [{"person": m["person"], "amt": m["amt"], "d": m["d"]}
                     for m in d["cg_marks"]],
        "insiderBuyers": d["ins_buyers"], "insiderBuyAmt": d["ins_buy_amt"],
        "insiderSellers": d["ins_sellers"],
        "lead": lead_chart,
        "priceSeries": _weekly(closes or [], 3.0) or None,
        "maSeries": ma_series,
        "avg200": d.get("avg200"),
        "extPct": round(d["ext_pct"] * 100, 1) if d.get("ext_pct") is not None else None,
        "peHistory": ([{"d": p["d"], "pe": round(p["pe"], 1)}
                       for p in d.get("pe_hist", [])
                       if p.get("d") and p.get("pe")][-40:] or None),
        "thin": s["sell_thin" if sell else "thin"],
    }

    return {
        "symbol": d["symbol"], "side": side, "rank": rank, "isPick": is_pick,
        "score": round(s["sell" if sell else "buy"]),
        "families": {k: round(v) for k, v in fams.items()},
        "gates": gates, "evidence": evidence,
        "headline": headline, "price": d["price"],
    }


def _headline(d: dict, s: dict, side: str, has_ma: bool = False
              ) -> tuple[str, str | None]:
    """The sentence, and the chart that proves it.

    The two used to be decided separately -- the sentence by this ladder, the
    chart by whichever family scored highest -- so a name could argue its
    drawdown in words while showing its revenue. Whatever the sentence leads
    with now names the chart beside it; `None` means nothing in particular,
    and the page falls back to ranking the families.
    """
    name = d["name"]
    dds = d.get("dd_stats")
    at_ma = (has_ma and d.get("ext_pct") is not None
             and abs(d["ext_pct"]) <= MA_TOUCH_BAND)
    if side == "BUY":
        # A trade the price chart already flags leads to no second chart of
        # its own; the flags are on the chart beside it.
        if d["cg_amt"] >= 1e6 and d["ins_cluster_buy"]:
            lead, chart = (f"Congress and company insiders both bought {name} "
                           f"inside a month"), None
        elif d["cg_amt"] >= 1e6:
            lead, chart = (f"Members of Congress disclosed "
                           f"~{_fmt_musd(d['cg_amt'])} of {name} purchases"), None
        elif d["ins_cluster_buy"]:
            lead, chart = (f"{d['ins_buyers']} {name} insiders bought their "
                           f"own stock inside a month"), None
        elif dds and dds["rarity"] >= 90 and dds["years"] >= RARITY_MIN_YEARS:
            pct = 100 - dds["rarity"]
            share = (f"only {pct:.0f}%" if pct >= 1 else "fewer than 1%")
            lead = (f"{name} has been this far below its high on {share} "
                    f"of days in {dds['years']:.0f} years")
            chart = "drawdown"
        elif at_ma:
            # Let the tail state the drawdown, from the same source the chip
            # uses -- saying it here too printed two different numbers.
            lead = f"{name} has pulled back to its 200-day average"
            chart = "ma"
        else:
            lead, chart = f"{name} screens cheap on several families at once", None
        tail = []
        if d.get("pe") and d.get("peer_pe") and d["pe"] < d["peer_pe"] * 0.8:
            tail.append(f"it trades at {d['pe']:.0f}× earnings against "
                        f"peers at {d['peer_pe']:.0f}×")
        elif dds and "its high" not in lead:
            tail.append(f"the stock sits {dds['dd'] * 100:.0f}% off its high")
        if d["rev_up"] is not None and d["rev_up"] >= d["rev_comps"] - 2:
            tail.append(f"revenue is up in {d['rev_up']} of the last "
                        f"{d['rev_comps']} quarters")
        return (lead + (" — " + ", and ".join(tail[:2]) if tail else "") + ".",
                chart)
    # SELL
    if (d.get("pe_own_pct") or 0) >= 88:
        lead = (f"{name} is priced near the top of its own record — a P/E "
                f"in its {d['pe_own_pct']:.0f}th percentile")
        chart = "pe"
    elif d["ins_cluster_sell"]:
        lead = (f"{d['ins_sellers']} {name} insiders each sold more than "
                f"$500K of stock inside a month")
        chart = None
    else:
        lead = f"{name} carries {d['neg7']} bearish stories this week"
        chart = None
    tail = []
    if d.get("ext_pct") and d["ext_pct"] > 0.08:
        tail.append(f"{d['ext_pct'] * 100:.0f}% above its 200-day average")
    if d.get("pe") and d.get("peer_pe") and d["pe"] > d["peer_pe"] * 1.2:
        tail.append(f"{d['pe']:.0f}× earnings against peers at "
                    f"{d['peer_pe']:.0f}×")
    return (lead + (" — " + ", and ".join(tail[:2]) if tail else "") + ".",
            chart)


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------

def run_scan(day: dt.date | None = None, force: bool = False) -> bool:
    """Score the universe, store the day's ideas. Returns True on a write."""
    day = day or dt.date.today()
    if not force and not is_trading_day(day):
        log(f"{day} is not a US trading day; no scan")
        return False
    latest = store.rpc("alpha_latest_day")
    if not force and latest == day.isoformat():
        log(f"scan for {day} already stored")
        return False

    resolve_results()

    rows = load_inputs()
    if len(rows) < 100:
        log(f"only {len(rows)} universe rows — index holdings look empty, "
            "not scanning")
        return False
    refresh_quotes(rows)

    digests = [x for x in (digest(r, day) for r in rows) if x]
    log(f"quotable: {len(digests)}/{len(rows)}")
    if len(digests) < 100:
        log("too few quotable names to trust a ranking; not scanning")
        return False

    ctx = build_ctx(digests)
    scored = {d["symbol"]: (d, score(d, ctx)) for d in digests}

    # Shortlist for the decade-history pass: the best coarse buys that clear
    # the gates, and the best anchored sells.
    buys = sorted((x for x in scored.values() if not buy_gates(x[0])),
                  key=lambda x: -x[1]["buy"])[:SHORTLIST]
    sells = sorted((x for x in scored.values()
                    if x[1]["sell_anchor"] and (x[0]["cap"] or 0) >= MIN_CAP
                    and not (x[0]["earn_days"] is not None
                             and 0 <= x[0]["earn_days"] <= 2)),
                   key=lambda x: -x[1]["sell"])[:SHORTLIST]
    short = {x[0]["symbol"]: x[0] for x in buys + sells}
    closes_by_sym = fill_history(list(short.values()))

    # Precise re-score with decade drawdown facts.
    for sym, d in short.items():
        d["dd_stats"] = _drawdown_stats(closes_by_sym.get(sym), d["price"])
    for sym in short:
        d = scored[sym][0]
        scored[sym] = (d, score(d, ctx))

    # Today's own stored pick must not cool itself down: a --force re-run of
    # the same day should be free to reach the same conclusion.
    cooldown = {p["symbol"] for p in
                (store.rpc("get_alpha_track_record",
                           {"p_days": COOLDOWN_DAYS}) or [])
                if p.get("day") != day.isoformat()}

    best_buy = None
    for d, s in sorted(scored.values(), key=lambda x: -x[1]["buy"]):
        if d["symbol"] in cooldown or buy_gates(d):
            continue
        if len(_strong(s["fams"], s["thin"])) < 2:
            continue
        best_buy = (d, s)
        break

    best_sell = None
    for d, s in sorted(scored.values(), key=lambda x: -x[1]["sell"]):
        if d["symbol"] in cooldown or not s["sell_anchor"]:
            continue
        if (d["cap"] or 0) < MIN_CAP:
            continue
        if d["earn_days"] is not None and 0 <= d["earn_days"] <= 2:
            continue
        if len(_strong(s["sell_f"], s["sell_thin"])) < 2:
            continue
        best_sell = (d, s)
        break

    if best_buy and best_sell:
        margin = 15.0 if is_event(best_buy[0]) else 5.0
        winner = ("SELL" if best_sell[1]["sell"] >= best_buy[1]["buy"] + margin
                  else "BUY")
    elif best_buy or best_sell:
        winner = "BUY" if best_buy else "SELL"
    else:
        log("no name cleared conviction and gates today; nothing stored")
        return False

    pick_d, pick_s = best_buy if winner == "BUY" else best_sell
    log(f"pick: {winner} {pick_d['symbol']} "
        f"({pick_s['buy' if winner == 'BUY' else 'sell']:.1f})")

    # Candidates: the next four clean names per side, pick excluded. Ranks
    # continue after the pick on its own side (pick=1, candidates 2..5) and
    # start at 1 on the other.
    ideas = [build_idea(pick_d, pick_s, winner, 1, True,
                        closes_by_sym.get(pick_d["symbol"]))]
    cand_b: list = []
    for d, s in sorted(scored.values(), key=lambda x: -x[1]["buy"]):
        if len(cand_b) >= 4:
            break
        if (d["symbol"] == pick_d["symbol"] or d["symbol"] in cooldown
                or buy_gates(d)):
            continue
        cand_b.append((d, s))
    cand_s: list = []
    for d, s in sorted(scored.values(), key=lambda x: -x[1]["sell"]):
        if len(cand_s) >= 4:
            break
        if (d["symbol"] == pick_d["symbol"] or d["symbol"] in cooldown
                or not s["sell_anchor"] or (d["cap"] or 0) < MIN_CAP
                or (d["earn_days"] is not None and 0 <= d["earn_days"] <= 2)):
            continue
        cand_s.append((d, s))
    start_b = 2 if winner == "BUY" else 1
    start_s = 2 if winner == "SELL" else 1
    for i, (d, s) in enumerate(cand_b):
        ideas.append(build_idea(d, s, "BUY", start_b + i, False,
                                closes_by_sym.get(d["symbol"])))
    for i, (d, s) in enumerate(cand_s):
        ideas.append(build_idea(d, s, "SELL", start_s + i, False,
                                closes_by_sym.get(d["symbol"])))

    n = store.rpc("record_alpha_day",
                  {"p_day": day.isoformat(), "p_ideas": ideas})
    log(f"stored {n} ideas for {day}")
    if n:
        warm_logos([i["symbol"] for i in ideas])
    return bool(n)


# ---------------------------------------------------------------------------
# Logos for the day's ideas
# ---------------------------------------------------------------------------

def warm_logos(symbols: list[str]) -> int:
    """Cache Logo.dev images for the names about to appear on the page.

    The hourly logo warmer walks a priority list thousands of symbols long and
    skips anything already checked, including rows it recorded as `missing` --
    so a name Logo.dev had no image for on the day it was first asked keeps its
    monogram for a month, whatever the page needs. This is at most nine symbols
    a day, so it just re-asks for the ideas being published: today's ideas are
    the one place a missing logo is actually visible.
    """
    if not market.configured():
        return 0
    rows = []
    for sym in symbols[:12]:
        try:
            rows.append(market.download_logo(sym, "stock"))
        except Exception as exc:                    # never fail a stored scan
            log(f"  logo {sym}: {type(exc).__name__}: {exc}")
    if not rows:
        return 0
    try:
        n = store.upsert_symbol_logos(rows)
    except store.StoreError as exc:
        log(f"  logo cache write: {exc}")
        return 0
    ok = sum(1 for r in rows if r.get("status") == "ok")
    log(f"logos: {ok}/{len(rows)} of today's ideas have an image")
    return n


# ---------------------------------------------------------------------------
# Yesterday's answer
# ---------------------------------------------------------------------------

def resolve_results() -> int:
    """Fill result_pct for past picks once the next session's close exists."""
    open_picks = store.rpc("alpha_unresolved") or []
    done = 0
    for p in open_picks:
        if not market.configured():
            break
        try:
            closes = market.closes(p["symbol"], years=1)
        except market.MarketError as exc:
            log(f"  result {p['symbol']}: {exc}")
            break
        after = [c for c in closes if c["d"] > p["day"]]
        if not after or not p.get("price"):
            continue
        pct = (after[0]["c"] / p["price"] - 1.0) * 100.0
        store.rpc("record_alpha_result", {
            "p_day": p["day"], "p_symbol": p["symbol"],
            "p_pct": round(pct, 2)})
        done += 1
        log(f"  result {p['day']} {p['side']} {p['symbol']}: "
            f"{pct:+.2f}% next session")
    return done
