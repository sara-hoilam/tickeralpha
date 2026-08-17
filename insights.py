"""
insights.py -- writes the market page's "Today's Brief" insights.

Run once each weekday morning by .github/workflows/insights.yml, before the
US open. Reads through the same public RPCs a visitor's browser calls, works
out what actually moved -- industries running together, names clearing
prices they have never closed above, runs weeks in the making -- alongside
the day's schedule and news, asks Claude to pick the three to five worth
knowing about, and writes the result to ledger.market_insight (0065).

Movement leads on purpose. The first version of this was handed the calendar
and nothing else, so it could only narrate the calendar, and it read like a
timetable. What a reader is talking about on a given morning is usually a
move.

The governing idea: the code decides what is important, the model only
explains it. Ranking, filtering, and every number are settled here in Python
before the API call; the model receives a short list of already-chosen
candidates. That bounds the cost to one small call a day, bounds the failure
surface -- it cannot surface an event that was never selected -- and makes
the output checkable, because a validator can prove every claim traces back
to an input.

Nothing here runs in the request path. The page reads a stored row, so the
token cost is one call a day whether the market page is opened twice or
twenty thousand times, and a visitor never waits on inference.

Usage:
    python insights.py                  write today's brief
    python insights.py --dry-run        print the candidates and the result,
                                        write nothing
    python insights.py --day 2026-08-17 generate for a specific day

Environment:
    ANTHROPIC_API_KEY            required
    SUPABASE_URL                 required (reads)
    SUPABASE_SERVICE_ROLE_KEY    required (writes; not needed for --dry-run)
    SUPABASE_ANON_KEY            optional; falls back to public/config.js
    ANTHROPIC_MODEL              optional; defaults to MODEL below
    ANTHROPIC_FALLBACK_MODEL     optional; retried once if the first model
                                 declines the request

Unlike the rest of the project this file is not standard-library-only: it
uses the official `anthropic` SDK, which is the supported way to call the
API and gives typed errors and retries for free. That dependency lives in
the GitHub Actions runner, not in the Render worker, so worker.py and
store.py stay dependency-free.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import envload  # noqa: F401  -- reads .env for local runs
import store

# The model that writes the brief. Opus is the default because the job runs
# once a day and the payload is small -- a few thousand input tokens against
# a handful of output ones -- so the price difference against a cheaper tier
# is cents a month. Set ANTHROPIC_MODEL to change it.
MODEL = os.environ.get("ANTHROPIC_MODEL") or "claude-opus-5"

# A refusal is unlikely on market copy, but a declined request would leave the
# page showing yesterday's brief. Naming a second model here retries once.
FALLBACK_MODEL = os.environ.get("ANTHROPIC_FALLBACK_MODEL") or ""

# Thinking is on by default on the current models and counts against
# max_tokens, so leave headroom above what the brief itself needs (~800).
MAX_TOKENS = 8000
# Three themes, three standout movers, two most-covered names, three
# releases, three reporters, four stories and the last session. The cap
# sits just above a full day so nothing is silently dropped off the end.
MAX_CANDIDATES = 20
MIN_INSIGHTS = 3
MAX_INSIGHTS = 5

# Length is a house-style preference, not a correctness rule, so the ceiling
# sits well clear of the wording the prompt asks for. A brief whose facts all
# check out is not worth discarding over a few characters -- the first run of
# this job lost one that was seven characters long. These numbers are quoted
# to the model in the prompt so it can measure itself against them.
HEADLINE_MAX = 70
BODY_MIN = 80
BODY_MAX = 400


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

# GitHub's cron is UTC year-round; the market is not. Everything the brief
# says about timing is Eastern, so the day is resolved in Eastern too.
def _eastern_now() -> dt.datetime:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # No tzdata (bare Windows, minimal container). The job runs in the
        # morning, so a fixed -5 lands on the right calendar day either side
        # of the DST boundary; only the printed hour would be off, and this
        # function is used for the date.
        return dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)


# NYSE closures. Extend each year -- a missing entry means the job runs on a
# holiday and finds nothing scheduled, which the quiet-day path handles; a
# wrong entry means a skipped day. Both fail safe.
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


def long_day(day: dt.date) -> str:
    return day.strftime("%A, %d %B %Y").replace(" 0", " ")


# ---------------------------------------------------------------------------
# Reads -- the same public RPCs the page calls
# ---------------------------------------------------------------------------

def _anon_key() -> str:
    key = os.environ.get("SUPABASE_ANON_KEY") or ""
    if key:
        return key
    # The anon key ships in every visitor's browser, so the committed
    # config.js is a legitimate source for it and one less GitHub secret to
    # keep in sync. It reaches read-only functions; the tables live in a
    # schema the API does not expose.
    try:
        import pathlib
        text = (pathlib.Path(__file__).resolve().parent
                / "public" / "config.js").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r'supabaseAnonKey:\s*"([^"]+)"', text)
    return m.group(1) if m else ""


def read_rpc(fn: str, args: dict | None = None):
    """Call one of the anon-readable read functions."""
    base = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    if base.endswith("/rest/v1"):
        base = base[: -len("/rest/v1")].rstrip("/")
    key = _anon_key()
    if not base or not key:
        raise SystemExit("SUPABASE_URL and an anon key are required to read.")

    req = urllib.request.Request(
        f"{base}/rest/v1/rpc/{fn}",
        data=json.dumps(args or {}).encode("utf-8"),
        method="POST",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise SystemExit(f"{fn} -> HTTP {exc.code}: {detail}") from exc
    return json.loads(raw) if raw.strip() else None


# ---------------------------------------------------------------------------
# Display formatting
# ---------------------------------------------------------------------------
# Every number the model is allowed to print is formatted here, once, and
# handed over as a finished string. The prompt then forbids any figure that
# was not copied character for character out of these, which turns the
# validation of "did it make this number up" into a substring test rather
# than a judgement call. Keep the wording aligned with the front end.

def fmt_num(v) -> str | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f == int(f) and abs(f) < 1e15:
        return str(int(f))
    return f"{f:.2f}".rstrip("0").rstrip(".")


def fmt_pct(v) -> str | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f"{f:+.2f}%"


def fmt_money(v) -> str | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # Thousands separated, because a four-figure share price is written
    # "$1,024.50" by anyone describing it and the fact should match.
    return f"${f:,.2f}"


def fmt_cap(v) -> str | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    for cut, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(f) >= cut:
            return f"${f / cut:.2f}{suffix}"
    return f"${f:.0f}"


# ---------------------------------------------------------------------------
# News dedupe -- mirrors newsKey() in news.html
# ---------------------------------------------------------------------------

def news_index(news: list) -> dict[str, list[dict]]:
    """Stories keyed by the ticker they are tagged with, newest first.

    Two things come out of this. A mover gets a headline attached, so the
    brief can say why it moved rather than only how far -- a 44% jump with
    no reason given is the least useful sentence on the page. And the count
    itself is a signal: the name with six stories against it is the one
    being discussed, whether or not it is the day's biggest percentage move.
    """
    by_sym: dict[str, list[dict]] = {}
    seen: set[str] = set()
    for a in news or []:
        sym = str(a.get("symbol") or "").upper()
        if not sym:
            continue
        key = news_key(a)
        if key in seen:
            continue                        # the same story from two wires
        seen.add(key)
        by_sym.setdefault(sym, []).append(a)
    for rows in by_sym.values():
        rows.sort(key=lambda a: str(a.get("published") or ""), reverse=True)
    return by_sym


def headline_facts(symbols: list[str], by_sym: dict[str, list[dict]],
                   limit: int = 2) -> list[str]:
    """Up to `limit` headlines covering the names in this candidate."""
    facts, used = [], 0
    for sym in symbols:
        for a in by_sym.get(str(sym).upper(), [])[:1]:
            title = str(a.get("title") or "").strip()
            if not title:
                continue
            facts.append(f"headline on {sym}: {title}")
            used += 1
            if used >= limit:
                return facts
    return facts


def news_key(article: dict) -> str:
    title = str(article.get("title") or "").lower()
    title = re.sub(r"[-–—|:]\s*[a-z .]{3,24}$", "", title)   # trailing publisher
    title = re.sub(r"[^a-z0-9 ]+", " ", title)
    toks = [w for w in title.split() if len(w) >= 3]
    return f"{article.get('symbol') or ''}|{' '.join(sorted(set(toks))[:10])}"


# Stories worth a slot even when they name no company the page tracks.
MACRO_WORDS = (
    "fed", "inflation", "cpi", "jobs", "payroll", "tariff", "rate", "rates",
    "gdp", "recession", "oil", "opec", "treasury", "yield", "dollar",
    "sanction", "election", "shutdown", "strike", "war",
)


# ---------------------------------------------------------------------------
# What is actually moving
# ---------------------------------------------------------------------------
# The brief was flat for a structural reason: it was handed the calendar and
# nothing else, so it could only ever narrate the calendar. What a reader is
# actually talking about on a given morning is usually a move -- a whole
# corner of the market running together, a name clearing a number it has
# never cleared before, a run that has been building for weeks. All of that
# is derivable from data already in the database, so it is computed here and
# offered as candidates in its own right.

# A theme needs several names moving together, not one big name dragging a
# cap-weighted average around: three members at 3% is a story about an
# industry, one member at 12% is a story about a company.
THEME_MEMBER_MOVE = 3.0
THEME_MIN_MEMBERS = 3
THEME_MIN_WEIGHTED = 1.5
# Below this the "industry" is a handful of microcaps nobody is discussing.
THEME_MIN_CAP = 2e10
# A "big mover" worth a slot of its own. Without a floor the ranking is a
# microcap leaderboard: percentage moves scale inversely with size, so the
# names people actually discuss never reach the top of it.
MOVER_NOTABLE_CAP = 2e10
# Enough separate stories to count as a conversation rather than one wire
# report syndicated twice.
BUZZ_MIN_STORIES = 2

# Round numbers a price crossing gets talked about: 10, 25, 50, 100, 250 ...
_ROUND_STEPS = (10, 25, 50, 100, 250, 500, 1000, 2000, 5000, 10000)


def round_number_crossed(prev_close, close) -> str | None:
    """The largest round number a close has just moved up through."""
    try:
        a, b = float(prev_close), float(close)
    except (TypeError, ValueError):
        return None
    hit = [s for s in _ROUND_STEPS if a < s <= b]
    return f"${hit[-1]:,}" if hit else None


def find_themes(industries: list) -> list[dict]:
    """Industries where a group of names moved together today."""
    themes = []
    for ind in industries or []:
        items = ind.get("items") or []
        weighted = ind.get("changePct")
        if weighted is None or (ind.get("marketCap") or 0) < THEME_MIN_CAP:
            continue
        direction = 1 if weighted > 0 else -1
        movers = [i for i in items
                  if i.get("changePct") is not None
                  and i["changePct"] * direction >= THEME_MEMBER_MOVE]
        if len(movers) < THEME_MIN_MEMBERS or abs(weighted) < THEME_MIN_WEIGHTED:
            continue
        movers.sort(key=lambda i: abs(i["changePct"]), reverse=True)
        themes.append({
            "industry": ind.get("industry"),
            "sector": ind.get("sector"),
            "weighted": weighted,
            "members": movers[:4],
            "n": len(movers),
            # Rank by how broad the move is and how hard it moved: a whole
            # industry up 4% outranks two names up 9%.
            "score": abs(weighted) * (len(movers) ** 0.5),
        })
    themes.sort(key=lambda t: t["score"], reverse=True)
    return themes


def price_story(symbol: str, quote: dict | None, bars: list | None) -> dict:
    """Multi-day context for one name: the run behind today's move."""
    story: dict = {}
    closes = []
    for b in (bars or [])[-260:]:
        try:
            closes.append(float(b.get("c")))
        except (TypeError, ValueError):
            pass

    if len(closes) >= 6:
        # "has been ripping" is a run, not a day.
        wk = (closes[-1] / closes[-6] - 1) * 100
        if abs(wk) >= 5:
            story["week"] = f"{wk:+.1f}% over five sessions"
    if len(closes) >= 22:
        mo = (closes[-1] / closes[-22] - 1) * 100
        if abs(mo) >= 10:
            story["month"] = f"{mo:+.1f}% over a month"
    if len(closes) >= 3:
        streak = 0
        for i in range(len(closes) - 1, 0, -1):
            if closes[i] > closes[i - 1]:
                streak += 1
            else:
                break
        if streak >= 3:
            story["streak"] = f"up {streak} sessions in a row"

    q = quote or {}
    price, high = q.get("price"), q.get("year_high")
    try:
        price, high = float(price), float(high)
        if price >= high:
            story["high"] = "at a 52-week high"
        elif high > 0 and (high - price) / high <= 0.02:
            story["high"] = f"within {((high - price) / high * 100):.1f}% of its 52-week high"
    except (TypeError, ValueError):
        pass

    if len(closes) >= 2:
        crossed = round_number_crossed(closes[-2], closes[-1])
        if crossed:
            story["crossed"] = f"closed above {crossed} for the first time in this run"
    return story


