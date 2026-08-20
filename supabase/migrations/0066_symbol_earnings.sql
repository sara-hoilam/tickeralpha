-- Ticker Alpha — per-symbol earnings history for the ticker page's Earnings tab
--
-- Apply after 0065. Safe to re-run.
--
-- ledger.earnings_event (0029) is a calendar, not a history: replace_earnings
-- empties the table and rewrites a ±90-day window on every run, so "did this
-- company beat last quarter, and the seven before it?" is a question it cannot
-- answer. This table is the same figures on the other axis — one symbol, as
-- many quarters back as FMP keeps them, and kept between runs.
--
-- It is filled on the same visit-driven refresh as prices, dividends and
-- analyst coverage: a company nobody has opened has no rows, and gets them the
-- first time somebody does.

create table if not exists ledger.earnings_report (
  symbol             text not null,
  date               date not null,
  eps_actual         double precision,
  eps_estimated      double precision,
  revenue_actual     double precision,
  revenue_estimated  double precision,
  fiscal_date        date,
  updated_at         timestamptz not null default now(),
  primary key (symbol, date)
);

create index if not exists earnings_report_symbol_date_idx
  on ledger.earnings_report (symbol, date desc);

alter table ledger.earnings_report enable row level security;

-- ---------------------------------------------------------------------------
-- Write — the worker only
-- ---------------------------------------------------------------------------
-- Replace rather than upsert: FMP restates an estimate after the fact, and a
-- quarter that quietly disappears from its history should disappear here too.
-- Scoped to one symbol, so one company's refresh cannot empty another's.
create or replace function public.replace_symbol_earnings(p_symbol text, p_rows jsonb)
returns integer
language plpgsql volatile security definer
set search_path = ledger, pg_temp
as $$
declare
  v_sym text := upper(trim(coalesce(p_symbol, '')));
  n integer;
begin
  if v_sym !~ '^[A-Z][A-Z.\-]{0,9}$' then
    return 0;
  end if;
  if p_rows is null or jsonb_typeof(p_rows) <> 'array' then
    return 0;
  end if;

  delete from ledger.earnings_report where symbol = v_sym;

  -- distinct on, not on conflict: the delete above means the only possible
  -- duplicate is one the payload carries itself, and ON CONFLICT DO UPDATE
  -- cannot resolve two rows proposed by the same insert -- it aborts. FMP does
  -- restate a quarter and send it twice, so the payload is deduplicated here
  -- instead, keeping whichever copy arrived first.
  insert into ledger.earnings_report
    (symbol, date, eps_actual, eps_estimated,
     revenue_actual, revenue_estimated, fiscal_date, updated_at)
  select distinct on (t.d)
         v_sym, t.d,
         nullif(t.r->>'epsActual','')::double precision,
         nullif(t.r->>'epsEstimated','')::double precision,
         nullif(t.r->>'revenueActual','')::double precision,
         nullif(t.r->>'revenueEstimated','')::double precision,
         nullif(t.r->>'fiscalDate','')::date,
         now()
    from (
      select nullif(e.value->>'date','')::date as d, e.value as r, e.ord
        from jsonb_array_elements(p_rows) with ordinality as e(value, ord)
    ) t
   where t.d is not null
   order by t.d, t.ord;

  get diagnostics n = row_count;
  return n;
end;
$$;

-- ---------------------------------------------------------------------------
-- Read — the ticker page
-- ---------------------------------------------------------------------------
-- Two things in one round trip, because the tab shows both and they live in
-- different tables: the quarters already reported, and the next date on the
-- calendar. 'reports' is null rather than [] when nothing has been fetched for
-- this symbol yet — the page says "not fetched" and "none published" in
-- different words, and cannot tell them apart from an empty array.
create or replace function public.get_symbol_earnings(p_symbol text)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  with me as (select upper(trim(coalesce(p_symbol, ''))) as sym)
  select jsonb_build_object(
    'symbol', (select sym from me),

    'reports', (
      select jsonb_agg(jsonb_build_object(
               'date', e.date,
               'epsActual', e.eps_actual,
               'epsEstimated', e.eps_estimated,
               'revenueActual', e.revenue_actual,
               'revenueEstimated', e.revenue_estimated,
               'fiscalDate', e.fiscal_date)
             order by e.date desc)
      from ledger.earnings_report e
      where e.symbol = (select sym from me)),

    'updatedAt', (
      select max(e.updated_at) from ledger.earnings_report e
      where e.symbol = (select sym from me)),

    -- The soonest dated announcement that has not reported yet. The calendar
    -- carries the estimate for it; the history will not until it lands.
    'next', (
      select jsonb_build_object(
               'date', v.date, 'time', v.time,
               'epsEstimated', v.eps_estimated,
               'revenueEstimated', v.revenue_estimated)
      from ledger.earnings_event v
      where v.symbol = (select sym from me)
        and v.date >= current_date
        and v.eps_actual is null
      order by v.date
      limit 1)
  );
$$;

do $$
begin
  execute 'revoke all on function public.replace_symbol_earnings(text, jsonb) from public, anon, authenticated';
  execute 'grant execute on function public.replace_symbol_earnings(text, jsonb) to service_role';

  execute 'revoke all on function public.get_symbol_earnings(text) from public';
  execute 'grant execute on function public.get_symbol_earnings(text) to anon, authenticated';
end $$;
