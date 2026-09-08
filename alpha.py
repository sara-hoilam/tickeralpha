"""alpha.py -- the nightly Alpha of the Day scan.

Every trading morning, before the open, the worker mines opportunity
seeds -- congress purchases, insider clusters, news catalysts, rare
drawdowns -- across the S&P 500 + Nasdaq-100 universe (plus mid-cap
names pulled in when a catalyst warrants it). It scores six families of
evidence and keeps a short list: one headline pick and runner-up buy and
sell candidates, written to ledger.alpha_pick (migration 0073) for the
Alpha of the Day page to read.

Story-first selection prefers a fresh catalyst with at least one solid
support claim (revenue trend, valuation, Street, or price context) and
only falls back to the old composite screen when no catalyst story clears.
Headline captions are deterministic and chart-aligned -- never routed
through insights.py or an LLM.

Load-bearing rules from the original dry run, still in force:

  * News gates at three negative stories in a week, not one.
  * A sell needs an anchor (expensive vs own history, insider selling
    cluster, or a news pile-up).
  * An event outranks a state: a sell must clear an event-driven buy by
    15 points, a stateful buy by 5.

Data comes from migration 0074's bulk read plus optional seed supplements
(migration 0085), a fresh FMP quote sweep, and decade closes for the
shortlist. Missing data never invents a score.

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

# Everything except `event` moves at the pace of quarters: a drawdown, a P/E,
# a revenue trend and a consensus target are all much the same on Tuesday as
# they were on Monday, which is why two consecutive mornings produced two
# nearly identical boards. `event` is the part that knows what happened this
# week, and it is weighted to matter.
W = {"hist": 20, "val": 12, "smart": 16, "fund": 12, "street": 12, "mom": 8,
     "event": 20}

FAMILY_LABEL = {
    "hist": "Price vs own history", "val": "Valuation vs peers",
    "smart": "Smart money", "fund": "Fundamentals",
    "street": "The Street", "mom": "Momentum & season",
    "event": "Catalyst & news",
}

# Only big news. A headline is significant when it names something that
# changes what the company is worth -- a deal, a number, a regulator, a
# lawsuit -- not when it merely mentions the ticker. Categories rather than
# counts: six wires covering one acquisition are one event, not six.
BIG_NEWS = {
    "deal": ("acquir", "merger", "to buy", "takeover", "buyout", "all-cash",
             "agrees to buy", "agreed to acquire", "bid for"),
    "guidance": ("raises guidance", "cuts guidance", "lowers guidance",
                 "raises outlook", "cuts outlook", "profit warning",
                 "warns on", "guidance cut", "guidance raise"),
    "results": ("beats estimates", "misses estimates", "tops estimates",
                "quarterly results", "earnings beat", "earnings miss",
                "posts loss", "record revenue", "results beat", "results miss"),
    "legal": ("lawsuit", "settlement", "probe", "investigation", "subpoena",
              "sec charges", "antitrust", "indict"),
    "leadership": ("steps down", "resigns", "new chief executive",
                   "names ceo", "ceo change", "ousted"),
    "capital": ("buyback", "share repurchase", "raises dividend",
                "cuts dividend", "suspends dividend", "stock split",
                "spinoff", "spin-off", "secondary offering"),
    "regulatory": ("fda approval", "fda rejects", "recall", "clearance",
                   "tariff", "sanction", "export ban"),
    "restructuring": ("layoff", "job cuts", "restructuring", "bankruptcy",
                      "chapter 11", "plant closure"),
    "activist": ("activist investor", "short seller", "13d filing"),
    "contract": ("wins contract", "awarded contract", "multiyear deal",
                 "partnership with"),
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

MIN_CAP = 10e9              # default universe floor
SEED_MIN_CAP = 1.5e9        # mid-caps with a congress/insider catalyst
CONGRESS_SEED_MIN = 1e6     # disclosed buy worth a headline seed
CG_EVENT_DAYS = 21          # a congress disclosure this old is still an event
SEED_CONGRESS = "congress_buy"
SEED_INSIDER = "insider_cluster_buy"
SEED_NEWS = "news_catalyst"
SEED_DIP = "rare_dip"
SEED_TYPES = (SEED_CONGRESS, SEED_INSIDER, SEED_NEWS, SEED_DIP)
SEED_LEAD_FAM = {
    SEED_CONGRESS: "smart",
    SEED_INSIDER: "smart",
    SEED_NEWS: "event",
    SEED_DIP: "hist",
}
# How long a name rests. REPEAT_DAYS covers every slot on the board: the old
# cooldown read only returned headline picks, so the eight candidates under
# each day's pick were never held back and came round again the next morning.
REPEAT_DAYS = 7
PICK_COOLDOWN_DAYS = 14     # and longer before it may lead again
# A P/E is a price over an earnings number, and both ends go wrong: a company
# earning almost nothing prints a ratio in the hundreds, and a quote that has
# not refreshed against fresh earnings prints one that never existed. Past
# this the figure says nothing about how a company is valued, so the scan
# treats it as no P/E at all rather than as an expensive one.
PE_MAX = 100.0
JOLT_MIN = 4.0              # a move worth calling a move, in percent
MOVE_STALE_DAYS = 4         # a three-day move read off older bars is not one
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
    # Over the count, not the gaps between them: dividing by len-1 let a value
    # above every stored one score 109, which the sell headline then printed
    # as "a P/E in its 109th percentile" -- and a name at the top of its own
    # record is exactly the name that headline is written for.
    return 100.0 * below / len(sorted_vals)


def _sane_pe(pe) -> float | None:
    """The ratio, or None when it is not one worth reading."""
    try:
        v = float(pe)
    except (TypeError, ValueError):
        return None
    return v if 0 < v <= PE_MAX else None


def _news_big(titles: list) -> int:
    """How many *kinds* of significant story broke, not how many outlets ran
    one. Wide coverage is measured separately, against the universe."""
    low = [str(t).lower() for t in (titles or []) if t]
    if not low:
        return 0
    return sum(1 for words in BIG_NEWS.values()
               if any(w in t for t in low for w in words))


def _ordinal(n: float) -> str:
    """91st, not 91th."""
    i = int(round(n))
    if 10 <= i % 100 <= 20:
        return f"{i}th"
    return f"{i}" + {1: "st", 2: "nd", 3: "rd"}.get(i % 10, "th")


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
    pe = _sane_pe(pe)
    if pe is None:
        return None
    vals = sorted(v for v in (_sane_pe(p.get("pe")) for p in (pe_history or []))
                  if v is not None)
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
    """Index universe (0074) plus mid-cap catalyst names outside SPX/NDX."""
    rows: list[dict] = []
    offset = 0
    while True:
        page = store.rpc("alpha_scan_inputs",
                         {"p_offset": offset, "p_limit": 120}) or []
        rows.extend(page)
        if len(page) < 120:
            break
        offset += 120
    known = {r["symbol"] for r in rows}
    extra_syms: list[str] = []
    try:
        extra_syms = [s for s in (store.rpc("alpha_seed_symbols") or [])
                      if s and s not in known]
    except store.StoreError as exc:
        log(f"seed symbol read skipped: {exc}")
    if extra_syms:
        try:
            extra = store.rpc("alpha_scan_extra",
                              {"p_symbols": extra_syms}) or []
            rows.extend(extra)
            preview = ", ".join(extra_syms[:5])
            if len(extra_syms) > 5:
                preview += "…"
            log(f"seed universe: +{len(extra)} names outside indexes "
                f"({preview})")
        except store.StoreError as exc:
            log(f"seed universe supplement skipped: {exc}")
    log(f"universe: {len(rows)} names")
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
    cg_lead_person = None
    cg_lead_amt = 0.0
    event_cut = (today - dt.timedelta(days=CG_EVENT_DAYS)).isoformat()
    for t in r.get("congress") or []:
        when = t.get("disclosed") or t.get("traded") or ""
        low = _amount_low(t.get("amount"))
        if t.get("side") == "Buy" and when >= event_cut:
            cg_recent_amt += low
            person = t.get("person")
            if person:
                cg_people.add(person)
                if low > cg_lead_amt:
                    cg_lead_person = person
                    cg_lead_amt = low
            cg_marks.append({"d": t.get("traded") or when,
                             "person": t.get("person"), "amt": low})
        elif t.get("side") == "Sell" and when >= event_cut:
            cg_sell_amt += low

    news = r.get("news") or {}
    neg7, severe7 = _news_tone(news.get("titles") or [])

    # The catalyst window: three days of headlines, the last print, and the
    # jolt in the tape. A three-day move is only a three-day move if the bars
    # it came from are current -- a cached series can be weeks old.
    n3 = r.get("news3") or {}
    n3_titles = n3.get("titles") or []
    move3d = None
    as_of = r.get("move3dAsOf")
    if r.get("move3d") is not None and as_of:
        try:
            fresh = (today - dt.date.fromisoformat(str(as_of)[:10])).days <= MOVE_STALE_DAYS
        except ValueError:
            fresh = False
        if fresh:
            move3d = float(r["move3d"])
    le = r.get("lastEarnings") or {}
    since_earn = earn_surprise = None
    if le.get("date"):
        try:
            since_earn = (today - dt.date.fromisoformat(str(le["date"])[:10])).days
        except ValueError:
            since_earn = None
        act, est = le.get("epsActual"), le.get("epsEstimated")
        if act is not None and est:
            earn_surprise = (float(act) - float(est)) / abs(float(est)) * 100.0

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
        "price": price, "cap": cap, "pe": _sane_pe(q.get("pe")),
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
        "cg_lead_person": cg_lead_person, "cg_lead_amt": cg_lead_amt,
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
        "season_month": today.month,
        "chg1d": q.get("changePct"),
        "move3d": move3d,
        "news3": int(n3.get("stories") or 0),
        "big_news": _news_big(n3_titles),
        "since_earn": since_earn,
        "earn_surprise": earn_surprise,
        "has_long": bool(r.get("longCloses")),
        "long_to": (r.get("longCloses") or {}).get("to"),
        "dd_stats": None,          # stage 2 fills this for the shortlist
    }


# ---------------------------------------------------------------------------
# Family scores
# ---------------------------------------------------------------------------

def _event_score(d: dict, ctx: dict, sell: bool) -> tuple[float, bool]:
    """How much is happening to this name right now, and whether anything is.

    Returns (score, thin). A quiet name scores a neutral 50 and is marked
    thin, so it is not punished for having no catalyst -- it simply cannot
    win on one it does not have.
    """
    score_, had = 50.0, False

    if d["big_news"]:
        had = True
        score_ += min(20.0, 9.0 * d["big_news"])
    if d["news3"] and ctx.get("news3") and _pct(ctx["news3"], d["news3"]) >= 85:
        had = True
        score_ += 5.0

    # A print just delivered is the largest re-rating moment a stock gets;
    # one a few days out is a scheduled one. Inside two days belongs to the
    # gates -- an idea published the night before earnings is a coin toss.
    if d["since_earn"] is not None and d["since_earn"] <= 3:
        had = True
        score_ += 14.0
        if d["earn_surprise"] is not None:
            # A beat helps the buy case and hurts the sell case; a miss the
            # other way round.
            helps = (d["earn_surprise"] > 0) != sell
            score_ += ((8.0 if helps else -6.0)
                       * min(1.0, abs(d["earn_surprise"]) / 20.0))
    elif d["earn_days"] is not None and 2 < d["earn_days"] <= 10:
        had = True
        score_ += 9.0

    # The jolt itself, over whichever window saw more of it.
    moves = [m for m in (d["chg1d"], d["move3d"]) if m is not None]
    jolt = max(moves, key=abs) if moves else None
    if jolt is not None and abs(jolt) >= JOLT_MIN:
        had = True
        # A dislocation is worth something to the side that fades it: a sharp
        # drop is a buy candidate's opening, a sharp run is a sell's.
        helps = (jolt < 0) if not sell else (jolt > 0)
        score_ += (16.0 if helps else -12.0) * min(1.0, abs(jolt) / 12.0)

    return max(0.0, min(100.0, score_)), not had


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
    if d.get("pe") and peer_pe:
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

    ev_buy, ev_thin = _event_score(d, ctx, sell=False)
    fams["event"] = ev_buy
    if ev_thin:
        thin.append("event")

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
        "event": _event_score(d, ctx, sell=True)[0],
    }
    sell = sum(W[f] * sell_f[f] for f in W) / total
    sell += 3.0 * min(2, d["neg7"])

    sell_anchor = ((d.get("pe_own_pct") or 0) >= 88
                   or d["ins_cluster_sell"] or d["neg7"] >= 3)
    sell_thin = [f for f in ("hist", "val", "fund", "street")
                 if f in thin] + (["smart"] if not had_smart else []) \
                + (["mom"] if d["ext_pct"] is None else []) \
                + (["event"] if ev_thin else [])

    return {"buy": buy, "sell": sell, "fams": fams, "sell_f": sell_f,
            "thin": thin, "sell_thin": sell_thin, "sell_anchor": sell_anchor}


def seed_eligible(d: dict) -> bool:
    """Congress or insider cluster: soft market-cap floor applies."""
    return (d["cg_amt"] >= CONGRESS_SEED_MIN or d["ins_cluster_buy"])


def _cap_floor(d: dict) -> float:
    return SEED_MIN_CAP if seed_eligible(d) else MIN_CAP


def buy_gates(d: dict) -> list[str]:
    """Reasons a name cannot be a buy today. Empty means clear."""
    why = []
    if d["severe7"]:
        why.append("severe headline")
    if d["neg7"] >= 3:
        why.append(f"negative news ×{d['neg7']}")
    if d["earn_days"] is not None and 0 <= d["earn_days"] <= 2:
        why.append("earnings <2d")
    if not d["cap"] or d["cap"] < _cap_floor(d):
        why.append("size")
    if d["rev_up"] is not None and d["rev_up"] < d["rev_comps"] / 2:
        why.append("revenue shrinking")
    return why


def _strong(fams: dict, thin: list[str]) -> list[str]:
    return [f for f, v in fams.items() if v >= 80 and f not in thin]


def is_event(d: dict) -> bool:
    return d["cg_amt"] >= CONGRESS_SEED_MIN or d["ins_cluster_buy"]


def _congress_who(d: dict) -> str:
    """Readable congress lead: prefer a named member over the generic."""
    person = d.get("cg_lead_person")
    if not person:
        return "Members of Congress"
    parts = str(person).split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[-1]}"
    return str(person)


def mine_seeds(d: dict) -> list[dict]:
    """Opportunity seeds for one digest, strongest catalyst first."""
    seeds: list[dict] = []
    dds = d.get("dd_stats")

    if d["cg_amt"] >= CONGRESS_SEED_MIN:
        seeds.append({
            "type": SEED_CONGRESS,
            "fam": "smart",
            "strength": min(100.0, 55.0 + 8.0 * math.log1p(d["cg_amt"] / 1e6)
                          + (6.0 if d.get("cg_lead_person") else 0.0)),
        })
    if d["ins_cluster_buy"]:
        seeds.append({
            "type": SEED_INSIDER,
            "fam": "smart",
            "strength": min(100.0, 58.0 + 5.0 * d["ins_buyers"]
                          + 3.0 * math.log1p(d["ins_buy_amt"] / 1e6)),
        })

    moves = [m for m in (d.get("chg1d"), d.get("move3d")) if m is not None]
    jolt = max(moves, key=abs) if moves else None
    news_strength = 50.0
    news_hit = False
    if d["big_news"]:
        news_hit = True
        news_strength += 8.0 * d["big_news"]
    if d["since_earn"] is not None and d["since_earn"] <= 3:
        news_hit = True
        news_strength += 14.0
    if jolt is not None and abs(jolt) >= JOLT_MIN:
        news_hit = True
        news_strength += 10.0 * min(1.0, abs(jolt) / 12.0)
    if news_hit:
        seeds.append({
            "type": SEED_NEWS,
            "fam": "event",
            "strength": min(100.0, news_strength),
        })

    rev_ok = (d["rev_up"] is None
              or d["rev_up"] >= d["rev_comps"] / 2)
    if (dds and dds["rarity"] >= 88 and dds["years"] >= RARITY_MIN_YEARS
            and rev_ok):
        seeds.append({
            "type": SEED_DIP,
            "fam": "hist",
            "strength": min(100.0, 48.0 + dds["rarity"] * 0.42),
        })

    seeds.sort(key=lambda s: -s["strength"])
    return seeds


def support_families(d: dict, s: dict) -> list[str]:
    """Solid support beyond the catalyst lead."""
    fams = s["fams"]
    thin = s["thin"]
    out: list[str] = []
    if (d["rev_up"] is not None and d["rev_up"] >= d["rev_comps"] - 2
            and "fund" not in thin and fams.get("fund", 50) >= 55):
        out.append("fund")
    if (d.get("pe") and d.get("peer_pe") and d["pe"] < d["peer_pe"] * 0.85
            and "val" not in thin and fams.get("val", 50) >= 50):
        out.append("val")
    if (d["upside"] is not None and d["upside"] > 0.1
            and "street" not in thin and fams.get("street", 50) >= 50):
        out.append("street")
    dds = d.get("dd_stats")
    if dds and dds["dd"] >= 0.08 and "hist" not in thin:
        out.append("hist")
    elif (d.get("dd_1y") and d["dd_1y"] >= 0.08
          and "hist" not in thin):
        out.append("hist")
    elif (d.get("ext_pct") is not None
          and abs(d["ext_pct"]) <= MA_TOUCH_BAND):
        out.append("hist")
    return out


def _claim_matches_seed(claim: dict, seed_type: str | None) -> bool:
    if not seed_type:
        return True
    if seed_type in (SEED_CONGRESS, SEED_INSIDER):
        return claim.get("seed") == seed_type
    if seed_type == SEED_NEWS:
        return claim.get("seed") == SEED_NEWS or (
            claim.get("fam") == "event" and claim.get("seed") is not False)
    if seed_type == SEED_DIP:
        return claim.get("seed") == SEED_DIP
    fam = SEED_LEAD_FAM.get(seed_type)
    return claim.get("fam") == fam if fam else True


def _last_headline_seed(day: dt.date) -> str | None:
    """Seed type from the most recent stored headline, for diversity."""
    try:
        payload = store.rpc("get_alpha_day")
    except store.StoreError:
        return None
    if not payload or not payload.get("ideas"):
        return None
    if payload.get("day") == day.isoformat():
        return None
    for idea in payload["ideas"]:
        if idea.get("isPick"):
            return (idea.get("evidence") or {}).get("seed")
    return None


# ---------------------------------------------------------------------------
# Context: universe-relative distributions
# ---------------------------------------------------------------------------

def build_ctx(digests: list[dict]) -> dict:
    by_ind: dict[str, list[float]] = {}
    pes = []
    for d in digests:
        if d.get("pe"):            # digest has already discarded the absurd
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
    # Wide coverage means something only against the market's own baseline: a
    # mega-cap is in the news every day of its life.
    news3 = sorted(d["news3"] for d in digests if d.get("news3"))
    return {"peer_pe": peer_pe, "median_pe": median_pe,
            "dds": dds, "ups": ups, "gaps": gaps, "news3": news3}


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
               closes: list | None, seed: str | None = None) -> dict:
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
                      "v": f"{_ordinal(d['pe_own_pct'])} pct"})
    if d["rev_up"] is not None:
        chips.append({"t": "up" if d["rev_up"] >= d["rev_comps"] - 2 else "mut",
                      "l": "Revenue up YoY",
                      "v": f"{d['rev_up']}/{d['rev_comps']} quarters"})
    if d["upside"] is not None and (d["upside"] > 0.12 or sell and d["upside"] < 0):
        chips.append({"t": "up" if d["upside"] > 0 else "down",
                      "l": "Consensus target",
                      "v": f"{d['upside'] * 100:+.0f}%"})
    if d["neg7"]:
        chips.append({"t": "down", "l": "Bearish stories 7d",
                      "v": str(d["neg7"])})
    ch_moves = [(m, w) for m, w in ((d.get("chg1d"), "1d"),
                                    (d.get("move3d"), "3d"))
                if m is not None]
    ch_jolt, ch_win = (max(ch_moves, key=lambda x: abs(x[0]))
                       if ch_moves else (None, ""))
    if ch_jolt is not None and abs(ch_jolt) >= JOLT_MIN:
        chips.append({"t": "down" if ch_jolt < 0 else "up",
                      "l": f"Move {ch_win}", "v": f"{ch_jolt:+.1f}%"})
    if d["big_news"]:
        chips.append({"t": "mut", "l": "Big news 3d",
                      "v": f"{d['big_news']} stor{'y' if d['big_news'] == 1 else 'ies'}"})
    if d["since_earn"] is not None and d["since_earn"] <= 3:
        chips.append({"t": "mut", "l": "Reported",
                      "v": "today" if d["since_earn"] == 0
                           else f"{d['since_earn']}d ago"})
    elif d["earn_days"] is not None and 2 < d["earn_days"] <= 10:
        chips.append({"t": "mut", "l": "Earnings in",
                      "v": f"{d['earn_days']} days"})

    # The series come first because what a name may *claim* depends on what
    # can be drawn beside the claim. Each `have` entry mirrors the matching
    # test in the page's CHART_KINDS; the two have to agree, or a sentence
    # will name a chart the carousel then quietly replaces.
    ma_series = _ma_series(closes) if closes else None
    # A drawdown chart with two years behind it argues nothing; when the
    # sentence could not use the rarity, the chart should not imply it.
    dd_ok = bool(dds and dds["years"] >= RARITY_MIN_YEARS)
    dd_series = _dd_series(closes) if closes and dd_ok else None
    seasonality = _seasonality(d.get("monthly_raw"))
    # Twelve, not eight: the chart draws the last eight and needs the four
    # before them to mark what each quarter is being compared against.
    rev_series = [{"e": q["e"], "r": q["r"]}
                  for q in (d.get("rev_q") or [])
                  if q.get("e") and q.get("r")][-12:] or None
    street_targets = [{"house": t.get("house"), "analyst": t.get("analyst"),
                       "target": t.get("target"), "d": t.get("published")}
                      for t in d.get("street_rows", [])
                      if t.get("target")][:5] or None
    pe_history = ([{"d": p["d"], "pe": round(_sane_pe(p.get("pe")), 1)}
                   for p in d.get("pe_hist", [])
                   if p.get("d") and _sane_pe(p.get("pe")) is not None][-40:]
                  or None)
    have = {
        "drawdown": len(dd_series or []) > 10,
        "ma": len(ma_series or []) > 20,
        "seasonality": sum(1 for x in (seasonality or [])
                           if x and x.get("r") is not None) >= 8,
        "targets": bool(street_targets or d.get("target_range")),
        "revenue": len(rev_series or []) >= 4,
        "pe": (len(pe_history or []) >= 8
               or bool(d.get("pe") and d.get("peer_pe"))),
    }
    claims = _claims(d, s, side, have)
    # A provisional sentence, so one idea built on its own still reads
    # correctly; run_scan settles the day's leads together afterwards.
    provisional = _pick_lead(claims, {}, seed)
    headline = (_compose(provisional, [c for c in claims if c is not provisional])
                if provisional else f"{d['name']} screens well today.")
    lead_chart = provisional["chart"] if provisional else None

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
        "ddSeries": dd_series,
        "seasonality": seasonality,
        "revSeries": rev_series,
        "streetTargets": street_targets,
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
        "seed": seed,
        "priceSeries": _weekly(closes or [], 3.0) or None,
        "maSeries": ma_series,
        "avg200": d.get("avg200"),
        "extPct": round(d["ext_pct"] * 100, 1) if d.get("ext_pct") is not None else None,
        "peHistory": pe_history,
        "thin": s["sell_thin" if sell else "thin"],
    }

    return {
        "symbol": d["symbol"], "side": side, "rank": rank, "isPick": is_pick,
        "score": round(s["sell" if sell else "buy"]),
        "families": {k: round(v) for k, v in fams.items()},
        "gates": gates, "evidence": evidence,
        "headline": headline, "price": d["price"],
        "seed": seed,
        # Consumed by spread_leads and removed there; record_alpha_day reads
        # named keys, so it could never reach the database in any case.
        "_claims": claims,
    }


LEAD_SLACK = 10.0     # how much claim strength variety may cost
REPEAT_SLACK = 6.0    # and how much more, per slide a chart already owns

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def _claims(d: dict, s: dict, side: str, have: dict) -> list[dict]:
    """Every true thing this idea can say about itself, strongest first.

    An idea can usually argue its case several ways. Choosing the top of a
    fixed ladder every time made five of nine slides argue their drawdown:
    price-vs-history carries the heaviest weight and is also what the buy
    screen selects for, so it wins that ladder again and again. Offering all
    the claims instead lets the day pick between them -- see `spread_leads`.

    A claim's strength is the score its own family earned for this idea, so
    "the most representative thing to say" falls out of the scoring rather
    than a hand-written order. The small bonuses only break ties inside a
    family, where two claims would otherwise be indistinguishable.

    `chart` is the picture that proves the claim; None means the sentence
    needs no chart of its own -- the trades are flagged on the price chart
    beside it -- and the page may illustrate the idea however it likes. A
    claim whose chart cannot be drawn for this idea is still allowed to be a
    tail, never a lead: the sentence and the picture have to agree.
    """
    name = d["name"]
    dds = d.get("dd_stats")
    fams = s["sell_f"] if side == "SELL" else s["fams"]
    thin = s["sell_thin" if side == "SELL" else "thin"]
    out: list[dict] = []

    # The move, over whichever window saw more of it. The price chart beside
    # the sentence already draws it, so these claims name no chart of their
    # own -- which also leaves the carousel free to illustrate them.
    moves = [(m, w) for m, w in ((d.get("chg1d"), "in a session"),
                                 (d.get("move3d"), "in three sessions"))
             if m is not None]
    jolt, window = max(moves, key=lambda x: abs(x[0])) if moves else (None, "")

    def add(fam, chart, lead, tail=None, bonus=0.0, seed=None):
        # A family with no evidence behind it scores a flat 50; letting it
        # lead would dress up an absence as an argument.
        if fam and fam in thin:
            return
        out.append({
            "fam": fam, "chart": chart, "lead": lead, "tail": tail,
            "score": (fams.get(fam, 50.0) if fam else 45.0) + bonus,
            "leadable": chart is None or bool(have.get(chart)),
            "seed": seed,
        })

    if side == "BUY":
        if d["cg_amt"] >= CONGRESS_SEED_MIN and d["ins_cluster_buy"]:
            who = _congress_who(d)
            add("smart", None,
                f"{who} and company insiders both bought {name} inside a month",
                "Congress and its own insiders have both been buying",
                bonus=6, seed=SEED_CONGRESS)
        if d["cg_amt"] >= CONGRESS_SEED_MIN:
            who = _congress_who(d)
            tail_who = who if d.get("cg_lead_person") else "Congress"
            add("smart", None,
                f"{who} disclosed ~{_fmt_musd(d['cg_amt'])} "
                f"of {name} purchases",
                f"{tail_who} disclosed ~{_fmt_musd(d['cg_amt'])} of purchases",
                bonus=5 if d.get("cg_lead_person") else 3,
                seed=SEED_CONGRESS)
        if d["ins_cluster_buy"]:
            add("smart", None,
                f"{d['ins_buyers']} {name} insiders bought their own stock "
                f"inside a month",
                f"{d['ins_buyers']} insiders bought inside a month",
                seed=SEED_INSIDER)
        if jolt is not None and jolt <= -JOLT_MIN:
            add("event", None,
                f"{name} has fallen {abs(jolt):.0f}% {window}",
                f"it is down {abs(jolt):.0f}% {window}", bonus=3,
                seed=SEED_NEWS)
        if d["since_earn"] is not None and d["since_earn"] <= 3:
            said = ("beating" if (d["earn_surprise"] or 0) > 0 else "missing"
                    ) if d["earn_surprise"] is not None else None
            add("event", None,
                f"{name} reported "
                + (f"this week, {said} on earnings" if said else "this week"),
                f"it reported "
                + (f"{said} on earnings" if said else "this week"),
                seed=SEED_NEWS)
        if d["big_news"]:
            add("event", None,
                f"{name} has {d['big_news']} significant "
                f"stor{'y' if d['big_news'] == 1 else 'ies'} in three days",
                f"significant news broke this week",
                bonus=2, seed=SEED_NEWS)
        if dds and dds["rarity"] >= 90 and dds["years"] >= RARITY_MIN_YEARS:
            pct = 100 - dds["rarity"]
            share = f"only {pct:.0f}%" if pct >= 1 else "fewer than 1%"
            add("hist", "drawdown",
                f"{name} has been this far below its high on {share} of days "
                f"in {dds['years']:.0f} years",
                "it has rarely been this far below its high", bonus=4,
                seed=SEED_DIP)
        if dds:
            add("hist", "drawdown",
                f"{name} trades {dds['dd'] * 100:.0f}% below its own high",
                f"the stock sits {dds['dd'] * 100:.0f}% off its high")
        if (d.get("ext_pct") is not None
                and abs(d["ext_pct"]) <= MA_TOUCH_BAND):
            add("hist", "ma",
                f"{name} has pulled back to its 200-day average",
                "it has pulled back to its 200-day average", bonus=2)
        if d.get("pe") and d.get("peer_pe") and d["pe"] < d["peer_pe"] * 0.8:
            add("val", "pe",
                f"{name} trades at {d['pe']:.0f}× earnings against peers "
                f"at {d['peer_pe']:.0f}×",
                f"it trades at {d['pe']:.0f}× earnings against peers "
                f"at {d['peer_pe']:.0f}×")
        if d["rev_up"] is not None and d["rev_up"] >= d["rev_comps"] - 2:
            add("fund", "revenue",
                f"{name} has grown revenue year on year in {d['rev_up']} of "
                f"its last {d['rev_comps']} quarters",
                f"revenue is up year on year in {d['rev_up']} of the last "
                f"{d['rev_comps']} quarters")
        if d["upside"] is not None and d["upside"] > 0.12:
            add("street", "targets",
                f"{name} trades {d['upside'] * 100:.0f}% below the Street's "
                f"median target",
                f"the median Street target sits {d['upside'] * 100:.0f}% higher")
        if d["season"] is not None and d["season"] > 0.5:
            add("mom", "seasonality",
                f"{name} has averaged {d['season']:+.1f}% in "
                f"{_MONTHS[d['season_month'] - 1]} over the years on file",
                f"{_MONTHS[d['season_month'] - 1]} has averaged "
                f"{d['season']:+.1f}% for it")
        add(None, None, f"{name} screens cheap on several families at once")
    else:
        if (d.get("pe_own_pct") or 0) >= 88:
            add("hist", "pe",
                f"{name} is priced near the top of its own record — a P/E in "
                f"its {_ordinal(d['pe_own_pct'])} percentile",
                f"its P/E sits in its own {_ordinal(d['pe_own_pct'])} percentile",
                bonus=4)
        if jolt is not None and jolt >= JOLT_MIN:
            add("event", None,
                f"{name} has run {jolt:.0f}% {window}",
                f"it is up {jolt:.0f}% {window}", bonus=3)
        if (d["since_earn"] is not None and d["since_earn"] <= 3
                and (d["earn_surprise"] or 0) < 0):
            add("event", None,
                f"{name} missed on earnings this week",
                "it missed on earnings this week")
        if d["ins_cluster_sell"]:
            add("smart", None,
                f"{d['ins_sellers']} {name} insiders each sold more than "
                f"$500K of stock inside a month",
                f"{d['ins_sellers']} insiders each sold over $500K")
        if dds and dds["dd"] <= 0.05:
            # The same drawdown chart that argues a cushion on the way down
            # argues the absence of one here: the line is against its ceiling.
            where = ("at its own high" if dds["dd"] < 0.01
                     else f"within {dds['dd'] * 100:.0f}% of its own high")
            add("hist", "drawdown",
                f"{name} is trading {where} with no cushion behind it",
                f"it sits {where}", bonus=-2)
        if d["season"] is not None and d["season"] < -0.5:
            add("mom", "seasonality",
                f"{name} has averaged {d['season']:+.1f}% in "
                f"{_MONTHS[d['season_month'] - 1]} over the years on file",
                f"{_MONTHS[d['season_month'] - 1]} has averaged "
                f"{d['season']:+.1f}% for it")
        if d.get("ext_pct") and d["ext_pct"] > 0.08:
            add("mom", "ma",
                f"{name} trades {d['ext_pct'] * 100:.0f}% above its 200-day "
                f"average",
                f"{d['ext_pct'] * 100:.0f}% above its 200-day average")
        if d.get("pe") and d.get("peer_pe") and d["pe"] > d["peer_pe"] * 1.2:
            add("val", "pe",
                f"{name} trades at {d['pe']:.0f}× earnings against peers "
                f"at {d['peer_pe']:.0f}×",
                f"{d['pe']:.0f}× earnings against peers at "
                f"{d['peer_pe']:.0f}×")
        if (d["rev_up"] is not None
                and d["rev_up"] * 2 < d["rev_comps"]):
            add("fund", "revenue",
                f"{name} has grown revenue year on year in only {d['rev_up']} "
                f"of its last {d['rev_comps']} quarters",
                f"revenue rose year on year in only {d['rev_up']} of "
                f"{d['rev_comps']} quarters")
        if d["upside"] is not None and d["upside"] < 0:
            add("street", "targets",
                f"{name} trades above the Street's median target",
                f"it is above the median Street target")
        if d["neg7"]:
            add(None, None,
                f"{name} carries {d['neg7']} bearish stories this week",
                f"{d['neg7']} bearish stories this week")

    out.sort(key=lambda c: -c["score"])
    return out


def _compose(chosen: dict, others: list[dict]) -> str:
    """The sentence: the chosen claim, then up to two from other families.

    Tails come from families the lead has not already spoken for, which is
    what stops a name arguing its drawdown twice with two different numbers.
    """
    tails, seen = [], {chosen["fam"]}
    for c in others:
        if len(tails) == 2:
            break
        if not c.get("tail") or c["fam"] in seen:
            continue
        if chosen["chart"] and c["chart"] == chosen["chart"]:
            continue
        tails.append(c["tail"])
        seen.add(c["fam"])
    return chosen["lead"] + (" — " + ", and ".join(tails) if tails else "") + "."


def _pick_lead(claims: list[dict], used: dict,
               seed: str | None = None) -> dict | None:
    """The claim this idea should lead with, given what the day has shown.

    Strongest first, except that a claim may be passed over for one within
    `LEAD_SLACK` of it whose chart the carousel has not used as often. A much
    weaker claim never wins: variety is worth a little strength, not the
    argument itself. When `seed` is set, only claims that match the day's
    catalyst may lead -- hist cannot steal a congress headline.
    """
    leadable = [c for c in claims if c["leadable"]]
    if seed:
        matched = [c for c in leadable if _claim_matches_seed(c, seed)]
        if matched:
            leadable = matched
    if not leadable:
        return None
    top = leadable[0]
    # A chart the day has already leaned on has to defend itself harder: each
    # slide it already owns buys the alternatives a little more room. Without
    # this a day of similar names -- which is what a screen returns -- argues
    # itself the same way nine times over, however wide the fixed slack is.
    saturation = REPEAT_SLACK * (used.get(top["chart"], 0) if top["chart"] else 0)
    floor = top["score"] - LEAD_SLACK - saturation
    pool = [c for c in leadable if c["score"] >= floor]
    # Claims that need no chart cost the day nothing, so they never look used.
    pool.sort(key=lambda c: (used.get(c["chart"], 0) if c["chart"] else 0,
                             -c["score"]))
    return pool[0]


def spread_leads(ideas: list[dict]) -> None:
    """Settle the day's sentences together rather than one slide at a time."""
    used: dict[str, int] = {}
    for idea in ideas:
        claims = idea.pop("_claims", None) or []
        seed = idea.get("seed")
        chosen = _pick_lead(claims, used, seed)
        if not chosen:
            continue
        if chosen["chart"]:
            used[chosen["chart"]] = used.get(chosen["chart"], 0) + 1
        idea["headline"] = _compose(
            chosen, [c for c in claims if c is not chosen])
        idea["evidence"]["lead"] = chosen["chart"]


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

    # Shortlist for the decade-history pass: top composite buys/sells plus
    # any catalyst names that might headline on a soft cap.
    buys = sorted((x for x in scored.values() if not buy_gates(x[0])),
                  key=lambda x: -x[1]["buy"])[:SHORTLIST]
    sells = sorted((x for x in scored.values()
                    if x[1]["sell_anchor"] and (x[0]["cap"] or 0) >= _cap_floor(x[0])
                    and not (x[0]["earn_days"] is not None
                             and 0 <= x[0]["earn_days"] <= 2)),
                   key=lambda x: -x[1]["sell"])[:SHORTLIST]
    short = {x[0]["symbol"]: x[0] for x in buys + sells}
    for d, s in scored.values():
        if d["symbol"] in short:
            continue
        if not buy_gates(d) and mine_seeds(d):
            short[d["symbol"]] = d
    closes_by_sym = fill_history(list(short.values()))

    # Precise re-score with decade drawdown facts.
    for sym, d in short.items():
        d["dd_stats"] = _drawdown_stats(closes_by_sym.get(sym), d["price"])
    for sym in short:
        d = scored[sym][0]
        scored[sym] = (d, score(d, ctx))

    # What has already been posted, in any slot. The read this used before
    # returned headline picks only, so the eight candidates under each day's
    # pick were never held back and came round again the next morning --
    # which is what made two consecutive boards look like one.
    #
    # Today's own stored ideas must not cool themselves down: a --force
    # re-run of the same day should be free to reach the same conclusion.
    today_iso = day.isoformat()
    posted, led = {}, {}
    for row in (store.rpc("alpha_recent_symbols",
                          {"p_days": max(REPEAT_DAYS, PICK_COOLDOWN_DAYS)}) or []):
        sym = row.get("symbol")
        if not sym:
            continue
        if row.get("lastDay") and row["lastDay"] != today_iso:
            posted[sym] = row["lastDay"]
        if row.get("lastPickDay") and row["lastPickDay"] != today_iso:
            led[sym] = row["lastPickDay"]

    def _rested(iso: str) -> int:
        try:
            return (day - dt.date.fromisoformat(str(iso)[:10])).days
        except (TypeError, ValueError):
            return 999

    # Any slot rests a week; leading again takes a fortnight.
    cooldown = {s for s, iso in posted.items() if _rested(iso) < REPEAT_DAYS}
    pick_cooldown = cooldown | {s for s, iso in led.items()
                                if _rested(iso) < PICK_COOLDOWN_DAYS}
    log(f"resting: {len(cooldown)} posted in {REPEAT_DAYS}d, "
        f"{len(pick_cooldown)} barred from leading")

    last_lead_seed = _last_headline_seed(day)
    if last_lead_seed:
        log(f"yesterday's headline seed: {last_lead_seed}")

    story_buys: list[tuple] = []
    for d, s in scored.values():
        if d["symbol"] in pick_cooldown or buy_gates(d):
            continue
        seeds = mine_seeds(d)
        if not seeds:
            continue
        support = support_families(d, s)
        if not support:
            continue
        best = seeds[0]
        penalty = 14.0 if best["type"] == last_lead_seed else 0.0
        rank = (best["strength"] + s["buy"] * 0.35
                + 6.0 * len(support) - penalty)
        story_buys.append((rank, d, s, best["type"]))
    story_buys.sort(key=lambda x: -x[0])
    best_story = story_buys[0][1:] if story_buys else None

    best_buy = None
    for d, s in sorted(scored.values(), key=lambda x: -x[1]["buy"]):
        if d["symbol"] in pick_cooldown or buy_gates(d):
            continue
        if len(_strong(s["fams"], s["thin"])) < 2:
            continue
        best_buy = (d, s)
        break

    best_sell = None
    for d, s in sorted(scored.values(), key=lambda x: -x[1]["sell"]):
        if d["symbol"] in pick_cooldown or not s["sell_anchor"]:
            continue
        if (d["cap"] or 0) < _cap_floor(d):
            continue
        if d["earn_days"] is not None and 0 <= d["earn_days"] <= 2:
            continue
        if len(_strong(s["sell_f"], s["sell_thin"])) < 2:
            continue
        best_sell = (d, s)
        break

    pick_seed = None
    if best_story:
        sd, ss, pick_seed = best_story
        story_sym, story_seed = sd["symbol"], pick_seed
        if best_sell and best_sell[1]["sell"] >= ss["buy"] + (
                15.0 if pick_seed in (SEED_CONGRESS, SEED_INSIDER) else 8.0):
            winner = "SELL"
            pick_d, pick_s = best_sell
            log(f"story pick {story_sym} seed={story_seed} edged by sell "
                f"{best_sell[0]['symbol']}")
            pick_seed = None
        else:
            winner = "BUY"
            pick_d, pick_s = sd, ss
            log(f"story pick: BUY {pick_d['symbol']} seed={pick_seed} "
                f"({ss['buy']:.1f})")
    elif best_buy and best_sell:
        margin = 15.0 if is_event(best_buy[0]) else 5.0
        winner = ("SELL" if best_sell[1]["sell"] >= best_buy[1]["buy"] + margin
                  else "BUY")
        pick_d, pick_s = best_buy if winner == "BUY" else best_sell
        log(f"composite pick: {winner} {pick_d['symbol']} "
            f"({pick_s['buy' if winner == 'BUY' else 'sell']:.1f})")
    elif best_buy or best_sell:
        winner = "BUY" if best_buy else "SELL"
        pick_d, pick_s = best_buy if best_buy else best_sell
        log(f"fallback pick: {winner} {pick_d['symbol']}")
    else:
        log("no name cleared conviction and gates today; nothing stored")
        return False

    if not best_story:
        log(f"pick: {winner} {pick_d['symbol']} "
            f"({pick_s['buy' if winner == 'BUY' else 'sell']:.1f})")

    def _story_candidates(side: str, limit: int) -> list[tuple]:
        """Up to `limit` names, round-robin across seed types."""
        buckets: dict[str, list] = {t: [] for t in SEED_TYPES}
        buckets["composite"] = []
        key = (lambda x: -x[1]["buy"]) if side == "BUY" else (
            lambda x: -x[1]["sell"])
        for d, s in sorted(scored.values(), key=key):
            if d["symbol"] == pick_d["symbol"] or d["symbol"] in cooldown:
                continue
            if side == "BUY":
                if buy_gates(d):
                    continue
            else:
                if (not s["sell_anchor"] or (d["cap"] or 0) < _cap_floor(d)
                        or (d["earn_days"] is not None
                            and 0 <= d["earn_days"] <= 2)):
                    continue
            seeds = mine_seeds(d)
            seed = seeds[0]["type"] if seeds else None
            bucket = seed if seed in buckets else "composite"
            buckets[bucket].append((d, s, seed))
        picked: list[tuple] = []
        order = list(SEED_TYPES) + ["composite"]
        while len(picked) < limit:
            moved = False
            for k in order:
                if buckets[k]:
                    picked.append(buckets[k].pop(0))
                    moved = True
                    if len(picked) >= limit:
                        break
            if not moved:
                break
        return picked

    ideas = [build_idea(pick_d, pick_s, winner, 1, True,
                        closes_by_sym.get(pick_d["symbol"]), pick_seed)]
    cand_b = _story_candidates("BUY", 4)
    cand_s = _story_candidates("SELL", 4)
    start_b = 2 if winner == "BUY" else 1
    start_s = 2 if winner == "SELL" else 1
    if len(cand_b) < 4 or len(cand_s) < 4:
        log(f"candidates: {len(cand_b)} buys, {len(cand_s)} sells cleared the "
            f"{REPEAT_DAYS}-day rest — showing a shorter board rather than "
            "repeating a name")
    for i, (d, s, seed) in enumerate(cand_b):
        ideas.append(build_idea(d, s, "BUY", start_b + i, False,
                                closes_by_sym.get(d["symbol"]), seed))
    for i, (d, s, seed) in enumerate(cand_s):
        ideas.append(build_idea(d, s, "SELL", start_s + i, False,
                                closes_by_sym.get(d["symbol"]), seed))

    # Sentences last: each idea now knows everything it could say, and the
    # day picks between them so nine slides do not make the same argument.
    spread_leads(ideas)

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