def fetch_price_stories(symbols: list[str]) -> dict[str, dict]:
    """One get_prices call per shortlisted name. Missing history is fine."""
    out: dict[str, dict] = {}
    for sym in symbols:
        try:
            d = read_rpc("get_prices", {"p_symbol": sym}) or {}
        except SystemExit:
            continue
        story = price_story(sym, d.get("quote"), d.get("bars"))
        if story:
            out[sym] = story
    return out


# ---------------------------------------------------------------------------
# Candidate selection -- deterministic, in Python
# ---------------------------------------------------------------------------

def build_candidates(brief: dict, caps: list, gainers: list, losers: list,
                     news: list, sectors: dict,
                     industries: list | None = None,
                     stories: dict | None = None) -> list[dict]:
    """The short list the model chooses from, in priority order."""
    out: list[dict] = []
    stories = stories or {}
    by_sym = news_index(news)
    # Today's move for any symbol the heatmap covers, so a name that surfaced
    # through the news can still be quoted with its price action.
    moves_by_symbol: dict[str, dict] = {}
    for row in list(gainers or []) + list(losers or []):
        sym = str(row.get("symbol") or "").upper()
        if sym:
            moves_by_symbol[sym] = row
    for ind in industries or []:
        for item in ind.get("items") or []:
            sym = str(item.get("symbol") or "").upper()
            if sym:
                moves_by_symbol.setdefault(sym, item)

    # 1. Themes -- a whole corner of the market moving together. This is the
    #    "memory stocks are ripping" case, and it is first because it is what
    #    a reader is most likely to have already heard about.
    for i, t in enumerate(find_themes(industries or [])[:3]):
        facts = [f"{t['industry']} {fmt_pct(t['weighted'])} today",
                 f"{t['n']} names in the industry moved more than "
                 f"{THEME_MEMBER_MOVE:.0f}%"]
        syms = []
        for m in t["members"]:
            sym = str(m.get("symbol") or "")
            if not sym:
                continue
            syms.append(sym)
            bits = [fmt_pct(m.get("changePct"))]
            price = fmt_money(m.get("price"))
            if price:
                bits.append(f"at {price}")
            for key in ("month", "week", "streak", "high", "crossed"):
                if key in stories.get(sym, {}):
                    bits.append(stories[sym][key])
            facts.append(f"{sym} {' '.join(b for b in bits if b)}")
        # Why, not just how far.
        facts += headline_facts(syms, by_sym, limit=3)
        out.append({
            "id": f"theme-{i + 1}",
            "kind": "theme",
            "when": None,
            "title": f"{t['industry']} moving together",
            "facts": facts,
            "symbols": syms,
            "url": next((by_sym[s][0].get("url") for s in syms
                         if by_sym.get(s)), None),
        })

    # 2. Standout single names, with the run and the reason behind them.
    #    Ranking purely on percentage hands the brief to microcaps: a $1.6B
    #    pharmaceutical up 44% will always beat a megacap up 7%, and nobody
    #    is discussing the pharmaceutical. So take the notable names first
    #    and keep one slot for the outright biggest move whatever its size.
    seen_syms = {s for c in out for s in c["symbols"]}
    pool = []
    for row in (gainers or []) + (losers or []):
        sym = str(row.get("symbol") or "")
        if not sym or sym in seen_syms:
            continue
        seen_syms.add(sym)
        pool.append(row)
    pool.sort(key=lambda r: abs(r.get("changePct") or 0), reverse=True)
    notable = [r for r in pool if (r.get("marketCap") or 0) >= MOVER_NOTABLE_CAP]
    # Notable names first, then fill the remaining slots from the whole pool
    # rather than leaving them empty when few large caps moved.
    standouts = list(notable[:2])
    for r in pool:
        if len(standouts) >= 3:
            break
        if r not in standouts:
            standouts.append(r)

    for i, row in enumerate(standouts[:3]):
        sym = str(row.get("symbol"))
        facts = [f"{sym} {fmt_pct(row.get('changePct'))} today"]
        price = fmt_money(row.get("price"))
        if price:
            facts.append(f"trading at {price}")
        cap = fmt_cap(row.get("marketCap"))
        if cap:
            facts.append(f"market cap {cap}")
        facts += list(stories.get(sym, {}).values())
        facts += headline_facts([sym], by_sym, limit=2)
        out.append({
            "id": f"move-{i + 1}",
            "kind": "move",
            "when": None,
            "title": f"{row.get('name') or sym} ({sym})",
            "facts": facts,
            "symbols": [sym],
            "url": (by_sym.get(sym, [{}])[0] or {}).get("url"),
        })

    # 3. What is being discussed. Story count is the closest thing in the
    #    database to the conversation happening on social and in the media,
    #    and it catches names the percentage ranking misses -- a megacap up
    #    4% with six stories against it is the day's talking point even
    #    though it is nowhere near the top of the gainers list.
    quoted = {s for c in out for s in c["symbols"]}
    buzz = sorted(((sym, rows) for sym, rows in by_sym.items()
                   if len(rows) >= BUZZ_MIN_STORIES and sym not in quoted),
                  key=lambda pair: len(pair[1]), reverse=True)
    for i, (sym, rows) in enumerate(buzz[:2]):
        facts = [f"{len(rows)} separate stories on {sym} in the last two days"]
        move = moves_by_symbol.get(sym) or {}
        if move.get("changePct") is not None:
            facts.append(f"{sym} {fmt_pct(move['changePct'])} today")
        price = fmt_money(move.get("price"))
        if price:
            facts.append(f"trading at {price}")
        facts += list(stories.get(sym, {}).values())
        facts += [f"headline: {str(a.get('title') or '').strip()}"
                  for a in rows[:3] if a.get("title")]
        out.append({
            "id": f"buzz-{i + 1}",
            "kind": "buzz",
            "when": None,
            "title": f"{sym} in the news",
            "facts": facts,
            "symbols": [sym],
            "url": (rows[0] or {}).get("url"),
        })

    # 3. Scheduled economics. High impact first: these move the whole market
    #    rather than one company, but they no longer crowd out everything
    #    else -- three slots, not five.
    econ = list(brief.get("economics") or [])
    rank = {"high": 0, "medium": 1}
    econ.sort(key=lambda e: (rank.get(str(e.get("impact") or "").lower(), 2),
                             e.get("time") or "99:99", e.get("event") or ""))
    for i, e in enumerate(econ[:3]):
        # Spelled out rather than "est." / "prev.": these strings are copied
        # into prose, and an abbreviation's full stop reads as a sentence end
        # to both a reader and the two-sentence check.
        facts = []
        for label, value in (("estimate", e.get("estimate")),
                             ("previous", e.get("previous")),
                             ("actual", e.get("actual"))):
            n = fmt_num(value)
            if n is not None:
                facts.append(f"{label} {n}")
        impact = str(e.get("impact") or "").lower()
        if impact:
            facts.append(f"impact: {impact}")
        out.append({
            "id": f"econ-{i + 1}",
            "kind": "economics",
            "when": e.get("time") or None,
            "title": e.get("event") or "Economic release",
            "facts": facts,
            "symbols": [],
            "url": None,
        })

    # 4. The biggest companies reporting. Already cap-sorted by the RPC.
    for i, e in enumerate((brief.get("earnings") or [])[:3]):
        facts = []
        cap = fmt_cap(e.get("marketCap"))
        if cap:
            facts.append(f"market cap {cap}")
        eps = fmt_money(e.get("epsEstimated"))
        if eps:
            facts.append(f"EPS estimate {eps}")
        actual = fmt_money(e.get("epsActual"))
        if actual:
            facts.append(f"EPS actual {actual}")
        session = {"bmo": "before the open", "amc": "after the close"}.get(
            str(e.get("time") or "").lower())
        if session:
            facts.append(f"reports {session}")
        out.append({
            "id": f"earn-{i + 1}",
            "kind": "earnings",
            "when": None,
            "title": f"{e.get('name') or e.get('symbol')} ({e.get('symbol')})",
            "facts": facts,
            "symbols": [e.get("symbol")] if e.get("symbol") else [],
            "url": None,
        })

    # 5. News themes. Deduped the way the news page dedupes, then the most
    #    recent distinct ones, preferring megacaps and macro subjects.
    big = {str(r.get("symbol") or "").upper() for r in (caps or [])}
    seen: set[str] = set()
    ranked: list[tuple[int, dict]] = []
    for a in news or []:
        key = news_key(a)
        if key in seen:
            continue
        seen.add(key)
        title = str(a.get("title") or "")
        sym = str(a.get("symbol") or "").upper()
        interesting = (sym and sym in big) or any(
            w in title.lower() for w in MACRO_WORDS)
        ranked.append((0 if interesting else 1, a))
    # Newest first, then float the interesting ones up. Two stable sorts
    # rather than one composite key, because the two run in opposite
    # directions.
    ranked.sort(key=lambda pair: str(pair[1].get("published") or ""), reverse=True)
    ranked.sort(key=lambda pair: pair[0])
    for i, (_, a) in enumerate(ranked[:4]):
        out.append({
            "id": f"news-{i + 1}",
            "kind": "news",
            "when": None,
            "title": str(a.get("title") or "").strip(),
            "facts": [s for s in [str(a.get("summary") or "").strip()] if s],
            "symbols": [str(a.get("symbol")).upper()] if a.get("symbol") else [],
            "url": a.get("url"),
        })

    # 6. One synthetic candidate carrying the last session's tape, so an
    #    insight can say what the market did without a second lookup.
    facts = []
    for row in (gainers or [])[:2]:
        pct = fmt_pct(row.get("changePct"))
        if pct:
            facts.append(f"{row.get('symbol')} {pct}")
    for row in (losers or [])[:2]:
        pct = fmt_pct(row.get("changePct"))
        if pct:
            facts.append(f"{row.get('symbol')} {pct}")
    best, worst = _sector_extremes(sectors)
    if best:
        facts.append(f"best sector {best}")
    if worst:
        facts.append(f"weakest sector {worst}")
    if facts:
        out.append({
            "id": "market-1",
            "kind": "market",
            "when": None,
            "title": "The last session",
            "facts": facts,
            "symbols": [str(r.get("symbol")).upper()
                        for r in (gainers or [])[:2] + (losers or [])[:2]
                        if r.get("symbol")],
            "url": None,
        })

    return out[:MAX_CANDIDATES]


