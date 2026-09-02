-- Ticker Alpha — freshness, and every Form 4 for one company
--
-- Apply after 0079. Safe to re-run.
--
-- Two data-quality faults, both of them "the page showed what it had rather
-- than what exists".
--
-- 1. A chart could be a session behind and never notice. get_prices called
--    the cache stale when it was twelve hours old, which asks how long ago we
--    fetched rather than what we fetched. A company refreshed at midday holds
--    a series that stops at the previous close -- the session it is missing
--    has not happened yet -- and stays "fresh" for another twelve hours,
--    which is a whole trading day where the newest bar never arrives.
--    ledger.last_session() names the session the cache should already hold,
--    and staleness is measured against that. The hourly re-arm in
--    request_prices keeps a market holiday, where the answer cannot advance,
--    from turning into a request loop.
--
-- 2. A company's own insider filings were filtered by a market-table rule.
--    ledger.insider_trade is a market-wide pull that keeps only filings over
--    $1M (or 1% of shares outstanding) -- the right floor for a table ranking
--    the whole market's Form 4s, and the wrong one for a company's own page,
--    where a chief executive buying eight hundred thousand dollars of stock
--    on the open market is exactly what a reader came to see. Those rows are
--    not merely hidden, they are never stored: the floor is applied at fetch.
--    So this adds a per-symbol store the worker fills on demand, unfiltered,
--    and a read that prefers it. It is a separate table because
--    replace_trades empties the market-wide one on every refresh.

-- ---------------------------------------------------------------------------
-- The session the cache should already hold
-- ---------------------------------------------------------------------------
-- US regular trading ends at 20:00 UTC (21:00 in winter); 22:00 leaves the
-- feed an hour to publish the close before anything is called missing.
-- Weekends step back to Friday. Market holidays are not modelled: on one, the
-- newest bar stays on the previous session and this reads a little eager,
-- which costs at most one refresh an hour and returns the same bars.
create or replace function ledger.last_session()
returns date
language sql stable
as $$
  select case extract(isodow from d.day)
           when 6 then d.day - 1          -- Saturday  -> Friday
           when 7 then d.day - 2          -- Sunday    -> Friday
           else d.day
         end
  from (
    select case
             when (now() at time zone 'UTC')::time >= time '22:00'
               then (now() at time zone 'UTC')::date
               else (now() at time zone 'UTC')::date - 1
           end as day
  ) d;
$$;

create or replace function public.get_prices(p_symbol text)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  select jsonb_build_object(
    'bars', coalesce((select b.bars from ledger.price_daily b
                      where b.symbol = upper(p_symbol)), '[]'::jsonb),
    'asOf', (select b.as_of from ledger.price_daily b where b.symbol = upper(p_symbol)),
    'quote', (
      select to_jsonb(x) - 'symbol' - 'updated_at'
      from (select q.* from ledger.quote_detail q
            where q.symbol = upper(p_symbol)) x),
    -- Behind the last session, or simply old. The first catches the case the
    -- clock alone cannot see: a series fetched during the session, complete
    -- as of that moment, and missing the close that came after it.
    -- coalesce outside the subquery, so a symbol with no row at all is stale
    -- rather than null: the old shape leaned on max() to guarantee a row.
    'stale', coalesce((
      select d.as_of is null
             or d.as_of < ledger.last_session()
             or d.updated_at < now() - interval '12 hours'
      from ledger.price_daily d where d.symbol = upper(p_symbol)), true),
    'lastSession', ledger.last_session());
$$;

-- ---------------------------------------------------------------------------
-- Every Form 4 for one company
-- ---------------------------------------------------------------------------
create table if not exists ledger.symbol_insider (
  symbol      text not null,
  filed       date not null,
  person      text not null,
  side        text not null,
  shares      double precision,
  price       double precision,
  amount      double precision,
  title       text,
  traded      date,
  updated_at  timestamptz not null default now(),
  -- One filing is one person, one side, one size, on one day. The feed
  -- repeats rows verbatim from time to time; this makes a repeat harmless.
  primary key (symbol, filed, person, side, shares)
);
create index if not exists symbol_insider_idx
  on ledger.symbol_insider (symbol, filed desc);
alter table ledger.symbol_insider enable row level security;

create table if not exists ledger.insider_request (
  symbol       text primary key,
  requested_at timestamptz not null default now(),
  done_at      timestamptz
);
alter table ledger.insider_request enable row level security;

-- The page asks; the worker answers. Re-asking inside the hour is the page
-- polling, not a new request.
create or replace function public.request_symbol_insiders(p_symbol text)
returns jsonb
language plpgsql volatile security definer
set search_path = ledger, pg_temp
as $$
declare sym text := upper(trim(coalesce(p_symbol, '')));
begin
  if sym !~ '^[A-Z][A-Z.\-]{0,9}$' then
    return jsonb_build_object('queued', false, 'reason', 'not a ticker');
  end if;

  insert into ledger.insider_request (symbol, requested_at, done_at)
  values (sym, now(), null)
  on conflict (symbol) do update
    set requested_at = case
          when ledger.insider_request.requested_at < now() - interval '1 hour'
            or ledger.insider_request.done_at is not null
          then now() else ledger.insider_request.requested_at end,
        done_at = case
          when ledger.insider_request.done_at < now() - interval '6 hours'
          then null else ledger.insider_request.done_at end;

  return jsonb_build_object('queued', true, 'symbol', sym);
