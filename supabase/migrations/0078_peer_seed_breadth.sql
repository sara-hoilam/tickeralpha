-- Ticker Alpha — competitors that are actually comparable
--
-- Apply after 0077. Safe to re-run.
--
-- Two faults, one visible and one structural.
--
-- The visible one: IREN — a bitcoin miner turned AI datacenter landlord —
-- was offered IBM, Accenture and Cognizant. It is filed under an industry
-- ("Information Technology Services") whose other members are consultancies
-- twenty times its size, and it was on no curated row, so the fallback ran.
--
-- The structural one is why that fallback produced them: it ordered the
-- industry by market cap, largest first, which for any company that is not
-- itself a giant returns the giants of a loosely-drawn bucket. A $10B company
-- was always going to be shown $250B companies. Size similarity is the
-- ordering that makes an industry fallback worth having, so that is what it
-- uses now: nearest by log market cap, which reads "within an order of
-- magnitude either way" and degrades gracefully when the bucket is thin.
--
-- The curated list also grows a long way past the household rivalries it
-- started as, because the fallback can only ever approximate: the companies a
-- reader means by "competitor" are a business fact, not an industry code. New
-- rows lead with the sectors where the codes mislead most — AI datacenters
-- and miners, semis, cyber, fintech, power and nuclear, defence and space.
--
-- One correction to an existing row: Kellanova (K) left the market in the
-- Mars acquisition, so General Mills no longer points at it.

