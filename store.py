"""
store.py -- the worker's connection to Supabase.

Everything goes through the write functions defined in
supabase/migrations/0002_ledger_write_api.sql. The `ledger` schema itself is
not exposed to the API, so there is no way to reach the tables directly even
with the service-role key -- which keeps the read surface honest.

Standard library only, same as the rest of the project.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
# Accept a project URL or a mistaken .../rest/v1 paste from the dashboard.
if SUPABASE_URL.endswith("/rest/v1"):
    SUPABASE_URL = SUPABASE_URL[: -len("/rest/v1")].rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""


class StoreError(RuntimeError):
    pass


def configured() -> bool:
    return bool(SUPABASE_URL and SERVICE_KEY)


def require_config():
    if not SUPABASE_URL:
        raise StoreError("SUPABASE_URL is not set. Copy .env.example to .env.")
    if not SERVICE_KEY:
        raise StoreError(
            "SUPABASE_SERVICE_ROLE_KEY is not set.\n"
            "  Supabase dashboard -> Project Settings -> API -> service_role.\n"
            "  Put it in .env (gitignored) or Render's environment tab.\n"
            "  Never commit it and never paste it into a chat.")


def rpc(fn: str, payload: dict | None = None, *, retries: int = 3):
    """Call one of the write functions. Retries transient failures."""
    require_config()
    url = f"{SUPABASE_URL}/rest/v1/rpc/{fn}"
    body = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "apikey": SERVICE_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })

    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            # 4xx is our bug -- a bad payload or a missing grant. Do not retry.
            if 400 <= exc.code < 500:
                raise StoreError(f"{fn} -> HTTP {exc.code}: {detail}") from exc
            last = StoreError(f"{fn} -> HTTP {exc.code}: {detail}")
        except Exception as exc:
            last = StoreError(f"{fn} -> {exc}")
        time.sleep(1.5 * (attempt + 1))
    raise last if last else StoreError(f"{fn} failed")


# ---------------------------------------------------------------------------
# Operations the worker needs
# ---------------------------------------------------------------------------

def upsert_directory(rows: list[dict], chunk: int = 2000) -> int:
    """Refresh the ticker directory. Chunked to keep request bodies sane."""
    total = 0
    for i in range(0, len(rows), chunk):
        part = [{"cik": r["cik"], "ticker": r["ticker"], "name": r["name"]}
                for r in rows[i:i + chunk]]
        total += rpc("upsert_directory", {"p_rows": part}) or 0
    return total


def upsert_market_symbols(rows: list[dict], chunk: int = 2000) -> int:
    """Upsert FMP ETF / crypto symbols into ledger.market_symbol."""
    total = 0
    for i in range(0, len(rows), chunk):
        part = rows[i:i + chunk]
        total += rpc("upsert_market_symbols", {"p_rows": part}) or 0
    return total


def ingest_company(company: dict, breakdowns: list[dict]) -> dict:
    """Write one company's quarters and revenue breakdowns in one transaction."""
    payload = {
        "cik": company["cik"],
        "ticker": company["ticker"],
        "name": company["name"],
        "exchange": company.get("exchange"),
        "sic": company.get("sic"),
        "fye_month": company.get("fiscalYearEndMonth"),
        "quarters": [{
            "end": q["end"], "start": q.get("start"),
            "fy": q["fy"], "fq": q["fq"], "basis": q["basis"],
            "form": q.get("form"), "accession": q.get("accession"),
            "filed": q.get("filed"),
            "lines": q["lines"], "provenance": q.get("provenance"),
        } for q in company["quarters"]],
        "breakdowns": breakdowns,
        "filings": [{
            "accession": f["accession"], "form": f["form"],
            "filed": f["filed"], "periodEnd": f.get("periodEnd") or None,
            "status": "ok",
        } for f in company.get("filings", [])[:12]],
    }
    return rpc("ingest_company", {"p": payload})


def claim_backfill() -> dict | None:
    return rpc("claim_backfill")


def finish_backfill(request_id: int, error: str | None = None) -> None:
    rpc("finish_backfill", {"p_id": request_id, "p_error": error})


def companies_due(hours: int = 6, limit: int = 50) -> list[dict]:
    return rpc("companies_due", {"p_hours": hours, "p_limit": limit}) or []


def filing_seen(accession: str) -> bool:
    return bool(rpc("filing_seen", {"p_accession": accession}))


