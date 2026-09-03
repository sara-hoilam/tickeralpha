"""
insights.py -- writes the market page's "Today's Brief" insights.

Run each morning by .github/workflows/insights.yml, before the US open, on
every day of the week -- on a weekend the prices are the last session's and
every fact says so. Reads through the same public RPCs a visitor's browser calls, works
out what actually moved -- industries running together, names clearing
prices they have never closed above, runs weeks in the making -- alongside
the day's schedule and news, asks Claude to pick the three to five worth
knowing about, and writes the result to ledger.market_insight (0065).

Attention leads on purpose. The first version narrated the calendar; the
second led with price change, which is structurally a microcap leaderboard.
This one ranks by attention times reach -- how many stories a subject drew,
sized by how much market value it touches -- with price action attached as
evidence. The scored table in the run log is the ranking, and "why didn't X
appear" is answerable from it.

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
# The scored shortlist sent to the model. Everything eligible is scored and
# the top of the table goes over; anything below the cut appears in the run
# log with the reason it scored where it did.
MAX_CANDIDATES = 12
# Calls per model before the run falls back to salvaging what came back.
ATTEMPTS = int(os.environ.get("INSIGHTS_ATTEMPTS", "3"))

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


def last_session(day: dt.date) -> dt.date:
    """The most recent trading day on or before `day`."""
    d = day
    for _ in range(10):
        if is_trading_day(d):
            return d
        d -= dt.timedelta(days=1)
    return d


def session_phrase(day: dt.date) -> str:
    """How to date a price move in the facts.

    On a trading day the quotes are today's. On a weekend or a holiday they
    are the last session's close, and calling that "today" would be a plain
    untruth in every fact built from it -- so the facts name the session,
    and the brief written from them inherits the wording.
    """
    s = last_session(day)
    return "today" if s == day else f"on {s:%A}"


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

# The releases FMP reports in thousands. Its economic calendar gives initial
# claims as "206", meaning 206,000 -- so a brief that quotes the fact verbatim
# says claims "landed at 206", which is not a number anyone recognises, and a
# brief that writes what it means says 206,000 and is rejected for quoting a
# figure that is not in the facts. That cost a whole run: the model asked for
# the sensible wording twice and the validator refused it twice. The unit
# belongs on the fact, so both problems go away at the source.
#
# The list is deliberately short: a release it guesses wrong about is out by a
# factor of a thousand, which is far worse than one that reads awkwardly, so
# anything ambiguous -- JOLTS openings, housing starts, reported in millions
# by some feeds and thousands by others -- is left as the feed sends it.
_THOUSANDS_EVENT = re.compile(
    r"jobless claims|initial claims|continuing claims"
    r"|nonfarm payroll|non-farm payroll|private payroll"
    r"|adp employment|challenger job cuts", re.I)


def econ_scale(event: str, value) -> float | None:
    """The release's value in the unit a reader would recognise.

    Only the count releases are scaled, and only when the figure still looks
    like a count in thousands: if the feed ever starts sending 206000 for the
    same event, the magnitude guard leaves it alone rather than turning it
    into 206 million.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if _THOUSANDS_EVENT.search(event or "") and abs(f) < 100_000:
        return f * 1000
    return f


def fmt_count(v) -> str | None:
    """A whole count with thousands separators: 206000 -> "206,000"."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f == int(f):
        return f"{int(f):,}"
    return f"{f:,.2f}".rstrip("0").rstrip(".")


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

def _age_hours(published) -> float | None:
    """Hours since a story's timestamp; None when it cannot be read."""
    s = str(published or "").strip()
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 3600


def curation_score(kind: str, cap: float | None, story_count: int,
                   newest_age_h: float | None, has_evidence: bool,
                   resolves_today: bool) -> tuple[float, str]:
    """The deterministic rank -- attention times reach, evidence attached.

    reach      how much market value the item touches, on a log scale;
               a macro topic or economic release reaches everything
    attention  distinct stories citing it over two days
    freshness  the newest supporting story is hours old, not yesterday's
    evidence   price action corroborates the story
    schedule   it resolves today

    The weights are set so a widely-covered macro story beats a megacap
    story, which beats a sector theme, which beats any single mover --
    unless the lower tier carries overwhelming attention. Percentage change
    is deliberately absent: it is evidence, never the ranking signal.
    """
    import math
    if kind in ("topic", "economics", "market"):
        reach = 10.0
    elif cap and cap > 0:
        reach = min(10.0, max(0.0, math.log10(cap) - 2))
    else:
        reach = 4.0
    attention = min(story_count, 6) * 1.5
    fresh = 2.0 if (newest_age_h is not None and newest_age_h < 12) else \
            1.0 if (newest_age_h is not None and newest_age_h < 24) else 0.0
    evidence = 2.0 if has_evidence else 0.0
    schedule = 2.0 if resolves_today else 0.0
    total = reach + attention + fresh + evidence + schedule
    parts = (f"reach {reach:.1f} + attention {attention:.1f} "
             f"+ fresh {fresh:.0f} + evidence {evidence:.0f} "
             f"+ schedule {schedule:.0f}")
    return round(total, 1), parts


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