def _sector_extremes(sectors: dict) -> tuple[str | None, str | None]:
    rows = (sectors or {}).get("sectors") or []
    scored = []
    for s in rows:
        stats = s.get("stats") or {}
        val = stats.get("ret1y") or stats.get("ret") or stats.get("ytd")
        pct = fmt_pct(val)
        if pct:
            scored.append((float(val), f"{s.get('sector')} {pct}"))
    if not scored:
        return None, None
    scored.sort(key=lambda p: p[0], reverse=True)
    return scored[0][1], scored[-1][1]


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You write the morning market brief for Ticker Alpha, a stock dashboard read by
individual investors who are not finance professionals.

You will be given a JSON list of candidates already selected for you: whole
industries moving together, individual names making unusual moves, the names
generating the most news coverage, scheduled economic releases, companies
reporting, recent stories, and the last session's tape. Your job is to pick
the 3-5 a reader will actually want to know about this morning and explain
each in plain English.

Write it the way a sharp market columnist would: energetic, specific, and
concrete. The energy has to come from the facts -- the size of a move, how
long it has been running, a price crossed for the first time -- and never
from adjectives, exclamation marks, or telling the reader something is
exciting. A precise number is more thrilling than an intensifier, and it has
the advantage of being true.

RULES - these are absolute:

