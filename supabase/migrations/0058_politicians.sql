-- Ticker Alpha — the Insider & Congress page
--
-- Apply after 0057. Safe to re-run.
--
-- Three things happen here, all in service of /politicians:
--
-- 1. A directory of sitting members of Congress (ledger.politician), synced
--    daily by the worker from the @unitedstates legislators file. The
--    disclosure feed only carries a name string; the directory is what turns
--    "Nancy Pelosi" into a party, a chamber, a district and — through the
--    bioguide id — a photo, served straight from unitedstates.github.io.
--
-- 2. Congress disclosures grow owner / district / link columns. FMP has
--    always sent them (owner is the "Spouse" / "Child" chip, link is the
--    actual disclosure PDF); the worker simply dropped them before this.
--    Until the next trades sweep runs, existing rows have them null — the
--    page treats null as "not stated".
--
-- 3. Read functions for the page: politician search, one politician's
--    trades, the most active traders, and the companies with the heaviest
--    insider activity. All read the same 60-day window the worker already
--    maintains; none of them add a single new fetch from any source.

-- ---------------------------------------------------------------------------
-- 1. The directory
-- ---------------------------------------------------------------------------

create table if not exists ledger.politician (
  bioguide   text primary key,
  full_name  text not null,
  first_name text,
  last_name  text,
  party      text,                -- 'D' / 'R' / 'I'
  chamber    text,                -- 'House' / 'Senate'
  state      text,
  district   text,
  -- Normalised join keys. The disclosure feed's person strings are plain
  -- "First Last"; the directory's official names carry middle names and
  -- suffixes, so exact-match alone would miss too many.
  norm       text not null,       -- lower(full_name)
  first_norm text,
  last_norm  text,
  updated_at timestamptz not null default now()
);
create index if not exists politician_norm_idx on ledger.politician (norm);
create index if not exists politician_last_idx on ledger.politician (last_norm);
alter table ledger.politician enable row level security;

create or replace function public.replace_politicians(p_rows jsonb)
returns integer
language plpgsql volatile security definer
set search_path = ledger, pg_temp
as $$
declare n integer;
begin
  -- Full replace: the source file is the complete current membership, so a
  -- member who leaves office disappears rather than lingering.
  delete from ledger.politician where bioguide is not null;
  insert into ledger.politician
    (bioguide, full_name, first_name, last_name, party, chamber, state,
     district, norm, first_norm, last_norm)
  select r->>'bioguide', r->>'full_name',
         nullif(r->>'first_name',''), nullif(r->>'last_name',''),
         nullif(r->>'party',''), nullif(r->>'chamber',''),
         nullif(r->>'state',''), nullif(r->>'district',''),
         lower(coalesce(r->>'full_name','')),
         lower(coalesce(nullif(r->>'first_name',''), '')),
         lower(coalesce(nullif(r->>'last_name',''), ''))
  from jsonb_array_elements(coalesce(p_rows, '[]'::jsonb)) r
  where nullif(r->>'bioguide','') is not null
    and nullif(r->>'full_name','') is not null
  on conflict (bioguide) do update
    set full_name = excluded.full_name, first_name = excluded.first_name,
        last_name = excluded.last_name, party = excluded.party,
        chamber = excluded.chamber, state = excluded.state,
        district = excluded.district, norm = excluded.norm,
        first_norm = excluded.first_norm, last_norm = excluded.last_norm,
        updated_at = now();
  get diagnostics n = row_count;
  return n;
end;
$$;

-- ---------------------------------------------------------------------------
-- 2. The columns the worker used to drop
-- ---------------------------------------------------------------------------

alter table ledger.congress_trade
  add column if not exists owner    text,   -- Self / Spouse / Child / Joint
  add column if not exists district text,   -- 'CA-11'
  add column if not exists link     text;   -- the disclosure document itself

create or replace function public.replace_trades(p_insiders jsonb, p_congress jsonb)
returns integer
language plpgsql volatile security definer
set search_path = ledger, pg_temp
as $$
declare n integer;
begin
  delete from ledger.insider_trade where id is not null;
  insert into ledger.insider_trade
    (filed, symbol, side, shares, amount, person, title, shares_out)
  select nullif(r->>'filed','')::date, upper(r->>'symbol'), r->>'side',
         (r->>'shares')::double precision, (r->>'amount')::double precision,
         r->>'person', nullif(r->>'title',''),
         nullif(r->>'shares_out','')::double precision
  from jsonb_array_elements(p_insiders) r
  where r->>'symbol' is not null;
  get diagnostics n = row_count;

  delete from ledger.congress_trade where id is not null;
  insert into ledger.congress_trade (disclosed, traded, symbol, person,
                                     chamber, side, amount,
                                     owner, district, link)
  select nullif(r->>'disclosed','')::date, nullif(r->>'traded','')::date,
         upper(r->>'symbol'), r->>'person', r->>'chamber', r->>'side',
         r->>'amount',
         nullif(r->>'owner',''), nullif(r->>'district',''), nullif(r->>'link','')
  from jsonb_array_elements(p_congress) r
  where r->>'symbol' is not null;
  return n;
end;
$$;

-- ---------------------------------------------------------------------------
-- 3. Reads
-- ---------------------------------------------------------------------------