# The editorial test, as thresholds. A company story earns a slot through
# size or breadth of coverage, never through percentage: $100B of market cap
# is a name readers know, and three distinct stories in two days is a
# conversation. A price move alone is not a candidate -- every day has a
# biggest loser, and its existence is not news.
COMPANY_STORY_CAP = 1e11
COMPANY_STORY_MIN = 3
BUZZ_MIN_STORIES = 3
BUZZ_MEGACAP_MIN = 2      # a megacap qualifies on lighter coverage
TOPIC_MIN_STORIES = 3
# Subjects that draw heavy coverage every single day of the year. Volume on
# an evergreen is background noise, not a story -- "Earnings" tops the count
# table every morning of earnings season and says nothing. The index names
# are the same: coverage of "S&P 500" is coverage of everything.
EVERGREEN_TOPICS = {"S&P 500", "Nasdaq", "Dow Jones", "Earnings", "Guidance",
                    "Dividends", "Buybacks", "Upgrades", "IPO", "M&A"}

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


# Words too generic to define a story cluster on their own.
_CLUSTER_STOP = {
    "stock", "stocks", "share", "shares", "market", "markets", "wall",
    "street", "price", "prices", "report", "reports", "says", "said",
    "week", "today", "year", "years", "billion", "million", "company",
    "companies", "investor", "investors", "trading", "trade", "close",
    "closes", "higher", "lower", "rise", "rises", "fall", "falls",
    "jump", "jumps", "gain", "gains", "drop", "drops", "amid", "after",
    "could", "would", "will", "what", "here", "this", "that", "with",
    "from", "over", "into", "more", "most", "their", "about", "them",
}


def find_news_clusters(news: list, taken_queries: set[str]) -> list[dict]:
    """Untagged stories converging on the same subject.

    The curated topic list catches the recurring conversations -- the Fed,
    tariffs, oil -- but a breaking story arrives under words no list
    anticipated: a strait, a country, a name. When three or more distinct
    untagged stories share an uncommon word inside two days, that word IS
    the day's story, whatever it is. This is how geopolitics that nobody
    scheduled gets into the brief.
    """
    untagged, seen = [], set()
    for a in news or []:
        if str(a.get("symbol") or ""):
            continue
        key = news_key(a)
        if key in seen:
            continue
        seen.add(key)
        untagged.append(a)

    by_word: dict[str, list[dict]] = {}
    for a in untagged:
        title = str(a.get("title") or "").lower()
        toks = {w for w in re.sub(r"[^a-z0-9 ]+", " ", title).split()
                if len(w) >= 4 and w not in _CLUSTER_STOP}
        for w in toks:
            by_word.setdefault(w, []).append(a)

    clusters = []
    used_urls: set[str] = set()
    for word, rows in sorted(by_word.items(),
                             key=lambda p: len(p[1]), reverse=True):
        if len(rows) < 3 or word in taken_queries:
            continue
        # A cluster's stories belong to one candidate; a second word carried
        # by the same articles is the same story wearing a synonym.
        fresh_rows = [a for a in rows if a.get("url") not in used_urls]
        if len(fresh_rows) < 3:
            continue
        used_urls.update(a.get("url") for a in fresh_rows)
        fresh_rows.sort(key=lambda a: str(a.get("published") or ""),
                        reverse=True)
        clusters.append((word, fresh_rows))
        if len(clusters) >= 2:
            break

    out = []
    for i, (word, rows) in enumerate(clusters):
        facts = [f"several separate stories in the last two days converge "
                 f"on '{word}'"]
        facts += [f"headline: {str(a.get('title') or '').strip()}"
                  for a in rows[:4] if a.get("title")]
        score, parts = curation_score(
            "topic", None, len(rows),
            _age_hours(rows[0].get("published")), False, False)
        out.append({
            "id": f"cluster-{i + 1}",
            "kind": "topic",
            "when": None,
            "title": f"The {word} story",
            "facts": facts,
            "symbols": [],
            "url": rows[0].get("url"),
            "score": score, "score_parts": parts,
        })
    return out