1. Use only the information in the candidates. Do not add events, companies,
   dates, or context from your own knowledge. If you are not certain from the
   input, leave it out.
2. Every number, percentage, ticker, and clock time you print must be copied
   character-for-character from a `facts` entry, the `when` field, or the
   title of the candidate you are citing. Never compute, convert, round, or
   estimate a figure. If a number is not there, write the sentence without it.
3. Every insight must cite exactly one candidate `id` in `source_id`, and no
   two insights may cite the same one.
4. Never give investment advice or make a directional prediction. Do not write
   that anything will rise, fall, rally, drop, beat, or miss. Do not use the
   words buy, sell, hold, bullish, bearish, overvalued, or undervalued, and do
   not name price targets. You describe what is scheduled and what it
   measures; the reader decides what to do about it.
5. Write for someone who does not know what CPI or an inverted curve is. No
   jargon without a plain-English gloss in the same sentence.
6. Describing a big move is not a prediction and is encouraged: say how far,
   over how long, and alongside what. Never suggest it continues, never say a
   name is cheap or expensive, and never tell the reader what to do. "Micron
   closed above $1,000 for the first time and is up 22% over a month" is
   right; "Micron is on a tear and there is more to come" is not.
7. Say why it moved when the candidate tells you. A `headline on ...` or
   `headline:` fact is the reason: use it, in your own words, in the same
   insight as the move. "Doximity jumped 32% after raising its full-year
   forecast" is worth reading; "Doximity jumped 32%" is half a sentence.
   When a candidate carries no headline you do not know the reason -- say
   what the move was and leave the cause out. Never reach for a plausible
   explanation, and never write "on no specific news", because a missing
   headline here is a gap in our data, not evidence that nothing happened.

