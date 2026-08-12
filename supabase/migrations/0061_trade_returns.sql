-- Ticker Alpha — what a disclosed trade's stock did since the trade date
--
-- Apply after 0060. Safe to re-run.
--
-- The Insider & Congress profile wants one number per row: how the stock has
-- moved since the day it was traded. The page has no way to ask for that
-- today. get_prices answers for a single symbol and hands back the whole
-- 300-bar OHLCV array to do it -- and one member's ninety days can span
-- sixty-one distinct tickers, so the honest client-side version is sixty-one
-- round trips and the better part of a megabyte to render one column.
--
-- This takes the (symbol, date) pairs the table is actually showing and
-- returns three numbers for each: the close on or before the trade date, the
-- latest close, and the percent between them. Pairs rather than symbols
-- because the same ticker is often traded on several days, and each row wants
-- its own baseline.
--
-- On or before, not on: trades are disclosed with calendar dates, and a trade
-- dated on a weekend or a holiday has no bar of its own. The last close before
-- it is the price that stood when the trade was made.

create or replace function public.get_trade_returns(p_pairs jsonb)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  with pairs as (
    select distinct
           upper(trim(x->>'s')) as symbol,
           (x->>'d')::date      as on_date
    from jsonb_array_elements(coalesce(p_pairs, '[]'::jsonb)) x
    where coalesce(trim(x->>'s'), '') ~ '^[A-Za-z][A-Za-z.\-]{0,9}$'
      and (x->>'d') ~ '^\d{4}-\d{2}-\d{2}$'
    limit 400                       -- a page cannot show more than this
  ),
  -- Explode each needed symbol once, not once per pair.
  allbars as (
    select s.symbol,
           (b->>'d')::date             as d,
           (b->>'c')::double precision as c
    from (select distinct symbol from pairs) s
    join ledger.price_daily pd on pd.symbol = s.symbol
    cross join lateral jsonb_array_elements(pd.bars) b
    where (b->>'c') is not null
  ),
  last_close as (
    select distinct on (symbol) symbol, c, d
    from allbars
    order by symbol, d desc
  ),
  base as (
    select distinct on (p.symbol, p.on_date) p.symbol, p.on_date, a.c, a.d
    from pairs p
    join allbars a on a.symbol = p.symbol and a.d <= p.on_date
    order by p.symbol, p.on_date, a.d desc
  )
  select coalesce(jsonb_object_agg(t.k, t.v), '{}'::jsonb)
  from (
    select b.symbol || '|' || to_char(b.on_date, 'YYYY-MM-DD') as k,
           jsonb_build_object(
             'base', b.c,
             'baseOn', b.d,          -- the bar actually used, for the tooltip
             'last', l.c,
             'lastOn', l.d,
             'ret', case when b.c > 0 then (l.c - b.c) / b.c * 100.0 end
           ) as v
    from base b
    join last_close l on l.symbol = b.symbol
  ) t;
$$;

do $$
begin
  execute 'revoke all on function public.get_trade_returns(jsonb) from public';
  execute 'grant execute on function public.get_trade_returns(jsonb) to anon, authenticated';
end $$;
