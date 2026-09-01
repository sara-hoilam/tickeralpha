-- Ticker Alpha — competitor comparison for the Industry tab
--
-- Apply after 0075. Safe to re-run.
--
-- The Industry tab used to read a company against a sector ETF, which is a
-- blend of five hundred strangers. A reader sizing up Coca-Cola wants Pepsi,
-- not XLP. Three page-facing reads make that possible:
--
--   get_industry_peers   who the natural competitors are: a hand-curated
--                        rivals list first (the famous head-to-heads), then
--                        every cached name filed under the same industry,
--                        largest first
--   get_peer_compare     one payload per ticker: quote metrics, a weekly
--                        year of closes, revenue quarters, the Street's
--                        median target, next earnings
--   get_peer_news        the latest stories across the whole peer set, so a
--                        rival's bad quarter is read as this company's
--                        weather
--
-- Everything reads tables the worker already fills (quote_detail,
-- price_daily, quarter, analyst, earnings_event, news); a cold peer is
-- warmed by the page through the existing request_prices path.

-- ---------------------------------------------------------------------------
-- Curated rivals
-- ---------------------------------------------------------------------------
-- Industry codes put Burger King's parent next to Starbucks and miss Wendy's
-- entirely (it is in no index we track). The famous rivalries are a small,
-- stable set, so they are written down. A row reads "when looking at SYMBOL,
-- these are the competitors, best first"; lookups also run in reverse and
-- across a row, so WEN finds MCD by appearing on MCD's row.

create table if not exists ledger.peer_seed (
  symbol text primary key,
  peers  text[] not null
);
alter table ledger.peer_seed enable row level security;

