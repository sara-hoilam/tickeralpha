-- Ticker Alpha — which charts are behind, and why
--
-- Apply after 0082. Safe to re-run.
--
-- 0082 asked the queue why a refresh had not landed. The answer came back
-- "nothing is waiting, nothing has failed" while charts stood still on 14
-- August, which rules the queue out and points at the fetch: a request can be
-- marked done without any bars having been written. So the question worth
-- asking is not what is queued but what is stale, and that is a property of
-- price_daily rather than of price_request.
--
-- stale_price_symbols answers it directly: every cached symbol whose newest
-- bar is behind the last session, how far behind, when its bars were last
-- written (updated_at only moves on a bars write, so it dates the last fetch
-- that returned anything), and whether a request is waiting for it.
--
-- price_queue_state gains a symbol filter, so one ticker can be asked about
-- by name instead of paged for.

drop function if exists public.price_queue_state(integer);

create or replace function public.price_queue_state(
    p_limit integer default 20, p_symbol text default null)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  with want as (select nullif(upper(trim(coalesce(p_symbol, ''))), '') as sym)
  select jsonb_build_object(
    'waiting', (select count(*) from ledger.price_request where done_at is null),
    'stuck',   (select count(*) from ledger.price_request
                where done_at is null and attempts >= 2),
    'rows', coalesce((
      select jsonb_agg(jsonb_build_object(
               'symbol', x.symbol,
               'requestedAt', x.requested_at,
               'doneAt', x.done_at,
               'attempts', x.attempts,
               'lastTry', x.last_try,
               'lastError', x.last_error,
               'barsAsOf', x.as_of,
               'barsUpdatedAt', x.updated_at)
             order by x.done_at nulls first, x.attempts desc, x.requested_at)
      from (
        select r.symbol, r.requested_at, r.done_at, r.attempts, r.last_try,
               r.last_error, d.as_of, d.updated_at
        from ledger.price_request r
        left join ledger.price_daily d on d.symbol = r.symbol, want
        where want.sym is null or r.symbol = want.sym
        order by r.done_at nulls first, r.attempts desc, r.requested_at
        limit greatest(1, least(coalesce(p_limit, 20), 200))
      ) x), '[]'::jsonb));
$$;

-- Every cached chart that is behind the last session, worst first.
create or replace function public.stale_price_symbols(p_limit integer default 50)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  select jsonb_build_object(
    'lastSession', ledger.last_session(),
    'cached', (select count(*) from ledger.price_daily),
    'behind', (select count(*) from ledger.price_daily d
               where d.as_of is null or d.as_of < ledger.last_session()),
    'rows', coalesce((
      select jsonb_agg(jsonb_build_object(
               'symbol', x.symbol,
               'asOf', x.as_of,
               'sessionsBehind', x.behind,
               'writtenAt', x.updated_at,
               'queued', x.queued)
             order by x.as_of nulls first, x.symbol)
      from (
        select d.symbol, d.as_of, d.updated_at,
               (select count(*) from generate_series(
                  coalesce(d.as_of, ledger.last_session() - 30) + 1,
                  ledger.last_session(), interval '1 day') g
                where extract(isodow from g) < 6) as behind,
               exists (select 1 from ledger.price_request r
                       where r.symbol = d.symbol and r.done_at is null) as queued
        from ledger.price_daily d
        where d.as_of is null or d.as_of < ledger.last_session()
        order by d.as_of nulls first, d.symbol
        limit greatest(1, least(coalesce(p_limit, 50), 500))
      ) x), '[]'::jsonb));
$$;

do $$
begin
  execute 'revoke all on function public.price_queue_state(integer, text) from public, anon, authenticated';
  execute 'grant execute on function public.price_queue_state(integer, text) to service_role';

  execute 'revoke all on function public.stale_price_symbols(integer) from public, anon, authenticated';
  execute 'grant execute on function public.stale_price_symbols(integer) to service_role';
end $$;
