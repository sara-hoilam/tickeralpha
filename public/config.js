// Ticker Alpha — deployed configuration.
//
// This file is what turns the page from "talks to a local server.py" into
// "talks to Supabase". It is served alongside index.html by Cloudflare Pages;
// locally it does not exist and the page falls back to server.py.
//
// The anon key is meant to be public. It ships in every visitor's browser and
// reaches exactly four read-only functions; the tables live in a schema the
// API does not expose. The service-role key must never appear here.

window.ALPHATICKER_CONFIG = {
  supabaseUrl: "https://etembhwwbqswzcepfhxp.supabase.co",
  supabaseAnonKey: "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImV0ZW1iaHd3YnFzd3pjZXBmaHhwIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODU1NjM1MDEsImV4cCI6MjEwMTEzOTUwMX0.LpaWoQxaF0Xl0FPRq7nK7aDi7Wr_hMelnPtXJzO3y9Y",

  // Google sign-in. The client id is public — it ships in the page the same way
  // the anon key does. The client secret belongs only in Supabase's provider
  // settings and must never appear in this file.
  googleClientId: "545683469665-p6u8svkptvshr5brkdn3ot99mrjnel4c.apps.googleusercontent.com",

  // Microsoft Clarity. Public project ID from clarity.microsoft.com → project
  // → Settings → Overview. Wired through @microsoft/clarity (see clarity-init.js).
  clarityProjectId: "xxfel0xa0x",

  // Google Analytics 4. The measurement ID (G-…) is public the same way, and
  // is the one gtag.js needs; the tag ID (GT-…) shown beside it in the GA4
  // admin belongs to a Google Tag container this site does not use.
  // See analytics.js.
  gaMeasurementId: "G-BTQ5F5F2GR",

  // Logo.dev publishable key (safe client-side). Logos are cached in Supabase
  // by the worker; this key is kept for reference / future CDN fallbacks.
  logoDevPublishableKey: "pk_RQkedGufR1uCvTvlztKeQg",

  // PostHog product analytics. Public client key and ingest host injected by
  // build.sh from POSTHOG_API_KEY / POSTHOG_HOST env vars. See posthog-init.js.
  posthogApiKey: "",
  posthogHost: "",
};