insert into ledger.peer_seed (symbol, peers) values
  ('KO',   '{PEP,KDP,MNST}'),
  ('PEP',  '{KO,KDP,MDLZ}'),
  ('MCD',  '{QSR,WEN,YUM,CMG}'),
  ('QSR',  '{MCD,WEN,YUM,JACK}'),
  ('WEN',  '{MCD,QSR,YUM,JACK}'),
  ('YUM',  '{MCD,QSR,WEN,DPZ}'),
  ('DPZ',  '{PZZA,YUM,MCD}'),
  ('CMG',  '{MCD,YUM,SG,CAVA}'),
  ('SBUX', '{MCD,CMG,YUM}'),
  ('NKE',  '{LULU,DECK,ONON,UAA}'),
  ('LULU', '{NKE,ONON,DECK,GPS}'),
  ('AAPL', '{MSFT,GOOGL,DELL,HPQ}'),
  ('MSFT', '{AAPL,GOOGL,AMZN,ORCL,CRM}'),
  ('GOOGL','{META,MSFT,AMZN,AAPL}'),
  ('META', '{GOOGL,SNAP,PINS,RDDT}'),
  ('AMZN', '{WMT,BABA,SHOP,TGT}'),
  ('WMT',  '{TGT,COST,KR,AMZN}'),
  ('TGT',  '{WMT,COST,KR}'),
  ('COST', '{WMT,TGT,BJ,KR}'),
  ('KR',   '{WMT,ACI,TGT}'),
  ('HD',   '{LOW,FND}'),
  ('LOW',  '{HD,FND}'),
  ('NVDA', '{AMD,INTC,AVGO,QCOM}'),
  ('AMD',  '{NVDA,INTC,QCOM,AVGO}'),
  ('INTC', '{AMD,NVDA,TSM,MU}'),
  ('TSM',  '{INTC,GFS,UMC}'),
  ('MU',   '{WDC,STX,INTC}'),
  ('QCOM', '{AVGO,NVDA,MRVL,TXN}'),
  ('AMAT', '{LRCX,KLAC,ASML}'),
  ('LRCX', '{AMAT,KLAC,ASML}'),
  ('V',    '{MA,AXP,PYPL}'),
  ('MA',   '{V,AXP,PYPL}'),
  ('PYPL', '{V,MA,XYZ,AFRM}'),
  ('JPM',  '{BAC,WFC,C,GS}'),
  ('BAC',  '{JPM,WFC,C}'),
  ('GS',   '{MS,JPM}'),
  ('MS',   '{GS,JPM,SCHW}'),
  ('SCHW', '{MS,IBKR,HOOD}'),
  ('COIN', '{HOOD,IBKR}'),
  ('HOOD', '{COIN,SCHW,IBKR}'),
  ('XOM',  '{CVX,COP,SHEL,BP}'),
  ('CVX',  '{XOM,COP,SHEL}'),
  ('PFE',  '{MRK,BMY,LLY,JNJ}'),
  ('LLY',  '{NVO,MRK,PFE}'),
  ('JNJ',  '{PFE,MRK,ABBV}'),
  ('UNH',  '{CI,ELV,HUM,CVS}'),
  ('TSLA', '{RIVN,LCID,F,GM}'),
  ('F',    '{GM,TSLA,STLA}'),
  ('GM',   '{F,TSLA,STLA}'),
  ('BA',   '{LMT,RTX,NOC,GD}'),
  ('LMT',  '{RTX,NOC,GD,BA}'),
  ('GE',   '{RTX,HON}'),
  ('HON',  '{GE,EMR,MMM}'),
  ('CAT',  '{DE,CMI}'),
  ('DE',   '{CAT,AGCO,CNH}'),
  ('DIS',  '{NFLX,CMCSA,WBD}'),
  ('NFLX', '{DIS,WBD,ROKU}'),
  ('T',    '{VZ,TMUS}'),
  ('VZ',   '{T,TMUS}'),
  ('TMUS', '{VZ,T}'),
  ('UPS',  '{FDX}'),
  ('FDX',  '{UPS}'),
  ('DAL',  '{UAL,AAL,LUV}'),
  ('UAL',  '{DAL,AAL,LUV}'),
  ('MAR',  '{HLT,H,IHG}'),
  ('HLT',  '{MAR,H,IHG}'),
  ('ABNB', '{BKNG,EXPE,MAR}'),
  ('BKNG', '{EXPE,ABNB,TRIP}'),
  ('UBER', '{LYFT,DASH}'),
  ('CRM',  '{MSFT,ORCL,NOW,HUBS}'),
  ('ORCL', '{MSFT,SAP,CRM,IBM}'),
  ('ADBE', '{MSFT,CRM,FIG}'),
  ('PLTR', '{SNOW,DDOG,AI}'),
  ('SNOW', '{DDOG,MDB,PLTR}'),
  ('PG',   '{UL,CL,KMB,CHD}'),
  ('CL',   '{PG,CHD,KMB}'),
  ('PM',   '{MO,BTI}'),
  ('MO',   '{PM,BTI}'),
  ('BUD',  '{TAP,STZ,SAM}'),
  ('TAP',  '{BUD,STZ,SAM}'),
  ('STZ',  '{BUD,TAP,SAM}'),
  ('GIS',  '{K,KHC,CPB}'),
  ('KHC',  '{GIS,CPB,CAG}'),
  ('CAG',  '{GIS,KHC,CPB}'),
  ('MDLZ', '{HSY,GIS,KHC}'),
  ('HSY',  '{MDLZ,GIS}')
on conflict (symbol) do update set peers = excluded.peers;

-- ---------------------------------------------------------------------------
-- Who the competitors are
-- ---------------------------------------------------------------------------
-- Curated rivals lead in their written order. The reverse and sibling reads
-- mean a ticker mentioned on any row inherits that row's whole set. Behind
-- the curated names, every cached symbol filed under the same industry
-- (index constituents and previously-opened companies alike) joins the list,
-- largest market cap first; sector is the fallback when no industry is on
-- file. `warm` says whether the compare payload will have prices, so the
-- page knows to ask for the cold ones.

