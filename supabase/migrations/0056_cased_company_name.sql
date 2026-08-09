-- Ticker Alpha — return the company name the way people write it
--
-- Apply after 0055. Safe to re-run.
--
-- EDGAR stores registered names in capitals and companyfacts hands them over
-- that way, so SpaceX's page was headed SPACE EXPLORATION TECHNOLOGIES CORP --
-- which reads as a different company from the one everything else calls Space
-- Exploration Technologies Corp.
--
-- The market feed already holds the same name properly cased, including the
-- capitals inside a word that no title-casing rule can recover: AstraZeneca,
-- not Astrazeneca. So this prefers ledger.quote.name over the filed name, and
-- only where the filed name is entirely uppercase -- a company already cased in
-- its own filings keeps exactly what it filed.
--
-- Doing it here rather than in the page means every reader of get_company gets
-- it, and a stale browser cache cannot undo it. Replaces 0003's.

create or replace function public.get_company(p_ticker text)
returns jsonb
language sql
stable
security definer
set search_path = ledger, pg_temp
as $$
  select jsonb_build_object(
    'ticker',             c.ticker,
    -- Uppercase-only names defer to the feed; anything already cased is left
    -- exactly as filed. The join is on the ticker, so the feed name is for this
    -- listing -- no further guard is worth the SQL to express it.
    'name',               case
                            when c.name ~ '[a-z]' then c.name
                            when coalesce(q.name, '') = '' then c.name
                            else q.name
                          end,
    'filedName',          c.name,
    'cik',                c.cik,
    'exchange',           c.exchange,
    'sic',                c.sic,
    'fiscalYearEndMonth', c.fye_month,
    'checkedAt',          c.checked_at,
    'source', jsonb_build_object(
      'facts',   'https://data.sec.gov/api/xbrl/companyfacts/CIK'
                 || lpad(c.cik::text, 10, '0') || '.json',
      'filings', 'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK='
                 || lpad(c.cik::text, 10, '0') || '&type=10-'),
    'quarters', coalesce((
      select jsonb_agg(jsonb_build_object(
               'end',        to_char(q2.period_end, 'YYYY-MM-DD'),
               'start',      to_char(q2.start_date, 'YYYY-MM-DD'),
               'fy',         q2.fy,
               'fq',         q2.fq,
               'label',      'Q' || q2.fq || ' ' || q2.fy,
               'basis',      q2.basis,
               'form',       q2.form,
               'accession',  q2.accession,
               'filed',      to_char(q2.filed_date, 'YYYY-MM-DD'),
               'lines',      q2.lines,
               'provenance', q2.provenance)
             order by q2.period_end desc)
      from ledger.quarter q2 where q2.cik = c.cik), '[]'::jsonb))
  from ledger.company c
  left join ledger.quote q on q.symbol = c.ticker
  where c.cik = (select t.cik from ledger.ticker t where t.ticker = upper(p_ticker));
$$;

do $$
begin
  execute 'revoke all on function public.get_company(text) from public';
  execute 'grant execute on function public.get_company(text) to anon, authenticated';
end $$;
