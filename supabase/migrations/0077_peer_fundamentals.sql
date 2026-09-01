-- Ticker Alpha — filed fundamentals in the peer comparison
--
-- Apply after 0076. Safe to re-run.
--
-- Two gaps the Industry tab could not close from the market feed alone:
--
--   *  A company whose quote carries no P/E reads as missing data, when the
--      truth is usually that it earns nothing to divide by. Shipping the
--      trailing diluted EPS from its own filings lets the page compute the
--      ratio where one exists and say "not meaningful" where it does not.
--
--   *  A peer nobody has ever opened has no filings cached at all, so its
--      revenue columns and its place on the revenue chart are blank with no
--      way to tell that apart from a company that files nothing. A count of
--      filed quarters lets the page ask the backfill queue for the ones it
--      is simply missing.
--
-- Same shape as 0076 otherwise; only the two fields are new.

create or replace function public.get_peer_compare(p_symbols text[])
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  with syms as (
    select upper(trim(u.s)) as symbol, min(u.ord) as ord
    from unnest(p_symbols) with ordinality u(s, ord)
    where trim(coalesce(u.s, '')) <> ''
    group by upper(trim(u.s))
    order by min(u.ord)
    limit 7
  )
  select coalesce(jsonb_agg(jsonb_build_object(
    'symbol',   s.symbol,
    'name',     coalesce(q.name, t.name),
    'sector',   pd.sector,
    'industry', pd.industry,
    'quote', case when q.symbol is null then null else jsonb_build_object(
      'price',     q.price,
      'changePct', q.change_pct,
      'cap',       q.market_cap,
      'pe',        q.pe,
      'pb',        q.pb,
      'divYield',  q.dividend_yield,
      'yearHigh',  q.year_high,
      'yearLow',   q.year_low,
      'avg50',     q.avg_50,
      'avg200',    q.avg_200) end,
    'closes',       wc.closes,
    'revenue',      rv.quarters,
    -- Trailing twelve months of diluted EPS, straight from the filings, so a
    -- P/E can be formed for a company the quote feed leaves blank -- and a
    -- negative figure can be reported as a loss rather than as no data.
    'epsTtm',       ep.eps_ttm,
    'filedQuarters', coalesce(fq.n, 0),
    'target', case when an.symbol is null then null else jsonb_build_object(
      'median',    an.target->'median',
      'consensus', an.target->'consensus',
      'rating',    an.consensus->>'rating') end,
    'nextEarnings', ee.next_earnings,
    'warm',         (pd.symbol is not null)
  ) order by s.ord), '[]'::jsonb)
  from syms s
  left join ledger.quote_detail q on q.symbol = s.symbol
  left join ledger.price_daily pd on pd.symbol = s.symbol
  left join ledger.ticker t       on t.ticker = s.symbol
  left join ledger.analyst an     on an.symbol = s.symbol
  left join lateral (
    select min(e.date) as next_earnings from ledger.earnings_event e
    where e.symbol = s.symbol and e.date >= current_date) ee on true
  left join lateral (
    -- every fifth daily bar of the last ~252, plus the newest bar so the
    -- line always ends at the last close the cache holds
    select jsonb_agg(jsonb_build_object('d', b.d, 'c', b.c) order by b.ord) as closes
    from (
      select e.value->>'d' as d, (e.value->>'c')::double precision as c, e.ord
      from jsonb_array_elements(pd.bars) with ordinality e(value, ord)
      where e.ord > jsonb_array_length(pd.bars) - 252
        and ((e.ord - 1) % 5 = 0 or e.ord = jsonb_array_length(pd.bars))
    ) b
    where b.c is not null
  ) wc on true
  left join lateral (
    select jsonb_agg(jsonb_build_object('e', x.period_end, 'r', x.rev)
                     order by x.period_end) as quarters
    from (
      select qt.period_end, (qt.lines->>'revenue')::double precision as rev
      from ledger.company c
      join ledger.quarter qt on qt.cik = c.cik
      where upper(c.ticker) = s.symbol
        and qt.lines->>'revenue' is not null
      order by qt.period_end desc
      limit 9
    ) x
  ) rv on true
  left join lateral (
    -- Four quarters or nothing: three of them summed is not a trailing year,
    -- and a company that tags EPS only in its annual report would otherwise
    -- get a ratio computed from a quarter of the earnings.
    select case when count(*) = 4 then sum(x.eps) end as eps_ttm
    from (
      select (qt.lines->>'epsDiluted')::double precision as eps
      from ledger.company c
      join ledger.quarter qt on qt.cik = c.cik
      where upper(c.ticker) = s.symbol
        and qt.lines->>'epsDiluted' is not null
      order by qt.period_end desc
      limit 4
    ) x
  ) ep on true
  left join lateral (
    select count(*)::int as n
    from ledger.company c
    join ledger.quarter qt on qt.cik = c.cik
    where upper(c.ticker) = s.symbol
  ) fq on true;
$$;

do $$
begin
  execute 'revoke all on function public.get_peer_compare(text[]) from public';
  execute 'grant execute on function public.get_peer_compare(text[]) to anon, authenticated';
end $$;
