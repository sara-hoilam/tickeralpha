-- Ticker Alpha — let a price re-request through after an hour, not twelve
--
-- Apply after 0063. Safe to re-run.
--
-- Every upsert_prices call marks the symbol's request done -- including the
-- empty write the worker makes when FMP refuses it -- and request_prices only
-- cleared done_at after twelve hours. During the August bandwidth outage that
-- combination froze every attempted symbol at its last good refresh: pages
-- kept re-asking, the twelve-hour rule kept refusing, and the queue sat empty
-- while the worker's timed jobs ran beside it. META stuck at Aug 6, F at
-- Aug 5, each symbol wherever its last success happened to fall.
--
-- The twelve-hour rule exists so a page polling get_prices does not spam the
-- worker with refetches of data it just fetched. One hour keeps that
-- protection -- the page polls for two minutes, not sixty -- and caps how
-- long a symbol can be held hostage by a failed attempt.

create or replace function public.request_prices(p_symbol text)
returns jsonb
language plpgsql volatile security definer
set search_path = ledger, pg_temp
as $$
declare sym text := upper(trim(coalesce(p_symbol, '')));
begin
  if sym !~ '^[A-Z][A-Z.\-]{0,9}$' then
    return jsonb_build_object('queued', false, 'reason', 'not a ticker');
  end if;

  insert into ledger.price_request (symbol, requested_at, done_at)
  values (sym, now(), null)
  on conflict (symbol) do update
    -- Re-asking within the hour is the page polling, not a new request.
    set requested_at = case
          when ledger.price_request.requested_at < now() - interval '1 hour'
            or ledger.price_request.done_at is not null
          then now() else ledger.price_request.requested_at end,
        done_at = case
          when ledger.price_request.done_at < now() - interval '1 hour'
          then null else ledger.price_request.done_at end;

  return jsonb_build_object('queued', true, 'symbol', sym);
end;
$$;

-- One-time: free everything the outage locked. The worker refetches each
-- symbol once; with the limit-aware drain pause now in the worker, a repeat
-- outage pauses politely instead of consuming the queue again.
update ledger.price_request set done_at = null
 where done_at is not null;

update ledger.long_close_request set done_at = null
 where done_at is not null;

do $$
begin
  execute 'revoke all on function public.request_prices(text) from public';
  execute 'grant execute on function public.request_prices(text) to anon, authenticated';
end $$;