STYLE (the character limits are enforced -- count them before you answer):
- headline: under 9 words and at most <HEADLINE_MAX> characters. Put the
  number in it where there is one -- "Memory names rip, Micron clears $1,000"
  beats "Semiconductor stocks rally". No colon-subtitle construction.
- body: exactly two sentences, <BODY_MIN>-<BODY_MAX> characters in total,
  no semicolons. Aim for about 40 words; <BODY_MAX> characters is the hard
  ceiling, not the target.
- Open on the hardest fact you have. Throat-clearing is banned: no
  "Investors will be watching", no "Markets are focused on".
- Name names. A theme insight should say which tickers moved and by how much,
  because that is what makes it a story rather than a statistic.
- Earn the interest with detail, not volume. No exclamation marks, and no
  "key", "crucial", "massive", "soaring" or "closely watched" -- if the move
  is big, the number already says so.
- Second sentence carries the "so what": what the move is tied to, what it
  says about demand, or what happens next on the calendar.

ORDERING: rank 1 is what a reader who follows the market is most likely to be
talking about this morning. That is usually a move, not a schedule entry: a
whole industry running together, a name clearing a price it has never closed
above, a run that has been building for weeks. Lead with those. A scheduled
release earns rank 1 only when it genuinely dominates the day, such as a Fed
decision or an inflation print. Give the calendar at most two of the slots --
a brief that is all scheduled events has buried the story.

symbols: tickers you name in the body, drawn only from that candidate's own
symbols list; an empty list if none.
event_time: copy the candidate's `when`, or null if it has none.\
"""
# Plain token replacement rather than %-formatting or .format(): this
# prompt is largely about percentages, and a literal "22% over a month"
# reads as a format spec to both of those.
for _token, _value in (("<HEADLINE_MAX>", HEADLINE_MAX),
                       ("<BODY_MIN>", BODY_MIN),
                       ("<BODY_MAX>", BODY_MAX)):
    SYSTEM_PROMPT = SYSTEM_PROMPT.replace(_token, str(_value))

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "insights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rank": {"type": "integer"},
                    "headline": {"type": "string"},
                    "body": {"type": "string"},
                    "kind": {"type": "string",
                             "enum": ["theme", "move", "buzz", "economics",
                                      "earnings", "news", "market"]},
                    "source_id": {"type": "string"},
                    "symbols": {"type": "array", "items": {"type": "string"}},
                    "event_time": {"type": ["string", "null"]},
                },
                "required": ["rank", "headline", "body", "kind", "source_id",
                             "symbols", "event_time"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["insights"],
    "additionalProperties": False,
}


def user_message(day: dt.date, candidates: list[dict], complaint: str = "") -> str:
    # The date line is the only temporal grounding the model gets, and it is
    # what stops "today" and "tomorrow" sliding by a day.
    parts = [
        f"Date: {long_day(day)} (US market open 09:30 ET)",
        "",
        "Candidates:",
        json.dumps(candidates, indent=1),
    ]
    if complaint:
        parts += ["", "Your previous attempt was rejected:", complaint,
                  "", "Write a new set that does not repeat that mistake."]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Validation -- any failure discards the whole run
