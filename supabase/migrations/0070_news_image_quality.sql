-- Ticker Alpha — every story deserves the article's own photograph
--
-- Apply after 0069. Safe to re-run.
--
-- 0068 scraped og:image only for stories whose feed image could not be
-- trusted — wire copy, and stories with none. The lead slot showed why that
-- is not enough: the feed's images are small thumbnails, and stretched
-- across the front page's lead they blur. The article page's own og:image
-- is the full-size photograph the publisher chose, so now every recent
-- story gets the scrape, worst-off first: no image at all, then wire copy
-- wearing a pasted-on stock photo, then everything else, newest first
-- within each class.

create or replace function public.news_image_queue(p_limit integer default 30)
returns jsonb
language sql stable security definer
set search_path = ledger, pg_temp
as $$
  select coalesce(jsonb_agg(jsonb_build_object('url', q.url, 'publisher', q.publisher)
                            order by q.klass, q.published desc), '[]'::jsonb)
  from (
    select n.url, n.publisher, n.published,
           case
             when n.image is null then 0
             when coalesce(n.publisher, '')
                  ~* '(newswire|business ?wire|accesswire|newsfile|prweb|press release)'
               then 1
             else 2
           end as klass
    from ledger.news n
    where n.published > now() - interval '48 hours'
      and n.image_checked_at is null
    order by 4, 3 desc
    limit least(greatest(coalesce(p_limit, 30), 1), 100)
  ) q;
$$;

do $$
begin
  execute 'revoke all on function public.news_image_queue(integer) from public, anon, authenticated';
  execute 'grant execute on function public.news_image_queue(integer) to service_role';
end $$;
