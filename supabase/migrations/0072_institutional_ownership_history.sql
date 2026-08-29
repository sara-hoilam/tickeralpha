-- Ticker Alpha — how much of a company institutions hold, quarter by quarter
--
-- Apply after 0071. Safe to re-run.
--
-- The Ownership tab shows who holds the stock today (0067); this is the other
-- axis — the total 13F position through time, for the ownership chart: bars
-- for shares held, a line for the slice of the company they add up to. One
-- row per quarter end, because 13F is a quarterly filing: there is no daily
-- or weekly series to store, whatever a chart's period picker might imply.
--
-- Filled on the same visit-driven refresh as prices and holders: a company
-- nobody has opened has no rows, and gets them the first time somebody does.

create table if not exists ledger.institutional_ownership (
  symbol        text not null,
  date          date not null,
  investors     integer,             -- institutions filing a position
  shares        double precision,    -- total 13F shares held
  value         double precision,    -- total position value at quarter end
  ownership_pct double precision,    -- percent of shares outstanding, as filed
  updated_at    timestamptz not null default now(),
  primary key (symbol, date)
);

create index if not exists institutional_ownership_symbol_date_idx
  on ledger.institutional_ownership (symbol, date desc);

alter table ledger.institutional_ownership enable row level security;

-- ---------------------------------------------------------------------------
-- Write — the worker only
-- ---------------------------------------------------------------------------
-- Replace rather than upsert, scoped to one symbol: FMP restates quarters
-- after amended filings, and a quarter that disappears from its history
-- should disappear here too. distinct on, not on conflict: the delete means
-- the only possible duplicate is one the payload carries itself, and
-- ON CONFLICT cannot resolve two rows proposed by the same insert.
create or replace function public.replace_symbol_ownership(p_symbol text, p_rows jsonb)
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

  delete from ledger.institutional_ownership where symbol = v_sym;

  insert into ledger.institutional_ownership
    (symbol, date, investors, shares, value, ownership_pct, updated_at)
  select distinct on (t.d)
         v_sym, t.d,
         nullif(t.r->>'investors','')::integer,
         nullif(t.r->>'shares','')::double precision,
         nullif(t.r->>'value','')::double precision,
         nullif(t.r->>'ownershipPct','')::double precision,
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
-- 'points' is null rather than [] when nothing has been fetched for this
-- symbol yet — "not fetched" and "no filings" read differently on the page.
-- sharesOut rides along so the page can compute the percent line itself for
-- quarters where the feed left ownership_pct empty.
create or replace function public.get_symbol_ownership(p_symbol text)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  with me as (select upper(trim(coalesce(p_symbol, ''))) as sym)
  select jsonb_build_object(
    'symbol', (select sym from me),

    'points', (
      select jsonb_agg(jsonb_build_object(
               'date', o.date,
               'investors', o.investors,
               'shares', o.shares,
               'value', o.value,
               'ownershipPct', o.ownership_pct)
             order by o.date)
      from ledger.institutional_ownership o
      where o.symbol = (select sym from me)),

    'sharesOut', (
      select q.shares from ledger.quote_detail q
      where q.symbol = (select sym from me)),

    'updatedAt', (
      select max(o.updated_at) from ledger.institutional_ownership o
      where o.symbol = (select sym from me))
  );
$$;

do $$
begin
  execute 'revoke all on function public.replace_symbol_ownership(text, jsonb) from public, anon, authenticated';
  execute 'grant execute on function public.replace_symbol_ownership(text, jsonb) to service_role';

  execute 'revoke all on function public.get_symbol_ownership(text) from public';
  execute 'grant execute on function public.get_symbol_ownership(text) to anon, authenticated';
end $$;