-- ---------------------------------------------------------------------------
-- Seed: more of the market, and the corner that started this
-- ---------------------------------------------------------------------------
insert into ledger.peer_seed (symbol, peers) values
  -- AI datacenters, neoclouds and the miners that became them. Industry codes
  -- scatter these across IT services, capital markets and software; as a
  -- group they are the clearest case for writing the answer down.
  ('IREN',  '{CRWV,NBIS,CIFR,WULF,APLD,CORZ}'),
  ('CRWV',  '{NBIS,IREN,APLD,CORZ}'),
  ('NBIS',  '{CRWV,IREN,APLD}'),
  ('APLD',  '{IREN,CRWV,NBIS,WULF}'),
  ('CIFR',  '{IREN,WULF,CORZ,HUT}'),
  ('WULF',  '{CIFR,IREN,APLD,HUT}'),
  ('CORZ',  '{IREN,CIFR,WULF,APLD}'),
  ('HUT',   '{CIFR,WULF,CORZ,RIOT}'),
  ('MARA',  '{RIOT,CLSK,CIFR,HUT}'),
  ('RIOT',  '{MARA,CLSK,CIFR,HUT}'),
  ('CLSK',  '{MARA,RIOT,CIFR}'),
  ('MSTR',  '{COIN,MARA,RIOT}'),
  ('EQIX',  '{DLR,AMT,IRM}'),
  ('DLR',   '{EQIX,AMT,IRM}'),
  ('AMT',   '{CCI,SBAC,EQIX}'),
  ('CCI',   '{AMT,SBAC}'),

  -- Semiconductors and the machines that make them
  ('AVGO',  '{NVDA,QCOM,TXN,MRVL}'),
  ('MRVL',  '{AVGO,QCOM,NVDA}'),
  ('TXN',   '{ADI,NXPI,MCHP}'),
  ('ADI',   '{TXN,NXPI,MCHP}'),
  ('NXPI',  '{TXN,ADI,STM}'),
  ('MCHP',  '{TXN,ADI,NXPI}'),
  ('ASML',  '{AMAT,LRCX,KLAC}'),
  ('KLAC',  '{AMAT,LRCX,ASML}'),
  ('ARM',   '{QCOM,NVDA,AVGO}'),
  ('SMCI',  '{DELL,HPE,NTAP}'),
  ('DELL',  '{HPQ,HPE,SMCI}'),
  ('HPE',   '{DELL,NTAP,SMCI}'),
  ('WDC',   '{STX,SNDK,MU}'),
  ('STX',   '{WDC,SNDK,MU}'),
  ('GFS',   '{TSM,UMC,INTC}'),

  -- Software, data and security
  ('NOW',   '{CRM,WDAY,MSFT,TEAM}'),
  ('WDAY',  '{NOW,CRM,ADP,PAYX}'),
  ('DDOG',  '{SNOW,MDB,NET,DT}'),
  ('MDB',   '{SNOW,DDOG,ORCL}'),
  ('NET',   '{DDOG,AKAM,FSLY,ZS}'),
  ('TEAM',  '{NOW,MNDY,ASAN,MSFT}'),
  ('CRWD',  '{PANW,ZS,S,FTNT}'),
  ('PANW',  '{CRWD,FTNT,ZS,CHKP}'),
  ('ZS',    '{CRWD,PANW,NET,S}'),
  ('FTNT',  '{PANW,CRWD,CHKP}'),
  ('IONQ',  '{RGTI,QBTS,QUBT}'),
  ('RGTI',  '{IONQ,QBTS,QUBT}'),
  ('QBTS',  '{IONQ,RGTI,QUBT}'),

  -- Marketplaces and consumer internet
  ('SHOP',  '{AMZN,ETSY,BIGC,WIX}'),
  ('ETSY',  '{EBAY,SHOP,AMZN}'),
  ('EBAY',  '{ETSY,AMZN,MELI}'),
  ('MELI',  '{AMZN,SE,BABA}'),
  ('SE',    '{MELI,BABA,PDD}'),
  ('BABA',  '{PDD,JD,BIDU}'),
  ('PDD',   '{BABA,JD,SE}'),
  ('JD',    '{BABA,PDD}'),
  ('SNAP',  '{PINS,RDDT,META}'),
  ('PINS',  '{SNAP,RDDT,META}'),
  ('RDDT',  '{PINS,SNAP,META}'),
  ('SPOT',  '{NFLX,SIRI,WMG}'),
  ('RBLX',  '{U,EA,TTWO}'),
  ('EA',    '{TTWO,RBLX,U}'),
  ('TTWO',  '{EA,RBLX,U}'),
  ('APP',   '{TTD,U,GOOGL}'),
  ('TTD',   '{APP,GOOGL,META}'),

  -- Payments, banks, markets and insurers
  ('SOFI',  '{HOOD,ALLY,LC,UPST}'),
  ('AFRM',  '{PYPL,XYZ,UPST,SOFI}'),
  ('XYZ',   '{PYPL,AFRM,TOST,FIS}'),
  ('WFC',   '{JPM,BAC,C}'),
  ('C',     '{JPM,BAC,WFC}'),
  ('PNC',   '{USB,TFC,FITB}'),
  ('USB',   '{PNC,TFC,FITB}'),
  ('TFC',   '{PNC,USB,FITB}'),
  ('AXP',   '{V,MA,COF,SYF}'),
  ('COF',   '{AXP,SYF,BAC}'),
  ('BLK',   '{BX,KKR,APO,TROW}'),
  ('BX',    '{KKR,APO,ARES,BLK}'),
  ('KKR',   '{BX,APO,ARES}'),
  ('APO',   '{KKR,BX,ARES}'),
  ('CME',   '{ICE,NDAQ,CBOE}'),
  ('ICE',   '{CME,NDAQ,CBOE}'),
  ('NDAQ',  '{ICE,CME,CBOE}'),
  ('PGR',   '{ALL,TRV,CB}'),
  ('ALL',   '{PGR,TRV,CB}'),
  ('TRV',   '{ALL,PGR,CB}'),

  -- Power, nuclear, oil and the things dug out of the ground
  ('CEG',   '{VST,NRG,TLN}'),
  ('VST',   '{CEG,NRG,TLN}'),
  ('NRG',   '{VST,CEG,TLN}'),
  ('NEE',   '{DUK,SO,AEP,D}'),
  ('DUK',   '{SO,NEE,AEP}'),
  ('SO',    '{DUK,NEE,AEP}'),
  ('OKLO',  '{SMR,NNE,LEU}'),
  ('SMR',   '{OKLO,NNE,BWXT}'),
  ('LEU',   '{OKLO,SMR,CCJ}'),
  ('CCJ',   '{LEU,UEC,DNN}'),
  ('FSLR',  '{ENPH,SEDG,RUN,NXT}'),
  ('ENPH',  '{SEDG,FSLR,RUN}'),
  ('COP',   '{XOM,CVX,EOG,OXY}'),
  ('EOG',   '{COP,DVN,FANG,OXY}'),
  ('OXY',   '{COP,EOG,DVN}'),
  ('SLB',   '{HAL,BKR,WFRD}'),
  ('HAL',   '{SLB,BKR}'),
  ('BKR',   '{SLB,HAL}'),
  ('MPC',   '{VLO,PSX,DINO}'),
  ('VLO',   '{MPC,PSX,DINO}'),
  ('PSX',   '{MPC,VLO,DINO}'),
  ('KMI',   '{WMB,OKE,ET}'),
  ('WMB',   '{KMI,OKE,ET}'),
  ('OKE',   '{WMB,KMI,ET}'),
  ('FCX',   '{SCCO,TECK,BHP}'),
  ('NEM',   '{AEM,GOLD,KGC}'),
  ('AEM',   '{NEM,GOLD,KGC}'),
  ('NUE',   '{STLD,CLF,X}'),
  ('STLD',  '{NUE,CLF,X}'),
  ('CLF',   '{X,NUE,STLD}'),
  ('LIN',   '{APD,DOW,SHW}'),
  ('APD',   '{LIN,DOW}'),
  ('SHW',   '{PPG,RPM,AXTA}'),

  -- Defence, space and heavy industry
  ('LHX',   '{RTX,NOC,LMT,GD}'),
  ('NOC',   '{LMT,RTX,GD,LHX}'),
  ('GD',    '{LMT,NOC,RTX,LHX}'),
  ('RTX',   '{LMT,NOC,GD,LHX}'),
  ('HWM',   '{TDG,HEI,RTX}'),
  ('TDG',   '{HWM,HEI}'),
  ('HEI',   '{TDG,HWM}'),
  ('AVAV',  '{KTOS,RKLB,LMT}'),
  ('KTOS',  '{AVAV,LHX,LMT}'),
  ('RKLB',  '{ASTS,LUNR,PL}'),
  ('ASTS',  '{RKLB,IRDM,GSAT}'),
  ('LUNR',  '{RKLB,ASTS,PL}'),
  ('EMR',   '{HON,ROK,ETN}'),
  ('ETN',   '{EMR,HON,PH}'),
  ('ROK',   '{EMR,ETN,HON}'),
  ('PH',    '{ETN,EMR,HON}'),
  ('MMM',   '{HON,GE,EMR}'),
  ('PCAR',  '{CMI,DE,CAT}'),
  ('CMI',   '{PCAR,CAT,DE}'),
  ('UNP',   '{CSX,NSC,CP,CNI}'),
  ('CSX',   '{UNP,NSC}'),
  ('NSC',   '{UNP,CSX}'),
  ('WM',    '{RSG,WCN}'),
  ('RSG',   '{WM,WCN}'),

  -- Cars that are not yet Tesla
  ('RIVN',  '{LCID,TSLA,F,GM}'),
  ('LCID',  '{RIVN,TSLA}'),
  ('NIO',   '{XPEV,LI,TSLA}'),
  ('XPEV',  '{NIO,LI,TSLA}'),
  ('LI',    '{NIO,XPEV,TSLA}'),

  -- Health
  ('ABBV',  '{JNJ,MRK,PFE,BMY}'),
  ('BMY',   '{PFE,MRK,ABBV}'),
  ('AMGN',  '{GILD,BIIB,REGN,VRTX}'),
  ('GILD',  '{AMGN,BIIB,VRTX}'),
  ('REGN',  '{VRTX,AMGN,BIIB}'),
  ('VRTX',  '{REGN,AMGN,GILD}'),
  ('MRNA',  '{BNTX,NVAX,PFE}'),
  ('BNTX',  '{MRNA,NVAX}'),
  ('NVO',   '{LLY,MRK,PFE}'),
  ('ISRG',  '{MDT,SYK,BSX}'),
  ('MDT',   '{SYK,BSX,ABT,ISRG}'),
  ('SYK',   '{MDT,BSX,ZBH}'),
  ('BSX',   '{MDT,SYK,ABT}'),
  ('ABT',   '{MDT,BSX,SYK,JNJ}'),
  ('CI',    '{UNH,ELV,HUM,CVS}'),
  ('ELV',   '{UNH,CI,HUM}'),
  ('CVS',   '{UNH,CI,MCK}'),
  ('MCK',   '{COR,CAH}'),
  ('COR',   '{MCK,CAH}'),
  ('CAH',   '{MCK,COR}'),

  -- Shops, brands and the places people go
  ('TJX',   '{ROST,BURL,TGT}'),
  ('ROST',  '{TJX,BURL}'),
  ('BURL',  '{TJX,ROST}'),
  ('DG',    '{DLTR,WMT,TGT}'),
  ('DLTR',  '{DG,WMT,TGT}'),
  ('ULTA',  '{ELF,EL,COTY}'),
  ('ELF',   '{ULTA,EL,COTY}'),
  ('EL',    '{ELF,COTY,ULTA}'),
  ('DECK',  '{NKE,ONON,SKX,CROX}'),
  ('ONON',  '{NKE,DECK,LULU}'),
  ('CROX',  '{DECK,SKX,NKE}'),
  ('SKX',   '{DECK,CROX,NKE}'),
  ('DHI',   '{LEN,PHM,NVR,TOL}'),
  ('LEN',   '{DHI,PHM,NVR}'),
  ('PHM',   '{DHI,LEN,NVR}'),
  ('NVR',   '{DHI,LEN,PHM}'),
  ('TOL',   '{DHI,LEN,PHM}'),
  ('EXPE',  '{BKNG,ABNB,TRIP}'),
  ('TRIP',  '{EXPE,BKNG,ABNB}'),
  ('LYFT',  '{UBER,DASH}'),
  ('DASH',  '{UBER,LYFT}'),
  ('LVS',   '{WYNN,MGM,CZR}'),
  ('WYNN',  '{LVS,MGM,CZR}'),
  ('MGM',   '{LVS,WYNN,CZR}'),
  ('DKNG',  '{FLUT,PENN,CZR}'),
  ('CCL',   '{RCL,NCLH}'),
  ('RCL',   '{CCL,NCLH}'),
  ('NCLH',  '{CCL,RCL}'),
  ('LUV',   '{DAL,UAL,AAL}'),
  ('AAL',   '{DAL,UAL,LUV}'),
  ('WBD',   '{DIS,NFLX,PARA,CMCSA}'),
  ('PARA',  '{WBD,DIS,CMCSA}'),
  ('CMCSA', '{CHTR,DIS,WBD}'),
  ('CHTR',  '{CMCSA,VZ,T}'),

  -- Food and drink
  ('MNST',  '{CELH,KDP,KO}'),
  ('CELH',  '{MNST,KDP}'),
  ('KDP',   '{KO,PEP,MNST}'),
  ('ADM',   '{BG,INGR}'),
  ('BG',    '{ADM,INGR}'),
  ('TSN',   '{PPC,HRL,CAG}'),
  ('HRL',   '{TSN,CAG,GIS}'),
  ('SJM',   '{HSY,GIS,CAG}'),
  ('CPB',   '{CAG,GIS,KHC}'),
  -- Kellanova left the market in the Mars acquisition; the row that pointed
  -- at it would have offered a ticker with no prices and no filings.
  ('GIS',   '{KHC,CPB,CAG,HSY}')
