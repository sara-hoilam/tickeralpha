/* Ticker Alpha — cached company logos (Logo.dev via Supabase).
 *
 * The worker downloads Logo.dev images for S&P 500 + top crypto and stores
 * them in ledger.symbol_logo. Pages call TickerLogos.hydrate(symbols, root)
 * after rendering elements marked with data-logo="AAPL".
 *
 * Two patterns:
 *   - Initials avatar (.dot / .av): logo covers the initials when cached
 *   - Beside-ticker chip (.tick-logo): empty until cached, then shows logo
 */
(function () {
  "use strict";

  if (window.TickerLogos) return;

  var cache = Object.create(null);

  function cfg() {
    return window.ALPHATICKER_CONFIG || window.LEDGER_CONFIG || {};
  }

  function injectCss() {
    if (document.getElementById("ticker-logos-css")) return;
    var s = document.createElement("style");
    s.id = "ticker-logos-css";
    s.textContent = [
      /* Initials avatars — logo replaces monogram when present.
         Match any hydrated [data-logo] chip (not .tick-logo), so calendar
         week avatars and similar spans contain the image instead of
         letting a full-size logo spill across the layout. */
      ".dot.has-logo,.av.has-logo,[data-logo].has-logo:not(.tick-logo){",
      "  background:#fff!important;color:transparent!important;",
      "  overflow:hidden;position:relative}",
      ".dot img.logo-img,.av img.logo-img,",
      "[data-logo].has-logo:not(.tick-logo) > img.logo-img{",
      "  position:absolute;inset:0;width:100%;height:100%;",
      "  object-fit:contain;padding:15%;box-sizing:border-box;",
      "  border-radius:inherit;display:block}",
      /* Beside-ticker chips — hidden until a real logo is available.
         Circular, and padded the same 15% as the avatars above: these sit
         next to .dot avatars on the same screens, and a rounded square beside
         a circle reads as two different systems rather than one. */
      ".tick-logo{",
      "  display:inline-block;flex-shrink:0;width:22px;height:22px;",
      "  border-radius:50%;overflow:hidden;",
      "  position:relative;box-sizing:border-box;",
      "  border:1px solid var(--rule, rgba(0,0,0,.08))}",
      /* Initials fill the badge until an image covers them, so the row keeps
         its alignment either way. */
      ".tick-logo .tick-ini{",
      "  position:absolute;inset:0;display:grid;place-items:center;",
      "  font-style:normal;font-weight:700;font-size:8.5px;letter-spacing:.02em;",
      "  color:#fff;font-family:inherit}",
      ".tick-logo.tick-logo-lg .tick-ini{font-size:10.5px}",
      ".tick-logo.tick-logo-xl .tick-ini{font-size:13px}",
      ".tick-logo.has-logo{background:#fff!important}",
      ".tick-logo.has-logo .tick-ini{display:none}",
      ".tick-logo.tick-logo-lg{width:28px;height:28px}",
      ".tick-logo.tick-logo-xl{width:36px;height:36px}",
      ".tick-logo img.logo-img{",
      "  position:absolute;inset:0;width:100%;height:100%;",
      "  object-fit:contain;padding:15%;box-sizing:border-box;",
      "  border-radius:inherit;display:block}",
      /* Row / chart header layouts */
      ".tsym-with-logo{",
      "  display:flex;flex-direction:row;align-items:center;gap:8px;",
      "  text-decoration:none;min-width:0}",
      ".tsym-with-logo .tsym-text{",
      "  display:flex;flex-direction:column;align-items:flex-start;gap:1px;",
      "  min-width:0}",
      ".c-sym-line{",
      "  display:inline-flex;align-items:center;gap:8px;min-width:0}",
      ".c-sym-line .tick-logo.tick-logo-lg{width:26px;height:26px}",
      ".ident-with-logo{",
      "  display:flex;flex-direction:row;align-items:flex-start;gap:12px}",
      ".ident-with-logo .tick-logo.tick-logo-xl{margin-top:2px}",
    ].join("");
    document.head.appendChild(s);
  }

  async function fetchLogos(symbols) {
    var C = cfg();
    if (!C.supabaseUrl || !C.supabaseAnonKey) return cache;

    var uniq = [];
    var seen = Object.create(null);
    (symbols || []).forEach(function (raw) {
      var s = String(raw || "").toUpperCase().trim();
      if (!s || seen[s]) return;
      seen[s] = 1;
      if (cache[s] && cache[s]._resolved) return;
      uniq.push(s);
    });
    if (!uniq.length) return cache;

    var tok = window.TA && window.TA.token && window.TA.token();
    var headers = {
      apikey: C.supabaseAnonKey,
      "Content-Type": "application/json",
    };
    if (tok) headers.Authorization = "Bearer " + tok;

    var r = await fetch(C.supabaseUrl + "/rest/v1/rpc/get_symbol_logos", {
      method: "POST",
      headers: headers,
      body: JSON.stringify({ p_symbols: uniq }),
    });
    if (!r.ok) return cache;

    var rows = await r.json();
    (Array.isArray(rows) ? rows : []).forEach(function (row) {
      if (!row || !row.symbol) return;
      cache[row.symbol] = {
        symbol: row.symbol,
        status: row.status,
        dataUrl: row.dataUrl || null,
        _resolved: true,
      };
    });
    uniq.forEach(function (s) {
      if (!cache[s]) {
        cache[s] = { symbol: s, status: "unknown", dataUrl: null, _resolved: true };
      } else {
        cache[s]._resolved = true;
      }
    });
    return cache;
  }

  function dataUrl(symbol) {
    var row = cache[String(symbol || "").toUpperCase().trim()];
    return row && row.dataUrl ? row.dataUrl : null;
  }

  function applyOne(el) {
    var sym = el.getAttribute("data-logo");
    var url = dataUrl(sym);
    if (!url) {
      el.classList.remove("has-logo");
      var dead = el.querySelector("img.logo-img");
      if (dead) dead.remove();
      return;
    }
    var existing = el.querySelector("img.logo-img");
    if (existing) {
      if (existing.getAttribute("src") !== url) existing.setAttribute("src", url);
      el.classList.add("has-logo");
      return;
    }
    var img = document.createElement("img");
    img.className = "logo-img";
    img.alt = "";
    img.decoding = "async";
    img.loading = "lazy";
    img.src = url;
    img.onerror = function () {
      el.classList.remove("has-logo");
      if (img.parentNode) img.remove();
    };
    el.classList.add("has-logo");
    el.appendChild(img);
  }

  function apply(root) {
    injectCss();
    var scope = root || document;
    scope.querySelectorAll("[data-logo]").forEach(applyOne);
  }

  async function hydrate(symbols, root) {
    try {
      await fetchLogos(symbols);
      apply(root);
    } catch (_) {
      /* keep initials / hide chips */
    }
  }

  /** Escapes for use in HTML attribute values. */
  function escAttr(s) {
    return String(s ?? "").replace(/[&<>"']/g, function (c) {
      return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c];
    });
  }

  /** Markup for a beside-ticker logo chip (hidden until hydrate finds an image). */
  /* Deterministic colour per symbol, so a badge keeps the same one everywhere. */
  function initialsColour(sym) {
    var h = 0;
    for (var i = 0; i < sym.length; i++) h = (h * 31 + sym.charCodeAt(i)) % 360;
    return "hsl(" + h + " 42% 38%)";
  }

  /* The chip used to be empty until a logo cached, and `display:none` with it,
     so a symbol Logo.dev has no image for (SPYI, most small caps) collapsed and
     shoved the ticker text left out of line with its neighbours. It now carries
     the first two letters on a coloured circle -- the same badge the market
     tables draw -- and the image simply covers that when one arrives. */
  function chip(symbol, sizeClass) {
    var sym = String(symbol || "").toUpperCase().trim();
    if (!sym) return "";
    var cls = "tick-logo" + (sizeClass ? " " + sizeClass : "");
    return '<span class="' + cls + '" data-logo="' + escAttr(sym) + '"'
         + ' style="background:' + initialsColour(sym) + '"'
         + ' aria-hidden="true"><i class="tick-ini">' + escAttr(sym.slice(0, 2))
         + '</i></span>';
  }

  /**
   * Set chart / heading symbol text and optional logo chip.
   * pass logo:false (or non-ticker labels like "Portfolio") to clear the chip.
   */
  function setHeading(symEl, logoEl, symbol, opts) {
    opts = opts || {};
    var label = symbol == null ? "" : String(symbol);
    if (symEl) symEl.textContent = label || "—";
    if (!logoEl) return;
    var sym = label.toUpperCase().trim();
    var isTicker = opts.logo !== false
      && /^[A-Z][A-Z0-9.]{0,9}$/.test(sym)
      && sym !== "PORTFOLIO"
      && sym !== "CASH";
    logoEl.innerHTML = "";
    logoEl.classList.remove("has-logo");
    if (!isTicker) {
      logoEl.removeAttribute("data-logo");
      logoEl.hidden = true;
      return;
    }
    logoEl.hidden = false;
    logoEl.setAttribute("data-logo", sym);
    hydrate([sym], logoEl.parentNode || logoEl);
  }

  injectCss();

  window.TickerLogos = {
    load: fetchLogos,
    apply: apply,
    hydrate: hydrate,
    dataUrl: dataUrl,
    chip: chip,
    setHeading: setHeading,
  };
})();
