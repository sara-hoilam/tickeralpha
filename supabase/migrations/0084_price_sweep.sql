-- Ticker Alpha — every cached chart is brought up to the last session nightly
--
-- Apply after 0083. Safe to re-run.
--
-- The stale report showed the shape of the problem: 869 charts cached, 757 of
-- them behind the last session, most last written the day somebody looked at
-- them. The feed was fine. Prices were only ever refreshed for two reasons --
-- a visitor opened the page, or the name sat in the warm pool of ~75 large
-- caps and movers -- and everything else kept the bars from its last visit.
-- The Alpha scan, the peer charts and the screeners read those bars straight
-- from the table, so they were quietly working from August.
--
-- The worker now sweeps: every cached symbol whose newest bar is behind the
-- last session is asked for only the bars it is missing, which the merge
-- below folds into the stored series. One small request per name per night,
-- not a 300-day refetch, so the bandwidth cost stays in the kilobytes.
--
-- swept_at records the attempt, whatever came back, so a name the feed no
-- longer answers for is asked once a night rather than every pass.

alter table ledger.price_daily add column if not exists swept_at timestamptz;

-- The names to sweep next: behind the last session and not tried in the last
-- twenty hours. Recently touched rows first -- a name somebody opened this
-- week matters more than one nobody has looked at since spring.
create or replace function public.price_sweep_due(p_limit integer default 25)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  select coalesce(jsonb_agg(jsonb_build_object('symbol', x.symbol, 'asOf', x.as_of)
                            order by x.updated_at desc, x.symbol), '[]'::jsonb)
  from (
    select d.symbol, d.as_of, d.updated_at
    from ledger.price_daily d
    where (d.as_of is null or d.as_of < ledger.last_session())
      and (d.swept_at is null or d.swept_at < now() - interval '20 hours')
    order by d.updated_at desc, d.symbol
    limit greatest(1, least(coalesce(p_limit, 25), 200))
  ) x;
$$;

-- Fold freshly fetched bars into the stored series. New bars win on a date
-- both sides hold (a late correction replaces the provisional close), the
-- series stays oldest-first, and anything older than the window the chart
-- keeps is dropped so the row does not grow without bound. An empty p_bars
-- only stamps swept_at: the chart is left exactly as it was.
create or replace function public.merge_price_bars(
    p_symbol text, p_bars jsonb, p_keep_days integer default 320)
returns jsonb
language plpgsql volatile security definer
set search_path = ledger, pg_temp
as $$
declare
  sym    text := upper(trim(coalesce(p_symbol, '')));
  cutoff date := ledger.last_session() - greatest(30, coalesce(p_keep_days, 320));
  merged jsonb;
  newest date;
begin
  if sym = '' then
    return jsonb_build_object('merged', false, 'reason', 'no symbol');
  end if;

  if p_bars is null or jsonb_typeof(p_bars) <> 'array'
     or jsonb_array_length(p_bars) = 0 then
    update ledger.price_daily set swept_at = now() where symbol = sym;
    return jsonb_build_object('merged', false, 'reason', 'no bars',
                              'asOf', (select as_of from ledger.price_daily
                                       where symbol = sym));
  end if;

  with fresh as (
    select n.value as bar, (n.value->>'d')::date as day
    from jsonb_array_elements(p_bars) n
    where n.value ? 'd' and n.value->>'c' is not null
  ),
  kept as (
    select e.value as bar, (e.value->>'d')::date as day
    from ledger.price_daily d
    cross join lateral jsonb_array_elements(d.bars) e
    where d.symbol = sym
      and not exists (select 1 from fresh f where f.day = (e.value->>'d')::date)
  ),
  joined as (
    select bar, day from kept
    union all
    select bar, day from fresh
  )
  select jsonb_agg(b.bar order by b.day), max(b.day)
    into merged, newest
  from joined b
  where b.day >= cutoff;

  if merged is null then
    update ledger.price_daily set swept_at = now() where symbol = sym;
    return jsonb_build_object('merged', false, 'reason', 'nothing in window');
  end if;

  insert into ledger.price_daily (symbol, bars, as_of, updated_at, swept_at)
  values (sym, merged, newest, now(), now())
  on conflict (symbol) do update
    set bars = excluded.bars, as_of = excluded.as_of,
        updated_at = now(), swept_at = now();

  return jsonb_build_object('merged', true, 'asOf', newest,
                            'bars', jsonb_array_length(merged));
end;
$$;

-- The stale report now says when each row was last swept, so "behind and
-- never swept" reads differently from "behind, swept last night, feed gave
-- nothing".
create or replace function public.stale_price_symbols(p_limit integer default 50)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  select jsonb_build_object(
    'lastSession', ledger.last_session(),
    'cached', (select count(*) from ledger.price_daily),
    'behind', (select count(*) from ledger.price_daily d
               where d.as_of is null or d.as_of < ledger.last_session()),
    'dueNow', (select count(*) from ledger.price_daily d
               where (d.as_of is null or d.as_of < ledger.last_session())
                 and (d.swept_at is null or d.swept_at < now() - interval '20 hours')),
    'rows', coalesce((
      select jsonb_agg(jsonb_build_object(
               'symbol', x.symbol,
               'asOf', x.as_of,
               'sessionsBehind', x.behind,
               'writtenAt', x.updated_at,
               'sweptAt', x.swept_at,
               'queued', x.queued)
             order by x.as_of nulls first, x.symbol)
      from (
        select d.symbol, d.as_of, d.updated_at, d.swept_at,
               (select count(*) from generate_series(
                  coalesce(d.as_of, ledger.last_session() - 30) + 1,
                  ledger.last_session(), interval '1 day') g
                where extract(isodow from g) < 6) as behind,
               exists (select 1 from ledger.price_request r
                       where r.symbol = d.symbol and r.done_at is null) as queued
        from ledger.price_daily d
        where d.as_of is null or d.as_of < ledger.last_session()
        order by d.as_of nulls first, d.symbol
        limit greatest(1, least(coalesce(p_limit, 50), 500))
      ) x), '[]'::jsonb));
$$;

do $$
begin
  execute 'revoke all on function public.price_sweep_due(integer) from public, anon, authenticated';
  execute 'grant execute on function public.price_sweep_due(integer) to service_role';

  execute 'revoke all on function public.merge_price_bars(text, jsonb, integer) from public, anon, authenticated';
  execute 'grant execute on function public.merge_price_bars(text, jsonb, integer) to service_role';

  execute 'revoke all on function public.stale_price_symbols(integer) from public, anon, authenticated';
  execute 'grant execute on function public.stale_price_symbols(integer) to service_role';
end $$;
