/* ===========================================================================
   Ticker Alpha — PostHog product analytics.

   Loaded in the <head> of every page, after config.js and analytics.js, so
   the PostHog global is ready for any capture calls that follow.

   The PostHog public API key and host are read from window.ALPHATICKER_CONFIG
   (populated by config.js at deploy time from POSTHOG_API_KEY / POSTHOG_HOST
   env vars) so neither value is ever hard-coded in source.

   User identification is wired to the site's "ta-auth" event, which nav.js
   fires whenever the session changes. This keeps identification consistent
   across all pages without duplicating logic.
=========================================================================== */
(function () {
  "use strict";

  const CFG = window.ALPHATICKER_CONFIG || window.LEDGER_CONFIG || {};
  const POSTHOG_KEY  = String(CFG.posthogApiKey  || "").trim();
  const POSTHOG_HOST = String(CFG.posthogHost     || "").trim();

  // Self-traffic guard: same hostname check analytics.js uses.
  const LOCAL = /^(localhost$|127\.|0\.0\.0\.0$|\[::1\]$)/.test(location.hostname);

  // Guard: warn loudly in development when config is absent, stay a no-op in
  // production. This matches the framework commandment for missing config.
  if (!POSTHOG_KEY) {
    if (LOCAL) {
      // eslint-disable-next-line no-console
      console.error(
        "[PostHog] posthogApiKey variable required by PostHog is missing or " +
        "un-configured, this causes events to be silently missed. " +
        "This error stops appearing once posthogApiKey is configured"
      );
    }
    return;
  }
  if (!POSTHOG_HOST) {
    if (LOCAL) {
      // eslint-disable-next-line no-console
      console.error(
        "[PostHog] posthogHost variable required by PostHog is missing or " +
        "un-configured, this causes events to be silently missed. " +
        "This error stops appearing once posthogHost is configured"
      );
    }
    return;
  }

  // ---- PostHog SDK stub + loader -----------------------------------------
  // The stub queues every call made before array.js finishes loading.
  // The script is served from the local vendor directory so no PostHog
  // domain URL appears in source (array.js is copied there by build.sh).
  /* eslint-disable */
  !function(t,e){var o,n,p,r;e.__SV||(window.posthog=e,e._i=[],e.init=function(i,s,a){function g(t,e){var o=e.split(".");2==o.length&&(t=t[o[0]],e=o[1]),t[e]=function(){t.push([e].concat(Array.prototype.slice.call(arguments,0)))}}(p=t.createElement("script")).type="text/javascript",p.crossOrigin="anonymous",p.async=!0,p.src="/vendor/posthog/array.js",(r=t.getElementsByTagName("script")[0]).parentNode.insertBefore(p,r);var u=e;for(void 0!==a?u=e[a]=[]:a="posthog",u.people=u.people||[],u.toString=function(t){var e="posthog";return"posthog"!==a&&(e+="."+a),t||(e+=" (stub)"),e},u.people.toString=function(){return u.toString(1)+".people (stub)"},o="init capture register register_once register_for_session unregister unregister_for_session getFeatureFlag getFeatureFlagResult isFeatureEnabled reloadFeatureFlags updateEarlyAccessFeatureEnrollment getEarlyAccessFeatures on onFeatureFlags onSessionId getSurveys getActiveMatchingSurveys renderSurvey canRenderSurvey getNextSurveyStep identify setPersonProperties group resetGroups setPersonPropertiesForFlags resetPersonPropertiesForFlags setGroupPropertiesForFlags resetGroupPropertiesForFlags reset get_distinct_id getGroups get_session_id get_session_replay_url alias set_config startSessionRecording stopSessionRecording sessionRecordingStarted captureException loadToolbar get_property getSessionProperty createPersonProfile opt_in_capturing opt_out_capturing has_opted_in_capturing has_opted_out_capturing clear_opt_in_out_capturing debug".split(" "),n=0;n<o.length;n++)g(u,o[n]);e._i.push([i,s,a])},e.__SV=1)}(document,window.posthog||[]);
  /* eslint-enable */

  posthog.init(POSTHOG_KEY, {
    api_host:          POSTHOG_HOST,
    defaults:          "2026-05-30",
    // autocapture is ON (the default) — clicks, form submissions and pageviews
    // are tracked automatically, so we capture only business-specific events
    // in addition.
  });

  // ---- User identification -----------------------------------------------
  // nav.js dispatches "ta-auth" whenever the session changes. This is the
  // single source of truth for who is signed in across all pages.
  const SESSION_KEY = "alphaticker-session";
  const session = () => {
    try { return JSON.parse(localStorage.getItem(SESSION_KEY) || "null"); } catch { return null; }
  };

  function identifyFromSession() {
    const s = session();
    if (!s || !s.user || !s.user.id) return;
    // Only the Supabase UUID goes in the distinct_id — never email or name.
    // PII belongs in person properties (set via identify's second argument),
    // and only what PostHog's own docs say to send.
    posthog.identify(s.user.id, {
      plan_tier: s.user.planTier || "free",
    });
  }

  // Identify immediately if the user is already signed in on page load.
  identifyFromSession();

  let wasSignedIn = !!(session() || {}).user;
  addEventListener("ta-auth", () => {
    const s = session();
    const nowIn = !!(s && s.user);
    if (nowIn) {
      identifyFromSession();
    } else if (wasSignedIn) {
      // Call reset() only on the transition from signed-in to signed-out, not
      // on an anonymous page load (which would discard the anonymous id).
      posthog.reset();
    }
    wasSignedIn = nowIn;
  });

  // ---- News article click delegate ---------------------------------------
  // All news article anchors carry data-ta-news; capture one event for every
  // page that renders them (Markets, Portfolio, News) without touching each.
  addEventListener("click", function (e) {
    const a = e.target && e.target.closest && e.target.closest("a[data-ta-news]");
    if (!a) return;
    posthog.capture("news_article_clicked", {
      source: a.getAttribute("data-ta-news") || undefined,
      ticker: a.getAttribute("data-ta-ticker") || undefined,
    });
  }, true);
})();
