-- Ticker Alpha — insider leaders per window, and a share-weighted price
--
-- Apply after 0058_politicians. Safe to re-run.
--
-- The Insider & Congress page draws a bar chart and a table from what is
-- meant to be the same set of companies, and they did not agree. Two reasons,
-- both fixed here.
--
-- The chart was aggregated in the browser from get_trades, which pages 100
-- rows at a time and was asked for 600. 5,851 filings clear the materiality
-- floor in ninety days, and get_trades returns them newest first, so those 600
-- covered six days -- every one of the 7D/15D/30D/60D/90D buttons redrew the
-- same six days. Companies whose last filing fell outside that sliver, which
-- is most of them, could not appear on the chart at all.
--
-- The table meanwhile called get_insider_leaders, which had no period argument
-- (it aggregated the whole stored table) and no materiality floor, while
-- get_trades applies abs(amount) > $1M or shares >= 1% of shares outstanding.
-- So even at 90D the two panels were counting different filings.
--
-- get_insider_leaders now takes p_days and applies the same floor get_trades
-- uses, so one call serves both panels and the period filter means the same
-- thing on each. It also returns a share-weighted average price per company,
-- which the expanded per-person breakdown shows alongside each filer.

create or replace function public.get_insider_leaders(
  p_limit integer default 15,
  p_days  integer default 90
)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  select coalesce(jsonb_agg(jsonb_build_object(
           'symbol', x.symbol, 'name', x.name,
           'filings', x.filings, 'buyAmt', x.buy_amt, 'sellAmt', x.sell_amt,
           'people', x.people, 'lastFiled', x.last_filed,
           'shares', x.shares, 'price', x.price)
         order by x.buy_amt + x.sell_amt desc), '[]'::jsonb)
  from (
    select i.symbol,
           coalesce(max(t.name), max(c.name)) as name,
           count(*)                                        as filings,
           sum(greatest(coalesce(i.amount, 0), 0))         as buy_amt,
           sum(greatest(-coalesce(i.amount, 0), 0))        as sell_amt,
           count(distinct i.person)                        as people,
           max(i.filed)                                    as last_filed,
           sum(abs(coalesce(i.shares, 0)))                 as shares,
           -- Share-weighted, not the mean of the per-filing prices: one
           -- 2M-share disposal and one 100-share gift are not two equal
           -- opinions about what the stock was worth that week.
           case when sum(abs(coalesce(i.shares, 0))) > 0
                then sum(abs(coalesce(i.amount, 0)))
                     / sum(abs(coalesce(i.shares, 0)))
           end                                             as price
    from ledger.insider_trade i
    left join ledger.ticker  t on t.ticker = i.symbol
    left join ledger.company c on c.ticker = i.symbol
    where i.symbol is not null
      and i.filed >= current_date - greatest(1, least(coalesce(p_days, 90), 90))
      -- The same materiality test get_trades applies, so the chart and the
      -- table are drawn from one population.
      and (
        abs(coalesce(i.amount, 0)) > 1000000
        or (
          coalesce(i.shares_out, 0) > 0
          and coalesce(i.shares, 0) / i.shares_out >= 0.01
        )
      )
    group by i.symbol
    order by sum(abs(coalesce(i.amount, 0))) desc
    limit greatest(1, least(coalesce(p_limit, 15), 50))
  ) x;
$$;

do $$
begin
  -- New signature with p_days; drop the prior 1-arg overload if present, so a
  -- one-argument call cannot become ambiguous between the two.
  begin
    execute 'drop function if exists public.get_insider_leaders(integer)';
  exception when others then null;
  end;
  execute 'revoke all on function public.get_insider_leaders(integer, integer) from public';
  execute 'grant execute on function public.get_insider_leaders(integer, integer) to anon, authenticated';
end $$;
