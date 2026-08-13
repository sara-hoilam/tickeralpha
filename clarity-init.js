/**
 * Microsoft Clarity — bootstrapped from @microsoft/clarity (npm).
 *
 * Loaded as a module on every page after config.js. The project ID is public
 * (it ships in the browser tag URL); set it on ALPHATICKER_CONFIG.
 */
import Clarity from "./vendor/clarity/index.js";

const CFG = window.ALPHATICKER_CONFIG || window.LEDGER_CONFIG || {};
const projectId = (CFG.clarityProjectId || "").trim();

/* The same self-traffic guards analytics.js applies, for the same reasons.
   Local previews serve the real config.js, so the hostname is the guard; and
   a browser marked internal (?ta_internal=1, stored by analytics.js, which
   always runs before this module) should not record sessions of the owner
   reading their own site. Clarity has no traffic_type equivalent, so the
   honest version is not to record at all. nav.js and analytics.js both
   null-check window.TAClarity, so leaving it unset is safe. */
const LOCAL_HOST = /^(localhost$|127\.|0\.0\.0\.0$|\[::1\]$)/.test(location.hostname);
let INTERNAL = false;
try { INTERNAL = localStorage.getItem("alphaticker-internal") === "1"; } catch {}

if (projectId && !LOCAL_HOST && !INTERNAL) {
  Clarity.init(projectId);
  // Expose the same API the npm package documents, so nav.js can identify
  // signed-in visitors without a second import.
  window.TAClarity = Clarity;

  // The cookie banner in analytics.js speaks for both tags, so honour the same
  // answer here rather than storing regardless. Declared straight after init
  // because this module is deferred and the choice was made long before it
  // ran; analytics.js reapplies it on every later change.
  let consent = null;
  try { consent = localStorage.getItem("alphaticker-consent"); } catch {}
  Clarity.consentV2({
    ad_Storage: "denied",
    analytics_Storage: consent === "granted" ? "granted" : "denied",
  });
}
