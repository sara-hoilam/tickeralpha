# How the backend works

Three moving parts, and only one of them talks to the SEC.

```
                      ┌──────────────────────────────┐
   SEC EDGAR ────────►│  Render: ledger-ingest       │
   (the only source)  │  worker.py — 1 instance      │
                      └──────────────┬───────────────┘
                                     │ writes, service_role key
                                     ▼
                      ┌──────────────────────────────┐
                      │  Supabase Postgres           │
                      │  ledger schema (unexposed)   │
                      │  + 4 read fns, 8 write fns   │
                      └──────────────┬───────────────┘
                                     │ reads, anon key
                                     ▼
                      ┌──────────────────────────────┐
                      │  Cloudflare Pages            │
                      │  dashboard.html — static     │
                      └──────────────────────────────┘
```

The browser never contacts the SEC, and the worker never serves a request.
They meet only in the database.

---

## 1. The sources

Everything comes from SEC EDGAR. There is no market-data vendor, no scraping
of finance sites, and no LLM in the request path — see §10 for the one place a
model does run, which is a nightly batch and not part of serving a page.

| What | Endpoint | Used for |
|---|---|---|
| Ticker directory | `sec.gov/files/company_tickers.json` | ticker → CIK, 10,412 tickers |
| Company facts | `data.sec.gov/api/xbrl/companyfacts/CIK…json` | the income statement and cash flow |
| Filing index | `data.sec.gov/submissions/CIK…json` | which filings exist, and when they were filed |
| The filings | `sec.gov/Archives/edgar/data/…/R*.htm` | revenue breakdowns by segment/product/geography |
| Daily index | `sec.gov/Archives/edgar/daily-index/…/form.YYYYMMDD.idx` | what was filed today, market-wide |

The last two matter for a reason that is easy to miss: **company facts carry
no dimensional detail.** The API gives consolidated totals only. To learn that
$63.3B of Alphabet's revenue was Search, you have to read the tables inside the
filing itself — the same ones the SEC's own viewer renders.

---

## 2. What happens inside one ingest

`worker.py ingest GOOGL` runs this, and it is the same path every other
trigger ends up in:

1. **Resolve** the ticker to a CIK from the directory.
2. **Fetch company facts** — one request, a few MB of XBRL.
3. **Normalise** every line item. Companies tag the same economics under
   different names, so each line has a priority list of tags: revenue tries
   `Revenues`, then the contract-revenue tags, then `RevenuesNetOfInterestExpense`
   for banks.
4. **Derive the quarters.** Quarterly columns are used as filed where they
   exist. **No company files a standalone fourth quarter** — the 10-K shows the
   full year — so Q4 is the annual figure minus the nine-month figure. The same
   subtraction recovers Q4 segment revenue.
5. **Fill the gaps.** Gross profit, operating expenses and pre-tax income are
   computed from surrounding lines when untagged, and marked `computed` in the
   provenance so the table can label them.
6. **Fetch the breakdowns** for the most recent 8 quarters. Each costs several
   requests to the filing's R-files, so older quarters are left until someone
   asks.
7. **Write it all** in one transaction.

Roughly 8 SEC requests and 6–45 seconds for a company nobody has fetched
before. Afterwards it is a ~10 ms database read.

---

## 3. When it writes to Supabase

Only ever through `store.ingest_company()`, which calls one Postgres function
that replaces that company's quarters and breakdowns **atomically**. A reader
never sees a half-written company. Quarters are deleted and reinserted rather
than merged, because a restatement can change figures years back and the newest
parse should win.

Four things trigger a write:

### a. A visitor asks for a company we do not have

The most important path, because someone is waiting.

```
browser: get_company('TSLA')      → null
browser: request_backfill('TSLA') → queued
   ↓  (the function refuses unknown tickers and collapses repeat
       asks inside 10 minutes, so this cannot be used to make us
       hammer the SEC)
worker:  claim_backfill()  → fetches from SEC → ingest_company()
browser: polls get_company every 3s, fills in when it lands
```

Measured live: **~80 seconds** — 47s of SEC fetching plus up to 20s waiting for
the worker's next poll. Once per company, ever.

### b. The daily sweep

Once a day after 22:00, the worker reads the SEC's daily index — a single
1.1 MB file listing every filing disseminated that day, of which ~180 are
10-Q/10-K — and re-ingests any tracked company that appears. This is what keeps
the database **ahead** of visitors rather than behind them.

### c. Staleness refresh

Every loop, up to 5 companies whose filing list has not been checked in
`COMPANY_RECHECK_HOURS` (default 6) get re-ingested. This is the safety net for
anything the sweep missed.

### d. Directory sync

Once every 24 hours, the ticker list is refreshed so newly listed companies
become searchable.

---

## 4. The loop, exactly

```python
while True:
    if 24h since last directory sync:  sync_directory()
    if drain_backfill():               continue   # skip the sleep
    if not swept today and hour >= 22: sweep(today)
    refresh_stale(5)
    sleep(BACKFILL_POLL_SECONDS)       # default 20s
```

The `continue` after `drain_backfill()` is deliberate: if a visitor is waiting,
the worker keeps draining the queue rather than sleeping or doing housekeeping.

**Failures are separated by kind.** A configuration problem — a missing service
key, a revoked grant — raises and stops the worker, because it will never
succeed and should page you. Anything else is logged and the loop continues,
because a single unparseable filing must not take the service down.

