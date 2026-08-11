-- Ticker Alpha — match politicians whose registered surname is two words
--
-- Apply after 0058. Safe to re-run.
--
-- The disclosure feed files "April Delaney"; the members directory registers
-- her surname as "McClain Delaney". 0058 matched on the whole last name plus
-- first initial, so she resolved to nobody and her card lost its photo and
-- party chip. The match now also accepts the final word of the registered
-- surname, which is the part a clerk shortens to.

create or replace function public.search_politicians(q text, lim integer default 8)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  with term as (
    select lower(trim(coalesce(q, ''))) as t
  ),
  traders as (
    select person, count(*) as trades, max(disclosed) as last_disclosed,
           max(chamber) as chamber_seen
    from ledger.congress_trade
    where person is not null and person ~* '[a-z]{2}'
    group by person
  ),
  hit as (
    select tr.person, tr.trades, tr.last_disclosed,
           p.bioguide, p.party, coalesce(p.chamber, tr.chamber_seen) as chamber,
           p.state, p.district
    from traders tr
    left join lateral (
      select * from ledger.politician p
      where p.norm = lower(tr.person)
         or (p.last_norm <> '' and
             left(p.first_norm, 1) = left(lower(tr.person), 1) and
             (p.last_norm = lower(split_part(tr.person, ' ', -1))
              or split_part(p.last_norm, ' ', -1) = lower(split_part(tr.person, ' ', -1))))
      order by (p.norm = lower(tr.person)) desc,
               (p.last_norm = lower(split_part(tr.person, ' ', -1))) desc
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
               left(p.first_norm, 1) = left(lower(trim(coalesce(p_person,''))), 1) and
               (p.last_norm = lower(split_part(trim(coalesce(p_person,'')), ' ', -1))
                or split_part(p.last_norm, ' ', -1)
                   = lower(split_part(trim(coalesce(p_person,'')), ' ', -1))))
        order by (p.norm = lower(trim(coalesce(p_person, '')))) desc,
                 (p.last_norm = lower(split_part(trim(coalesce(p_person,'')), ' ', -1))) desc
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

do $$
begin
  execute 'revoke all on function public.search_politicians(text, integer) from public';
  execute 'grant execute on function public.search_politicians(text, integer) to anon, authenticated';
  execute 'revoke all on function public.get_politician(text) from public';
  execute 'grant execute on function public.get_politician(text) to anon, authenticated';
end $$;
