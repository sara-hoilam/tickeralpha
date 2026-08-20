-- Ticker Alpha — institutional (13F) holders for the ticker page's Ownership tab
--
-- Apply after 0066. Safe to re-run.
--
-- The Ownership tab could say who had been buying and selling — insider Form 4s
-- and congressional disclosures — but not who actually owns the company, which
-- is the first question the tab's name asks. 13F holdings answer it.
--
-- Filled on the same visit-driven refresh as prices, dividends and earnings, so
-- a company nobody has opened has no rows and gets them the first time somebody
-- does. The fetch is allowed to fail: 13F data sits above FMP's entry plans, and
-- the page says so rather than showing an empty card.

create table if not exists ledger.institutional_holder (
  symbol        text not null,
  holder        text not null,
  cik           text,
  shares        double precision,
  market_value  double precision,
  share_change  double precision,
  reported      date,
  updated_at    timestamptz not null default now(),
  primary key (symbol, holder)
);

create index if not exists institutional_holder_symbol_shares_idx
  on ledger.institutional_holder (symbol, shares desc nulls last);

alter table ledger.institutional_holder enable row level security;

-- ---------------------------------------------------------------------------
-- Write — the worker only
-- ---------------------------------------------------------------------------
-- Replace rather than upsert, scoped to one symbol: a fund that exited stops
-- appearing in the filing, and a stale row would keep it on the page for ever.
create or replace function public.replace_symbol_holders(p_symbol text, p_rows jsonb)
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

  delete from ledger.institutional_holder where symbol = v_sym;

  -- distinct on, not on conflict: the delete above means the only possible
  -- duplicate is one the payload carries itself, and ON CONFLICT DO UPDATE
  -- cannot resolve two rows proposed by the same insert -- it aborts. Two
  -- filings under one name, or two names that truncate to the same 120
  -- characters, collapse to the larger position.
  insert into ledger.institutional_holder
    (symbol, holder, cik, shares, market_value, share_change, reported, updated_at)
  select distinct on (t.h)
         v_sym, t.h,
         nullif(t.r->>'cik',''),
         t.shares,
         nullif(t.r->>'value','')::double precision,
         nullif(t.r->>'change','')::double precision,
         nullif(t.r->>'date','')::date,
         now()
    from (
      select left(trim(e.value->>'holder'), 120) as h,
             nullif(e.value->>'shares','')::double precision as shares,
             e.value as r, e.ord
        from jsonb_array_elements(p_rows) with ordinality as e(value, ord)
    ) t
   where nullif(t.h, '') is not null
   order by t.h, t.shares desc nulls last, t.ord;

  get diagnostics n = row_count;
  return n;
end;
$$;

-- ---------------------------------------------------------------------------
-- Read — the ticker page
-- ---------------------------------------------------------------------------
-- 'holders' is null rather than [] when nothing has been fetched for this
-- symbol yet. The page has different things to say about "not fetched" and
-- "fetched, and nobody files against this ticker", and cannot tell them apart
-- from an empty array.
--
-- sharesOut rides along from the stored quote so the page can express a
-- position as a share of the company without a second round trip. It is the
-- only figure here that does not come from a 13F.
create or replace function public.get_symbol_holders(p_symbol text, p_limit integer default 25)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  with me as (
    select upper(trim(coalesce(p_symbol, ''))) as sym,
           greatest(1, least(coalesce(p_limit, 25), 100)) as lim
  )
  select jsonb_build_object(
    'symbol', (select sym from me),

    'holders', (
      select jsonb_agg(jsonb_build_object(
               'holder', x.holder, 'cik', x.cik, 'shares', x.shares,
               'value', x.market_value, 'change', x.share_change,
               'reported', x.reported)
             order by x.shares desc nulls last)
      from (
        select h.* from ledger.institutional_holder h
        where h.symbol = (select sym from me)
        order by h.shares desc nulls last
        limit (select lim from me)
      ) x),

    -- Totals span every holder on file, not just the page of them returned.
    'holderCount', (
      select count(*)::integer from ledger.institutional_holder h
      where h.symbol = (select sym from me)),
    'totalShares', (
      select sum(h.shares) from ledger.institutional_holder h
      where h.symbol = (select sym from me)),
    'reported', (
      select max(h.reported) from ledger.institutional_holder h
      where h.symbol = (select sym from me)),

    'sharesOut', (
      select q.shares from ledger.quote_detail q
      where q.symbol = (select sym from me))
  );
$$;

do $$
begin
  execute 'revoke all on function public.replace_symbol_holders(text, jsonb) from public, anon, authenticated';
  execute 'grant execute on function public.replace_symbol_holders(text, jsonb) to service_role';

  execute 'revoke all on function public.get_symbol_holders(text, integer) from public';
  execute 'grant execute on function public.get_symbol_holders(text, integer) to anon, authenticated';
end $$;