on conflict (symbol) do update set peers = excluded.peers;

-- ---------------------------------------------------------------------------
-- Discovery: curated first, then the industry ordered by *similarity*
-- ---------------------------------------------------------------------------
create or replace function public.get_industry_peers(p_symbol text)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  with me as (
    select upper(trim(p_symbol)) as symbol
  ),
  mine as (
    select q.market_cap as cap
    from ledger.quote_detail q, me where q.symbol = me.symbol
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
             order by z.pri, z.ord, z.cap_gap nulls last,
                      z.market_cap desc nulls last, z.peer)
      from (
        select p.peer, p.pri, p.ord,
               coalesce(q.name, ihn.name, t.name) as name,
               q.market_cap,
               (pd.symbol is not null) as warm,
               -- How far apart the two companies are in size, in orders of
               -- magnitude. Zero for a curated peer, so the written order
               -- still decides there; for the industry fallback it is the
               -- whole ranking, which is what keeps a mid-cap from being
               -- read against the giants of a broadly drawn industry.
               case when p.pri < 3 then 0
                    when q.market_cap > 0 and (select cap from mine) > 0
                      then abs(ln(q.market_cap) - ln((select cap from mine)))
               end as cap_gap
        from pool p
        left join ledger.quote_detail q on q.symbol = p.peer
        left join ledger.price_daily pd on pd.symbol = p.peer
        left join lateral (
          select min(ih.name) as name from ledger.index_holding ih
          where ih.symbol = p.peer) ihn on true
        left join ledger.ticker t on t.ticker = p.peer
        order by p.pri, p.ord,
                 case when p.pri < 3 then 0
                      when q.market_cap > 0 and (select cap from mine) > 0
                        then abs(ln(q.market_cap) - ln((select cap from mine)))
                 end nulls last,
                 q.market_cap desc nulls last, p.peer
        limit 8
      ) z), '[]'::jsonb));
$$;

do $$
begin
  execute 'revoke all on function public.get_industry_peers(text) from public';
  execute 'grant execute on function public.get_industry_peers(text) to anon, authenticated';
end $$;