# ---------------------------------------------------------------------------

class Rejected(Exception):
    pass


# Two families, and the second is the one that matters now the brief is
# allowed to be energetic. Describing a move that happened is the point;
# implying it carries on is a prediction wearing a description's clothes,
# and it is the sentence a reader would act on.
BANNED = [
    # advice
    "buy", "sell", "bullish", "bearish", "overvalued", "undervalued",
    "price target", "should invest", "worth a look", "one to watch",
    # prediction and continuation
    "we expect", "will rise", "will fall", "will beat", "will miss",
    "poised to", "set to surge", "outperform", "underperform",
    "more to come", "room to run", "further upside", "just getting started",
    "only the beginning", "next leg", "keep climbing", "keep running",
    "likely to continue", "should continue", "expect more", "no sign of",
    "shows no signs", "don't miss", "do not miss", "if the trend holds",
]

# Uppercase words that are not tickers. Short and explicit on purpose -- a
# name that slips through is a rejected run, which is the safe direction.
NOT_TICKERS = {
    "US", "USA", "UK", "EU", "GDP", "CPI", "PCE", "PPI", "PMI", "ISM", "FOMC",
    "EPS", "AI", "OPEC", "ET", "Q1", "Q2", "Q3", "Q4", "IPO", "CEO", "CFO",
    "FDA", "SEC", "ECB", "BOJ", "NYSE", "S&P", "ADP", "JOLTS", "NAHB", "TIC",
}

# A comma only belongs to a number when it separates thousands. Written as
# [\d,]* this swallowed the comma in "a previous 34, so it offers" and then
# looked for "34," in the facts, rejecting a run whose numbers were all real.
# Asking the brief to explain why a stock moved introduces a failure the
# other gates cannot see: a fabricated cause is ordinary prose with no
# invented number and no unknown ticker in it. "Eton rose 44% after reporting
# positive trial data" reads perfectly and may be entirely invented. So a
# causal claim is only allowed where the candidate actually carries a
# headline to support it.
_CAUSAL = re.compile(
    r"\b(after|following|because of|due to|on the back of|thanks to|"
    r"driven by|prompted by|sparked by|triggered by|in response to|"
    r"on news|on reports)\b", re.I)
# "after the close" is a session reference, not a cause.
_CAUSAL_OK = re.compile(
    r"\b(after|following)\s+(the\s+)?(close|open|bell|market|hours|session|"
    r"a\s+\d|\d)", re.I)

_NUM_TOKEN = re.compile(
    r"\d{1,2}:\d{2}"                         # clock times
    r"|\$?\d+(?:,\d{3})*(?:\.\d+)?[BMTK]?%?"  # 3.1  $223.45B  1,234  15.6%
)
_CAPS_TOKEN = re.compile(r"\b[A-Z][A-Z.\-]{1,5}\b")
_ABBREV = ("U.S.", "U.K.", "E.U.", "Inc.", "Corp.", "Co.", "Ltd.", "Jr.",
           "vs.", "approx.", "St.", "Dr.", "a.m.", "p.m.")


def _sentence_count(body: str) -> int:
    """Count sentence ends, ignoring the full stops that are not ones."""
    masked = body
    for a in _ABBREV:
        masked = masked.replace(a, "_" * len(a))
    # A decimal point sits between two digits; a sentence end does not.
    masked = re.sub(r"(?<=\d)\.(?=\d)", "_", masked)
    return len(re.findall(r"[.!?](?=\s|$)", masked.strip()))


def _numeric(tok: str) -> tuple[float, int] | None:
    """A token's value and how many decimals it was written to."""
    if ":" in tok:
        return None                        # a clock time, matched literally
    s = tok.replace(",", "").lstrip("$").rstrip("%").rstrip("BMTK")
    try:
        value = float(s)
    except ValueError:
        return None
    return value, len(s.split(".")[1]) if "." in s else 0


def _numbers_in(text: str) -> list[float]:
    out = []
    for tok in _NUM_TOKEN.findall(text):
        n = _numeric(tok)
        if n is not None:
            out.append(n[0])
    return out


def _number_is_sourced(tok: str, hay: str, hay_values: list[float]) -> bool:
    """Is this figure the candidate's, allowing for how prose renders it?

    The check is on the value, not the characters. A fact of "+6.80%" is
    written "6.8%" by anyone describing it, and "$1,024.50" and "$1024.50"
    are the same price -- rejecting those as fabrications cost two runs
    before this existed. An invented figure still has no source value to
    match, which is the thing worth catching.
    """
    if tok in hay:
        return True
    n = _numeric(tok)
    if n is None:
        return False
    value, decimals = n
    for h in hay_values:
        if abs(h - value) < 1e-9:
            return True
        # Quoting 22.4 as "22" is a fair simplification, not an invention.
        if round(h, decimals) == value:
            return True
    return False


def _haystack(cand: dict) -> str:
    bits = list(cand.get("facts") or [])
    if cand.get("when"):
        bits.append(str(cand["when"]))
    bits.append(str(cand.get("title") or ""))
    return " ".join(bits)


