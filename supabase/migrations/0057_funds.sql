-- Ticker Alpha — mutual funds and money market funds as a first-class asset
--
-- Apply after 0056. Safe to re-run.
--
-- ledger.market_symbol carried ETFs, crypto and the three indexes, so a fund
-- typed into the search box found nothing and a fund held in a portfolio was
-- counted as a stock. FMP prices funds perfectly well -- SPAXX and VMFXX quote
-- at their $1.00 NAV, FXAIX and VFIAX at a real one, all with daily history --
-- so the gap was ours.
--
-- Two kinds rather than one. A money market fund is pinned to $1.00 by design:
-- it never moves, and its whole return arrives as a monthly dividend. Marking
-- it separately lets the portfolio treat it as the cash-like thing it is,
-- rather than reporting a holding that is flat for ever, while still grouping
-- it under Funds where a reader looks for it.
--
-- search_companies is deliberately untouched. It already reads market_symbol
-- with no filter on kind, so a fund becomes searchable the moment it is
-- inserted -- and that function carries the crypto, index and brand-nickname
-- aliases, which are not worth risking for a change it does not need.

-- 'index' is kept: 0048 added it for SPX / IXIC / DJI and dropping it here
-- would take the index pages with it.
alter table ledger.market_symbol drop constraint if exists market_symbol_kind_check;
alter table ledger.market_symbol
  add constraint market_symbol_kind_check
  check (kind in ('etf', 'crypto', 'index', 'fund', 'money_market'));

-- Replaces 0048's, changing one line: the list of kinds it will accept. The
-- symbol pattern, the name requirement and the conflict behaviour are as they
-- were, because a fund sync has no reason to relax any of them.
create or replace function public.upsert_market_symbols(p_rows jsonb)
returns integer
language plpgsql volatile security definer
set search_path = ledger, pg_temp
as $$
declare n integer := 0;
begin
  if p_rows is null or jsonb_typeof(p_rows) <> 'array' then
    return 0;
  end if;

  insert into ledger.market_symbol (symbol, name, kind, exchange, updated_at)
  select upper(trim(r->>'symbol')),
         trim(r->>'name'),
         lower(trim(r->>'kind')),
         nullif(trim(r->>'exchange'), ''),
         now()
  from jsonb_array_elements(p_rows) r
  where nullif(trim(r->>'symbol'), '') is not null
    and nullif(trim(r->>'name'), '') is not null
    and lower(trim(r->>'kind')) in ('etf', 'crypto', 'index', 'fund', 'money_market')
    and upper(trim(r->>'symbol')) ~ '^[A-Z][A-Z0-9.\-]{0,9}$'
  on conflict (symbol) do update set
    name = excluded.name,
    kind = excluded.kind,
    exchange = excluded.exchange,
    updated_at = now();

  get diagnostics n = row_count;
  return n;
end;
$$;

-- ---------------------------------------------------------------------------
-- Money market funds, by hand
-- ---------------------------------------------------------------------------
-- company-screener?isFund=true returns a thousand-odd mutual funds and none of
-- these: a money market fund has no meaningful market capitalisation to rank
-- by, so it falls out of the screen. They are also among the funds most likely
-- to be held, so they are seeded rather than left to discovery. Names are FMP's
-- own, read back from its profile endpoint.
insert into ledger.market_symbol (symbol, name, kind, updated_at)
values
  ('SPAXX', 'Fidelity Government Money Market Fund',       'money_market', now()),
  ('FDRXX', 'Fidelity Cash Reserves',                      'money_market', now()),
  ('SPRXX', 'Fidelity Money Market Fund',                  'money_market', now()),
  ('FZFXX', 'Fidelity Treasury Money Market Fund',         'money_market', now()),
  ('VMFXX', 'Vanguard Federal Money Market Fund',          'money_market', now()),
  ('VMRXX', 'Vanguard Cash Reserves Federal Money Market', 'money_market', now()),
  ('VUSXX', 'Vanguard Treasury Money Market Fund',         'money_market', now()),
  ('SWVXX', 'Schwab Value Advantage Money Fund',           'money_market', now()),
  ('SNAXX', 'Schwab Value Advantage Money Fund Ultra',     'money_market', now()),
  ('SNSXX', 'Schwab U.S. Treasury Money Fund',             'money_market', now())
on conflict (symbol) do update
  set name = coalesce(excluded.name, ledger.market_symbol.name),
      kind = excluded.kind,
      updated_at = now();

do $$
begin
  execute 'revoke all on function public.upsert_market_symbols(jsonb) from public, anon, authenticated';
  execute 'grant execute on function public.upsert_market_symbols(jsonb) to service_role';
end $$;
