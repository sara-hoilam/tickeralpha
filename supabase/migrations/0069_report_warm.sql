-- Ticker Alpha — which front-table tickers still need today's fetch
--
-- Apply after 0068. Safe to re-run.
--
-- The Markets Today tables (market cap, gainers, losers) are the front door:
-- most first clicks land on one of their tickers. Report data is fetched on
-- demand, so the first visitor of the day used to wait out the whole fetch.
-- The worker now warms those names itself, and this function is its memory:
-- given the candidate list, it answers with the subset whose report data is
-- missing or older than the page's own staleness threshold. Because the
-- answer comes from the database rather than worker state, a restart resumes
-- where it left off instead of re-fetching everything.

create or replace function public.reports_due(p_symbols jsonb,
                                              p_hours integer default 12)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  select coalesce(jsonb_agg(s.sym order by s.ord), '[]'::jsonb)
  from (
    -- Deduplicated but order-preserving: the caller lists names in priority
    -- order (largest caps first), and the batch that gets fetched is the
    -- front of this answer.
    select upper(trim(e.value)) as sym, min(e.ord) as ord
    from jsonb_array_elements_text(p_symbols) with ordinality e(value, ord)
    where upper(trim(e.value)) ~ '^[A-Z][A-Z.\-]{0,9}$'
    group by upper(trim(e.value))
  ) s
  left join ledger.price_daily d on d.symbol = s.sym
  where d.symbol is null
     or d.updated_at < now() - make_interval(hours => greatest(1, coalesce(p_hours, 12)));
$$;

do $$
begin
  execute 'revoke all on function public.reports_due(jsonb, integer) from public, anon, authenticated';
  execute 'grant execute on function public.reports_due(jsonb, integer) to service_role';
end $$;