def build_topic_candidates(topics: list, news: list, brief: dict,
                           moves_by_symbol: dict,
                           session: str = "today") -> list[dict]:
    """The macro conversation: curated subjects with spiking story counts.

    This is how politics, policy and geopolitics enter the brief -- a tariff
    threat, a Fed split, an oil-supply scare arrive as stories matching a
    topic query, not as anything a price screen would surface. Each candidate
    carries its matching headlines, the same-day release on the same subject
    when there is one, and the moves of any names those stories tag, so the
    insight can say what happened, what it touches, and what the tape did
    about it.
    """
    out = []
    # The RPC's count is against the whole stored corpus -- a ranking hint,
    # not a fact about the last two days. Attention here is the number of
    # matching stories in the 48-hour feed, and it lives in the score, not
    # in the facts: coverage weight is the curator's business, and a brief
    # that quotes it is talking about its own plumbing.
    scored_topics = []
    for t in topics or []:
        word = str(t.get("word") or "").strip()
        if not word or word in EVERGREEN_TOPICS:
            continue
        query = str(t.get("query") or word).lower()
        # Word-boundary matching, not substring: "war" must not count
        # "software" or "warns". Short queries take only a plural; longer
        # ones ("geopolit", "tariff") extend to their derived forms.
        if len(query) >= 5:
            pattern = re.compile(r"\b" + re.escape(query) + r"\w*", re.I)
        else:
            pattern = re.compile(r"\b" + re.escape(query) + r"s?\b", re.I)
        matching = [a for a in news or []
                    if pattern.search(str(a.get("title") or "") + " "
                                      + str(a.get("summary") or ""))]
        if len(matching) < TOPIC_MIN_STORIES:
            continue
        matching.sort(key=lambda a: str(a.get("published") or ""), reverse=True)
        scored_topics.append((word, query, matching))
    scored_topics.sort(key=lambda p: len(p[2]), reverse=True)

    for i, (word, query, matching) in enumerate(scored_topics[:4]):
        facts = [f"{word} is one of the most-covered subjects in the news "
                 f"right now"]
        syms: list[str] = []
        for a in matching[:3]:
            title = str(a.get("title") or "").strip()
            if title:
                facts.append(f"headline: {title}")
            sym = str(a.get("symbol") or "").upper()
            if sym and sym not in syms:
                syms.append(sym)
        # The tape's verdict on the story, where the stories name names.
        evidence = False
        for sym in syms[:3]:
            move = moves_by_symbol.get(sym) or {}
            if move.get("changePct") is not None:
                facts.append(f"{sym} {fmt_pct(move['changePct'])} {session}")
                evidence = True
        # A release on the same subject landing today turns talk into a time.
        resolves = False
        for e in brief.get("economics") or []:
            event = str(e.get("event") or "").lower()
            if query in event or any(w in event for w in query.split()):
                when = e.get("time") or ""
                facts.append(f"related release today: {e.get('event')}"
                             + (f" at {when}" if when else ""))
                resolves = True
                break
        score, parts = curation_score(
            "topic", None, len(matching),
            _age_hours(matching[0].get("published")) if matching else None,
            evidence, resolves)
        out.append({
            "id": f"topic-{i + 1}",
            "kind": "topic",
            "when": None,
            "title": f"The {word} story" if not word[0].isupper() else word,
            "facts": facts,
            "symbols": syms[:4],
            "url": (matching[0] or {}).get("url") if matching else None,
            "score": score, "score_parts": parts,
        })
    return out


# ---------------------------------------------------------------------------
# Candidate selection -- deterministic, in Python
# ---------------------------------------------------------------------------

