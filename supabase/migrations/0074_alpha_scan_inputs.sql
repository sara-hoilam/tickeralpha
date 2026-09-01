-- Ticker Alpha — everything the nightly Alpha of the Day scan reads, in bulk
--
-- Apply after 0073. Safe to re-run.
--
-- The scan (alpha.py) scores the S&P 500 + Nasdaq-100 universe every trading
-- morning. Almost all of its raw material is already in the ledger — index
-- membership, quotes, insider and congress tapes, news, analyst actions,
-- earnings dates, EDGAR revenue quarters — but the page-facing read functions
-- serve one symbol at a time, which would cost five hundred round trips.
-- These functions serve the same facts shaped for the scan: one paged call
-- for the whole universe, one bulk call for the shortlist's price history.
-- All of it is service_role only; the page keeps reading 0073's functions.

-- ---------------------------------------------------------------------------
-- The universe, with per-symbol aggregates
-- ---------------------------------------------------------------------------
-- Paged (order by symbol) so no single response has to carry ~530 rows of
-- jsonb through PostgREST's statement timeout. Aggregates are grouped in one
-- pass per source table rather than per-symbol laterals — the trade, news and
-- earnings tables are small, but 530 probes each would still be the slow way
-- around. Missing pieces come back null: "we have no analyst rows for this
-- name" is an answer the scan handles, not an error.
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
      'updatedAt', q.updated_at) end,

    'insider', case when i.symbol is null then null else jsonb_build_object(
      'buyN', i.buy_n, 'buyAmt', i.buy_amt, 'buyers', i.buyers,
      'sellN', i.sell_n, 'sellAmt', i.sell_amt,
      'bigSellers', i.big_sellers) end,

    'congress', cg.trades,

    'news', case when nw.symbol is null then null else jsonb_build_object(
      'stories', nw.stories, 'titles', nw.titles) end,

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

    'nextEarnings', ee.next_earnings,

    'revenue', rv.quarters,

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
  left join cg     on cg.symbol = u.symbol
  left join nw     on nw.symbol = u.symbol
  left join ledger.analyst an on an.symbol = u.symbol
  left join ee     on ee.symbol = u.symbol
  left join ledger.price_close_long lc on lc.symbol = u.symbol
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

-- ---------------------------------------------------------------------------
-- Decade closes for the shortlist
-- ---------------------------------------------------------------------------
-- The drawdown-rarity family needs the full daily series, which is far too
-- heavy to ship for the whole universe. The scan asks for its ~50 shortlisted
-- names only. Same content as public.get_long_closes, minus the staleness
-- decoration and gated to the worker.
create or replace function public.alpha_long_history(p_symbols text[])
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  select coalesce(jsonb_object_agg(l.symbol, l.closes), '{}'::jsonb)
  from ledger.price_close_long l
  where l.symbol = any (
    select upper(trim(s))
    from unnest(coalesce(p_symbols, '{}'::text[])) s
    limit 80);
$$;

-- ---------------------------------------------------------------------------
-- Has today's scan already run?  Which picks still need a result?
-- ---------------------------------------------------------------------------
create or replace function public.alpha_latest_day()
returns date
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  select max(day) from ledger.alpha_pick;
$$;

-- Headline picks whose next-session return has not been recorded yet. The
-- scan resolves these each morning once the following close exists.
create or replace function public.alpha_unresolved(p_max_days integer default 21)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  select coalesce(jsonb_agg(jsonb_build_object(
           'day', a.day, 'symbol', a.symbol,
           'side', a.side, 'price', a.price)
         order by a.day), '[]'::jsonb)
  from ledger.alpha_pick a
  where a.is_pick
    and a.result_pct is null
    and a.day <  current_date
    and a.day >= current_date - greatest(1, least(coalesce(p_max_days, 21), 90));
$$;

do $$
begin
  execute 'revoke all on function public.alpha_scan_inputs(integer, integer) from public, anon, authenticated';
  execute 'grant execute on function public.alpha_scan_inputs(integer, integer) to service_role';

  execute 'revoke all on function public.alpha_long_history(text[]) from public, anon, authenticated';
  execute 'grant execute on function public.alpha_long_history(text[]) to service_role';

  execute 'revoke all on function public.alpha_latest_day() from public, anon, authenticated';
  execute 'grant execute on function public.alpha_latest_day() to service_role';

  execute 'revoke all on function public.alpha_unresolved(integer) from public, anon, authenticated';
  execute 'grant execute on function public.alpha_unresolved(integer) to service_role';
end $$;