end;
$$;

create or replace function public.pending_symbol_insiders(p_limit integer default 5)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  select coalesce(jsonb_agg(r.symbol order by r.requested_at), '[]'::jsonb)
  from (
    select symbol, requested_at from ledger.insider_request
    where done_at is null
    order by requested_at
    limit greatest(1, least(coalesce(p_limit, 5), 25))
  ) r;
$$;

-- Replace semantics per symbol: the fetch returns the company's whole recent
-- history, so anything not in it has been withdrawn or amended away.
create or replace function public.replace_symbol_insiders(
    p_symbol text, p_rows jsonb)
returns integer
language plpgsql volatile security definer
set search_path = ledger, pg_temp
as $$
declare n integer := 0; sym text := upper(trim(coalesce(p_symbol, '')));
begin
  if sym !~ '^[A-Z][A-Z.\-]{0,9}$' then
    return 0;
  end if;

  delete from ledger.symbol_insider where symbol = sym;

  if p_rows is not null and jsonb_typeof(p_rows) = 'array' then
    insert into ledger.symbol_insider
      (symbol, filed, person, side, shares, price, amount, title, traded)
    select distinct on (sym, (r->>'filed')::date, coalesce(r->>'person', '—'),
                        coalesce(r->>'side', '?'),
                        coalesce((r->>'shares')::double precision, 0))
           sym,
           (r->>'filed')::date,
           coalesce(nullif(trim(r->>'person'), ''), '—'),
           coalesce(nullif(trim(r->>'side'), ''), '?'),
           coalesce((r->>'shares')::double precision, 0),
           nullif(r->>'price', '')::double precision,
           nullif(r->>'amount', '')::double precision,
           nullif(trim(r->>'title'), ''),
           nullif(r->>'traded', '')::date
    from jsonb_array_elements(p_rows) r
    where (r->>'filed') is not null
      and (r->>'filed') ~ '^\d{4}-\d{2}-\d{2}';
    get diagnostics n = row_count;
  end if;

  update ledger.insider_request set done_at = now() where symbol = sym;
  return n;
end;
$$;

-- What the company page reads. No materiality floor: this is one company's
-- own filings, where a small open-market purchase by an officer is signal
-- rather than noise. Falls back to the market-wide table until the first
-- per-symbol fetch lands, so nothing goes blank waiting for the worker.
create or replace function public.get_symbol_insiders(
    p_symbol text, p_days integer default 90)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  with me as (select upper(trim(p_symbol)) as symbol),
  win as (select current_date
                 - greatest(1, least(coalesce(p_days, 90), 365)) as cut),
  own as (
    select jsonb_agg(jsonb_build_object(
             'filed', s.filed, 'traded', s.traded, 'person', s.person,
             'title', s.title, 'side', s.side, 'shares', s.shares,
             'price', s.price, 'amount', s.amount)
           order by s.filed desc, abs(coalesce(s.amount, 0)) desc) as rows,
           max(s.updated_at) as fetched
    from ledger.symbol_insider s, me, win
    where s.symbol = me.symbol and s.filed >= win.cut
  ),
  fallback as (
    select jsonb_agg(jsonb_build_object(
             'filed', i.filed, 'traded', null, 'person', i.person,
             'title', i.title, 'side', i.side, 'shares', i.shares,
             'price', null, 'amount', i.amount)
           order by i.filed desc, abs(coalesce(i.amount, 0)) desc) as rows
    from ledger.insider_trade i, me, win
    where i.symbol = me.symbol and i.filed >= win.cut
  )
  select jsonb_build_object(
    'symbol', (select symbol from me),
    'trades', coalesce((select rows from own),
                       (select rows from fallback), '[]'::jsonb),
    -- The page needs to know which of the two it is looking at: the fallback
    -- is filtered to $1M filings and cannot be read as a complete record.
    'complete', (select rows is not null from own),
    'fetchedAt', (select fetched from own));
$$;

do $$
begin
  execute 'revoke all on function public.get_prices(text) from public';
  execute 'grant execute on function public.get_prices(text) to anon, authenticated';

  execute 'revoke all on function public.get_symbol_insiders(text, integer) from public';
  execute 'grant execute on function public.get_symbol_insiders(text, integer) to anon, authenticated';

  execute 'revoke all on function public.request_symbol_insiders(text) from public';
  execute 'grant execute on function public.request_symbol_insiders(text) to anon, authenticated';

  execute 'revoke all on function public.pending_symbol_insiders(integer) from public, anon, authenticated';
  execute 'grant execute on function public.pending_symbol_insiders(integer) to service_role';

  execute 'revoke all on function public.replace_symbol_insiders(text, jsonb) from public, anon, authenticated';
  execute 'grant execute on function public.replace_symbol_insiders(text, jsonb) to service_role';
end $$;
