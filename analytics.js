/* ===========================================================================
   Ticker Alpha — Google Analytics 4 (gtag.js).

   One module, loaded in the head of every page after config.js, so an event
   name cannot drift between the three pages the way it would if each of them
   called gtag directly. Pages call window.TAnalytics; nothing else touches
   gtag or dataLayer.

   Google Tag Manager is deliberately absent. Running a GTM container beside
   this snippet is the usual cause of every page view being counted twice.

   ---------------------------------------------------------------------------
   The parameter names below are registered as custom dimensions in the GA4
   property. GA4 keeps a parameter only if its name matches a registration
   exactly, and drops anything else without saying so, so these spellings are
   load-bearing:

     event-scoped   ticker, source, screener_name, range, gate
     user-scoped    plan_tier, signup_cohort

   search_term (search), method (login / sign_up) and content_type
   (select_content) are built in and need no registration.

   ---------------------------------------------------------------------------
   Events this site sends

     page_view            every load, plus every pushState navigation on the
                          company page. Sent by hand -- automatic page views
                          are off -- so each one can carry a content group.
     view_ticker          a company report opened            ticker, source
     add_to_watchlist     a company followed                 ticker, source
     remove_from_watchlist                                   ticker, source
     run_screener         a market view chosen               screener_name
     change_chart_range   a chart timeframe changed          ticker, range
     search               a search acted on                  search_term
     select_content       a news article opened              content_type, source
     login / sign_up      Supabase auth                      method

   The sign-in walls, in the order one visitor meets them:

     gate_view            a wall was shown                   gate, ticker
     gate_click           its button was pressed             gate
     signup_modal_view    the sheet opened                   gate
     signup_modal_accept  Continue with Google pressed       gate
     signup_modal_dismiss closed without signing in          gate

   `gate` says which wall, and is the whole point of the funnel -- without it
   there is one undifferentiated "someone saw a wall" number and no way to
   tell which of them earns accounts. Keep it to this list:

     report  watchlist  earnings  correlation  drawdown  seasonality
     portfolio  generic

   `source` says where on the site an action started. Keep it to this list --
   inventing a value per button turns the dimension back into noise:

     home_feed  news_feed  market_page  ticker_page  screener_results
     watchlist  search_results  ticker_tape

   Subscription events (view_item_list, select_item, begin_checkout, purchase)
   are not wired up because there is no pricing page or payment provider yet.
   When there is, send them through track() under those standard names so the
   monetisation reports work, and fire `purchase` on the success page rather
   than before redirecting away.
=========================================================================== */
(function () {
  "use strict";

  const CFG = window.ALPHATICKER_CONFIG || window.LEDGER_CONFIG || {};
  const GA_ID = String(CFG.gaMeasurementId || "").trim();
  const HOSTED = !!(CFG.supabaseUrl && CFG.supabaseAnonKey);

  const SESSION_KEY = "alphaticker-session";      // written by nav.js
  const CONSENT_KEY = "alphaticker-consent";
  const SEEN_KEY    = "alphaticker-ga-accounts";  // ids we have sent sign_up for
  const IDS_KEY     = "alphaticker-ga-ids";       // ids written to profile this session

  const store = {
    get(k){ try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v){ try { localStorage.setItem(k, v); } catch {} },
    del(k){ try { localStorage.removeItem(k); } catch {} },
  };

  /* ---- gtag ------------------------------------------------------------- */
  window.dataLayer = window.dataLayer || [];
  function gtag(){ dataLayer.push(arguments); }
  window.gtag = gtag;

  /* ---- consent ----------------------------------------------------------
     Consent Mode v2, decided by region.

     Defaulting the whole world to denied was accurate but produced no
     measurement at all: every visitor started denied, most never answered
     the banner, and GA4 saw nothing but cookieless pings. Prior consent for
     first-party analytics is an EEA and UK requirement, not a global one.

     So Europe is asked, and everywhere else is granted by default and never
     sees a banner. An explicit choice always wins over the regional default,
     in both directions -- somebody who declined stays declined wherever they
     are, and the footer's "Cookies" link lets anyone change their mind.

     Detection is by IANA time zone because the default has to be pushed
     before the tag configures itself, and there is no point in the sequence
     where a geo-IP round trip could be awaited. It deliberately errs toward
     asking: every Europe/* and Atlantic/* zone is treated as consent-first
     even where it is not, plus the EU's outermost regions, because
     over-asking is the harmless direction and under-asking is not.

     Only analytics storage is ever asked for. The site runs no advertising
     products, so the three ad signals stay denied for everyone. */
  let consent = store.get(CONSENT_KEY);           // "granted" | "denied" | null

  const EU_OUTERMOST =
    /^(Africa\/Ceuta|Indian\/(Reunion|Mayotte)|America\/(Guadeloupe|Martinique|Cayenne|Miquelon))$/;

  function consentFirstRegion() {
    let tz = "";
    try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch {}
    if (!tz) return true;                         // unknown: ask
    return /^(Europe|Atlantic)\//.test(tz) || EU_OUTERMOST.test(tz);
  }

  const askFirst = consentFirstRegion();
  const analyticsDefault =
    consent === "granted" ? "granted" :
    consent === "denied"  ? "denied"  :
    askFirst ? "denied" : "granted";

  gtag("consent", "default", {
    ad_storage: "denied",
    ad_user_data: "denied",
    ad_personalization: "denied",
    analytics_storage: analyticsDefault,
    wait_for_update: 500,
  });
  if (consent === "granted") gtag("consent", "update", { analytics_storage: "granted" });

  /* ---- the tag ----------------------------------------------------------
     Built from config.js rather than hard-coded in each page's head, so the
     measurement ID has one home and build.sh can inject it per environment.
     Absent locally (server.py serves an empty config.js), where every call
     below becomes a no-op rather than an error. */
  if (GA_ID) {
    const s = document.createElement("script");
    s.async = true;
    s.src = "https://www.googletagmanager.com/gtag/js?id=" + encodeURIComponent(GA_ID);
    document.head.appendChild(s);

    gtag("js", new Date());
    // Page views are sent by hand; see pageView below.
    gtag("config", GA_ID, { send_page_view: false });
  }

  /* ---- events ------------------------------------------------------------ */
  // GA4 caps an event at 25 parameters and a parameter value at 100
  // characters, and silently truncates past that. Doing it here means a long
  // company name arrives shortened rather than mangled.
  const MAX_PARAMS = 25, MAX_LEN = 100;

  function clean(params) {
    const out = {};
    let n = 0;
    for (const k in params) {
      const v = params[k];
      if (v === undefined || v === null || v === "") continue;
      if (n >= MAX_PARAMS) break;
      out[k] = typeof v === "string" && v.length > MAX_LEN ? v.slice(0, MAX_LEN) : v;
      n++;
    }
    return out;
  }

  function track(name, params) {
    if (!GA_ID || !name) return;
    gtag("event", name, clean(params || {}));
  }

  /** A URL path mapped to a stable, human-readable section. These become a
      reporting dimension, so the set stays small on purpose. */
  function contentGroupFor(path) {
    const parts = String(path || "/").split("?");
    const file = (parts[0].split("/").pop() || "index.html").toLowerCase();
    const qs = parts[1] || "";
    if (file === "" || file === "index.html")
      return /(^|&)tab=watchlist(&|$)/.test(qs) ? "Watchlist" : "Markets";
    if (file === "news.html" || file === "news") return "News";
    if (file === "earnings.html" || file === "earnings") return "Earnings";
    if (file === "company.html" || file === "company") return "Company";
    if (file === "portfolio.html" || file === "portfolio") return "Portfolio";
    return "Other";
  }

  // `src` says which part of the site a link was followed from. It belongs in
  // the event that reads it, not in the page's identity -- left in, one
  // company report would show up as half a dozen different pages.
  function withoutSource(href) {
    try {
      const u = new URL(href, location.origin);
      u.searchParams.delete("src");
      return u;
    } catch { return null; }
  }

  function pageView(path) {
    const raw = path || (location.pathname + location.search);
    const u = withoutSource(raw);
    const p = u ? u.pathname + u.search : raw;
    const here = withoutSource(location.href);
    track("page_view", {
      page_path: p,
      page_location: here ? here.href : location.href,
      page_title: document.title,
      content_group: contentGroupFor(p),
    });
  }

  /* ---- who is signed in --------------------------------------------------
     The user id is the Supabase UUID and nothing else. An email address, a
     name or anything else that identifies a person breaches the Analytics
     terms, and the remedy is Google deleting the property's data. */
  function identify(opts) {
    if (!GA_ID || !opts || !opts.userId) return;
    gtag("config", GA_ID, { user_id: opts.userId, send_page_view: false });
    gtag("set", "user_properties", {
      plan_tier: opts.planTier || "free",
      signup_cohort: opts.signupCohort || undefined,
    });
  }

  function clearIdentity() {
    if (!GA_ID) return;
    gtag("config", GA_ID, { user_id: null, send_page_view: false });
    gtag("set", "user_properties", { plan_tier: null, signup_cohort: null });
  }

  const session = () => {
    try { return JSON.parse(store.get(SESSION_KEY) || "null"); } catch { return null; }
  };

  function identifyFromSession() {
    const u = (session() || {}).user;
    if (!u || !u.id) return null;
    identify({
      userId: u.id,
      planTier: u.planTier,
      signupCohort: String(u.createdAt || "").slice(0, 7),   // "2026-08"
    });
    return u;
  }

  /** GoTrue has no sign-up event -- a new account signs in like any other --
      so an account is treated as new when this browser meets it for the first
      time and it was opened minutes ago. The judgement waits for the date,
      because the redirect flow learns the account's id from the token and its
      age a moment later, and deciding without it would rule out every
      sign-up that arrives that way. */
  function maybeSignUp(u) {
    if (!u || !u.id || !u.createdAt) return;
    let seen = [];
    try { seen = JSON.parse(store.get(SEEN_KEY) || "[]"); } catch {}
    if (seen.indexOf(u.id) >= 0) return;
    store.set(SEEN_KEY, JSON.stringify(seen.concat(u.id).slice(-20)));
    if (Date.now() - Date.parse(u.createdAt) < 10 * 60 * 1000)
      track("sign_up", { method: "supabase" });
  }

  /* ---- the identifiers that join GA4 to Supabase -------------------------
     Stored against the profile row so the two datasets can be joined in SQL
     later. Only worth doing once consent is granted: without analytics
     storage the client id lives for one page and joins to nothing. */
  function gaGet(field) {
    return new Promise(resolve => {
      let settled = false;
      const done = v => { if (!settled) { settled = true; resolve(v || null); } };
      // gtag never calls back when the tag is blocked, so do not wait forever.
      // Generously, though: this is queued before gtag.js has finished
      // loading, and nothing is waiting on the answer.
      setTimeout(() => done(null), 10000);
      try { gtag("get", GA_ID, field, done); } catch { done(null); }
    });
  }

  async function captureGaIds(userId) {
    if (!GA_ID || !HOSTED || !userId || consent !== "granted") return;
    let already = null;
    try { already = sessionStorage.getItem(IDS_KEY); } catch {}
    if (already === userId) return;

    const token = (session() || {}).access_token;
    if (!token) return;

    // One at a time, not Promise.all. Two `get` calls in the same tick and the
    // tag answers only the second, every time, on the first pair after a page
    // load -- so asking for both at once is how the client id ends up never
    // being stored at all.
    const clientId = await gaGet("client_id");
    if (!clientId) return;
    const sessionId = await gaGet("session_id");

    try {
      const r = await fetch(`${CFG.supabaseUrl}/rest/v1/rpc/set_ga_ids`, {
        method: "POST",
        headers: {
          apikey: CFG.supabaseAnonKey,
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ p_client_id: clientId, p_session_id: sessionId }),
      });
      if (r.ok) { try { sessionStorage.setItem(IDS_KEY, userId); } catch {} }
    } catch {}
  }

  /* ---- auth changes ------------------------------------------------------
     nav.js owns the session and announces every change on `ta-auth`. It
     announces more than once for a single sign-in -- the account's id arrives
     with the token and the rest of it a request later -- so a login is going
     from no session to one, not from one user id to another. A page that
     loads already signed in is not a login at all. */
  let signedIn = !!(session() || {}).user;
  let currentUserId = (identifyFromSession() || {}).id || null;
  if (signedIn) {
    maybeSignUp((session() || {}).user);
    captureGaIds(currentUserId);
  }

  addEventListener("ta-auth", () => {
    const u = identifyFromSession() || (session() || {}).user || null;
    const nowIn = !!(session() || {}).user;

    maybeSignUp(u);
    if (nowIn && !signedIn) track("login", { method: "supabase" });
    if (!nowIn && signedIn) clearIdentity();

    signedIn = nowIn;
    currentUserId = u ? u.id || null : null;
    if (currentUserId) captureGaIds(currentUserId);
  });

  /* ---- news links --------------------------------------------------------
     Every article on the site is a plain outbound anchor. Marking them up
     rather than binding a handler at each render site keeps the three pages
     to one attribute each. */
  addEventListener("click", e => {
    const a = e.target && e.target.closest && e.target.closest("a[data-ta-news]");
    if (!a) return;
    track("select_content", {
      content_type: "news_article",
      source: a.getAttribute("data-ta-news"),
      ticker: a.getAttribute("data-ta-ticker") || undefined,
    });
  }, true);

  /* ---- consent banner ----------------------------------------------------
     A visitor in the EEA has to choose before anything is stored, and until
     they do GA4 sends cookieless pings. The wording here is the minimum that
     is accurate; the copy and the policy it links to belong to whoever owns
     privacy for the business. */
  const CONSENT_CSS = `
.ta-consent {
  position: fixed; left: 16px; right: 16px; bottom: 16px; z-index: 200;
  max-width: 620px; margin: 0 auto;
  display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
  padding: 14px 18px;
  background: var(--card, #fff); color: var(--ink, #111);
  border: 1px solid var(--rule, #e3e3e3); border-radius: 12px;
  box-shadow: var(--shadow, 0 8px 30px rgba(0,0,0,.12));
  font-size: 13px; line-height: 1.5;
}
.ta-consent p { margin: 0; flex: 1; min-width: 220px; color: var(--ink-2, #444); }
.ta-consent-btns { display: flex; gap: 8px; flex-shrink: 0; }
.ta-consent button {
  font: inherit; font-size: 13px; font-weight: 600; cursor: pointer;
  border-radius: 8px; padding: 8px 16px;
}
.ta-consent button[data-decline] {
  background: transparent; color: var(--ink-2, #444);
  border: 1px solid var(--rule, #e3e3e3);
}
.ta-consent button[data-decline]:hover { border-color: var(--ink-3, #888); }
.ta-consent button[data-accept] {
  background: var(--accent, #2a78d6); color: #fff; border: 0;
}
.ta-consent button[data-accept]:hover { filter: brightness(1.08); }
@media (max-width: 520px) {
  .ta-consent { flex-direction: column; align-items: stretch; }
  .ta-consent-btns button { flex: 1; }
}`;

  function setConsent(choice) {
    consent = choice === "granted" ? "granted" : "denied";
    store.set(CONSENT_KEY, consent);
    gtag("consent", "update", { analytics_storage: consent });
    applyClarityConsent();
    if (consent === "granted" && currentUserId) captureGaIds(currentUserId);
  }

  /** Clarity is loaded by clarity-init.js and reads the same choice, but it is
      a module and may land after this script, so the state is reapplied once
      the page has finished loading. */
  function applyClarityConsent() {
    const C = window.TAClarity;
    if (!C || typeof C.consentV2 !== "function") return;
    try {
      C.consentV2({
        ad_Storage: "denied",
        analytics_Storage: consent === "granted" ? "granted" : "denied",
      });
    } catch {}
  }
  addEventListener("load", applyClarityConsent);

  function showBanner() {
    const style = document.createElement("style");
    style.textContent = CONSENT_CSS;
    document.head.appendChild(style);

    const el = document.createElement("div");
    el.className = "ta-consent";
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-label", "Cookie choices");
    el.innerHTML = `
      <p>We use cookies to measure how this site is used, so we can see which
         pages are worth building on. Nothing is shared with advertisers.</p>
      <div class="ta-consent-btns">
        <button type="button" data-decline>Decline</button>
        <button type="button" data-accept>Accept</button>
      </div>`;
    const answer = choice => { setConsent(choice); el.remove(); };
    el.querySelector("[data-decline]").onclick = () => answer("denied");
    el.querySelector("[data-accept]").onclick = () => answer("granted");
    document.body.appendChild(el);
  }

  // Only where consent has to come first. Everywhere else the default is
  // already granted, so a banner would be asking a question whose answer has
  // no effect -- the footer's "Cookies" link is how those visitors opt out.
  if (consent === null && askFirst && (GA_ID || CFG.clarityProjectId)) {
    if (document.readyState === "loading")
      addEventListener("DOMContentLoaded", showBanner);
    else showBanner();
  }

  /* ---- the public surface ------------------------------------------------ */
  window.TAnalytics = {
    track,
    pageView,
    contentGroupFor,
    identify,
    clearIdentity,
    setConsent,
    consent: () => consent,
    /** Clears the stored choice and shows the banner again — for testing the
        consent flow without hand-editing localStorage. */
    resetConsent(){ store.del(CONSENT_KEY); consent = null; showBanner(); },
  };

  // One page view per load. Identification above ran first, so events that
  // follow in this session carry the user id.
  pageView();
})();
