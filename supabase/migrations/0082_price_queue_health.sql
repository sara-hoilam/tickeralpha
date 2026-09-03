-- Ticker Alpha — a price queue that cannot be blocked by one bad symbol
--
-- Apply after 0081. Safe to re-run.
--
-- Lululemon's chart stopped on 14 August and stayed there, while the market
-- page and the nightly scan carried on. That combination is the tell: the
-- timed jobs were fine and the *queue* was not.
--
-- pending_prices is a strict FIFO -- `where done_at is null order by
-- requested_at limit 5` -- and a request leaves the queue only when the
-- worker marks it done. Two paths never mark it: an account-level refusal
-- (deliberately, so the retry survives a bandwidth pause) and any unexpected
-- exception (accidentally). Either way the row keeps its original
-- requested_at, which means it keeps the head of the queue. It is then
-- retried every twenty seconds, forever, and once five such rows accumulate
-- nothing else in the queue is ever reached again: every visitor's request is
-- accepted, queued, and starved behind them. Charts already cached keep
-- rendering, which is why the symptom reads as "some stocks lag" rather than
-- as an outage.
--
-- Three changes, all here plus a guard per symbol in the worker:
--
--   *  the queue records attempts, when it last tried, and why it failed
--   *  pending_prices orders by attempts before requested_at and skips a row
--      that was tried recently, so a chronic failure can never outrank a
--      fresh request and can never be retried in a tight loop
--   *  a row that has failed six times retires itself, keeping the error for
--      diagnosis; the page asking again gives it a fresh six

alter table ledger.price_request
  add column if not exists attempts   integer not null default 0,
  add column if not exists last_try   timestamptz,
  add column if not exists last_error text;

-- Fresh requests first, then whatever has failed least. A row is invisible
-- for two minutes per attempt it has already made, so a symbol that cannot be
-- fetched costs the queue one slot briefly rather than one slot permanently.
create or replace function public.pending_prices(p_limit integer default 5)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  select coalesce(jsonb_agg(r.symbol order by r.attempts, r.requested_at), '[]'::jsonb)
  from (
    select symbol, requested_at, attempts
    from ledger.price_request
    where done_at is null
      and (last_try is null
           or last_try < now() - (least(greatest(attempts, 1), 10)
                                  * interval '2 minutes'))
    order by attempts, requested_at
    limit greatest(1, least(coalesce(p_limit, 5), 25))
  ) r;
$$;

-- Called on every failed attempt, including the ones that used to vanish.
create or replace function public.note_price_attempt(
    p_symbol text, p_error text default null)
returns jsonb
language plpgsql volatile security definer
set search_path = ledger, pg_temp
as $$
declare sym text := upper(trim(coalesce(p_symbol, ''))); n integer;
begin
  update ledger.price_request
     set attempts   = attempts + 1,
         last_try   = now(),
         last_error = left(nullif(trim(coalesce(p_error, '')), ''), 300),
         -- Six failures is not a queue problem any more, it is this symbol.
         -- Retiring it keeps the queue moving; request_prices clears done_at
         -- an hour later, so a visitor asking again gives it a fresh six.
         done_at    = case when attempts + 1 >= 6 then now() else done_at end
   where symbol = sym
  returning attempts into n;
  return jsonb_build_object('symbol', sym, 'attempts', coalesce(n, 0),
                            'retired', coalesce(n, 0) >= 6);
end;
$$;

-- What the queue is actually doing, for the worker's `queue` command.
create or replace function public.price_queue_state(p_limit integer default 20)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
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
        left join ledger.price_daily d on d.symbol = r.symbol
        order by r.done_at nulls first, r.attempts desc, r.requested_at
        limit greatest(1, least(coalesce(p_limit, 20), 100))
      ) x), '[]'::jsonb));
$$;

do $$
begin
  execute 'revoke all on function public.pending_prices(integer) from public, anon, authenticated';
  execute 'grant execute on function public.pending_prices(integer) to service_role';

  execute 'revoke all on function public.note_price_attempt(text, text) from public, anon, authenticated';
  execute 'grant execute on function public.note_price_attempt(text, text) to service_role';

  execute 'revoke all on function public.price_queue_state(integer) from public, anon, authenticated';
  execute 'grant execute on function public.price_queue_state(integer) to service_role';
end $$;