def validate(payload, candidates: list[dict]) -> list[dict]:
    """Return the accepted insights, or raise Rejected naming the gate."""
    by_id = {c["id"]: c for c in candidates}

    # 1. Shape.
    if not isinstance(payload, dict):
        raise Rejected("shape: response was not a JSON object")
    rows = payload.get("insights")
    if not isinstance(rows, list) or not (MIN_INSIGHTS <= len(rows) <= MAX_INSIGHTS):
        raise Rejected(f"shape: expected {MIN_INSIGHTS}-{MAX_INSIGHTS} insights, "
                       f"got {len(rows) if isinstance(rows, list) else 'none'}")
    for r in rows:
        for field in ("rank", "headline", "body", "kind", "source_id"):
            if not isinstance(r, dict) or r.get(field) in (None, ""):
                raise Rejected(f"shape: an insight is missing {field}")
    ranks = sorted(int(r["rank"]) for r in rows)
    if ranks != list(range(1, len(rows) + 1)):
        raise Rejected(f"shape: ranks must be 1..{len(rows)}, got {ranks}")

    # 2. Source integrity -- the claim has to trace to something we chose.
    used: set[str] = set()
    for r in rows:
        sid = r["source_id"]
        if sid not in by_id:
            raise Rejected(f"source: '{sid}' is not one of the candidate ids")
        if sid in used:
            raise Rejected(f"source: '{sid}' was cited twice")
        used.add(sid)

    for r in rows:
        cand = by_id[r["source_id"]]
        text = f"{r['headline']} {r['body']}"
        hay = _haystack(cand)

        # 3. Number provenance. Bare years pass; every other figure has to
        #    come from the candidate we handed over.
        hay_values = _numbers_in(hay)
        for tok in _NUM_TOKEN.findall(text):
            if re.fullmatch(r"(19|20)\d{2}", tok):
                continue
            if _number_is_sourced(tok, hay, hay_values):
                continue
            raise Rejected(f"number: '{tok}' in {r['source_id']} is not in "
                           f"that candidate's facts")

        # 4. Symbol integrity.
        allowed = {str(s).upper() for s in (cand.get("symbols") or [])}
        for s in (r.get("symbols") or []):
            if str(s).upper() not in allowed:
                raise Rejected(f"symbol: '{s}' is not one of "
                               f"{r['source_id']}'s symbols")
        for tok in _CAPS_TOKEN.findall(r["body"]):
            if tok in NOT_TICKERS or tok.upper() in allowed:
                continue
            # Headlines carry tickers of their own, and quoting one back is
            # sourced -- the haystack is the candidate we handed over.
            if tok in hay:
                continue
            raise Rejected(f"symbol: '{tok}' in {r['source_id']} is not a "
                           f"ticker from that candidate")

        # 5. Advice language.
        low = text.lower()
        for phrase in BANNED:
            pattern = (r"\b" + re.escape(phrase) + r"\b" if " " not in phrase
                       else re.escape(phrase))
            if re.search(pattern, low):
                raise Rejected(f"advice: '{phrase}' appears in {r['source_id']}")

        # 6. A cause needs a source. Where the candidate carries no headline
        #    we do not know why the move happened, and a plausible-sounding
        #    reason is the one kind of fabrication the other gates cannot
        #    see -- it contains no invented number and no unknown ticker.
        has_headline = any(str(f).lower().startswith("headline")
                           for f in (cand.get("facts") or []))
        if not has_headline and cand["kind"] != "news":
            claim = _CAUSAL.search(r["body"])
            if claim and not _CAUSAL_OK.search(r["body"]):
                raise Rejected(
                    f"cause: {r['source_id']} carries no headline, so "
                    f"'{claim.group(0)}' in the body attributes a reason we "
                    f"have no source for")

        # 7. Length.
        if len(r["headline"]) > HEADLINE_MAX:
            raise Rejected(f"length: headline is {len(r['headline'])} chars in "
                           f"{r['source_id']} (max {HEADLINE_MAX})")
        if not (BODY_MIN <= len(r["body"]) <= BODY_MAX):
            raise Rejected(f"length: body is {len(r['body'])} chars in "
                           f"{r['source_id']} (want {BODY_MIN}-{BODY_MAX})")
        if _sentence_count(r["body"]) != 2:
            raise Rejected(f"length: body is not two sentences in "
                           f"{r['source_id']}")

    # 8. Liveliness. The first version of this brief read like the calendar
    #    in prose, because the calendar was most of what it was given and the
    #    ranking rule put it on top. Now that moves are offered as candidates,
    #    a brief that ignores every one of them on a day when the market
    #    actually moved has buried the story -- which is a ranking failure,
    #    not a matter of taste.
    live = {c["id"] for c in candidates if c["kind"] in ("theme", "move", "buzz")}
    if live and not any(r["source_id"] in live for r in rows):
        raise Rejected("liveliness: the market moved today but every insight "
                       "cites the calendar or the news")

    return rows


# ---------------------------------------------------------------------------
# The model call
# ---------------------------------------------------------------------------

