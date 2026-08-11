/* ===========================================================================
   Ticker Alpha — sign-in gates.

   One module for every wall on the site, so a gate looks and behaves the same
   whether it sits over a chart, a list of earnings, or the back seven pages of
   a research report.

   It has to run in two quite different places:

     app pages     nav.js is already there and owns the session. This module
                   asks it who is signed in and hands sign-in straight back to
                   it, so there is still exactly one place that talks to
                   Supabase Auth.
     report pages  the reports are finished artefacts -- their own stylesheet,
                   their own fonts, their own A4 sheet -- and deliberately load
                   no nav. Here this module reads the session itself and starts
                   the Google round trip on its own.

   The styles are injected rather than added to nav.css for that second reason:
   the reports do not load nav.css, and a wall that is invisible on the page it
   is most needed on is worse than no wall at all.

   What this is and is not
   -----------------------
   This is a conversion gate, not access control. The content is rendered and
   then covered, so anyone who opens the developer tools can still read it.
   Making that impossible means not sending the content until the request
   carries a token, which is a server change -- see the note in README. The
   point here is that the ordinary visitor meets a reason to sign up at the
   moment they want the thing, which is what actually moves the number.
=========================================================================== */
(function () {
  "use strict";

  const CFG = window.ALPHATICKER_CONFIG || window.LEDGER_CONFIG || {};
  const TOKEN_KEY = "alphaticker-session";
  const STYLE_ID = "ta-gate-css";

  // analytics.js is not loaded on the report pages, so every call goes through
  // this rather than assuming the tag is there.
  const GA = () => window.TAnalytics || { track() {} };
  const esc = s => String(s ?? "").replace(/[&<>"]/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  /* ---- who is signed in -------------------------------------------------- */

  function session() {
    try { return JSON.parse(localStorage.getItem(TOKEN_KEY) || "null"); }
    catch { return null; }
  }

  function signedIn() {
    if (window.TA && typeof window.TA.signedIn === "function") return window.TA.signedIn();
    const s = session();
    return !!(s && s.user);
  }

  /** Start the Google round trip. On an app page nav.js already knows how;
      on a report page we build the same authorize URL ourselves so the visitor
      lands back on the report they were reading rather than on the home page. */
  function signIn() {
    if (window.TA && typeof window.TA.signIn === "function") return window.TA.signIn();
    if (!CFG.supabaseUrl || !CFG.googleClientId) {
      // No configuration here means no working sign-in. Send them somewhere
      // that has it rather than opening a Google page that errors.
      location.href = new URL("../index.html", location.href).href;
      return;
    }
    const back = location.origin + location.pathname + location.search;
    location.href = `${CFG.supabaseUrl}/auth/v1/authorize` +
      `?provider=google&redirect_to=${encodeURIComponent(back)}`;
  }

  /* Supabase hands the session back in the URL fragment. nav.js does this on
     the app pages; a report page has no nav.js, so without this the visitor
     would come back from Google signed in everywhere except the document they
     signed in to read. Guarded so the two never both consume the fragment. */
  function captureRedirect() {
    const h = location.hash.slice(1);
    if (!h) return;
    const p = new URLSearchParams(h);
    const tok = p.get("access_token");
    if (!tok) return;
    let user = {};
    try {
      const body = JSON.parse(atob(tok.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
      const m = body.user_metadata || {};
      user = { id: body.sub, email: body.email, name: m.full_name,
               picture: m.avatar_url, planTier: m.plan_tier };
    } catch {}
    try {
      localStorage.setItem(TOKEN_KEY, JSON.stringify({
        access_token: tok, refresh_token: p.get("refresh_token"), user }));
    } catch {}
    history.replaceState(null, "", location.pathname + location.search);
  }
  if (!window.TA) captureRedirect();

  /* ---- styles ------------------------------------------------------------ */
  /* Every colour falls back twice: the app pages define --card/--ink/--rule,
     the reports define --paper/--ink/--rule and nothing else, and a page that
     defines neither still has to render something legible. */

  function injectStyles() {
    if (document.getElementById(STYLE_ID)) return;
    const css = `
/* The peek is a maximum, but the card is the point -- a wall whose offer is
   cut in half is worse than no peek at all. The floor is the card's own
   height plus the veil's padding, so a region inside a short scroll box (the
   earnings list is capped at 590px) still shows the whole thing. */
.gate-rest{position:relative;overflow:hidden;
  max-height:var(--gate-peek,300px);min-height:248px}
.gate-rest>.gate-blur{filter:blur(7px) saturate(.75);opacity:.55;
  pointer-events:none;user-select:none;-webkit-user-select:none}
.gate-veil{position:absolute;inset:0;z-index:2;
  display:flex;align-items:flex-start;justify-content:center;
  padding:18px 16px;
  background:linear-gradient(to bottom,
    rgba(255,255,255,0) 0%,
    var(--gate-fade,rgba(253,253,252,.72)) 34%,
    var(--gate-fade-2,rgba(253,253,252,.94)) 100%)}
.gate-card{width:min(360px,100%);text-align:center;
  background:var(--card,var(--paper,#fff));
  color:var(--ink,#14181D);
  border:1px solid var(--rule,#E4E2DC);border-radius:14px;
  box-shadow:0 14px 38px -18px rgba(17,24,32,.5);
  padding:18px 20px 16px;
  font-family:inherit}
.gate-card .gate-lock{width:26px;height:26px;display:block;margin:0 auto 9px;
  color:var(--ink-3,var(--muted,#797F86))}
.gate-card b{display:block;font-size:15.5px;font-weight:650;line-height:1.3;
  letter-spacing:-.01em;margin-bottom:5px;color:var(--ink,#14181D)}
.gate-card p{margin:0 0 13px;font-size:13px;line-height:1.45;
  color:var(--ink-2,var(--ink-soft,#3A4149))}
.gate-cta{display:flex;align-items:center;justify-content:center;gap:9px;
  width:100%;border:0;border-radius:9px;cursor:pointer;
  padding:11px 14px;font:inherit;font-size:14px;font-weight:600;
  background:#1a1d23;color:#fff}
.gate-cta:hover{background:#0e1116}
.gate-cta svg{width:17px;height:17px;flex-shrink:0}
.gate-fine{display:block;margin-top:9px;font-size:11.5px;
  color:var(--ink-3,var(--muted,#797F86))}
@media (prefers-color-scheme:dark){
  .gate-veil{--gate-fade:rgba(17,20,26,.72);--gate-fade-2:rgba(17,20,26,.94)}
}
[data-theme=dark] .gate-veil{--gate-fade:rgba(17,20,26,.72);
  --gate-fade-2:rgba(17,20,26,.94)}
[data-theme=light] .gate-veil{--gate-fade:rgba(253,253,252,.72);
  --gate-fade-2:rgba(253,253,252,.94)}
@media (max-width:520px){
  .gate-card{padding:15px 15px 13px}
  .gate-card b{font-size:14.5px}
}
/* A gated page must never print the blurred half. */
@media print{ .gate-rest{display:none!important} }
`;
    const el = document.createElement("style");
    el.id = STYLE_ID;
    el.textContent = css;
    document.head.appendChild(el);
  }

  /* ---- the wall ---------------------------------------------------------- */

  const GOOGLE_MARK = `<svg viewBox="0 0 24 24" aria-hidden="true">
    <path fill="#fff" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
    <path fill="#fff" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
    <path fill="#fff" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/>
    <path fill="#fff" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/>
  </svg>`;

  const LOCK_MARK = `<svg class="gate-lock" viewBox="0 0 20 20" fill="none"
      stroke="currentColor" stroke-width="1.5" stroke-linecap="round"
      stroke-linejoin="round" aria-hidden="true">
    <rect x="4" y="8.5" width="12" height="8" rx="1.6"/>
    <path d="M7 8.5V6a3 3 0 0 1 6 0v2.5"/>
  </svg>`;

  // Every live gate, so One Tap -- which signs someone in without reloading --
  // can take the walls down where they stand instead of leaving a page that
  // says "sign in" to somebody who just did.
  const live = new Set();

  /**
   * Put a wall over part of the page.
   *
   * @param host    the element whose children are being gated
   * @param opts.items   selector for the children to consider (default: all)
   * @param opts.keep    how many of them stay readable (default 0)
   * @param opts.peek    how much of the walled part shows through, in px
   * @param opts.title   the headline on the card
   * @param opts.note    the line under it
   * @param opts.cta     the button label
   * @param opts.reason  short slug for analytics -- see the gate vocabulary
   * @returns true if a wall was put up
   */
  function gate(host, opts) {
    opts = opts || {};
    if (!host || signedIn()) return false;
    if (host.querySelector(":scope > .gate-rest")) return false;   // already walled

    const all = opts.items
      ? Array.from(host.querySelectorAll(opts.items))
      : Array.from(host.children);
    const keep = Math.max(0, opts.keep | 0);
    const hide = all.slice(keep);
    if (!hide.length) return false;

    injectStyles();

    // The blurred children move into a wrapper and the card sits beside them
    // rather than inside, so the card itself is not blurred. Wrapping is also
    // why nothing here has to measure anything: the veil covers its parent,
    // and it keeps covering it when the window changes size.
    const rest = document.createElement("div");
    rest.className = "gate-rest";
    if (opts.peek) rest.style.setProperty("--gate-peek", opts.peek + "px");

    const blur = document.createElement("div");
    blur.className = "gate-blur";
    blur.setAttribute("aria-hidden", "true");

    hide[0].parentNode.insertBefore(rest, hide[0]);
    hide.forEach(el => blur.appendChild(el));
    rest.appendChild(blur);

    const reason = opts.reason || "generic";
    const veil = document.createElement("div");
    veil.className = "gate-veil";
    veil.innerHTML = `
      <div class="gate-card" role="group" aria-label="Sign in to continue">
        ${LOCK_MARK}
        <b>${esc(opts.title || "Sign up to see the rest")}</b>
        <p>${esc(opts.note || "Free account. Takes a few seconds.")}</p>
        <button type="button" class="gate-cta">
          ${GOOGLE_MARK}${esc(opts.cta || "Continue with Google")}
        </button>
        <span class="gate-fine">${esc(opts.fine || "Free — no card required")}</span>
      </div>`;
    rest.appendChild(veil);

    veil.querySelector(".gate-cta").onclick = () => {
      GA().track("gate_click", { gate: reason });
      // On an app page the sheet is right here. A report has no nav and so no
      // sheet; sending it straight to Google would quietly drop the email
      // route on the one wall that matters most, so it goes to the app with
      // the report to come back to and opens the sheet there.
      if (window.TA && typeof window.TA.showSignupModal === "function") {
        window.TA.showSignupModal({ reason });
      } else {
        const back = location.pathname + location.search;
        location.href = new URL("../index.html", location.href).pathname +
          `?auth=1&next=${encodeURIComponent(back)}`;
      }
    };

    live.add({ host, rest, blur });
    GA().track("gate_view", { gate: reason });
    return true;
  }

  /** Put back everything every wall was hiding, in place. */
  function release() {
    live.forEach(g => {
      const { rest, blur } = g;
      if (!rest.parentNode) return;
      while (blur.firstChild) rest.parentNode.insertBefore(blur.firstChild, rest);
      rest.remove();
    });
    live.clear();
  }

  addEventListener("ta-auth", () => { if (signedIn()) release(); });

  window.TAGate = { gate, release, signedIn, signIn, session };
})();
