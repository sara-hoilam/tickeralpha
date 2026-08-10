# Analytics

Two tags run on the site. **Microsoft Clarity** records sessions and heatmaps;
**Google Analytics 4** counts what people did. Both are loaded from the head of
every page, both read their public ID from `config.js`, and both answer to the
same cookie banner.

There is **no Google Tag Manager**, deliberately. A GTM container running beside
a gtag.js snippet is the usual reason a property reports every page view twice.

## Where things are

| What | Where |
|---|---|
| The tag, the event API, consent | `analytics.js` |
| The event catalogue and the `source` vocabulary | the comment block at the top of `analytics.js` |
| Measurement ID | `public/config.js` → `gaMeasurementId` |
| Build-time override | `GA_MEASUREMENT_ID` in `build.sh` |
| Clarity | `clarity-init.js` |
| The stored GA identifiers | `supabase/migrations/0025_ga_ids.sql` |

Pages never call `gtag` directly. They call `window.TAnalytics` — `track`,
`pageView`, `identify` — so an event name has one spelling in one file rather
than three spellings in three.

Locally, `server.py` serves an empty `config.js`, so there is no measurement ID
and every call is a no-op. Nothing is reported from a development machine.

## The parameter names are load-bearing

GA4 keeps an event parameter only if a custom dimension has been registered
under exactly that name, and drops anything else **without reporting an error**.
These are registered on the property:

- event-scoped: `ticker`, `source`, `screener_name`, `range`, `gate`
- user-scoped: `plan_tier`, `signup_cohort`

`gate` is new and **still has to be registered** in GA4 Admin → Custom
definitions before any of the wall events carry it. Until it is, `gate_view`
and friends arrive with the parameter silently dropped, which reads as every
wall performing identically.

`search_term`, `method` and `content_type` are built in and need no
registration. Renaming any of the six above in the code without also renaming
the registration means the values silently stop arriving.

A newly registered dimension takes up to **48 hours** to appear in the standard
reports even when its values are plainly visible in DebugView. That wait is
expected and is not a fault.

## Page views

Automatic page views are off (`send_page_view: false`) and each one is sent by
hand, for two reasons. It lets every page view carry a content group —
`Markets`, `Watchlist`, `News`, `Company` — and it covers the company page,
which swaps companies with `pushState` and never reloads, so the tag would
otherwise hear about the first company of a visit and nothing after it.

The `src` parameter that some links carry says which part of the site a link was
followed from. It is read into the `view_ticker` event and then stripped from
`page_path`, so one company report is one page rather than one per entry point.

## Who is signed in

The `user_id` is the Supabase UUID and nothing else. Sending an email address, a
name, or anything else that identifies a person breaches the Google Analytics
terms, and the remedy is Google deleting the property's data.

`user_id` is set from the session that `nav.js` stores, so it survives a reload
without another round trip. A visitor who has not signed in is simply absent
from those reports rather than guessed at.

`profile.ga_client_id` is what allows GA4 data and Supabase data to be joined in
SQL later, and is what a server-side event has to quote to say which visitor it
belongs to. It is written once and then held: a visitor who clears their cookies
gets a new client id, and taking it would disconnect the account from everything
already reported under the old one.

## Consent

Consent Mode v2, defaults denied, pushed before the tag configures itself. Only
`analytics_storage` is ever asked for — the site runs no advertising products,
so the three ad signals stay denied for everyone. Until a visitor answers, GA4
sends cookieless pings and Clarity records nothing durable.

The banner's wording is the minimum that is accurate. **The copy, and the policy
it ought to link to, need whoever owns privacy for the business to sign them
off** — this is a compliance question, not an analytics one.

To re-test the flow in a browser, run `TAnalytics.resetConsent()` in the
console.

## Checking it works

Install the Google Analytics Debugger extension and open GA4 Admin → DebugView.

- One page load produces **exactly one** `page_view`, carrying a non-empty
  `content_group`.
- Moving between pages produces one more each, with no duplicates. So does
  opening a second company on the company page, and so does the browser's back
  button.
- Signing in produces `login`, and `sign_up` on a new account. Events after that
  in the same session carry a `user_id`.
- Starring a company produces `add_to_watchlist` with both `ticker` and `source`
  filled in.
- The signed-in visitor's `profile` row has a `ga_client_id`.

## Not built yet

**The subscription funnel.** There is no pricing page and no payment provider,
so `view_item_list`, `select_item`, `begin_checkout` and `purchase` are not
wired up. When they are, send them through `track()` under those standard names
so the monetisation reports work, and fire `purchase` on the post-payment
success page rather than before redirecting away.

Two things have to happen alongside that, and neither can be done from the
codebase:

1. **The payment provider's domain must be added to the unwanted-referrals list**
   in the GA4 stream settings. If a visitor is sent through the provider's own
   domain and back, GA4 otherwise credits every conversion to the provider
   rather than to the channel that earned it. Tell us which provider and it can
   be configured.
2. **Renewals need the Measurement Protocol.** They are charged from a billing
   webhook with no browser present, so the event is sent server-side against the
   stored `ga_client_id`. That needs an API secret from Admin → Data streams →
   Ticker Alpha → Measurement Protocol API secrets. It is a server-side
   credential: Render environment only, never `public/config.js`.