create or replace function public.get_industry_peers(p_symbol text)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  with me as (
    select upper(trim(p_symbol)) as symbol
  ),
  ind as (
    select
      coalesce(
        (select pd.industry from ledger.price_daily pd, me where pd.symbol = me.symbol),
        (select min(ih.industry) from ledger.index_holding ih, me
         where ih.symbol = me.symbol and ih.industry is not null)) as industry,
      (select pd.sector from ledger.price_daily pd, me where pd.symbol = me.symbol) as sector
  ),
  curated as (
    -- pri 0: my own row, in its written order. pri 1: rows naming me.
    -- pri 2: everyone else on a row naming me (my siblings). One combined
    -- key per peer, so a duplicate keeps the ord of its *best* pri rather
    -- than mixing a good pri with another entry's ord.
    select x.peer,
           min(x.pri * 1000 + x.ord) / 1000 as pri,
           min(x.pri * 1000 + x.ord) % 1000 as ord
    from me, lateral (
      select p.peer, 0 as pri, p.ord
      from ledger.peer_seed ps,
           lateral unnest(ps.peers) with ordinality p(peer, ord)
      where ps.symbol = me.symbol
      union all
      select ps.symbol, 1, 0
      from ledger.peer_seed ps
      where me.symbol = any(ps.peers)
      union all
      select p.peer, 2, p.ord
      from ledger.peer_seed ps,
           lateral unnest(ps.peers) with ordinality p(peer, ord)
      where me.symbol = any(ps.peers)
    ) x
    where x.peer <> me.symbol
    group by x.peer
  ),
  same_industry as (
    select y.symbol as peer
    from ind, me, lateral (
      select ih.symbol from ledger.index_holding ih
      where ind.industry is not null and ih.industry = ind.industry
      union
      select pd.symbol from ledger.price_daily pd
      where (ind.industry is not null and pd.industry = ind.industry)
         or (ind.industry is null and ind.sector is not null
             and pd.sector = ind.sector)
    ) y
    where y.symbol <> me.symbol
      and y.symbol not in (select peer from curated)
  ),
  pool as (
    select peer, pri, ord from curated
    union all
    select peer, 3, 0 from same_industry
  )
  select jsonb_build_object(
    'symbol',   (select symbol from me),
    'sector',   (select sector from ind),
    'industry', (select industry from ind),
    'peers', coalesce((
      select jsonb_agg(jsonb_build_object(
               'symbol', z.peer,
               'name',   z.name,
               'cap',    z.market_cap,
               'curated', z.pri < 3,
               'warm',   z.warm)
             order by z.pri, z.ord, z.market_cap desc nulls last, z.peer)
      from (
        select p.peer, p.pri, p.ord,
               coalesce(q.name, ihn.name, t.name) as name,
               q.market_cap,
               (pd.symbol is not null) as warm
        from pool p
        left join ledger.quote_detail q on q.symbol = p.peer
        left join ledger.price_daily pd on pd.symbol = p.peer
        left join lateral (
          select min(ih.name) as name from ledger.index_holding ih
          where ih.symbol = p.peer) ihn on true
        left join ledger.ticker t on t.ticker = p.peer
        order by p.pri, p.ord, q.market_cap desc nulls last, p.peer
        limit 8
      ) z), '[]'::jsonb));
$$;

-- ---------------------------------------------------------------------------
-- The comparison payload
-- ---------------------------------------------------------------------------
-- One row per ticker, the company itself included, capped at seven. Closes
-- are a weekly year sampled from the daily bars -- enough for an indexed
-- performance line without shipping five megabytes of candles. Revenue is
-- the filed figure, nine quarters, same {e,r} shape the alpha scan uses.

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
    -- line always ends today
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
  ) rv on true;
$$;

-- ---------------------------------------------------------------------------
-- News across the peer set
-- ---------------------------------------------------------------------------
-- A rival's terrible quarter is this company's read-through. The page hands
-- over the whole peer set and gets the freshest stories tagged with any of
-- those tickers, newest first.

create or replace function public.get_peer_news(
  p_symbols text[], p_limit integer default 12)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  with syms as (
    select distinct upper(trim(u.s)) as symbol
    from unnest(p_symbols) u(s)
    where trim(coalesce(u.s, '')) <> ''
    limit 8
  )
  select coalesce(jsonb_agg(x.j), '[]'::jsonb)
  from (
    select jsonb_build_object(
      'url', n.url, 'title', n.title, 'summary', n.summary,
      'image', n.image, 'publisher', n.publisher,
      'symbol', n.symbol, 'published', n.published) as j
    from ledger.news n
    where n.symbol in (select symbol from syms)
    order by n.published desc nulls last
    limit least(greatest(coalesce(p_limit, 12), 1), 30)
  ) x;
$$;

-- ---------------------------------------------------------------------------
-- Grants — page-facing reads, nothing writable
-- ---------------------------------------------------------------------------
do $$
begin
  execute 'revoke all on function public.get_industry_peers(text) from public';
  execute 'grant execute on function public.get_industry_peers(text) to anon, authenticated';

  execute 'revoke all on function public.get_peer_compare(text[]) from public';
  execute 'grant execute on function public.get_peer_compare(text[]) to anon, authenticated';

  execute 'revoke all on function public.get_peer_news(text[], integer) from public';
  execute 'grant execute on function public.get_peer_news(text[], integer) to anon, authenticated';
end $$;
