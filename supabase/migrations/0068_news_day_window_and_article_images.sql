-- Ticker Alpha — a 24-hour front page, and the articles' own photographs
--
-- Apply after 0067. Safe to re-run.
--
-- Two front-page complaints with one cause. The home page fetched the newest
-- 120 rows, which on a wire this busy covers four or five hours, not a day.
-- And most of those rows are press releases whose "image" is a generic stock
-- photo the feed pastes on — the "Press Release" banners — not anything from
-- the story itself.
--
-- So: a read that serves the whole last day in one call, seating newsroom
-- stories ahead of wire copy when the day is bigger than the cap; and image
-- provenance on ledger.news, so the worker can swap a pasted-on photo for the
-- article page's own og:image and the next feed refresh cannot paste back
-- over it.

-- Where an image came from: null for the feed's own, 'article' once the
-- worker has read it off the article page. checked_at marks the attempt,
-- success or not, so no URL is scraped twice.
alter table ledger.news add column if not exists image_src text;
alter table ledger.news add column if not exists image_checked_at timestamptz;

-- ---------------------------------------------------------------------------
-- Read — the front page's whole day in one call
-- ---------------------------------------------------------------------------
-- When the window holds more stories than the cap, the wire services are the
-- ones that overflow: a press release dropped from the tail costs the page
-- nothing, a Reuters story dropped from the tail is the page. Summaries are
-- trimmed because a card never shows more than a couple of sentences.
create or replace function public.get_news_home(p_hours integer default 24,
                                                p_limit integer default 500)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  with win as (
    select n.*,
           coalesce(n.publisher, '')
             ~* '(newswire|business ?wire|accesswire|newsfile|prweb|press release)'
             as wire
    from ledger.news n
    where n.published > now()
        - make_interval(hours => least(greatest(coalesce(p_hours, 24), 1), 48))
  ),
  pick as (
    select * from win
    order by wire, published desc
    limit least(greatest(coalesce(p_limit, 500), 1), 600)
  )
  select jsonb_build_object(
    'total',  (select count(*) from win),
    'newest', (select max(published) from win),
    'articles', coalesce((
      select jsonb_agg(jsonb_build_object(
               'url', a.url, 'title', a.title,
               'summary', case when length(a.summary) > 300
                               then left(a.summary, 297) || '…' else a.summary end,
               'image', a.image, 'imageSrc', a.image_src,
               'publisher', a.publisher, 'symbol', a.symbol,
               'published', a.published)
             order by a.published desc)
      from pick a), '[]'::jsonb));
$$;

-- ---------------------------------------------------------------------------
-- Worker — which articles still need a real photograph
-- ---------------------------------------------------------------------------
-- Recent stories whose image cannot be trusted: wire copy (whatever image it
-- carries is decoration) and anything with no image at all. Newest first, so
-- the stories on the front page are the first to get their picture.
create or replace function public.news_image_queue(p_limit integer default 20)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  select coalesce(jsonb_agg(jsonb_build_object('url', q.url, 'publisher', q.publisher)
                            order by q.published desc), '[]'::jsonb)
  from (
    select n.url, n.publisher, n.published
    from ledger.news n
    where n.published > now() - interval '48 hours'
      and n.image_checked_at is null
      and (n.image is null
           or coalesce(n.publisher, '')
              ~* '(newswire|business ?wire|accesswire|newsfile|prweb|press release)')
    order by n.published desc
    limit least(greatest(coalesce(p_limit, 20), 1), 100)
  ) q;
$$;

-- Every attempted URL is recorded, found or not — image_checked_at is what
-- keeps a page that has no og:image from being fetched again every cycle.
create or replace function public.set_news_images(p_rows jsonb)
returns integer
language plpgsql volatile security definer
set search_path = ledger, pg_temp
as $$
declare n integer;
begin
  if p_rows is null or jsonb_typeof(p_rows) <> 'array' then
    return 0;
  end if;
  update ledger.news t
     set image            = coalesce(nullif(r.value->>'image', ''), t.image),
         image_src        = case when nullif(r.value->>'image', '') is not null
                                 then 'article' else t.image_src end,
         image_checked_at = now()
    from jsonb_array_elements(p_rows) r
   where t.url = r.value->>'url';
  get diagnostics n = row_count;
  return n;
end;
$$;

-- ---------------------------------------------------------------------------
-- upsert_news — the feed must not paste back over a scraped photograph
-- ---------------------------------------------------------------------------
-- Same body as 0025 except the conflict arm: once image_src is 'article' the
-- stored image is the article's own and the feed's copy is ignored.
create or replace function public.upsert_news(p_rows jsonb, p_keywords jsonb)
returns integer
language plpgsql volatile security definer
set search_path = ledger, pg_temp
set statement_timeout = '60s'
as $$
declare n integer;
begin
  insert into ledger.news (url, title, summary, image, publisher, symbol,
                           published, kind, updated_at)
  select distinct on (r->>'url')
         r->>'url', r->>'title', r->>'summary', r->>'image', r->>'publisher',
         nullif(r->>'symbol',''), nullif(r->>'published','')::timestamptz,
         r->>'kind', now()
  from jsonb_array_elements(p_rows) r
  where r->>'url' is not null and r->>'title' is not null
  order by r->>'url'
  on conflict (url) do update
    set title = excluded.title, summary = excluded.summary,
        image = case when news.image_src = 'article' and news.image is not null
                     then news.image else excluded.image end,
        publisher = excluded.publisher,
        symbol = excluded.symbol, published = excluded.published,
        updated_at = now();
  get diagnostics n = row_count;

  delete from ledger.news_keyword where word is not null;
  insert into ledger.news_keyword (word, query, count, kind, ord, updated_at)
  select distinct on (r->>'word')
         r->>'word', coalesce(nullif(r->>'query',''), r->>'word'),
         0, r->>'kind', (r->>'ord')::integer, now()
  from jsonb_array_elements(p_keywords) r
  where r->>'word' is not null
  order by r->>'word';

  delete from ledger.news where published < now() - interval '7 days';

  -- Same match rules the filter uses, once per refresh rather than once per
  -- page view. statement_timeout is raised above so a large corpus cannot
  -- abort the write the way it aborted the anon read.
  update ledger.news_keyword w
     set count = (
           select count(*)::integer from ledger.news n
           where n.symbol = upper(coalesce(w.query, w.word))
              or n.title   ilike '%' || coalesce(w.query, w.word) || '%'
              or n.summary ilike '%' || coalesce(w.query, w.word) || '%'),
         updated_at = now()
   where w.word is not null;

  return n;
end;
$$;

do $$
begin
  execute 'revoke all on function public.get_news_home(integer, integer) from public';
  execute 'grant execute on function public.get_news_home(integer, integer) to anon, authenticated';

  execute 'revoke all on function public.news_image_queue(integer) from public, anon, authenticated';
  execute 'grant execute on function public.news_image_queue(integer) to service_role';

  execute 'revoke all on function public.set_news_images(jsonb) from public, anon, authenticated';
  execute 'grant execute on function public.set_news_images(jsonb) to service_role';

  execute 'revoke all on function public.upsert_news(jsonb, jsonb) from public, anon, authenticated';
  execute 'grant execute on function public.upsert_news(jsonb, jsonb) to service_role';
end $$;
