-- Ticker Alpha — a lower bar for an insider filing
--
-- Apply after 0080. Safe to re-run.
--
-- The materiality floor drops from $1M to $200K; the 1%-of-shares-outstanding
-- alternative is unchanged. $1M was set to keep a table that ranks the whole
-- market's Form 4s readable, but it also threw away most of what an officer
-- actually does: a chief executive putting a few hundred thousand dollars of
-- their own money into their own company on the open market is a smaller
-- number and a better signal than a scheduled seven-figure sale.
--
-- Two consequences worth stating.
--
-- Volume. Around 5,900 filings clear $1M over ninety days. A fifth of that
-- floor admits several times as many -- comfortably past the 8,000 rows the
-- worker used to write in a single call, which would have quietly truncated
-- the oldest days and shortened the window the page reads. append_trades
-- writes the same content in chunks: the first call clears both tables and
-- carries its share of the rows, the rest add to it, so no single request has
-- to carry the whole ninety days.
--
-- Cost. None at the vendor: the floor is applied after the pull, so the same
-- pages are fetched either way. What grows is rows stored, not bandwidth.

create or replace function public.get_trades(
  p_limit integer default 20,
  p_offset integer default 0,
  p_days integer default 7,
  p_kind text default null,
  p_symbols text[] default null
)
returns jsonb
language plpgsql stable security definer
set search_path = ledger, pg_temp
as $$
declare
  v_limit  integer := greatest(1, least(coalesce(p_limit, 20), 100));
  v_offset integer := greatest(0, coalesce(p_offset, 0));
  v_days   integer := greatest(1, least(coalesce(p_days, 7), 90));
  v_kind   text    := lower(nullif(trim(coalesce(p_kind, '')), ''));
  v_cut    date    := current_date - v_days;
  v_ins_min double precision := 200000;   -- $200K
  v_pct    double precision := 0.01;      -- 1% of shares outstanding
  v_syms   text[];
  v_insiders jsonb := '[]'::jsonb;
  v_congress jsonb := '[]'::jsonb;
  v_ins_total integer := 0;
  v_con_total integer := 0;
begin
  select array_agg(distinct upper(trim(s)))
    into v_syms
    from unnest(coalesce(p_symbols, array[]::text[])) as s
   where upper(trim(s)) ~ '^[A-Z][A-Z.\-]{0,9}$';

  if v_kind is null or v_kind = 'insider' or v_kind = 'insiders' then
    select count(*)::integer into v_ins_total
    from ledger.insider_trade i
    where i.filed >= v_cut
      and (v_syms is null or i.symbol = any (v_syms))
      and (
        abs(coalesce(i.amount, 0)) > v_ins_min
        or (
          coalesce(i.shares_out, 0) > 0
          and coalesce(i.shares, 0) / i.shares_out >= v_pct
        )
      );

    select coalesce(jsonb_agg(jsonb_build_object(
             'filed', x.filed, 'symbol', x.symbol,
             'name', x.name, 'side', x.side, 'shares', x.shares,
             'amount', x.amount, 'person', x.person, 'title', x.title,
             'sharesOut', x.shares_out)
           order by x.filed desc nulls last, x.amount desc nulls last), '[]'::jsonb)
      into v_insiders
    from (
      select i.filed, i.symbol, i.side, i.shares, i.amount, i.person, i.title,
             i.shares_out, coalesce(t.name, c.name) as name
      from ledger.insider_trade i
      left join ledger.ticker  t on t.ticker = i.symbol
      left join ledger.company c on c.ticker = i.symbol
      where i.filed >= v_cut
        and (v_syms is null or i.symbol = any (v_syms))
        and (
          abs(coalesce(i.amount, 0)) > v_ins_min
          or (
            coalesce(i.shares_out, 0) > 0
            and coalesce(i.shares, 0) / i.shares_out >= v_pct
          )
        )
      order by i.filed desc nulls last, i.amount desc nulls last
      limit v_limit offset v_offset
    ) x;
  end if;

  if v_kind is null or v_kind = 'congress' then
    select count(*)::integer into v_con_total
    from ledger.congress_trade g
    where g.disclosed >= v_cut
      and (v_syms is null or g.symbol = any (v_syms));

    select coalesce(jsonb_agg(jsonb_build_object(
             'disclosed', x.disclosed, 'traded', x.traded, 'symbol', x.symbol,
             'name', x.name, 'person', x.person, 'chamber', x.chamber,
             'side', x.side, 'amount', x.amount)
           order by x.disclosed desc nulls last), '[]'::jsonb)
      into v_congress
    from (
      select g.disclosed, g.traded, g.symbol, g.person, g.chamber, g.side, g.amount,
             coalesce(t.name, c.name) as name
      from ledger.congress_trade g
      left join ledger.ticker  t on t.ticker = g.symbol
      left join ledger.company c on c.ticker = g.symbol
      where g.disclosed >= v_cut
        and (v_syms is null or g.symbol = any (v_syms))
      order by g.disclosed desc nulls last
      limit v_limit offset v_offset
    ) x;
  end if;

  return jsonb_build_object(
    'insiders', v_insiders,
    'congress', v_congress,
    'insidersTotal', v_ins_total,
    'congressTotal', v_con_total,
    'days', v_days,
    'limit', v_limit,
    'offset', v_offset,
    'minInsiderAmount', v_ins_min,
    'minCongressAmount', 0,
    'minSharesPct', v_pct);