---

## 5. Why exactly one instance

**The SEC's rate limit is per IP, not per user or per process.** `edgar.py`
paces itself at ~8 requests/second, which is under their ceiling — but that
guarantee only holds if there is one process doing the pacing. Two Render
instances would be two limiters and double the request rate, and the SEC blocks
IPs that ignore the limits.

`render.yaml` pins `numInstances: 1` for this reason. Scaling belongs on the
read path, which the worker never touches — Supabase serves those, and it can
scale freely because it makes no SEC requests at all.

---

## 6. How the browser reads

The browser holds the **anon key**, which is public by design and ships in the
page. It reaches exactly four functions:

| Function | Returns |
|---|---|
| `search_companies(q)` | typeahead over the 10,412-ticker directory |
| `get_company(ticker)` | the whole company: identity + every quarter |
| `get_segments(ticker, end)` | revenue breakdowns for one quarter |
| `request_backfill(ticker)` | queues an uningested company |

The tables live in a `ledger` schema that is **not exposed to the API**.
Verified from outside with only the anon key:

- `GET /rest/v1/company` → 404, not in the public schema cache
- `Accept-Profile: ledger` → *"Only the following schemas are exposed: public,
  graphql_public"*
- `POST` into the backfill queue → 404; it must go through the guarded function

So a visitor cannot read a table, cannot switch schemas, and cannot bypass the
queue's ticker validation.

---

## 7. Caching

| Layer | Lifetime | Why |
|---|---|---|
| `.cache/` on disk | 6h for company facts, 30 days for filing documents | a filed document never changes |
| Supabase | until the next ingest | this is the read path |
| Cloudflare | as configured | the page itself |

The disk cache is a build-time optimisation, not a data store. It is safe to
delete, and on Render it is lost on redeploy — the worker simply refetches.
**Nothing raw is kept**, which is why the whole US market fits in ~450 MB:
parse once, store the result, discard the source.

---

## 8. What is not in the backend

- **No LLM.** No model runs at any point; there is no token cost to operating
  this.
- **No web search.** Adding it would make accuracy worse, not better — press
  coverage quotes non-GAAP figures, and none of the reconciliation checks could
  validate a scraped number.
- **No 8-K earnings releases yet.** A company announces results by press
  release before the 10-Q lands, so there is a 2–6 week window where the
  dashboard shows the prior quarter. Roughly one large-cap in ten sits in that
  window at any moment. Closing it means parsing 8-K Item 2.02 exhibits, which
  are not XBRL-tagged — so they should be stored and labelled separately rather
  than blended in.

---

## 9. Operating it

```bash
python worker.py stats                # coverage summary
python worker.py ingest GOOGL AAPL    # force-refresh specific companies
python worker.py seed 500             # build out coverage
python worker.py sweep 2026-07-31     # re-run one day's filings
python worker.py backfill             # drain the queue once
```

`filing.status` is the honesty mechanism: a filing that fails to parse is
recorded as `failed` with its error and surfaces as "no breakdown available" —
never as a guess. `python worker.py stats` reports `filings_failed`, and a
rising count is the signal that a filer has changed how it renders its tables.

---

## 10. The one model call

The market page opens with **Today's Brief**: three to five short paragraphs
on what is scheduled for the day and why it matters. Those are written by
Claude, once each weekday morning, by `.github/workflows/insights.yml`.

It is worth being precise about where that sits, because it is the only part
of the product that is not deterministic:

```
GitHub Actions, 10:30 UTC ──▶ insights.py
   (weekdays only)              │  reads the same public RPCs a visitor calls
                                │  finds themes, movers, runs, milestones,
                                │    the most-covered names, and the
                                │    headline behind each move
                                │  picks ~20 candidates in Python
                                │  one Claude API call
                                │  seven validation gates
                                ▼
                        ledger.market_insight   ◀── the page reads this
```

**No model runs while a page is being served.** The browser reads a stored
row, so the token cost is one call a day whether the page is opened twice or
twenty thousand times, and nobody waits on inference. If the job fails, the
previous day's brief stays up with its own date on it.

Two properties keep it honest, and both live in `insights.py` rather than in
the prompt:

* **The code decides what is important; the model only explains it.** Ranking
  and filtering are settled in Python before the call — the model receives a
  short list of already-chosen candidates and cannot surface an event that was
  never selected. Those candidates lead with movement, not the schedule: an
  industry whose members ran together, a name at a 52-week high, a month-long
  climb. A brief that ignores every one of them on a day the market moved is
  rejected as a ranking failure.
* **Every number is copied, not computed.** Each candidate carries its figures
  as pre-formatted strings, and a validator rejects the whole run if any
  number in the output is not a substring of the candidate it cites. Other
  gates reject advice language — including the continuation claims that a
  livelier voice invites, "room to run" and its cousins — plus unknown
  tickers, missing sources, and briefs that bury the day's real story. A
  rejected run writes nothing.
* **A reason needs a source.** Asking the brief to say *why* a stock moved
  opens the one hole the other checks cannot see: a fabricated cause is
  ordinary prose, with no invented number and no unknown ticker in it. So a
  causal claim ("after…", "driven by…") is rejected unless the candidate it
  cites actually carries a headline. Where no headline exists the brief
  states the move and stops.

`python insights.py --dry-run` prints the candidates and the generated brief
without writing, which is how to check the output after changing the prompt.
