-- Industry-level heatmap.
--
-- ledger.heatmap has carried an `industry` column since 0007, but
-- public.get_heatmap() groups by sector and never returns it, so the browser
-- could only ever draw the sector treemap. This adds the industry grouping the
-- drill-down needs, from the rows already being written -- no new FMP calls.
--
-- One call serves all three views: the compact strip on the market page, the
-- full industry treemap, and a single industry's constituents. The universe is
-- a few hundred names, so shipping the items with the groups costs less than a
-- second round trip per industry.
--
-- `change_pct` per industry is CAP-WEIGHTED, not a plain average: an industry's
-- move is what its money did, and a simple mean lets a micro-cap swing it as
-- hard as a trillion-dollar name. Note this differs from FMP's
-- industry-performance-snapshot, which reports an unweighted averageChange.

create or replace function public.get_industry_heatmap()
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  select coalesce(jsonb_agg(t.s order by t.cap desc), '[]'::jsonb)
  from (
    select jsonb_build_object(
             'industry', industry,
             'sector', min(sector),
             'marketCap', sum(market_cap),
             -- sum(cap * chg) / sum(cap), guarded against a zero denominator.
             'changePct', case
               when sum(market_cap) > 0 then
                 sum(market_cap * coalesce(change_pct, 0)) / sum(market_cap)
               else null end,
             'count', count(*),
             'items', jsonb_agg(jsonb_build_object(
                        'symbol', symbol, 'name', name, 'sector', sector,
                        'marketCap', market_cap, 'price', price,
                        'changePct', change_pct)
                      order by market_cap desc)) as s,
           sum(market_cap) as cap
    from ledger.heatmap
    where market_cap is not null
      and industry is not null
      and industry <> ''
    group by industry
  ) t;
$$;

revoke all on function public.get_industry_heatmap() from public;
grant execute on function public.get_industry_heatmap() to anon, authenticated;

comment on function public.get_industry_heatmap() is
  'Heatmap grouped by industry: cap-weighted day change, total market cap and '
  'the constituent rows, largest cap first. Drives the industry treemap and '
  'the per-industry drill-down.';