def record_failure(accession: str, cik: int, form: str,
                   filed: str, period_end: str | None, error: str) -> None:
    rpc("record_filing_failure", {
        "p_accession": accession, "p_cik": cik, "p_form": form,
        "p_filed": filed, "p_period_end": period_end, "p_error": error})


def stats() -> dict:
    return rpc("ledger_stats") or {}


# ---------------------------------------------------------------------------
# Market data (FMP)
# ---------------------------------------------------------------------------

def upsert_quotes(rows: list[dict]) -> int:
    return rpc("upsert_quotes", {"p_rows": rows}) or 0


def replace_index_holdings(index_symbol: str, rows: list[dict]) -> int:
    """Replace constituent rows for one major index (SPX / IXIC / DJI)."""
    return rpc("replace_index_holdings",
               {"p_index": index_symbol, "p_rows": rows}) or 0


def replace_movers(kind: str, rows: list[dict], as_of: str | None) -> int:
    return rpc("replace_movers",
               {"p_kind": kind, "p_rows": rows, "p_as_of": as_of}) or 0


def replace_sectors(rows: list[dict], as_of: str | None) -> int:
    return rpc("replace_sectors", {"p_rows": rows, "p_as_of": as_of}) or 0


def upsert_intraday(symbol: str, points: list[dict], as_of: str | None) -> None:
    rpc("upsert_intraday",
        {"p_symbol": symbol, "p_points": points, "p_as_of": as_of})


def skip_intraday(symbol: str) -> None:
    rpc("skip_intraday", {"p_symbol": symbol})


def pending_intraday(limit: int = 5) -> list[str]:
    return rpc("pending_intraday", {"p_limit": limit}) or []


def watchlisted_symbols(limit: int = 40) -> list[str]:
    return rpc("watchlisted_symbols", {"p_limit": limit}) or []


def replace_heatmap(rows: list[dict], as_of: str | None) -> int:
    return rpc("replace_heatmap", {"p_rows": rows, "p_as_of": as_of}) or 0


def replace_sector_history(rows: list[dict]) -> int:
    return rpc("replace_sector_history", {"p_rows": rows}) or 0


def pending_long_closes(limit: int = 5) -> list[str]:
    return rpc("pending_long_closes", {"p_limit": limit}) or []


def upsert_long_closes(symbol: str, closes: list[dict]) -> int:
    return rpc("upsert_long_closes",
               {"p_symbol": symbol, "p_closes": closes}) or 0


def replace_sector_risk(rows: list[dict]) -> int:
    return rpc("replace_sector_risk", {"p_rows": rows}) or 0


def replace_trades(insiders: list[dict], congress: list[dict]) -> int:
    return rpc("replace_trades",
               {"p_insiders": insiders, "p_congress": congress}) or 0


def append_trades(insiders: list[dict], congress: list[dict],
                  reset: bool = False) -> int:
    return rpc("append_trades", {"p_insiders": insiders,
                                 "p_congress": congress,
                                 "p_reset": reset}) or 0


def replace_politicians(rows: list[dict]) -> int:
    return rpc("replace_politicians", {"p_rows": rows}) or 0


def replace_trade_flow(rows: list[dict]) -> int:
    return rpc("replace_trade_flow", {"p_rows": rows}) or 0


def replace_earnings(rows: list[dict]) -> int:
    return rpc("replace_earnings", {"p_rows": rows}) or 0


def replace_economic_calendar(rows: list[dict]) -> int:
    return rpc("replace_economic_calendar", {"p_rows": rows}) or 0


def replace_symbol_dividends(symbol: str, rows: list[dict]) -> int:
    return rpc("replace_symbol_dividends", {
        "p_symbol": symbol,
        "p_rows": rows,
    }) or 0


def replace_symbol_earnings(symbol: str, rows: list[dict]) -> int:
    return rpc("replace_symbol_earnings", {
        "p_symbol": symbol,
        "p_rows": rows,
    }) or 0


def replace_symbol_ownership(symbol: str, rows: list[dict]) -> int:
    """Rewrite one symbol's quarterly institutional-ownership history."""
    return rpc("replace_symbol_ownership", {
        "p_symbol": symbol,
        "p_rows": rows,
    }) or 0


def replace_symbol_holders(symbol: str, rows: list[dict]) -> int:
    return rpc("replace_symbol_holders", {
        "p_symbol": symbol,
        "p_rows": rows,
    }) or 0


