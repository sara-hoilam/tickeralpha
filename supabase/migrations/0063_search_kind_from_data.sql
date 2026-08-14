-- Ticker Alpha — the search takes each ticker's kind from the data
--
-- Apply after 0061. Subsumes 0062: this is the same function with one more
-- correction, so applying this alone is enough, and applying both in order
-- is also safe.
--
-- 0062's kind filter exposed that the kinds themselves were part-hardcoded.
-- market_symbol's kinds are real data -- the worker syncs FMP's etf-list,
-- cryptocurrency-list and fund screener into it daily -- but every row from
-- the SEC directory was stamped 'stock' by this query, and the dedupe
-- prefers the SEC row. So an ETF that files with the SEC, which the large
-- ones do, was a 'stock' here: BITB, SPY and IBIT under Stocks, invisible
-- under ETFs. The SEC directory knows nothing about asset kinds; it is a
-- ticker-to-CIK map. The kind now comes from market_symbol wherever the
-- symbol appears there, for both directory rows and alias hits, and 'stock'
-- is only the fallback for symbols the FMP lists genuinely do not carry.
--
-- Generated from 0062, which was generated from 0052; the ranking is
-- untouched.

create or replace function public.search_companies(q text, lim int default 12, p_kind text default null)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  with params as (
    select trim(coalesce(q, '')) as q,
           lower(trim(coalesce(q, ''))) as qlow,
           -- Drop "and" then non-alphanumerics: "johnson and johnson" → johnsonjohnson,
           -- "jp morgan" → jpmorgan, "brk.b" → brkb.
           regexp_replace(
             regexp_replace(lower(trim(coalesce(q, ''))), '\yand\y', ' ', 'g'),
             '[^a-z0-9]+', '', 'g') as qcompact,
           greatest(1, least(coalesce(lim, 12), 25)) as lim
  ),
  crypto_alias(alias, symbol) as (
    values
      ('bitcoin', 'BTCUSD'), ('btc', 'BTCUSD'), ('btcusd', 'BTCUSD'),
      ('ethereum', 'ETHUSD'), ('eth', 'ETHUSD'), ('ethusd', 'ETHUSD'),
      ('solana', 'SOLUSD'), ('sol', 'SOLUSD'), ('solusd', 'SOLUSD'),
      ('dogecoin', 'DOGEUSD'), ('doge', 'DOGEUSD'), ('dogeusd', 'DOGEUSD'),
      ('ripple', 'XRPUSD'), ('xrp', 'XRPUSD'), ('xrpusd', 'XRPUSD'),
      ('cardano', 'ADAUSD'), ('ada', 'ADAUSD'), ('adausd', 'ADAUSD'),
      ('avalanche', 'AVAXUSD'), ('avax', 'AVAXUSD'), ('avaxusd', 'AVAXUSD'),
      ('polkadot', 'DOTUSD'), ('dot', 'DOTUSD'), ('dotusd', 'DOTUSD'),
      ('chainlink', 'LINKUSD'), ('link', 'LINKUSD'), ('linkusd', 'LINKUSD'),
      ('litecoin', 'LTCUSD'), ('ltc', 'LTCUSD'), ('ltcusd', 'LTCUSD'),
      ('polygon', 'POLUSD'), ('matic', 'POLUSD'), ('pol', 'POLUSD'), ('polusd', 'POLUSD'),
      ('binance', 'BNBUSD'), ('bnb', 'BNBUSD'), ('bnbusd', 'BNBUSD'),
      ('toncoin', 'TONUSD'), ('ton', 'TONUSD'), ('tonusd', 'TONUSD'),
      ('shiba', 'SHIBUSD'), ('shib', 'SHIBUSD'), ('shibusd', 'SHIBUSD')
  ),
  index_alias(alias, symbol) as (
    values
      ('s&p', 'SPX'), ('s&p500', 'SPX'), ('s&p 500', 'SPX'), ('sp500', 'SPX'),
      ('spx', 'SPX'), ('gspc', 'SPX'), ('snp', 'SPX'),
      ('nasdaq', 'IXIC'), ('nasdaq composite', 'IXIC'), ('ixic', 'IXIC'),
      ('comp', 'IXIC'), ('ndx', 'IXIC'),
      ('dow', 'DJI'), ('dow jones', 'DJI'), ('djia', 'DJI'), ('dji', 'DJI')
  ),
  -- Brand / nickname → primary listing (SEC ticker or market_symbol).
  company_alias(alias, symbol) as (
    values
      ('spacex', 'SPCX'), ('space x', 'SPCX'), ('space-x', 'SPCX'),
      ('space exploration', 'SPCX'), ('space exploration technologies', 'SPCX'),
      ('google', 'GOOGL'), ('alphabet', 'GOOGL'),
      ('facebook', 'META'), ('fb', 'META'), ('meta platforms', 'META'),
      ('berkshire', 'BRK-B'), ('berkshire hathaway', 'BRK-B'),
      ('brkb', 'BRK-B'), ('brk.b', 'BRK-B'), ('brk/b', 'BRK-B'), ('brk-b', 'BRK-B'),
      ('brka', 'BRK-A'), ('brk.a', 'BRK-A'), ('brk/a', 'BRK-A'), ('brk-a', 'BRK-A'),
      ('jp morgan', 'JPM'), ('j p morgan', 'JPM'), ('j.p. morgan', 'JPM'),
      ('jpmorgan chase', 'JPM'),
      ('johnson and johnson', 'JNJ'), ('johnson & johnson', 'JNJ'),
      ('j and j', 'JNJ'), ('j&j', 'JNJ'),
      ('coca cola', 'KO'), ('coca-cola', 'KO'),
      ('mcdonalds', 'MCD'), ('mc donalds', 'MCD'), ('mc donald''s', 'MCD'),
      ('procter and gamble', 'PG'), ('procter & gamble', 'PG'), ('p&g', 'PG'),
      ('at&t', 'T'), ('att', 'T'),
      ('ibm', 'IBM'),
      ('amazon', 'AMZN'), ('amazon.com', 'AMZN'),
      ('microsoft', 'MSFT'),
      ('nvidia', 'NVDA'),
      ('apple', 'AAPL'),
      ('netflix', 'NFLX'),
      ('tesla', 'TSLA'),
      ('visa', 'V'),
      ('mastercard', 'MA'),
      ('walmart', 'WMT'),
      ('disney', 'DIS'), ('walt disney', 'DIS'),
      ('boeing', 'BA'),
      ('lockheed', 'LMT'), ('lockheed martin', 'LMT'),
      ('palantir', 'PLTR'),
      ('snowflake', 'SNOW'),
      ('crowdstrike', 'CRWD'),
      ('advanced micro devices', 'AMD'),
      ('broadcom', 'AVGO'),
      ('salesforce', 'CRM'),
      ('paypal', 'PYPL'),
      ('uber', 'UBER'),
      ('lyft', 'LYFT'),
      ('airbnb', 'ABNB'),
      ('square', 'XYZ'), ('block', 'XYZ'),
      ('snapchat', 'SNAP'),
      ('exxon mobil', 'XOM'), ('exxonmobil', 'XOM'),
      ('chevron', 'CVX'),
      ('unitedhealth', 'UNH'), ('united health', 'UNH'),
      ('eli lilly', 'LLY'),
      ('novo nordisk', 'NVO'),
      ('taiwan semiconductor', 'TSM'), ('tsmc', 'TSM'),
      ('alibaba', 'BABA')
  ),
  major_crypto(symbol) as (
    select distinct symbol from crypto_alias
  ),
  sec as (
    select t.ticker,
           t.name,
           t.cik,
           -- The kind the daily FMP sync recorded, when it recorded one. An
           -- ETF that files with the SEC (BITB, SPY) is in both directories,
           -- and 'stock' was this query's word, not the data's.
           coalesce(mk.kind, 'stock') as kind,
           0 as kind_rank
      from ledger.ticker t
      left join ledger.market_symbol mk on mk.symbol = t.ticker,
           params p
     where p.q <> ''
       and (
         t.ticker like upper(p.q) || '%'
         or t.name ilike '%' || p.q || '%'
         or (length(p.qcompact) >= 2
             and regexp_replace(upper(t.ticker), '[^A-Z0-9]+', '', 'g') = upper(p.qcompact))
         or (length(p.qcompact) >= 3
             and regexp_replace(lower(t.name), '[^a-z0-9]+', '', 'g')
                 like '%' || p.qcompact || '%')
       )
  ),
  mkt as (
    select m.symbol as ticker,
           m.name,
           null::integer as cik,
           m.kind,
           case m.kind
             when 'index' then 0
             when 'etf' then 1
             else 2
           end as kind_rank
      from ledger.market_symbol m, params p
     where p.q <> ''
       and (
         m.symbol like upper(p.q) || '%'
         or m.name ilike '%' || p.q || '%'
         or (length(p.qcompact) >= 2
             and regexp_replace(upper(m.symbol), '[^A-Z0-9]+', '', 'g') = upper(p.qcompact))
         or (length(p.qcompact) >= 3
             and regexp_replace(lower(m.name), '[^a-z0-9]+', '', 'g')
                 like '%' || p.qcompact || '%')
       )
       and not exists (
             select 1 from ledger.ticker t where t.ticker = m.symbol)
  ),
  alias_hits as (
    select coalesce(t.ticker, m.symbol) as ticker,
           coalesce(t.name, m.name) as name,
           t.cik,
           coalesce(m.kind, 'stock'::text) as kind,
           0 as kind_rank
      from company_alias a
      join params p on a.alias = p.qlow
      left join ledger.ticker t on t.ticker = a.symbol
      left join ledger.market_symbol m on m.symbol = a.symbol
     where t.ticker is not null or m.symbol is not null
  ),
  combined as (
    select * from sec
    union all
    select * from mkt
    union all
    select * from alias_hits
  ),
  ranked as (
    select distinct on (c.ticker)
           c.ticker, c.cik, c.name, c.kind, c.kind_rank,
           p.q, p.qlow, p.qcompact
      from combined c, params p
     order by c.ticker,
              c.kind_rank,
              (c.cik is not null) desc
  )
  select coalesce(
    jsonb_agg(jsonb_build_object(
      'ticker', s.ticker,
      'cik', s.cik,
      'name', s.name,
      'kind', s.kind)),
    '[]'::jsonb)
  from (
    select r.ticker, r.cik, r.name, r.kind
      from ranked r
     where coalesce(nullif(trim(lower(p_kind)), ''), 'all') not in
             ('stock', 'etf', 'crypto', 'fund')
        or (lower(p_kind) = 'fund' and r.kind in ('fund', 'money_market'))
        or r.kind = lower(p_kind)
     order by
              (exists (
                 select 1 from index_alias a
                  where a.alias = r.qlow and a.symbol = r.ticker)) desc,
              (exists (
                 select 1 from company_alias a
                  where a.alias = r.qlow and a.symbol = r.ticker)) desc,
              (exists (
                 select 1 from crypto_alias a
                  where a.alias = r.qlow and a.symbol = r.ticker)) desc,
              (r.ticker in (select symbol from major_crypto)
                and (r.ticker like upper(r.q) || '%'
                     or r.name ilike '%' || r.q || '%')) desc,
              (r.ticker = upper(r.q)
                or regexp_replace(upper(r.ticker), '[^A-Z0-9]+', '', 'g')
                     = upper(r.qcompact)) desc,
              (r.name ilike r.q || '%') desc,
              (r.name ilike '%' || r.q || '%') desc,
              (r.ticker like upper(r.q) || '%'
                and r.name ilike '%' || r.q || '%') desc,
              (r.ticker like upper(r.q) || '%') desc,
              r.kind_rank,
              (exists (select 1 from ledger.quarter x where x.cik = r.cik)) desc,
              length(r.ticker), r.ticker
     limit (select lim from params)
  ) s;
$$;

do $$
begin
  begin
    execute 'drop function if exists public.search_companies(text, integer)';
  exception when others then null;
  end;
  execute 'revoke all on function public.search_companies(text, integer, text) from public';
  execute 'grant execute on function public.search_companies(text, integer, text) to anon, authenticated';
end $$;
