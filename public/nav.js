/* ===========================================================================
   Ticker Alpha — shared top navigation.

   One script, injected into every page, so the bar cannot drift between them.
   It renders: a stats strip (the market at a glance, the way CoinMarketCap
   opens with global crypto stats), then the bar itself — brand, sections,
   ticker search, and sign-in.

   The brand always returns to Markets Today.
=========================================================================== */
(function () {
  "use strict";

  const CFG = window.ALPHATICKER_CONFIG || window.LEDGER_CONFIG || {};
  const HOSTED = !!(CFG.supabaseUrl && CFG.supabaseAnonKey);
  // analytics.js loads first on every page. The fallback is only so a missing
  // one breaks analytics rather than the search box.
  const GA = window.TAnalytics || { track(){}, pageView(){} };
  const esc = s => String(s ?? "").replace(/[&<>"]/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  async function rpc(fn, args) {
    if (!HOSTED) throw new Error("no data source");
    const r = await fetch(`${CFG.supabaseUrl}/rest/v1/rpc/${fn}`, {
      method: "POST",
      headers: { apikey: CFG.supabaseAnonKey, "Content-Type": "application/json" },
      body: JSON.stringify(args || {}),
    });
    if (!r.ok) throw new Error(`${fn} ${r.status}`);
    return r.json();
  }

  /* Brand nicknames whose legal name does not contain the query (SpaceX →
     SPACE EXPLORATION TECHNOLOGIES CORP). Mirrored in migration 0052; this
     client boost still helps before that RPC is applied. */
  const COMPANY_SEARCH_ALIASES = {
    spacex: "SPCX", "space x": "SPCX", "space-x": "SPCX",
    "space exploration": "SPCX",
    google: "GOOGL", alphabet: "GOOGL",
    facebook: "META", fb: "META",
    berkshire: "BRK-B", "berkshire hathaway": "BRK-B",
    brkb: "BRK-B", "brk.b": "BRK-B", "brk/b": "BRK-B", "brk-b": "BRK-B",
    brka: "BRK-A", "brk.a": "BRK-A", "brk-a": "BRK-A",
    "jp morgan": "JPM", "j.p. morgan": "JPM",
    "johnson and johnson": "JNJ", "johnson & johnson": "JNJ", "j&j": "JNJ",
    "coca cola": "KO", "coca-cola": "KO",
    mcdonalds: "MCD", "mc donalds": "MCD",
    "procter and gamble": "PG", "procter & gamble": "PG", "p&g": "PG",
    "at&t": "T", att: "T",
  };

  function tickerOf(row){
    return String((row && (row.ticker || row.symbol)) || "").toUpperCase();
  }

  /** search_companies with nickname boost (SpaceX, Google, Facebook, …). */
  async function searchCompanies(term, lim){
    const q = String(term || "").trim();
    if (!q) return [];
    const args = { q };
    if (lim != null) args.lim = lim;
    let items = [];
    try { items = (await rpc("search_companies", args)) || []; } catch { items = []; }
    const alias = COMPANY_SEARCH_ALIASES[q.toLowerCase()];
    if (!alias) return items;
    const rest = items.filter(x => tickerOf(x) !== alias);
    const hit = items.find(x => tickerOf(x) === alias);
    if (hit) return [hit, ...rest];
    try {
      const extra = (await rpc("search_companies", { q: alias, lim: 5 })) || [];
      const row = extra.find(x => tickerOf(x) === alias);
      if (row) return [row, ...rest];
    } catch {}
    return items;
  }

  const page = (location.pathname.split("/").pop() || "index.html").toLowerCase();
  const here = p => page === p || (p === "index.html" && page === "");

  document.body.insertAdjacentHTML("afterbegin", `
    <div class="nav-strip" id="nav-strip">
      <div class="nav-tape" id="nav-tape"></div>
    </div>
    <nav class="nav"><div class="nav-in">
      <a class="nav-brand" href="index.html" aria-label="Ticker Alpha home">
        <img class="nav-mark" id="nav-mark" src="brand-logo-light.png" width="30" height="30" alt="">
        <b>Ticker&nbsp;Alpha</b>
      </a>

      <div class="nav-links">
        <a href="index.html"   class="${here("index.html") ? "on" : ""}">Markets</a>
        <a href="news.html"    class="${here("news.html") ? "on" : ""}">News</a>
        <a href="earnings.html" class="${here("earnings.html") ? "on" : ""}">Earnings</a>
        <a href="reports.html" class="${here("reports.html") || here("company.html") ? "on" : ""}">Company Report</a>
        <a href="portfolio.html" class="${here("portfolio.html") ? "on" : ""}">Portfolio</a>
      </div>

      <div class="nav-search">
        <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="9" cy="9" r="6"
          fill="none" stroke="currentColor" stroke-width="2"/><path d="M13.5 13.5 18 18"
          stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
        <input type="text" id="nav-q" placeholder="Search a ticker or company"
               autocomplete="off" role="combobox" aria-expanded="false" aria-controls="nav-ac">
        <kbd>/</kbd>
        <div class="nav-ac" id="nav-ac" role="listbox"></div>
      </div>

      <div class="nav-right">
        <button class="nav-theme" id="nav-theme" aria-label="Switch colour theme"></button>
        <div id="nav-auth"></div>
      </div>
    </div></nav>
    <div class="nav-onetap" id="nav-onetap" hidden></div>

    <!-- Phones get a tab bar instead of the links, which are hidden below
         1080px. Without it every section but Markets was unreachable except by
         typing a URL. Thumb-reachable and fixed, the way a trading app does it. -->
    <nav class="tabbar" aria-label="Sections">
        <a href="index.html" class="${here("index.html") ? "on" : ""}"><svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l5-6 4 3.5 5-8"/><path d="M14 6.5h4v4"/><path d="M3 21h18"/></svg><span>Markets</span></a>
        <a href="news.html" class="${here("news.html") ? "on" : ""}"><svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5h11v14H4z"/><path d="M15 9h5v8a2 2 0 0 1-2 2h-3"/><path d="M7 8.5h5M7 12h5M7 15.5h3"/></svg><span>News</span></a>
        <a href="earnings.html" class="${here("earnings.html") ? "on" : ""}"><svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="5" width="17" height="15" rx="2"/><path d="M3.5 10h17"/><path d="M8 3.5v3M16 3.5v3"/></svg><span>Earnings</span></a>
        <a href="reports.html" class="${here("reports.html") || here("company.html") ? "on" : ""}"><svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3.5h8l4 4V20a.5.5 0 0 1-.5.5h-11A.5.5 0 0 1 6 20z"/><path d="M14 3.5V8h4"/><path d="M9 13h6M9 16.5h4"/></svg><span>Reports</span></a>
        <a href="portfolio.html" class="${here("portfolio.html") ? "on" : ""}"><svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 8.5h17V19a1 1 0 0 1-1 1h-15a1 1 0 0 1-1-1z"/><path d="M9 8.5V6a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2.5"/><path d="M3.5 13h17"/></svg><span>Portfolio</span></a>
    </nav>
  `);

  // Publish the bar's height so a page's own sticky elements can sit below it
  // instead of underneath it.
  const bar = document.querySelector("nav.nav");
  const measure = () =>
    document.documentElement.style.setProperty("--nav-h", bar.offsetHeight + "px");
  measure();
  addEventListener("resize", measure);

  /* ---- theme ------------------------------------------------------------ */
  const themeBtn = document.getElementById("nav-theme");
  const ICON = { dark: "☾", light: "☀" };
  const markImg = document.getElementById("nav-mark");
  // The tab follows the *browser's* theme, not the site's. A black mark
  // vanishes on a dark tab strip and a white one vanishes on a light strip,
  // so this tracks prefers-color-scheme rather than the in-app toggle -- and
  // keeps tracking it, because the OS can change theme while the tab is open.
  const tabDark = matchMedia("(prefers-color-scheme: dark)");
  function setFavicon() {
    let link = document.querySelector('link[data-ta-favicon]');
    if (!link) {
      link = document.createElement("link");
      link.rel = "icon";
      link.type = "image/png";
      link.setAttribute("data-ta-favicon", "1");
      document.head.appendChild(link);
    }
    link.href = tabDark.matches ? "favicon-dark.png" : "favicon-light.png";
  }
  setFavicon();
  tabDark.addEventListener("change", setFavicon);
  function setTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    themeBtn.textContent = t === "dark" ? ICON.light : ICON.dark;
    themeBtn.title = t === "dark" ? "Switch to light" : "Switch to dark";
    if (markImg) markImg.src = t === "dark" ? "brand-logo-dark.png" : "brand-logo-light.png";
    try { localStorage.setItem("alphaticker-theme", t); } catch {}
    window.dispatchEvent(new CustomEvent("themechange", { detail: t }));
  }
  themeBtn.onclick = () =>
    setTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");
  let t0 = null;
  try { t0 = localStorage.getItem("alphaticker-theme") || localStorage.getItem("ledger-theme"); } catch {}
  setTheme(t0 || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
  window.setTheme = setTheme;

  /* ---- ticker tape ------------------------------------------------------ */
  // A marquee of index ETFs and large caps, each one a link to its page.
  //
  // The animation moves one copy of the list the width of one copy, with a
  // second copy behind it, so the seam never shows and the loop needs no
  // JavaScript per frame. Duration scales with content so a longer tape does
  // not scroll faster.
  const pct = v => v === null || v === undefined ? "—"
    : `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
  const dir = v => (v >= 0 ? "up" : "down");
  const price = v => v === null || v === undefined ? ""
    : v >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 0 })
                : v.toFixed(2);

  async function tape() {
    const host = document.getElementById("nav-tape");
    if (!HOSTED) return;
    let rows;
    try {
      rows = await rpc("get_ticker_tape", { p_limit: 180 });
    } catch {
      // Before 0009 is applied, fall back to the watchlist so the strip is
      // still a tape rather than an empty bar.
      try { rows = await rpc("get_market_summary", { p_view: "market_cap", p_limit: 50 }); }
      catch { return; }
    }
    if (!rows || !rows.length) return;

    const one = rows.map(r =>
      `<a class="tape-i" href="company.html?t=${encodeURIComponent(r.symbol)}&src=ticker_tape"` +
      ` title="${esc(r.name || r.symbol)}">` +
      `<span class="tape-s">${esc(r.symbol)}</span>` +
      `<span class="tape-p">${price(r.price)}</span>` +
      `<span class="${dir(r.changePct)}">${pct(r.changePct)}</span></a>`).join("");

    host.innerHTML =
      `<div class="tape-run" aria-label="Live prices">${one}</div>` +
      `<div class="tape-run" aria-hidden="true">${one}</div>`;

    // ~55px a second reads comfortably without being distracting.
    const w = host.firstElementChild.scrollWidth;
    host.style.setProperty("--tape-w", w + "px");
    host.style.setProperty("--tape-t", Math.max(30, w / 55) + "s");
  }
  tape();

  /* ---- ticker search ---------------------------------------------------- */
  const q = document.getElementById("nav-q"), ac = document.getElementById("nav-ac");
  let items = [], idx = -1, timer = null;

  const close = () => { ac.classList.remove("open"); q.setAttribute("aria-expanded", "false"); idx = -1; };
  const open = t => {
    // What was typed, not what was chosen: the term is the question, and a
    // search nobody could answer is the interesting half of the report.
    const term = q.value.trim();
    if (term) GA.track("search", { search_term: term });
    location.href = `company.html?t=${encodeURIComponent(t)}&src=search_results`;
  };

  q.addEventListener("input", () => {
    clearTimeout(timer);
    const term = q.value.trim();
    if (!term) return close();
    timer = setTimeout(async () => {
      try {
        items = await searchCompanies(term);
        idx = -1;
        if (!items.length) return close();
        ac.innerHTML = items.map((x, i) => {
          const kind = x.kind && x.kind !== "stock" ? ` · ${x.kind}` : "";
          return `<div role="option" data-i="${i}" aria-selected="false">` +
            `<b>${esc(x.ticker)}</b><span>${esc(x.name)}${esc(kind)}</span></div>`;
        }).join("");
        ac.classList.add("open");
        q.setAttribute("aria-expanded", "true");
      } catch { close(); }
    }, 130);
  });
  ac.addEventListener("mousedown", e => {
    const d = e.target.closest("[data-i]");
    if (d) { e.preventDefault(); open(items[+d.dataset.i].ticker); }
  });
  q.addEventListener("keydown", e => {
    if (!ac.classList.contains("open")) {
      if (e.key === "Enter" && q.value.trim()) open(q.value.trim().toUpperCase());
      return;
    }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      idx = (idx + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length;
      [...ac.children].forEach((c, i) => c.setAttribute("aria-selected", i === idx));
    } else if (e.key === "Enter") {
      e.preventDefault(); open((idx >= 0 ? items[idx] : items[0]).ticker);
    } else if (e.key === "Escape") close();
  });
  q.addEventListener("blur", () => setTimeout(close, 120));
  // "/" focuses search, the way every other market site behaves.
  addEventListener("keydown", e => {
    if (e.key === "/" && !/^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)) {
      e.preventDefault(); q.focus();
    }
  });

  /* ---- auth ------------------------------------------------------------- */
  // Supabase Auth over its REST endpoints -- no SDK, same as everything else
  // here. Google sign-in only appears once a client id is configured; without
  // one the button would open a Google page that immediately errors.
  const auth = document.getElementById("nav-auth");
  const TOKEN_KEY = "alphaticker-session";

  const session = () => {
    try { return JSON.parse(localStorage.getItem(TOKEN_KEY) || "null"); } catch { return null; }
  };
  const saveSession = s => {
    try { s ? localStorage.setItem(TOKEN_KEY, JSON.stringify(s))
            : localStorage.removeItem(TOKEN_KEY); } catch {}
  };

  // The pages need to know who is signed in and be told when that changes.
  // Without this the session is trapped in this closure and every page invents
  // its own copy, which is how two of them end up disagreeing.
  // Access tokens expire after about an hour. Supabase rejects a request
  // carrying a stale one with 401 -- including the public functions that need
  // no account at all -- so an unrefreshed token does not merely break the
  // watchlist, it empties every page. recover() is what the pages call when
  // that happens: renew if we can, sign out cleanly if we cannot, and either
  // way let the caller continue anonymously rather than showing nothing.
  let refreshing = null;

  async function recover(){
    if (refreshing) return refreshing;
    refreshing = (async () => {
      const s = session();
      if (s && s.refresh_token) {
        try {
          const r = await fetch(
            `${CFG.supabaseUrl}/auth/v1/token?grant_type=refresh_token`, {
              method: "POST",
              headers: { apikey: CFG.supabaseAnonKey, "Content-Type": "application/json" },
              body: JSON.stringify({ refresh_token: s.refresh_token }),
            });
          const d = await r.json();
          if (r.ok && d.access_token) {
            saveSession({ ...s, access_token: d.access_token,
                          refresh_token: d.refresh_token || s.refresh_token });
            return d.access_token;
          }
        } catch {}
      }
      // Cannot renew: drop it so the next call goes out anonymously and the
      // page keeps working, signed out.
      saveSession(null);
      renderAuth();
      announce();
      return null;
    })();
    const tok = await refreshing;
    refreshing = null;
    return tok;
  }

  window.TA = {
    session,
    token: () => (session() || {}).access_token || null,
    signedIn: () => !!(session() && session().user),
    signIn,
    showSignupModal,
    recover,
    searchCompanies,
    onAuthChange(fn){ addEventListener("ta-auth", () => fn(window.TA.signedIn())); },
  };
  const announce = () => dispatchEvent(new CustomEvent("ta-auth"));

  /** Tell Clarity who is signed in. Only custom-id is required; Clarity hashes
      it before upload. Safe no-op when Clarity is not configured. */
  function identifyClarity() {
    const C = window.TAClarity;
    if (!C || typeof C.identify !== "function") return;
    const u = (session() || {}).user;
    if (!u) return;
    const id = u.email || u.name;
    if (!id) return;
    try { C.identify(id, undefined, undefined, u.name || undefined); } catch {}
  }
  addEventListener("ta-auth", identifyClarity);
  // clarity-init.js is a module and may land after this script.
  addEventListener("load", identifyClarity);

  function renderAuth() {
    const s = session();
    if (s && s.user) {
      const name = s.user.name || s.user.email || "Account";
      const pic = s.user.picture;
      auth.innerHTML =
        `<div class="nav-acct">
           <button class="nav-user" id="nav-user" aria-haspopup="menu" aria-expanded="false">
             ${pic ? `<img src="${esc(pic)}" alt="">`
                   : `<span class="nav-avatar">${esc(name[0].toUpperCase())}</span>`}
             <span class="nav-uname">${esc(name.split(" ")[0])}</span>
             <svg class="nav-caret" viewBox="0 0 10 6" aria-hidden="true"><path d="M1 1l4 4 4-4"
               fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
           </button>
           <div class="nav-menu" id="nav-menu" role="menu" hidden>
             <div class="nav-who">
               <b>${esc(name)}</b>
               ${s.user.email ? `<span>${esc(s.user.email)}</span>` : ""}
             </div>
             <button role="menuitem" id="nav-signout">Sign out</button>
           </div>
         </div>`;

      const btn = document.getElementById("nav-user");
      const menu = document.getElementById("nav-menu");
      const shut = () => { menu.hidden = true; btn.setAttribute("aria-expanded", "false"); };
      btn.onclick = e => {
        e.stopPropagation();
        menu.hidden = !menu.hidden;
        btn.setAttribute("aria-expanded", String(!menu.hidden));
      };
      document.addEventListener("click", shut);
      addEventListener("keydown", e => { if (e.key === "Escape") shut(); });
      document.getElementById("nav-signout").onclick = signOut;
    } else {
      auth.innerHTML = `<button class="nav-login" id="nav-login">Log in</button>`;
      document.getElementById("nav-login").onclick = signIn;
    }
  }

  function signOut() {
    const s = session();
    // Tell Supabase too, so the refresh token is actually revoked rather than
    // just forgotten by this browser.
    if (s && s.access_token) {
      fetch(`${CFG.supabaseUrl}/auth/v1/logout`, {
        method: "POST",
        headers: { apikey: CFG.supabaseAnonKey, Authorization: `Bearer ${s.access_token}` },
      }).catch(() => {});
    }
    if (window.google && google.accounts && google.accounts.id) {
      try { google.accounts.id.disableAutoSelect(); } catch {}
    }
    saveSession(null);
    renderAuth();
    announce();
  }

  function signIn() {
    if (!HOSTED) return alert("Sign-in needs the site configuration (config.js).");
    if (!CFG.googleClientId) {
      alert("Google sign-in is not configured yet.\n\n" +
            "Add googleClientId to config.js and enable Google in Supabase " +
            "Authentication → Providers.");
      return;
    }
    const redirect = location.origin + location.pathname + location.search;
    location.href = `${CFG.supabaseUrl}/auth/v1/authorize` +
      `?provider=google&redirect_to=${encodeURIComponent(redirect)}`;
  }

  /** What each gate is actually offering. A modal that names the thing the
      visitor just reached for converts; one that says "unlock the full
      potential" is wallpaper, and was what every gate used to say. Keep the
      keys in step with the `gate` values sent to analytics. */
  const SIGNUP_REASONS = {
    watchlist: "Sign up to follow this company",
    report:    "Sign up to read the full report",
    earnings:  "Sign up to see the full earnings calendar",
    seasonality: "Sign up to see 10 years of monthly returns",
    correlation: "Sign up to compare this against any other ticker",
    drawdown:  "Sign up to see the full drawdown history",
    portfolio: "Sign up to save this portfolio",
  };
  const SIGNUP_NOTES = {
    watchlist: "Your list follows you across devices, with earnings dates.",
    report:    "Eight pages of valuation, peers and what would break the thesis.",
    earnings:  "Every company reporting, not just the first two.",
  };

  /** Google-only signup sheet — shown when someone reaches a gate without an
      account. Matches the conversion modal pattern: one CTA, privacy link,
      dismiss. */
  function showSignupModal(opts) {
    if (window.TA.signedIn()) return;
    if (document.getElementById("auth-modal")) return;
    const reason = (opts && opts.reason) || "generic";
    const title = SIGNUP_REASONS[reason] ||
      "Sign up below to unlock the full potential of Ticker Alpha";
    const note = SIGNUP_NOTES[reason] || "";
    GA.track("signup_modal_view", { gate: reason });

    const el = document.createElement("div");
    el.id = "auth-modal";
    el.className = "auth-modal";
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-modal", "true");
    el.setAttribute("aria-labelledby", "auth-modal-title");
    el.innerHTML = `
      <div class="auth-modal-card">
        <button type="button" class="auth-modal-x" aria-label="Close">×</button>
        <h2 id="auth-modal-title">${esc(title)}</h2>
        ${note ? `<p class="auth-why">${esc(note)}</p>` : ""}
        <p class="auth-legal">By continuing, you agree to our
          <a href="privacy.html" target="_blank" rel="noopener">privacy policy</a>.</p>
        <button type="button" class="auth-google" id="auth-google">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path fill="#fff" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
            <path fill="#fff" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
            <path fill="#fff" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/>
            <path fill="#fff" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/>
          </svg>
          Continue with Google
        </button>
        <p class="auth-sso">Single sign-on (SSO)</p>
        <button type="button" class="auth-modal-close">Close</button>
      </div>`;
    document.body.appendChild(el);

    const shut = () => {
      el.remove();
      document.removeEventListener("keydown", onKey);
      GA.track("signup_modal_dismiss", { gate: reason });
    };
    const onKey = e => { if (e.key === "Escape") shut(); };
    document.addEventListener("keydown", onKey);
    el.querySelector(".auth-modal-x").onclick = shut;
    el.querySelector(".auth-modal-close").onclick = shut;
    el.addEventListener("click", e => { if (e.target === el) shut(); });
    el.querySelector("#auth-google").onclick = () => {
      el.remove();
      document.removeEventListener("keydown", onKey);
      GA.track("signup_modal_accept", { gate: reason });
      signIn();
    };
    try { el.querySelector("#auth-google").focus(); } catch {}
  }

  // A one-line explanation under the bar. Sign-in failures used to leave no
  // trace at all in the page, only in the console.
  function authNote(msg) {
    let el = document.getElementById("nav-note");
    if (!el) {
      el = document.createElement("div");
      el.id = "nav-note";
      el.className = "nav-note";
      document.querySelector("nav.nav").after(el);
    }
    el.innerHTML = `<span>${esc(msg)}</span><button aria-label="Dismiss">&times;</button>`;
    el.querySelector("button").onclick = () => el.remove();
  }

  // Supabase returns the session in the URL fragment after the round trip.
  (function captureRedirect() {
    const h = location.hash.slice(1);
    if (!h) return;
    const p = new URLSearchParams(h);

    // Google or GoTrue can also come back with a reason it did not work.
    const err = p.get("error_description") || p.get("error");
    if (err) {
      authNote(`Sign-in failed: ${decodeURIComponent(err.replace(/\+/g, " "))}`);
      history.replaceState(null, "", location.pathname + location.search);
      return;
    }

    const tok = p.get("access_token");
    if (!tok) return;
    let user = {};
    try {
      const body = JSON.parse(atob(tok.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
      const m = body.user_metadata || {};
      // `sub` is the account's UUID. It is the only identifier that may be
      // sent to Google Analytics, so it is kept alongside the display fields
      // rather than re-derived from the token everywhere it is needed.
      user = { id: body.sub, email: body.email, name: m.full_name,
               picture: m.avatar_url, planTier: m.plan_tier };
    } catch {}
    saveSession({ access_token: tok, refresh_token: p.get("refresh_token"), user });
    history.replaceState(null, "", location.pathname + location.search);
    // A page that was rendered signed-out needs to hear about this.
    setTimeout(announce, 0);
  })();

  renderAuth();

  // The redirect flow only ever sees the access token, and that carries the
  // account's id but not the date it was opened. One call fills in the rest
  // and it is then in the stored session for good, so this runs once per
  // browser rather than once per page. Sessions saved before the id was kept
  // at all are repaired by the same call.
  (async function hydrateUser() {
    const s = session();
    if (!HOSTED || !s || !s.access_token || !s.user) return;
    if (s.user.id && s.user.createdAt) return;
    try {
      const r = await fetch(`${CFG.supabaseUrl}/auth/v1/user`, {
        headers: { apikey: CFG.supabaseAnonKey, Authorization: `Bearer ${s.access_token}` },
      });
      if (!r.ok) return;
      const u = await r.json();
      if (!u || !u.id) return;
      const m = u.user_metadata || {};
      const now = session();
      if (!now) return;
      saveSession({ ...now, user: { ...now.user, id: u.id, createdAt: u.created_at,
                                    planTier: m.plan_tier } });
      announce();
    } catch {}
  })();

  /* ---- Google One Tap --------------------------------------------------- */
  // The prompt in the top-right corner. Requires a client id; silently absent
  // without one rather than showing a control that cannot work.
  //
  // The credential Google hands back is exchanged for a Supabase session. That
  // exchange only succeeds if this same client id is listed under "Authorized
  // Client IDs" on Supabase's Google provider -- without it GoTrue has no
  // reason to trust the token and answers 400. A failure there used to be
  // swallowed, which looked exactly like nothing happening, so it is reported
  // now and the redirect flow is offered as the way through.
  function adoptSession(d) {
    const u = d.user || {};
    const m = u.user_metadata || {};
    saveSession({
      access_token: d.access_token,
      refresh_token: d.refresh_token,
      user: { id: u.id, email: u.email, name: m.full_name || m.name,
              picture: m.avatar_url || m.picture, createdAt: u.created_at,
              planTier: m.plan_tier },
    });
    renderAuth();
    announce();
  }

  /* One Tap needs a nonce of its own.

     With use_fedcm_for_prompt the browser mints a nonce and puts it in the
     id_token whether or not we asked for one. Sending that token to Supabase
     without the matching nonce fails with "Passed nonce and nonce in id_token
     should either both exist or not" -- which is why the button worked (it goes
     through the redirect grant) and the pop-up did not.

     Google is given the SHA-256 of the nonce, which is what lands in the token;
     Supabase is given the raw value and hashes it to compare. Handing the same
     string to both would fail the comparison just as surely. */
  async function makeNonce() {
    const bytes = crypto.getRandomValues(new Uint8Array(32));
    const raw = btoa(String.fromCharCode(...bytes))
      .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(raw));
    const hashed = [...new Uint8Array(digest)]
      .map(b => b.toString(16).padStart(2, "0")).join("");
    return { raw, hashed };
  }

  if (CFG.googleClientId && !session()) {
    const s = document.createElement("script");
    s.src = "https://accounts.google.com/gsi/client";
    s.async = true;
    s.onload = async () => {
      if (!window.google || !google.accounts || !google.accounts.id) return;
      // No SubtleCrypto (an insecure origin) means no verifiable nonce, so the
      // pop-up is skipped rather than offered and then refused.
      if (!(crypto && crypto.subtle)) return;
      let nonce;
      try { nonce = await makeNonce(); } catch { return; }
      google.accounts.id.initialize({
        client_id: CFG.googleClientId,
        nonce: nonce.hashed,
        callback: async res => {
          let d = null, status = 0;
          try {
            const r = await fetch(
              `${CFG.supabaseUrl}/auth/v1/token?grant_type=id_token`, {
                method: "POST",
                headers: { apikey: CFG.supabaseAnonKey, "Content-Type": "application/json" },
                body: JSON.stringify({ provider: "google", id_token: res.credential,
                                       nonce: nonce.raw }),
              });
            status = r.status;
            d = await r.json();
          } catch (e) {
            console.error("[auth] one-tap exchange failed", e);
          }
          if (d && d.access_token) return adoptSession(d);

          // Deliberately not falling through to the redirect. Chaining one
          // broken sign-in into another only moves the failure somewhere the
          // reason is harder to see.
          const why = (d && (d.error_description || d.msg || d.error)) || `HTTP ${status}`;
          console.error("[auth] one-tap rejected:", why);
          authNote(`Google sign-in was refused: ${why}`);
        },
        auto_select: false,
        cancel_on_tap_outside: true,
        use_fedcm_for_prompt: true,
      });
      google.accounts.id.prompt();
    };
    document.head.appendChild(s);
  }

  /* ---- site footer ------------------------------------------------------ */
  if (!document.querySelector(".site-foot")) {
    document.body.insertAdjacentHTML("beforeend", `
      <footer class="site-foot">
        <span>© ${new Date().getFullYear()} Ticker Alpha · Logos by <a href="https://logo.dev" target="_blank" rel="noopener">Logo.dev</a></span>
        <span class="site-foot-links">
          <a href="privacy.html">Privacy Policy</a>
          <a href="news.html">News</a>
          <a href="earnings.html">Earnings</a>
          <a href="company.html">Company Report</a>
          <a href="portfolio.html">Portfolio</a>
        </span>
      </footer>`);
  }
})();