def upsert_news(rows: list[dict], keywords: list[dict]) -> int:
    return rpc("upsert_news", {"p_rows": rows, "p_keywords": keywords}) or 0


def news_image_queue(limit: int = 30) -> list[dict]:
    """Recent articles still wanting their page's own photograph, worst-off
    first: no image, then wire copy wearing a stock photo, then the rest."""
    return rpc("news_image_queue", {"p_limit": limit}) or []


def set_news_images(rows: list[dict]) -> int:
    """Record one scrape attempt per row ({url, image}); image may be None."""
    return rpc("set_news_images", {"p_rows": rows}) or 0


def upsert_prices(symbol: str, bars: list[dict], quote: dict | None,
                  as_of: str | None) -> int:
    return rpc("upsert_prices", {"p_symbol": symbol, "p_bars": bars,
                                 "p_quote": quote, "p_as_of": as_of}) or 0


def reports_due(symbols: list[str], hours: int = 12) -> list[str]:
    """Which of these symbols have no report data, or data older than
    ``hours``. Order-preserving, so the caller's priority order holds."""
    return rpc("reports_due", {"p_symbols": symbols, "p_hours": hours}) or []


def note_price_attempt(symbol: str, error: str | None = None) -> dict:
    return rpc("note_price_attempt",
               {"p_symbol": symbol, "p_error": error}) or {}


def price_queue_state(limit: int = 20, symbol: str | None = None) -> dict:
    return rpc("price_queue_state",
               {"p_limit": limit, "p_symbol": symbol}) or {}


def stale_price_symbols(limit: int = 50) -> dict:
    return rpc("stale_price_symbols", {"p_limit": limit}) or {}


def pending_symbol_insiders(limit: int = 5) -> list[str]:
    return rpc("pending_symbol_insiders", {"p_limit": limit}) or []


def replace_symbol_insiders(symbol: str, rows: list[dict]) -> int:
    return rpc("replace_symbol_insiders",
               {"p_symbol": symbol, "p_rows": rows}) or 0


def pending_prices(limit: int = 5) -> list[str]:
    return rpc("pending_prices", {"p_limit": limit}) or []


def upsert_company_extras(symbol: str, monthly: list[dict] | None,
                          sector: str | None, industry: str | None,
                          pe_history: list[dict] | None = None,
                          profile: dict | None = None,
                          employee_history: list[dict] | None = None) -> int:
    payload = {"p_symbol": symbol, "p_monthly": monthly,
               "p_sector": sector, "p_industry": industry}
    if pe_history is not None:
        payload["p_pe_history"] = pe_history
    if profile is not None:
        payload["p_profile"] = profile
    if employee_history is not None:
        payload["p_employee_history"] = employee_history
    return rpc("upsert_company_extras", payload) or 0


def pending_profiles(limit: int = 20) -> list[str]:
    return rpc("pending_profiles", {"p_limit": limit}) or []


def pending_employee_history(limit: int = 20) -> list[str]:
    return rpc("pending_employee_history", {"p_limit": limit}) or []


def upsert_benchmark(symbol: str, closes: list[dict]) -> int:
    return rpc("upsert_benchmark", {"p_symbol": symbol, "p_closes": closes}) or 0


def replace_industry_pe(rows: list[dict], as_of: str | None) -> int:
    return rpc("replace_industry_pe", {"p_rows": rows, "p_as_of": as_of}) or 0


def upsert_sentiment(key: str, score: float, rating: str | None = None,
                     previous: float | None = None, source: str | None = None) -> None:
    rpc("upsert_sentiment", {
        "p_key": key, "p_score": score, "p_rating": rating,
        "p_previous": previous, "p_source": source,
    })


def upsert_analyst(symbol: str, row: dict) -> int:
    return rpc("upsert_analyst", {"p_symbol": symbol, "p_row": row}) or 0


def pending_analyst(limit: int = 5) -> list[str]:
    return rpc("pending_analyst", {"p_limit": limit}) or []


def upsert_symbol_logos(rows: list[dict]) -> int:
    """Cache Logo.dev image bytes (and status) for ticker / crypto symbols."""
    if not rows:
        return 0
    return rpc("upsert_symbol_logos", {"p_rows": rows}) or 0


def logos_due(targets: list[dict], limit: int = 40) -> list[dict]:
    """Preferred symbols that still need a Logo.dev fetch (or are stale)."""
    return rpc("logos_due", {"p_targets": targets, "p_limit": limit}) or []
