-- Ticker Alpha — catalysts, and a memory of what has already been posted
--
-- Apply after 0078. Safe to re-run.
--
-- Two faults in the daily scan; this carries what the scoring needs for both.
--
-- The ideas repeated. ledger.alpha_pick has stored all nine of each day's
-- ideas since 0073, but the only read over it, get_alpha_track_record,
-- filters to `is_pick` -- so the cooldown the scan applies could only ever
-- see the one headline name. The eight candidates beneath it were free to
-- return the next morning, and did. alpha_recent_symbols reports every
-- symbol the table holds in a window, with the last day it appeared at all
-- and the last day it led, so a name can be held back for a week whatever
-- slot it filled.
--
-- The ideas were also slow. Every family the score blends -- drawdown, P/E,
-- revenue trend, consensus targets -- moves at the pace of quarters, so the
-- ranking that produced today's board mostly produced yesterday's too.
-- Nothing in it knew what had happened this week. These are the inputs a
-- catalyst family needs:
--
--   news3          headlines from the last three days, so significance can
--                  be read off the words rather than a seven-day tone count
--   lastEarnings   the most recent print inside ten days, with the estimate
--                  it was measured against
--   changePct      the daily move, which quote_detail already stores
--   move3d         three sessions, for names whose daily bars are cached,
--                  shipped with the date it ends on so a stale cache cannot
--                  masquerade as a recent move
--
-- Everything else is 0075's function unchanged.

-- ---------------------------------------------------------------------------
-- What has been posted lately -- every slot, not just the headline
-- ---------------------------------------------------------------------------
create or replace function public.alpha_recent_symbols(p_days integer default 14)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  select coalesce(jsonb_agg(jsonb_build_object(
           'symbol',      x.symbol,
           'lastDay',     x.last_day,
           'lastPickDay', x.last_pick_day,
           'appearances', x.n)
         order by x.last_day desc), '[]'::jsonb)
  from (
    select a.symbol,
           max(a.day)                          as last_day,
           max(a.day) filter (where a.is_pick) as last_pick_day,
           count(*)                            as n
    from ledger.alpha_pick a
    where a.day >= current_date
                 - greatest(1, least(coalesce(p_days, 14), 120))
    group by a.symbol
  ) x;
$$;