def call_model(day: dt.date, candidates: list[dict], model: str,
               complaint: str = "") -> tuple[dict, dict]:
    try:
        import anthropic
    except ImportError:
        raise SystemExit("the anthropic package is required: pip install anthropic")

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": RESULT_SCHEMA},
        },
        messages=[{"role": "user",
                   "content": user_message(day, candidates, complaint)}],
    )

    if response.stop_reason == "refusal":
        raise Refused(getattr(response.stop_details, "category", None))

    text = next((b.text for b in response.content if b.type == "text"), "")
    usage = {
        "model": response.model,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    try:
        return json.loads(text), usage
    except json.JSONDecodeError as exc:
        raise Rejected(f"shape: response was not valid JSON ({exc})")


class Refused(Exception):
    pass


def generate(day: dt.date, candidates: list[dict]) -> tuple[list[dict], dict]:
    """One call, one retry on a rejected result, and a model fallback."""
    models = [MODEL] + ([FALLBACK_MODEL] if FALLBACK_MODEL else [])
    complaint = ""
    last: Rejected | None = None

    for model in models:
        # Two calls is the hard ceiling per model: one attempt, one retry
        # carrying the validator's complaint back to the model.
        for attempt in range(2):
            try:
                payload, usage = call_model(day, candidates, model, complaint)
            except Refused as exc:
                print(f"[insights] {model} declined the request ({exc})")
                break                      # a retry would decline the same way
            try:
                rows = validate(payload, candidates)
            except Rejected as exc:
                last = exc
                complaint = str(exc)
                print(f"[insights] rejected (attempt {attempt + 1}, {model}): {exc}")
                print(f"[insights] rejected text: "
                      f"{json.dumps(payload)[:1200]}")
                continue
            return rows, usage

    raise SystemExit(f"insights: no acceptable result "
                     f"({last if last else 'the model declined'})")


# ---------------------------------------------------------------------------
# The quiet-day path
# ---------------------------------------------------------------------------

def quiet_day_rows(day: dt.date) -> list[dict]:
    """No candidates at all: say so honestly, with no API call."""
    return [{
        "rank": 1,
        "headline": "No scheduled US releases today",
        "body": ("The economic calendar is empty and no large company is due "
                 "to report. The market takes its direction from news and "
                 "from what happened in the last session."),
        "kind": "market",
        "symbols": [],
        "event_time": None,
        "source": "empty calendar",
        "source_url": None,
        "model": "none",
    }]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def to_rows(insights: list[dict], candidates: list[dict], model: str) -> list[dict]:
    by_id = {c["id"]: c for c in candidates}
    rows = []
    for r in sorted(insights, key=lambda x: int(x["rank"])):
        cand = by_id[r["source_id"]]
        rows.append({
            "rank": int(r["rank"]),
            "headline": r["headline"],
            "body": r["body"],
            "kind": r.get("kind") or cand["kind"],
            "symbols": [str(s).upper() for s in (r.get("symbols") or [])],
            "event_time": r.get("event_time") or None,
            "source": cand.get("title"),
            "source_url": cand.get("url"),
            "model": model,
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the candidates and the result, write nothing")
    ap.add_argument("--day", help="YYYY-MM-DD; defaults to today in Eastern")
    args = ap.parse_args()

    day = (dt.date.fromisoformat(args.day) if args.day
           else _eastern_now().date())

    if not is_trading_day(day):
        print(f"[insights] {day} is not a US trading day; nothing to write.")
        return 0

    brief = read_rpc("get_market_brief", {"p_day": day.isoformat()}) or {}
    caps = read_rpc("get_market_summary",
                    {"p_view": "market_cap", "p_limit": 10}) or []
    gainers = read_rpc("get_market_summary",
                       {"p_view": "gainers", "p_limit": 5}) or []
    losers = read_rpc("get_market_summary",
                      {"p_view": "losers", "p_limit": 5}) or []
    news = read_rpc("get_news_feed", {"p_hours": 48, "p_limit": 60}) or []
    try:
        sectors = read_rpc("get_sector_risk") or {}
    except SystemExit:
        sectors = {}                       # optional context, never fatal
    try:
        industries = read_rpc("get_industry_heatmap") or []
    except SystemExit:
        industries = []

    # The names worth a second lookup: whatever is in a theme, plus the day's
    # biggest movers. One get_prices call each buys the run behind the move --
    # the difference between "jumped today" and "has been climbing for a
    # month", which is usually the more interesting half of the story.
    themes = find_themes(industries)[:3]
    shortlist: list[str] = []
    for t in themes:
        shortlist += [str(m.get("symbol")) for m in t["members"] if m.get("symbol")]
    shortlist += [str(r.get("symbol")) for r in (gainers or [])[:4] if r.get("symbol")]
    shortlist += [str(r.get("symbol")) for r in (losers or [])[:2] if r.get("symbol")]
    seen: set[str] = set()
    shortlist = [s for s in shortlist
                 if s and not (s in seen or seen.add(s))][:10]
    stories = fetch_price_stories(shortlist)

    by_symbol_counts = {s: len(v) for s, v in news_index(news).items()}
    candidates = build_candidates(brief, caps, gainers, losers, news, sectors,
                                  industries, stories)
    kinds: dict[str, int] = {}
    for c in candidates:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    print(f"[insights] {day}: {len(candidates)} candidates "
          f"({', '.join(f'{v} {k}' for k, v in kinds.items())}) "
          f"from {len(industries)} industries, {len(news)} stories; "
          f"price history for {len(stories)}/{len(shortlist)} names")
    for t in themes:
        print(f"[insights] theme: {t['industry']} {fmt_pct(t['weighted'])} "
              f"with {t['n']} movers")
    # When a theme everyone is discussing does not appear, the question is
    # which threshold it missed. Show the near misses rather than leaving it
    # to guesswork.
    if industries:
        ranked = sorted(industries,
                        key=lambda i: abs(i.get("changePct") or 0), reverse=True)
        chosen = {t["industry"] for t in themes}
        print("[insights] biggest industry moves:")
        for ind in ranked[:6]:
            movers = [i for i in (ind.get("items") or [])
                      if i.get("changePct") is not None
                      and abs(i["changePct"]) >= THEME_MEMBER_MOVE]
            why = ("used" if ind.get("industry") in chosen else
                   "cap too small" if (ind.get("marketCap") or 0) < THEME_MIN_CAP
                   else f"only {len(movers)} movers"
                   if len(movers) < THEME_MIN_MEMBERS
                   else "move too small")
            print(f"    {str(ind.get('industry'))[:38]:<38} "
                  f"{fmt_pct(ind.get('changePct')) or '   —':>8}  "
                  f"cap {fmt_cap(ind.get('marketCap')) or '—':>9}  "
                  f"{len(movers)} movers  [{why}]")
    if by_symbol_counts:
        top = sorted(by_symbol_counts.items(), key=lambda p: p[1], reverse=True)
        print("[insights] most covered: " +
              ", ".join(f"{s} ({n})" for s, n in top[:6]))

    if args.dry_run:
        print(json.dumps(candidates, indent=2))

    if not candidates:
        rows = quiet_day_rows(day)
        usage = {}
    else:
        insights, usage = generate(day, candidates)
        rows = to_rows(insights, candidates, usage.get("model") or MODEL)

    print(json.dumps(rows, indent=2))
    if usage:
        print(f"[insights] {usage['model']}: {usage['input_tokens']} in, "
              f"{usage['output_tokens']} out")

    if args.dry_run:
        print("[insights] dry run; nothing written.")
        return 0

    written = store.rpc("replace_market_insights",
                        {"p_day": day.isoformat(), "p_rows": rows})
    print(f"[insights] wrote {written} insights for {day}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