def build_candidates(brief: dict, caps: list, gainers: list, losers: list,
                     news: list, sectors: dict,
                     industries: list | None = None,
                     stories: dict | None = None,
                     topics: list | None = None,
                     session: str = "today") -> list[dict]:
    """Everything eligible, scored, best first.

    The order the model receives is the curator's ranking: attention times
    reach, with price action as attached evidence. Eligibility is enforced
    here -- a move alone is not a candidate -- and the score is computed
    here, so what leads the brief is decided by rules that can be read,
    logged and argued with, not by whatever the model found salient.
    """
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

    # The macro conversation first -- curated topics with spiking story
    # counts, then breaking subjects no list anticipated.
    topic_cands = build_topic_candidates(topics or [], news, brief,
                                         moves_by_symbol, session)
    out += topic_cands
    taken = {str(t.get("query") or t.get("word") or "").lower()
             for t in (topics or [])}
    out += find_news_clusters(news, taken)

    # Themes -- a whole corner of the market moving together: the "memory
    # stocks are ripping" case. Breadth is what qualifies it; the headlines
    # attached are what let the brief say why.
    for i, t in enumerate(find_themes(industries or [])[:3]):
        facts = [f"{t['industry']} {fmt_pct(t['weighted'])} {session}",
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
        headlines = headline_facts(syms, by_sym, limit=3)
        facts += headlines
        newest = min((a for s in syms for a in by_sym.get(s, [])[:1]),
                     key=lambda a: _age_hours(a.get("published")) or 999,
                     default=None)
        score, parts = curation_score(
            "theme", sum(m.get("marketCap") or 0 for m in t["members"]) or None,
            sum(len(by_sym.get(s, [])) for s in syms),
            _age_hours(newest.get("published")) if newest else None,
            True, False)
        out.append({
            "id": f"theme-{i + 1}",
            "kind": "theme",
            "when": None,
            "title": f"{t['industry']} moving together",
            "facts": facts,
            "symbols": syms,
            "url": next((by_sym[s][0].get("url") for s in syms
                         if by_sym.get(s)), None),
            "score": score, "score_parts": parts,
        })

    # Single names -- but a move alone is not a candidate. A mover earns a
    # slot through size or coverage, because every day has a biggest loser
    # and its existence is not news. One slot stays for the outright biggest
    # move on the screen, clearly labeled, so a genuine oddity can still be
    # mentioned; its score keeps it at the bottom of the table.
    seen_syms = {s for c in out for s in c["symbols"]}
    pool = []
    for row in (gainers or []) + (losers or []):
        sym = str(row.get("symbol") or "")
        if not sym or sym in seen_syms:
            continue
        seen_syms.add(sym)
        pool.append(row)
    pool.sort(key=lambda r: abs(r.get("changePct") or 0), reverse=True)
    qualified = [r for r in pool
                 if (r.get("marketCap") or 0) >= COMPANY_STORY_CAP
                 or len(by_sym.get(str(r.get("symbol")), [])) >= COMPANY_STORY_MIN]
    standouts = qualified[:2]
    raw = next((r for r in pool if r not in standouts), None)
    if raw is not None:
        standouts = standouts + [raw]

    for i, row in enumerate(standouts):
        sym = str(row.get("symbol"))
        n_stories = len(by_sym.get(sym, []))
        facts = [f"{sym} {fmt_pct(row.get('changePct'))} {session}"]
        price = fmt_money(row.get("price"))
        if price:
            facts.append(f"trading at {price}")
        cap_s = fmt_cap(row.get("marketCap"))
        if cap_s:
            facts.append(f"market cap {cap_s}")
        facts += list(stories.get(sym, {}).values())
        facts += headline_facts([sym], by_sym, limit=2)
        if row is raw and not n_stories:
            facts.append("largest single move on the screen; no headline "
                         "in our data explains it")
        newest = by_sym.get(sym, [None])[0]
        score, parts = curation_score(
            "move", row.get("marketCap"), n_stories,
            _age_hours(newest.get("published")) if newest else None,
            True, False)
        out.append({
            "id": f"move-{i + 1}",
            "kind": "move",
            "when": None,
            "title": f"{row.get('name') or sym} ({sym})",
            "facts": facts,
            "symbols": [sym],
            "url": (by_sym.get(sym, [{}])[0] or {}).get("url"),
            "score": score, "score_parts": parts,
        })

    # What is being discussed, name by name. Story count is the closest
    # thing in the database to the conversation on social and in the media,
    # and it finds what no price screen can: a megacap up 1% with five
    # stories against it. A megacap qualifies on lighter coverage because
    # two stories about $3T reach more readers than four about $3B.
    quoted = {s for c in out for s in c["symbols"]}
    buzz = sorted(((sym, rows) for sym, rows in by_sym.items()
                   if sym not in quoted
                   and (len(rows) >= BUZZ_MIN_STORIES
                        or (len(rows) >= BUZZ_MEGACAP_MIN
                            and (moves_by_symbol.get(sym, {}).get("marketCap")
                                 or 0) >= COMPANY_STORY_CAP))),
                  key=lambda pair: len(pair[1]), reverse=True)
    for i, (sym, rows) in enumerate(buzz[:2]):
        facts = [f"{sym} is drawing repeated coverage in the last two days"]
        move = moves_by_symbol.get(sym) or {}
        if move.get("changePct") is not None:
            facts.append(f"{sym} {fmt_pct(move['changePct'])} {session}")
        price = fmt_money(move.get("price"))
        if price:
            facts.append(f"trading at {price}")
        facts += list(stories.get(sym, {}).values())
        facts += [f"headline: {str(a.get('title') or '').strip()}"
                  for a in rows[:3] if a.get("title")]
        score, parts = curation_score(
            "buzz", move.get("marketCap"), len(rows),
            _age_hours(rows[0].get("published")),
            move.get("changePct") is not None, False)
        out.append({
            "id": f"buzz-{i + 1}",
            "kind": "buzz",
            "when": None,
            "title": f"{sym} in the news",
            "facts": facts,
            "symbols": [sym],
            "url": (rows[0] or {}).get("url"),
            "score": score, "score_parts": parts,
        })

    # Scheduled economics -- high impact only. A medium-impact survey never
    # leads the brief again; the full calendar lives in the panel beside it.
    econ = [e for e in (brief.get("economics") or [])
            if str(e.get("impact") or "").lower() == "high"]
    econ.sort(key=lambda e: (e.get("time") or "99:99", e.get("event") or ""))
    for i, e in enumerate(econ[:3]):
        # Spelled out rather than "est." / "prev.": these strings are copied
        # into prose, and an abbreviation's full stop reads as a sentence end
        # to both a reader and the two-sentence check.
        facts = []
        event_name = str(e.get("event") or "")
        for label, value in (("estimate", e.get("estimate")),
                             ("previous", e.get("previous")),
                             ("actual", e.get("actual"))):
            scaled = econ_scale(event_name, value)
            n = fmt_count(scaled) if scaled is not None else None
            if n is not None:
                facts.append(f"{label} {n}")
        impact = str(e.get("impact") or "").lower()
        if impact:
            facts.append(f"impact: {impact}")
        score, parts = curation_score("economics", None, 0, None,
                                      e.get("actual") is not None, True)
        out.append({
            "id": f"econ-{i + 1}",
            "kind": "economics",
            "when": e.get("time") or None,
            "title": e.get("event") or "Economic release",
            "facts": facts,
            "symbols": [],
            "url": None,
            "score": score, "score_parts": parts,
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
        bell = {"bmo": "before the open", "amc": "after the close"}.get(
            str(e.get("time") or "").lower())
        if bell:
            facts.append(f"reports {bell}")
        esym = str(e.get("symbol") or "").upper()
        score, parts = curation_score(
            "earnings", e.get("marketCap"), len(by_sym.get(esym, [])),
            None, False, True)
        out.append({
            "id": f"earn-{i + 1}",
            "kind": "earnings",
            "when": None,
            "title": f"{e.get('name') or e.get('symbol')} ({e.get('symbol')})",
            "facts": facts,
            "symbols": [esym] if esym else [],
            "url": None,
            "score": score, "score_parts": parts,
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
        nsym = str(a.get("symbol") or "").upper()
        score, parts = curation_score(
            "news", (moves_by_symbol.get(nsym, {}).get("marketCap")
                     if nsym else None), 1,
            _age_hours(a.get("published")), False, False)
        out.append({
            "id": f"news-{i + 1}",
            "kind": "news",
            "when": None,
            "title": str(a.get("title") or "").strip(),
            "facts": [s for s in [str(a.get("summary") or "").strip()] if s],
            "symbols": [nsym] if nsym else [],
            "url": a.get("url"),
            "score": score, "score_parts": parts,
        })

    # 6. One synthetic candidate carrying the last session's tape, so an
    #    insight can say what the market did without a second lookup.
    # Only names a reader would recognise: the tape's job is context, and
    # "ETON +44.26%" is a screener line, not context.
    facts = []
    tape_syms = []
    for row in (gainers or []) + (losers or []):
        if (row.get("marketCap") or 0) < COMPANY_STORY_CAP:
            continue
        pct = fmt_pct(row.get("changePct"))
        if pct and len(tape_syms) < 4:
            facts.append(f"{row.get('symbol')} {pct}")
            tape_syms.append(str(row.get("symbol")).upper())
    best, worst = _sector_extremes(sectors)
    if best:
        facts.append(f"best sector {best}")
    if worst:
        facts.append(f"weakest sector {worst}")
    if facts:
        # Context, not a story: whole-market reach but no coverage, no
        # resolution, so it settles into the middle of the table and never
        # leads unless the day is genuinely empty.
        out.append({
            "id": "market-1",
            "kind": "market",
            "when": None,
            "title": "The last session",
            "facts": facts,
            "symbols": tape_syms,
            "url": None,
            "score": 10.0, "score_parts": "reach 10.0 (context only)",
        })

    # The table the model receives IS the ranking: best first, ties broken
    # stably by build order so reruns are deterministic.
    out.sort(key=lambda c: -(c.get("score") or 0))
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
You choose what leads the morning brief for Ticker Alpha the way a
front-page editor would: the story the most readers are already talking
about, sized by how much of the market it can move. Your readers are
individual investors who are not finance professionals.

You will be given a JSON list of candidates already selected and scored for
you: macro topics with spiking coverage, whole industries moving together,
the most-covered names, big single-name moves, scheduled releases, companies
reporting, recent stories, and the last session's tape. Pick the 3-5 a
reader will actually bring up in conversation this morning and explain each
in plain English.

Write it the way a sharp market columnist would: energetic, specific, and
concrete. The energy has to come from the facts -- the size of a move, how
long it has been running, a price crossed for the first time -- and never
from adjectives, exclamation marks, or telling the reader something is
exciting. A precise number is more thrilling than an intensifier, and it
has the advantage of being true.

THE SHAPE OF AN INSIGHT -- story first, market second:
- The headline states the story itself, the way a newspaper would: what
  happened or what is at stake. Never the coverage ("X draws 400 stories"),
  never the ranking, never our screen.
- First sentence: the event, told from the supplied headlines -- who did
  what, what changed, what is at stake.
- Second sentence: what the market did or watches because of it, with the
  numbers -- the moves, the levels, the milestones, the time it resolves.
- Coverage weight is the curator's business. It decided the order you
  received; it never appears in your prose, as a number or otherwise. If
  breadth of discussion is itself the point, say it plainly ("dominating
  the news flow") -- one qualitative phrase, no figures.

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

ORDERING: candidates arrive sorted by a curation score -- reach times
attention, with price action as attached evidence -- and that order is the
default. Rank 1 is the story with the widest reach and the heaviest
coverage: macro and policy first when they are live, then big-company
events, then industry themes. A single stock's move leads only when it
carries broad coverage or sits inside a larger story you can name from the
facts. Demoting a top-scored candidate needs a reason visible in the facts
of the one you promote. Give the calendar at most two slots, and at most
one insight may be a bare single-stock move.

Tie every insight to why it matters beyond the names in it -- what it does
to rates, to a sector, to the index, or to what people pay -- using only
the supplied facts.

EXAMPLES of the judgement being asked for:

Good lead: "Hopes for a US-Iran deal faded after the White House threatened
strikes over the Strait of Hormuz. Energy was the day's strongest sector at
+3.1%, with defence names higher alongside." The story leads, told from the
headlines; the tape corroborates in sentence two.

Good lead: "The Fed decides on rates at 14:00, and the statement's wording
sets what savers earn and borrowers pay across the economy." Maximum reach,
resolves today -- the one case where the calendar rightly takes rank 1.

Good: "Memory chipmakers jumped together after contract prices rose again:
the industry gained 6.8% and Micron closed above $1,000 for the first
time." Breadth, a sourced cause, and a milestone people repeat.

Good: "Apple is dominating the news flow, from an EU ruling on App Store
fees to reports of an in-house memory controller, even as the stock barely
moved at 1.4%." The story is the coverage itself -- said qualitatively,
with the events named, and not a count in sight.

Bad: "War shows up in 990 stories as oil climbs." That is the curator
talking about its own plumbing. The reader wants the story those articles
tell: "Hopes for a US-Iran deal faded as supply risks grew, and oil moved
up."

Bad: "Amrize drops 8.94%, the largest decline on our screen. No headline
explains the move." Every day has a biggest loser -- its existence is not
news, the name is not one readers know, and there is no story. Mention such
a move last if at all, never as the lead.

Bad: "Homebuilder sentiment prints 35" as rank 1. A medium-impact survey
leading the brief means the real story got buried.

Bad: "Eton Pharmaceuticals jumps 44%." A $1.6B company moving 44% is less
market impact than a megacap moving 0.5%, and with no headline you cannot
even say why. Percentage is the weakest form of importance.

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
                             "enum": ["topic", "theme", "move", "buzz",
                                      "economics", "earnings", "news",
                                      "market"]},
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
    payload = [{k: v for k, v in c.items()
                if k not in ("score", "score_parts")} for c in candidates]
    shut = not is_trading_day(day)
    parts = [
        f"Date: {long_day(day)} (US market open 09:30 ET)" if not shut else
        f"Date: {long_day(day)} -- the US market is closed today. Every price "
        f"move below is from {last_session(day):%A}'s session and the facts "
        f"date it that way; write it that way too, never as today's move.",
        "",
        "Candidates, in the curator's ranked order (best first):",
        json.dumps(payload, indent=1),
    ]
    if complaint:
        parts += ["", "Your previous attempt was rejected:", complaint,
                  "", "Write a new set that does not repeat that mistake."]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Validation -- a failed gate discards the insight, and usually the run
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
           "vs.", "approx.", "St.", "Dr.", "a.m.", "p.m.",
           # Dated announcements are everywhere in news bodies, and "Aug. 28"
           # once cost a whole run: every retry wrote the date the natural
           # way, and every retry was counted as three sentences.
           "Jan.", "Feb.", "Mar.", "Apr.", "Jun.", "Jul.", "Aug.",
           "Sept.", "Sep.", "Oct.", "Nov.", "Dec.",
           "Mr.", "Mrs.", "Ms.", "Prof.", "Sen.", "Rep.", "Gov.",
           "Dept.", "No.")


def _sentence_count(body: str) -> int:
    """Count sentence ends, ignoring the full stops that are not ones."""
    masked = body
    for a in _ABBREV:
        masked = masked.replace(a, "_" * len(a))
    # A decimal point sits between two digits; a sentence end does not.
    masked = re.sub(r"(?<=\d)\.(?=\d)", "_", masked)
    # An initial — "J. Powell", "Michelle W. Bowman" — is a capital letter
    # alone before another capitalised word, not the end of anything.
    masked = re.sub(r"\b[A-Z]\.(?=\s+[A-Z])", "__", masked)
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



def _check_insight(r: dict, cand: dict, global_hay: str,
                 global_values: list[float]) -> None:
    """The gates one insight has to pass on its own account.

    Separated from the whole-brief gates below so a single bad insight can be
    dropped without discarding the four good ones beside it.
    """
    text = f"{r['headline']} {r['body']}"
    hay = _haystack(cand)

    # 3. Number provenance. Bare years pass; every other figure has to
    #    come from somewhere in the candidate list.
    hay_values = _numbers_in(hay)
    for tok in _NUM_TOKEN.findall(text):
        if re.fullmatch(r"(19|20)\d{2}", tok):
            continue
        if _number_is_sourced(tok, hay, hay_values):
            continue
        if _number_is_sourced(tok, global_hay, global_values):
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
        # The token runs to a word boundary, so a hyphenated compound
        # hands over the joining hyphen with it: "a US-backed venture"
        # arrives as "US-", "U.S.-based" as "U.S.-", "AI-driven" as
        # "AI-". No ticker begins or ends in a joiner, and each of those
        # spellings is a word already known not to be one -- but only
        # once the joiner is off. Two runs of a good brief were thrown
        # away for "US-" before this stripped it.
        #
        # Only the outside is stripped: BRK.B and BF-B carry theirs in
        # the middle, and those are real symbols.
        core = tok.strip(".-")
        if not core:
            continue
        # "U.S." arrives as "U.S"; dotted abbreviations are the same
        # words as their undotted forms, not tickers.
        if core in NOT_TICKERS or core.replace(".", "") in NOT_TICKERS:
            continue
        if core.upper() in allowed or tok.upper() in allowed:
            continue
        # Headlines carry tickers of their own, and quoting one back is
        # sourced -- from the cited candidate or any sibling.
        if tok in hay or tok in global_hay or core in hay or core in global_hay:
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


def validate(payload, candidates: list[dict],
             salvage: bool = False) -> list[dict]:
    """Return the accepted insights, or raise Rejected naming the gate.

    With ``salvage`` set, an insight that fails a gate of its own is dropped
    and the rest are kept, provided enough survive and the whole-brief gates
    still hold. A run that ends with no brief at all is the worst outcome
    available -- the page then says the brief has not been written yet -- and
    it is not worth accepting because one of five insights quoted a figure
    the checker could not find.
    """
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

    # Numbers and names are checked against the cited candidate first, then
    # against everything else we handed over. An insight that weaves in a
    # sibling candidate -- housing starts alongside building permits, a
    # mover inside the story that explains it -- is quoting our data
    # correctly; only a figure found nowhere is an invention.
    global_hay = " ".join(_haystack(c) for c in candidates)
    global_values = _numbers_in(global_hay)

    # Each insight is judged on its own, then the brief as a whole.
    kept, dropped = [], []
    for r in rows:
        try:
            _check_insight(r, by_id[r["source_id"]], global_hay, global_values)
        except Rejected as exc:
            if not salvage:
                raise
            dropped.append(str(exc))
            continue
        kept.append(r)
    if dropped:
        for why in dropped:
            print(f"[insights] dropped one insight: {why}")
        if len(kept) < MIN_INSIGHTS:
            raise Rejected(f"salvage: only {len(kept)} of {len(rows)} insights "
                           f"passed, and a brief needs {MIN_INSIGHTS} "
                           f"({'; '.join(dropped)})")
        # Ranks are the reading order and must stay 1..n with no gap.
        # Copies, so renumbering never edits the caller's payload -- a
        # salvage attempt has to leave the answer it read exactly as it was.
        rows = [dict(r) for r in sorted(kept, key=lambda r: int(r["rank"]))]
        for i, r in enumerate(rows, 1):
            r["rank"] = i

    # 8. Attention. When a story-class candidate -- a topic, theme, or
    #    heavily-covered name -- scores at or near the top of the table, a
    #    brief that cites none of them has buried what people are actually
    #    talking about. That is a ranking failure, not a matter of taste.
    scores = [c.get("score") or 0 for c in candidates]
    top = max(scores) if scores else 0
    top_is_story = any(c["kind"] in ("topic", "theme", "buzz")
                       and (c.get("score") or 0) >= top
                       for c in candidates)
    if top_is_story and top >= 15:
        story_ids = {c["id"] for c in candidates
                     if c["kind"] in ("topic", "theme", "buzz")}
        if not any(r["source_id"] in story_ids for r in rows):
            raise Rejected("attention: the top-scored candidate is a story "
                           "but no insight cites any topic, theme, or "
                           "covered name")

    # 9. One bare mover at most. Every day has a big percentage move; a
    #    brief that is a list of them is a screener, not a brief.
    n_moves = sum(1 for r in rows if by_id[r["source_id"]]["kind"] == "move")
    if n_moves > 1:
        raise Rejected(f"movers: {n_moves} insights cite bare single-stock "
                       f"moves; at most one may")

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


# A gate the model keeps tripping needs more than the failure text back. The
# number gate is the one worth spelling out: the model is not inventing a
# figure, it is tidying one -- writing the claims count in full, dropping a
# separator, converting a unit -- and the fix is to copy the characters.
_COMPLAINT_HINTS = (
    ("number:", "Copy each figure exactly as the facts spell it. Do not "
                "rescale a value, add or remove a thousands separator, or "
                "convert a unit. Where the facts give no figure for what you "
                "want to say, write the sentence without one."),
    ("cause:",  "Only a candidate whose facts include a headline can support "
                "a reason. For the others, say what happened without saying "
                "why."),
    ("length:", "Two sentences, and count the characters before you answer."),
)


def _complaint(exc: Rejected) -> str:
    text = str(exc)
    for prefix, hint in _COMPLAINT_HINTS:
        if text.startswith(prefix):
            return f"{text}\n{hint}"
    return text


def generate(day: dt.date, candidates: list[dict]) -> tuple[list[dict], dict]:
    """Up to three calls per model, then a salvaged brief before giving up."""
    models = [MODEL] + ([FALLBACK_MODEL] if FALLBACK_MODEL else [])
    complaint = ""
    last: Rejected | None = None
    # Every answer that came back, in the order it arrived. A rejected
    # payload is usually four good insights and one that tripped a gate, and
    # that is worth keeping if nothing better arrives.
    answers: list[tuple[dict, dict]] = []

    for model in models:
        # Three calls per model: two rejections in a row used to end the run
        # with no brief at all, and they were often two different mistakes
        # rather than one the model could not fix.
        for attempt in range(ATTEMPTS):
            try:
                payload, usage = call_model(day, candidates, model, complaint)
            except Refused as exc:
                print(f"[insights] {model} declined the request ({exc})")
                break                      # a retry would decline the same way
            answers.append((payload, usage))
            try:
                rows = validate(payload, candidates)
            except Rejected as exc:
                last = exc
                complaint = _complaint(exc)
                print(f"[insights] rejected (attempt {attempt + 1}, {model}): {exc}")
                print(f"[insights] rejected text: "
                      f"{json.dumps(payload)[:1200]}")
                continue
            return rows, usage

    # Nothing passed whole. A shorter brief made of the insights that did
    # pass beats no brief: the page would otherwise say the brief has not
    # been written, all day, because of one bad line in five.
    for payload, usage in answers:
        try:
            rows = validate(payload, candidates, salvage=True)
        except Rejected as exc:
            print(f"[insights] not salvageable: {exc}")
            continue
        print(f"[insights] salvaged {len(rows)} insights from a rejected "
              f"answer; the failed ones were dropped.")
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
    ap.add_argument("--only-if-missing", action="store_true",
                    help="exit without writing when the day already has a "
                         "brief; for the catch-up run")
    args = ap.parse_args()

    day = (dt.date.fromisoformat(args.day) if args.day
           else _eastern_now().date())

    # The brief runs every day. On a weekend or a holiday there is no session
    # to report, but there is a weekend's news and a Friday close to carry it,
    # which is the reading anyone opening the page on a Sunday wants. What
    # changes is the wording: session_phrase dates every price fact to the
    # session it came from, so nothing calls Friday's close "today".
    session = session_phrase(day)
    if session != "today":
        print(f"[insights] {day}: US market closed; prices are from "
              f"{last_session(day)} and the facts say so.")

    brief = read_rpc("get_market_brief", {"p_day": day.isoformat()}) or {}

    # The catch-up run is a safety net for a first attempt that was delayed
    # or rejected, not a rewrite: a brief already written for this day stands.
    # get_market_brief falls back to the most recent day it has, so the check
    # is on insightsDay -- "insights is non-empty" would be true every day.
    if args.only_if_missing and brief.get("insightsDay") == day.isoformat():
        print(f"[insights] {day} already has a brief; nothing to do.")
        return 0
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
    # The macro-attention signal: each curated subject (The Fed, Inflation,
    # Tariffs, ...) with a live story count against the corpus.
    try:
        keywords = (read_rpc("get_news", {"p_q": None, "p_limit": 1})
                    or {}).get("keywords") or []
    except SystemExit:
        keywords = []
    topics = [k for k in keywords if k.get("kind") == "topic"]

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
                                  industries, stories, topics, session)
    print(f"[insights] {day}: {len(candidates)} candidates from "
          f"{len(industries)} industries, {len(news)} stories, "
          f"{len(topics)} topics; price history for "
          f"{len(stories)}/{len(shortlist)} names")
    # The scored table is the ranking; "why didn't X appear" is answered
    # here, not by guesswork.
    print("[insights] curation table:")
    for c in candidates:
        print(f"    {c.get('score', 0):>5}  {c['id']:<9} {c['kind']:<9} "
              f"{str(c['title'])[:44]:<44} [{c.get('score_parts', '')}]")
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
