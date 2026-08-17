-- Ticker Alpha — Today's Brief on the market page
--
-- Apply after 0064. Safe to re-run.
--
-- The market page opens on a list and a chart, which answer "what moved" but
-- never "what happens today". Both halves of that answer already exist in the
-- database and neither is reachable from a signed-out page:
--
--   * ledger.economic_event (0045) holds the US macro calendar, but the only
--     reader is get_portfolio_calendar, which filters earnings to p_symbols
--     and so returns an empty brief for a visitor with no portfolio.
--   * ledger.earnings_event carries market_cap since 0048, so "the four
--     biggest companies reporting today" is an index scan — but the only
--     reader is the earnings sidebar, which knows nothing about macro events.
--
-- So: one table for the written insights, one reader that bundles insights,
-- macro events and cap-ranked earnings into a single round trip, and one
-- writer for the daily job.
--
-- The insights are written once a day by .github/workflows/insights.yml, not
-- per visit. That is the whole reason this table exists rather than the page
-- calling a model: the token cost is one call a day whether the page is read
-- twice or twenty thousand times, and a visitor is never waiting on inference.

create table if not exists ledger.market_insight (
  day          date     not null,
  rank         smallint not null,
  headline     text     not null,
  body         text     not null,
  kind         text,                  -- economics | earnings | news | market
  symbols      text[]   not null default '{}',
  event_time   text,                  -- ET clock string, '08:30', when there is one
  source       text,                  -- the event or headline it was drawn from
  source_url   text,                  -- the story link, when kind = 'news'
  model        text,                  -- which model wrote it, for auditing
  created_at   timestamptz not null default now(),
  primary key (day, rank)
);

create index if not exists market_insight_day_idx
  on ledger.market_insight (day desc);

alter table ledger.market_insight enable row level security;

-- ---------------------------------------------------------------------------
-- Write — the daily job only
-- ---------------------------------------------------------------------------
-- Replace rather than upsert: a day's insights are written as a set, and a
-- re-run must not leave rank 4 from this morning sitting under ranks 1-3 from
-- this afternoon. Deleting and inserting inside one function is one
-- transaction, so a reader never sees the day half-written.
create or replace function public.replace_market_insights(p_day date, p_rows jsonb)
returns integer
language plpgsql volatile security definer
set search_path = ledger, pg_temp
as $$
declare n integer;
declare d date := coalesce(p_day, current_date);
begin
  if p_rows is null or jsonb_typeof(p_rows) <> 'array' then
    return 0;                          -- a failed generation must not empty the day
  end if;

  delete from ledger.market_insight where day = d;

  if jsonb_array_length(p_rows) = 0 then
    return 0;
  end if;

  insert into ledger.market_insight
    (day, rank, headline, body, kind, symbols, event_time, source, source_url,
     model, created_at)
  select d,
         nullif(r->>'rank','')::smallint,
         r->>'headline',
         r->>'body',
         nullif(r->>'kind',''),
         -- jsonb_array_elements_text throws on a scalar, and a generator that
         -- emits "symbols": null instead of [] must not fail the whole write.
         coalesce((select array_agg(upper(s))
                   from jsonb_array_elements_text(
                          case when jsonb_typeof(r->'symbols') = 'array'
                               then r->'symbols' else '[]'::jsonb end) s
                   where nullif(trim(s), '') is not null), '{}'),
         nullif(r->>'event_time',''),
         nullif(r->>'source',''),
         nullif(r->>'source_url',''),
         nullif(r->>'model',''),
         now()
  from jsonb_array_elements(p_rows) r
  where nullif(r->>'headline','') is not null
    and nullif(r->>'body','') is not null
    and nullif(r->>'rank','') is not null
  on conflict (day, rank) do update set
    headline = excluded.headline,
    body = excluded.body,
    kind = excluded.kind,
    symbols = excluded.symbols,
    event_time = excluded.event_time,
    source = excluded.source,
    source_url = excluded.source_url,
    model = excluded.model,
    created_at = now();

  get diagnostics n = row_count;
  return n;
end;
$$;

-- ---------------------------------------------------------------------------
-- Read — the market page
-- ---------------------------------------------------------------------------
-- One call returns the whole section. Three separate RPCs would have meant
-- three round trips for a block that is above the fold and must not arrive in
-- pieces.
create or replace function public.get_market_brief(p_day date default null)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  with target as (
    select coalesce(p_day, current_date) as day
  ),
  -- The brief is written in the morning, so before the first run of the day --
  -- and on a weekend or holiday, when there is no run at all -- fall back to
  -- the most recent day that has one. The page says which day it is showing
  -- rather than passing yesterday's brief off as today's.
  ins_day as (
    select max(i.day) as day
    from ledger.market_insight i, target t
    where i.day <= t.day
  ),
  ins as (
    select jsonb_agg(jsonb_build_object(
             'rank', i.rank,
             'headline', i.headline,
             'body', i.body,
             'kind', i.kind,
             'symbols', to_jsonb(i.symbols),
             'eventTime', i.event_time,
             'source', i.source,
             'sourceUrl', i.source_url)
           order by i.rank) as rows
    from ledger.market_insight i, ins_day d
    where d.day is not null and i.day = d.day
  ),
  econ as (
    select jsonb_agg(jsonb_build_object(
             'kind', 'economics',
             'date', e.event_date,
             'time', e.event_time,
             'event', e.event,
             'impact', e.impact,
             'previous', e.previous,
             'estimate', e.estimate,
             'actual', e.actual)
           order by case lower(coalesce(e.impact,''))
                      when 'high' then 0 when 'medium' then 1 else 2 end,
                    e.event_time nulls last, e.event) as rows
    from ledger.economic_event e, target t
    where e.event_date = t.day and e.country = 'US'
  ),
  -- Below $1B is shells, OTC stubs and delisted tickers -- the same floor the
  -- earnings page applies, for the same reason (see 0048).
  earn_all as (
    select e.date, e.symbol, e.time, e.eps_estimated, e.eps_actual,
           e.market_cap,
           coalesce(t2.name, c.name, q.name, e.name, e.symbol) as name
    from ledger.earnings_event e
    cross join target t
    left join ledger.ticker  t2 on t2.ticker = e.symbol
    left join ledger.company c  on c.ticker  = e.symbol
    left join ledger.quote   q  on q.symbol  = e.symbol
    where e.date = t.day
      and coalesce(e.market_cap, 0) >= 1e9
  ),
  earn as (
    select jsonb_agg(jsonb_build_object(
             'kind', 'earnings',
             'date', a.date,
             'symbol', a.symbol,
             'name', a.name,
             'time', a.time,
             'marketCap', a.market_cap,
             'epsEstimated', a.eps_estimated,
             'epsActual', a.eps_actual)
           order by a.market_cap desc nulls last, a.symbol) as rows
    from (select * from earn_all
           order by market_cap desc nulls last, symbol
           limit 12) a
  )
  select jsonb_build_object(
    'day', (select day from target),
    'insightsDay', (select day from ins_day),
    'insights', coalesce((select rows from ins), '[]'::jsonb),
    'economics', coalesce((select rows from econ), '[]'::jsonb),
    'earnings', coalesce((select rows from earn), '[]'::jsonb),
    'earningsTotal', (select count(*) from earn_all)
  );
$$;

do $$
begin
  execute 'revoke all on function public.replace_market_insights(date, jsonb) from public, anon, authenticated';
  execute 'grant execute on function public.replace_market_insights(date, jsonb) to service_role';

  execute 'revoke all on function public.get_market_brief(date) from public';
  execute 'grant execute on function public.get_market_brief(date) to anon, authenticated';
end $$;