-- ---------------------------------------------------------------------------
-- Scan inputs, with what is happening now
-- ---------------------------------------------------------------------------
create or replace function public.alpha_scan_inputs(
  p_offset integer default 0, p_limit integer default 120)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  with uni as (
    select h.symbol,
           max(h.name)     filter (where h.index_symbol = 'SPX') as spx_name,
           max(h.name)                                           as any_name,
           max(h.industry) filter (where h.index_symbol = 'SPX') as spx_ind,
           max(h.industry)                                       as any_ind,
           bool_or(h.index_symbol = 'SPX')  as in_spx,
           bool_or(h.index_symbol = 'IXIC') as in_ndx
    from ledger.index_holding h
    where h.index_symbol in ('SPX', 'IXIC')
      and h.symbol ~ '^[A-Z][A-Z.\-]{0,9}$'
    group by h.symbol
    order by h.symbol
    offset greatest(0, coalesce(p_offset, 0))
    limit  greatest(1, least(coalesce(p_limit, 120), 200))
  ),
  ins as (
    select i.symbol,
           count(*)             filter (where i.side = 'Buy')  as buy_n,
           sum(abs(coalesce(i.amount, 0)))
                                filter (where i.side = 'Buy')  as buy_amt,
           count(distinct i.person)
                                filter (where i.side = 'Buy')  as buyers,
           count(*)             filter (where i.side = 'Sell') as sell_n,
           sum(abs(coalesce(i.amount, 0)))
                                filter (where i.side = 'Sell') as sell_amt,
           count(distinct i.person)
                                filter (where i.side = 'Sell'
                                          and abs(coalesce(i.amount, 0)) > 500000)
                                                               as big_sellers
    from ledger.insider_trade i
    where i.filed >= current_date - 30
      and i.symbol in (select symbol from uni)
    group by i.symbol
  ),
  itr as (
    select t.symbol, jsonb_agg(jsonb_build_object(
             'filed', t.filed, 'side', t.side,
             'amount', t.amount, 'person', t.person)
           order by abs(coalesce(t.amount, 0)) desc) as trades
    from (
      select i.symbol, i.filed, i.side, i.amount, i.person,
             row_number() over (partition by i.symbol
                                order by abs(coalesce(i.amount, 0)) desc) as rn
      from ledger.insider_trade i
      where i.filed >= current_date - 30
        and i.symbol in (select symbol from uni)
    ) t
    where t.rn <= 8
    group by t.symbol
  ),
  cg as (
    select c.symbol,
           jsonb_agg(jsonb_build_object(
             'traded', c.traded, 'disclosed', c.disclosed,
             'person', c.person, 'chamber', c.chamber,
             'side', c.side, 'amount', c.amount)
           order by coalesce(c.disclosed, c.traded) desc) as trades
    from ledger.congress_trade c
    where coalesce(c.disclosed, c.traded) >= current_date - 90
      and c.symbol in (select symbol from uni)
    group by c.symbol
  ),
  nw as (
    select n.symbol,
           count(*) as stories,
           jsonb_agg(n.title order by n.published desc)
             filter (where n.title is not null) as titles
    from ledger.news n
    where n.published >= now() - interval '7 days'
      and n.symbol in (select symbol from uni)
    group by n.symbol
  ),
  -- The catalyst window. Seven days of tone is the right length for a gate
  -- (a fortnight of bad press is a condition, not an event) and far too long
  -- to call something news.
  nw3 as (
    select n.symbol,
           count(*) as stories,
           jsonb_agg(n.title order by n.published desc)
             filter (where n.title is not null) as titles
    from ledger.news n
    where n.published >= now() - interval '3 days'
      and n.symbol in (select symbol from uni)
    group by n.symbol
  ),
  le as (
    select distinct on (e.symbol)
           e.symbol, e.date, e.eps_actual, e.eps_estimated
    from ledger.earnings_event e
    where e.date < current_date
      and e.date >= current_date - 10
      and e.symbol in (select symbol from uni)
    order by e.symbol, e.date desc
  ),
  ee as (
    select e.symbol, min(e.date) as next_earnings
    from ledger.earnings_event e
    where e.date >= current_date
      and e.symbol in (select symbol from uni)
    group by e.symbol
  )
  select coalesce(jsonb_agg(jsonb_build_object(
    'symbol',   u.symbol,
    'name',     coalesce(u.spx_name, u.any_name),
    'industry', coalesce(u.spx_ind, u.any_ind, pd.industry),
    'sector',   pd.sector,
    'inSpx',    u.in_spx,
    'inNdx',    u.in_ndx,

    'quote', case when q.symbol is null then null else jsonb_build_object(
      'price', q.price, 'pe', q.pe, 'marketCap', q.market_cap,
      'yearHigh', q.year_high, 'yearLow', q.year_low,
      'avg50', q.avg_50, 'avg200', q.avg_200,
      'changePct', q.change_pct, 'previousClose', q.previous_close,
      'updatedAt', q.updated_at) end,

    'insider', case when i.symbol is null then null else jsonb_build_object(
      'buyN', i.buy_n, 'buyAmt', i.buy_amt, 'buyers', i.buyers,
      'sellN', i.sell_n, 'sellAmt', i.sell_amt,
      'bigSellers', i.big_sellers) end,

    'insiderTrades', itr.trades,

    'congress', cg.trades,

    'news', case when nw.symbol is null then null else jsonb_build_object(
      'stories', nw.stories, 'titles', nw.titles) end,

    'news3', case when nw3.symbol is null then null else jsonb_build_object(
      'stories', nw3.stories, 'titles', nw3.titles) end,

    'analyst', case when an.symbol is null then null else jsonb_build_object(
      'target', an.target,
      'consensus', an.consensus,
      'downgrades30', (
        select count(*) from jsonb_array_elements(coalesce(an.grades, '[]'::jsonb)) g
        where lower(g->>'action') like 'down%'
          and (g->>'date') >= (current_date - 30)::text),
      'upgrades30', (
        select count(*) from jsonb_array_elements(coalesce(an.grades, '[]'::jsonb)) g
        where lower(g->>'action') like 'up%'
          and (g->>'date') >= (current_date - 30)::text)) end,

    'analystTargets', (
      select jsonb_agg(jsonb_build_object(
               'house', x.house, 'analyst', x.analyst,
               'target', x.target, 'published', x.published))
      from (
        select e.value->>'house' as house,
               e.value->>'analyst' as analyst,
               nullif(e.value->>'target','')::double precision as target,
               e.value->>'published' as published
        from jsonb_array_elements(coalesce(an.news, '[]'::jsonb)) e
        where nullif(e.value->>'target','') is not null
        order by e.value->>'published' desc nulls last
        limit 6
      ) x),

    'nextEarnings', ee.next_earnings,
    'lastEarnings', case when le.symbol is null then null else jsonb_build_object(
      'date', le.date, 'epsActual', le.eps_actual,
      'epsEstimated', le.eps_estimated) end,

    'revenue', rv.quarters,

    -- Three sessions of price, and the day it ends on: a move read off a
    -- stale cache is not a recent move, so the scan checks before using it.
    'move3d',     mv.move3d,
    'move3dAsOf', mv.as_of,

    'peHistory', pd.pe_history,
    'monthly',   pd.monthly,

    'longCloses', case when lc.symbol is null then null else jsonb_build_object(
      'from', lc.first_day, 'to', lc.last_day,
      'updatedAt', lc.updated_at) end
  ) order by u.symbol), '[]'::jsonb)
  from uni u
  left join ledger.quote_detail q on q.symbol = u.symbol
  left join ledger.price_daily pd on pd.symbol = u.symbol
  left join ins i  on i.symbol  = u.symbol
  left join itr    on itr.symbol = u.symbol
  left join cg     on cg.symbol = u.symbol
  left join nw     on nw.symbol = u.symbol
  left join nw3    on nw3.symbol = u.symbol
  left join ledger.analyst an on an.symbol = u.symbol
  left join ee     on ee.symbol = u.symbol
  left join le     on le.symbol = u.symbol
  left join ledger.price_close_long lc on lc.symbol = u.symbol
  left join lateral (
    select case when count(*) = 4 and min(b.c) > 0
                then (max(b.c) filter (where b.rn = 4)
                      / max(b.c) filter (where b.rn = 1) - 1) * 100 end as move3d,
           max(b.d) as as_of
    from (
      select (e.value->>'c')::double precision as c,
             e.value->>'d' as d,
             row_number() over (order by e.ord) as rn
      from jsonb_array_elements(pd.bars) with ordinality e(value, ord)
      where e.ord > jsonb_array_length(pd.bars) - 4
    ) b
  ) mv on true
  left join lateral (
    select jsonb_agg(jsonb_build_object('e', x.period_end, 'r', x.rev)
                     order by x.period_end) as quarters
    from (
      select qt.period_end, (qt.lines->>'revenue')::double precision as rev
      from ledger.company c
      join ledger.quarter qt on qt.cik = c.cik
      where upper(c.ticker) = u.symbol
        and qt.lines->>'revenue' is not null
      order by qt.period_end desc
      limit 13
    ) x
  ) rv on true;
$$;

do $$
begin
  execute 'revoke all on function public.alpha_recent_symbols(integer) from public, anon, authenticated';
  execute 'grant execute on function public.alpha_recent_symbols(integer) to service_role';

  execute 'revoke all on function public.alpha_scan_inputs(integer, integer) from public, anon, authenticated';
  execute 'grant execute on function public.alpha_scan_inputs(integer, integer) to service_role';
end $$;
