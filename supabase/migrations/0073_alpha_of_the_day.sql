-- Ticker Alpha — Alpha of the Day: one scored idea list per day, kept forever
--
-- Apply after 0072. Safe to re-run.
--
-- The nightly scan scores the S&P 500 + Nasdaq-100 universe and keeps a short
-- list: one headline pick and the runner-up buy and sell candidates. This
-- table is that list, one row per (day, symbol), written in one shot when the
-- scan finishes. History stays: yesterday's ideas are tomorrow's track record,
-- and the scan itself reads the last 14 days back to enforce its repeat
-- cooldown ("not picked in the last 14 days").
--
-- result_pct starts null and is filled in by the next scan, once the day
-- after the call is known — a pick without a result is simply one whose
-- next session hasn't closed yet.

create table if not exists ledger.alpha_pick (
  day        date not null,
  symbol     text not null,
  side       text not null check (side in ('BUY', 'SELL')),
  rank       integer not null,            -- 1 = the pick; then 2.. per side
  is_pick    boolean not null default false,
  score      integer not null,            -- composite 0-100
  families   jsonb,                       -- per-family scores, e.g. {"hist":71,"val":88,...}
  gates      jsonb,                       -- gate checks the pick cleared, [{"label":...,"ok":true},...]
  evidence   jsonb,                       -- the chips: congress $, insider cluster, P/E vs peers, ...
  headline   text,                        -- the one-sentence case shown on the page
  price      double precision,            -- last price when scored
  result_pct double precision,            -- next-session return, filled by a later scan
  created_at timestamptz not null default now(),
  primary key (day, symbol)
);

create index if not exists alpha_pick_day_idx
  on ledger.alpha_pick (day desc);

alter table ledger.alpha_pick enable row level security;

-- ---------------------------------------------------------------------------
-- Write — the worker only
-- ---------------------------------------------------------------------------
-- Replace the whole day rather than upsert: a re-run of the scan (a fixed
-- input, a manual retry) should leave exactly its own list, not a union with
-- the earlier attempt's. distinct on, not on conflict, for the same reason
-- as 0072: after the delete the only possible duplicate is one the payload
-- carries itself.
create or replace function public.record_alpha_day(p_day date, p_ideas jsonb)
returns integer
language plpgsql volatile security definer
set search_path = ledger, pg_temp
as $$
declare
  n integer;
begin
  if p_day is null or p_ideas is null or jsonb_typeof(p_ideas) <> 'array' then
    return 0;
  end if;

  delete from ledger.alpha_pick where day = p_day;

  insert into ledger.alpha_pick
    (day, symbol, side, rank, is_pick, score,
     families, gates, evidence, headline, price, created_at)
  select distinct on (t.sym)
         p_day, t.sym,
         upper(t.r->>'side'),
         coalesce(nullif(t.r->>'rank','')::integer, 99),
         coalesce((t.r->>'isPick')::boolean, false),
         nullif(t.r->>'score','')::integer,
         t.r->'families',
         t.r->'gates',
         t.r->'evidence',
         nullif(t.r->>'headline',''),
         nullif(t.r->>'price','')::double precision,
         now()
    from (
      select upper(trim(coalesce(e.value->>'symbol',''))) as sym, e.value as r, e.ord
        from jsonb_array_elements(p_ideas) with ordinality as e(value, ord)
    ) t
   where t.sym ~ '^[A-Z][A-Z.\-]{0,9}$'
     and upper(t.r->>'side') in ('BUY', 'SELL')
     and nullif(t.r->>'score','') is not null
   order by t.sym, t.ord;

  get diagnostics n = row_count;
  return n;
end;
$$;

-- The next scan writes yesterday's outcome once it knows the close that
-- followed the call. Scoped to one (day, symbol) so a restated price can be
-- corrected without touching the rest of the day.
create or replace function public.record_alpha_result(
  p_day date, p_symbol text, p_pct double precision)
returns integer
language plpgsql volatile security definer
set search_path = ledger, pg_temp
as $$
declare
  v_sym text := upper(trim(coalesce(p_symbol, '')));
  n integer;
begin
  if p_day is null or v_sym !~ '^[A-Z][A-Z.\-]{0,9}$' then
    return 0;
  end if;
  update ledger.alpha_pick
     set result_pct = p_pct
   where day = p_day and symbol = v_sym;
  get diagnostics n = row_count;
  return n;
end;
$$;

-- ---------------------------------------------------------------------------
-- Read — the Alpha of the Day page
-- ---------------------------------------------------------------------------
-- Like get_market_brief: no day means the most recent scored day, so the page
-- always shows the last run rather than a blank weekend. 'day' says which run
-- the ideas belong to — the page dates the banner from it, not from today.
create or replace function public.get_alpha_day(p_day date default null)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  with d as (
    select coalesce(p_day, max(day)) as day
    from ledger.alpha_pick
  )
  select jsonb_build_object(
    'day', (select day from d),

    'ideas', (
      select jsonb_agg(jsonb_build_object(
               'symbol', a.symbol,
               'side', a.side,
               'rank', a.rank,
               'isPick', a.is_pick,
               'score', a.score,
               'families', a.families,
               'gates', a.gates,
               'evidence', a.evidence,
               'headline', a.headline,
               'price', a.price,
               'resultPct', a.result_pct)
             order by a.is_pick desc, a.score desc, a.symbol)
      from ledger.alpha_pick a
      where a.day = (select day from d))
  );
$$;

-- Headline picks only, newest first: the "last week's picks" strip and the
-- track-record page — and the scan's own 14-day repeat cooldown, which is
-- why service_role can call it too.
create or replace function public.get_alpha_track_record(p_days integer default 30)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  select coalesce(jsonb_agg(jsonb_build_object(
           'day', a.day,
           'symbol', a.symbol,
           'side', a.side,
           'score', a.score,
           'price', a.price,
           'resultPct', a.result_pct)
         order by a.day desc), '[]'::jsonb)
  from ledger.alpha_pick a
  where a.is_pick
    and a.day >= current_date - greatest(1, least(coalesce(p_days, 30), 365));
$$;

do $$
begin
  execute 'revoke all on function public.record_alpha_day(date, jsonb) from public, anon, authenticated';
  execute 'grant execute on function public.record_alpha_day(date, jsonb) to service_role';

  execute 'revoke all on function public.record_alpha_result(date, text, double precision) from public, anon, authenticated';
  execute 'grant execute on function public.record_alpha_result(date, text, double precision) to service_role';

  execute 'revoke all on function public.get_alpha_day(date) from public';
  execute 'grant execute on function public.get_alpha_day(date) to anon, authenticated';

  execute 'revoke all on function public.get_alpha_track_record(integer) from public';
  execute 'grant execute on function public.get_alpha_track_record(integer) to anon, authenticated, service_role';
end $$;
