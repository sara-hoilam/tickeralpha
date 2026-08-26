-- Ticker Alpha — the Discover carousels deserve the articles' own photographs
--
-- Apply after 0070. Safe to re-run.
--
-- The News page learned to tell a real article photograph (image_src =
-- 'article', scraped from the story's own page) from the feed's pasted-on
-- thumbnails, and to distrust wire publishers' decoration. The Markets Today
-- and Portfolio pages read the same corpus through get_discover_news,
-- get_news and get_news_feed — none of which said where an image came from,
-- so those carousels kept printing stock photos.
--
-- Three changes, all additive to callers:
--   - get_discover_news returns imageSrc and ranks by *trusted* images —
--     an article's own photo, or a feed image on a non-wire story — instead
--     of any image at all;
--   - get_news returns imageSrc on each article;
--   - get_news_feed now carries image + imageSrc, so the portfolio page's
--     merge of feed rows stops producing pictureless cards.

create or replace function public.get_discover_news(p_limit integer default 12)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  with top_cap as (
    select q.symbol
    from ledger.quote q
    where q.market_cap is not null
      and q.price is not null
    order by q.market_cap desc nulls last
    limit 20
  ),
  -- Prefer stories with an image worth printing: the article page's own
  -- photograph, or a feed image on a story no wire service decorated.
  ranked as (
    select n.url, n.title, n.summary, n.image, n.image_src, n.publisher,
           n.symbol, n.published,
           -- image_src is null for feed images, and null = 'article' is
           -- null, not false — which order by ... desc would seat FIRST.
           (n.image is not null and n.image <> '' and
            (coalesce(n.image_src, '') = 'article'
             or coalesce(n.publisher, '')
                !~* '(newswire|business ?wire|accesswire|newsfile|prweb|press release)'))
             as has_image
    from ledger.news n
    where n.published > now() - interval '7 days'
      and (
        n.symbol in (select symbol from top_cap)
        -- Ticker mentioned in the headline even when FMP left symbol null.
        or exists (
          select 1 from top_cap t
          where length(t.symbol) >= 2
            and n.title ~* ('(^|[^A-Za-z])' || t.symbol || '([^A-Za-z]|$)'))
        or n.title ~* '(fed|fomc|tariff|tariffs|interest rates?|inflation|wars?|sanctions?|white house|treasury|recession|jobs report|ukraine|gaza|china trade|central bank|congress|regulation|geopolit|s&p[[:space:]]*500|nasdaq[[:space:]]*(composite|100)|dow jones)'
        or n.summary ~* '(fed|fomc|tariff|interest rates?|inflation|wars?|sanctions?|white house|treasury|recession|jobs report|ukraine|central bank|geopolit|s&p[[:space:]]*500)'
      )
  )
  select coalesce(jsonb_agg(jsonb_build_object(
           'url', f.url,
           'title', f.title,
           'summary', case when length(f.summary) > 220
                           then left(f.summary, 217) || '…' else f.summary end,
           'image', f.image,
           'imageSrc', f.image_src,
           'publisher', f.publisher,
           'symbol', f.symbol,
           'published', f.published)
         order by f.has_image desc, f.published desc), '[]'::jsonb)
  from (
    select *
    from ranked
    order by has_image desc, published desc
    limit greatest(1, least(coalesce(p_limit, 12), 24))
  ) f;
$$;

-- Same body as 0022 plus imageSrc on each article.
create or replace function public.get_news(p_q text default null,
                                           p_limit integer default 24,
                                           p_offset integer default 0)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  select jsonb_build_object(
    'total', (
      select count(*) from ledger.news n
      where coalesce(p_q, '') = ''
         or n.symbol = upper(p_q)
         or n.title ilike '%' || p_q || '%'
         or n.summary ilike '%' || p_q || '%'),

    'articles', coalesce((
      select jsonb_agg(jsonb_build_object(
               'url', a.url, 'title', a.title, 'summary', a.summary,
               'image', a.image, 'imageSrc', a.image_src,
               'publisher', a.publisher,
               'symbol', a.symbol, 'published', a.published)
             order by a.published desc)
      from (
        select * from ledger.news n
        where coalesce(p_q, '') = ''
           or n.symbol = upper(p_q)
           or n.title ilike '%' || p_q || '%'
           or n.summary ilike '%' || p_q || '%'
        order by n.published desc
        limit greatest(1, least(coalesce(p_limit, 24), 60))
        offset greatest(0, coalesce(p_offset, 0))
      ) a), '[]'::jsonb),

    'keywords', coalesce((
      select jsonb_agg(jsonb_build_object(
               'word', w.word,
               'query', coalesce(w.query, w.word),
               'count', w.count,
               'kind', w.kind)
             order by case w.kind when 'topic' then 0 else 1 end,
                      case when w.kind = 'topic' then -w.count
                           else coalesce(w.ord, 999) end,
                      w.word)
      from ledger.news_keyword w
      where w.count > 0), '[]'::jsonb),

    'newest', (select max(published) from ledger.news));
$$;

-- Same shape as 0008 plus image + imageSrc: the portfolio page merges feed
-- rows into its carousel, and rows without pictures made pictureless cards.
create or replace function public.get_news_feed(p_hours integer default 24,
                                                p_limit integer default 30)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  select coalesce(jsonb_agg(jsonb_build_object(
           'url', f.url, 'title', f.title, 'summary', f.summary,
           'image', f.image, 'imageSrc', f.image_src,
           'symbol', f.symbol, 'publisher', f.publisher,
           'published', f.published) order by f.published desc), '[]'::jsonb)
  from (
    select n.url, n.title,
           -- Two sentences is enough to know whether to read the rest.
           case when length(n.summary) > 220
                then left(n.summary, 217) || '…' else n.summary end as summary,
           n.image, n.image_src, n.symbol, n.publisher, n.published
    from ledger.news n
    where n.published > now() - make_interval(hours => greatest(1, p_hours))
    order by n.published desc
    limit greatest(1, least(coalesce(p_limit, 30), 60))
  ) f;
$$;

do $$
declare f text;
begin
  foreach f in array array[
    'public.get_discover_news(integer)',
    'public.get_news(text, integer, integer)',
    'public.get_news_feed(integer, integer)'
  ] loop
    execute format('revoke all on function %s from public', f);
    execute format('grant execute on function %s to anon, authenticated', f);
  end loop;
end $$;