end;
$$;

-- ---------------------------------------------------------------------------
-- Writing the market-wide tape in pieces
-- ---------------------------------------------------------------------------
-- replace_trades sends everything in one call, which stops scaling once a
-- lower floor multiplies the row count. This is the same write split up:
-- `p_reset` empties both tables and is passed on the first chunk only, so
-- the tables are never committed empty -- a reader during the refresh sees
-- part of the new tape rather than none of it.
create or replace function public.append_trades(
    p_insiders jsonb default '[]'::jsonb,
    p_congress jsonb default '[]'::jsonb,
    p_reset boolean default false)
returns integer
language plpgsql volatile security definer
set search_path = ledger, pg_temp
as $$
declare n integer := 0;
begin
  if coalesce(p_reset, false) then
    delete from ledger.insider_trade where id is not null;
    delete from ledger.congress_trade where id is not null;
  end if;

  if p_insiders is not null and jsonb_typeof(p_insiders) = 'array' then
    insert into ledger.insider_trade
      (filed, symbol, side, shares, amount, person, title, shares_out)
    select nullif(r->>'filed', '')::date,
           upper(trim(r->>'symbol')),
           nullif(trim(r->>'side'), ''),
           nullif(r->>'shares', '')::double precision,
           nullif(r->>'amount', '')::double precision,
           nullif(trim(r->>'person'), ''),
           nullif(trim(r->>'title'), ''),
           nullif(r->>'shares_out', '')::double precision
    from jsonb_array_elements(p_insiders) r
    where nullif(trim(r->>'symbol'), '') is not null;
    get diagnostics n = row_count;
  end if;

  if p_congress is not null and jsonb_typeof(p_congress) = 'array' then
    insert into ledger.congress_trade
      (disclosed, traded, symbol, person, chamber, side, amount,
       owner, district, link)
    select nullif(r->>'disclosed', '')::date,
           nullif(r->>'traded', '')::date,
           upper(trim(r->>'symbol')),
           nullif(trim(r->>'person'), ''),
           nullif(trim(r->>'chamber'), ''),
           nullif(trim(r->>'side'), ''),
           nullif(trim(r->>'amount'), ''),
           nullif(r->>'owner', ''),
           nullif(r->>'district', ''),
           nullif(r->>'link', '')
    from jsonb_array_elements(p_congress) r
    where nullif(trim(r->>'symbol'), '') is not null;
  end if;

  return n;
end;
$$;

do $$
begin
  execute 'revoke all on function public.get_trades(integer, integer, integer, text, text[]) from public';
  execute 'grant execute on function public.get_trades(integer, integer, integer, text, text[]) to anon, authenticated';

  execute 'revoke all on function public.append_trades(jsonb, jsonb, boolean) from public, anon, authenticated';
  execute 'grant execute on function public.append_trades(jsonb, jsonb, boolean) to service_role';
end $$;