-- One politician row, as the page shows it. The lateral picks the directory
-- entry for a disclosure name: exact normalised match first, then same last
-- name + same first initial, which is what catches "W. Gregory Steube"
-- filed as "Greg Steube".
create or replace function public.search_politicians(q text, lim integer default 8)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  with term as (
    select lower(trim(coalesce(q, ''))) as t
  ),
  -- Everyone who appears in the stored disclosure window, with their counts.
  traders as (
    select person, count(*) as trades, max(disclosed) as last_disclosed,
           max(chamber) as chamber_seen
    from ledger.congress_trade
    -- The feed occasionally files a row under a bare number where the name
    -- should be; a person needs at least two consecutive letters.
    where person is not null and person ~* '[a-z]{2}'
    group by person
  ),
  -- Directory joined on; a trader with no directory row still matches.
  hit as (
    select tr.person, tr.trades, tr.last_disclosed,
           p.bioguide, p.party, coalesce(p.chamber, tr.chamber_seen) as chamber,
           p.state, p.district
    from traders tr
    left join lateral (
      select * from ledger.politician p
      where p.norm = lower(tr.person)
         or (p.last_norm <> '' and
             p.last_norm = lower(split_part(tr.person, ' ', -1)) and
             left(p.first_norm, 1) = left(lower(tr.person), 1))
      order by (p.norm = lower(tr.person)) desc
      limit 1
    ) p on true
    where (select t from term) = ''
       or lower(tr.person) like '%' || (select t from term) || '%'
  )
  select coalesce(jsonb_agg(jsonb_build_object(
           'person', h.person, 'trades', h.trades,
           'lastDisclosed', h.last_disclosed, 'bioguide', h.bioguide,
           'party', h.party, 'chamber', h.chamber,
           'state', h.state, 'district', h.district)
         order by h.trades desc, h.person), '[]'::jsonb)
  from (select * from hit
        order by trades desc, person
        limit greatest(1, least(coalesce(lim, 8), 30))) h;
$$;

-- Everything stored for one person: identity plus every disclosure in the
-- window, company names joined the same way get_trades joins them.
create or replace function public.get_politician(p_person text)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  select jsonb_build_object(
    'identity', (
      select to_jsonb(x) from (
        select p.bioguide, p.full_name as "fullName", p.party, p.chamber,
               p.state, p.district
        from ledger.politician p
        where p.norm = lower(trim(coalesce(p_person, '')))
           or (p.last_norm <> '' and
               p.last_norm = lower(split_part(trim(coalesce(p_person,'')), ' ', -1)) and
               left(p.first_norm, 1) = left(lower(trim(coalesce(p_person,''))), 1))
        order by (p.norm = lower(trim(coalesce(p_person, '')))) desc
        limit 1
      ) x),
    'trades', coalesce((
      select jsonb_agg(jsonb_build_object(
               'disclosed', g.disclosed, 'traded', g.traded,
               'symbol', g.symbol, 'name', coalesce(t.name, c.name),
               'chamber', g.chamber, 'side', g.side, 'amount', g.amount,
               'owner', g.owner, 'district', g.district, 'link', g.link)
             order by g.traded desc nulls last, g.disclosed desc nulls last)
      from ledger.congress_trade g
      left join ledger.ticker  t on t.ticker = g.symbol
      left join ledger.company c on c.ticker = g.symbol
      where lower(g.person) = lower(trim(coalesce(p_person, '')))), '[]'::jsonb));
$$;

-- The most active traders in the window — the "follow the regulars" rail.
create or replace function public.get_congress_leaders(p_limit integer default 12)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  select public.search_politicians('', p_limit);
$$;

-- Companies ranked by insider activity over the stored window: how many
-- filings, how much bought, how much sold. Buys and sells are kept apart --
-- a company where one officer bought $10M and another sold $10M is not a
-- company where nothing happened, which is what a net figure would say.
create or replace function public.get_insider_leaders(p_limit integer default 15)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  select coalesce(jsonb_agg(jsonb_build_object(
           'symbol', x.symbol, 'name', x.name,
           'filings', x.filings, 'buyAmt', x.buy_amt, 'sellAmt', x.sell_amt,
           'people', x.people, 'lastFiled', x.last_filed)
         order by x.buy_amt + x.sell_amt desc), '[]'::jsonb)
  from (
    select i.symbol, coalesce(max(t.name), max(c.name)) as name,
           count(*) as filings,
           sum(greatest(coalesce(i.amount, 0), 0))       as buy_amt,
           sum(greatest(-coalesce(i.amount, 0), 0))      as sell_amt,
           count(distinct i.person) as people,
           max(i.filed) as last_filed
    from ledger.insider_trade i
    left join ledger.ticker  t on t.ticker = i.symbol
    left join ledger.company c on c.ticker = i.symbol
    where i.symbol is not null
    group by i.symbol
    order by sum(abs(coalesce(i.amount, 0))) desc
    limit greatest(1, least(coalesce(p_limit, 15), 50))
  ) x;
$$;

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

do $$
begin
  execute 'revoke all on function public.replace_politicians(jsonb) from public, anon, authenticated';
  execute 'grant execute on function public.replace_politicians(jsonb) to service_role';
  execute 'revoke all on function public.replace_trades(jsonb, jsonb) from public, anon, authenticated';
  execute 'grant execute on function public.replace_trades(jsonb, jsonb) to service_role';
  execute 'revoke all on function public.search_politicians(text, integer) from public';
  execute 'grant execute on function public.search_politicians(text, integer) to anon, authenticated';
  execute 'revoke all on function public.get_politician(text) from public';
  execute 'grant execute on function public.get_politician(text) to anon, authenticated';
  execute 'revoke all on function public.get_congress_leaders(integer) from public';
  execute 'grant execute on function public.get_congress_leaders(integer) to anon, authenticated';
  execute 'revoke all on function public.get_insider_leaders(integer) from public';
  execute 'grant execute on function public.get_insider_leaders(integer) to anon, authenticated';
end $$;
