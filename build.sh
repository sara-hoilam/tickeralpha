#!/usr/bin/env bash
# Build the static site for Cloudflare Pages.
#
# Pages settings:
#   Build command:          bash build.sh
#   Build output directory: public
#
# Pages plus the shared navigation, no bundler:
#   index.html    Markets Today — the landing page
#   news.html     market news
#   earnings.html earnings calendar
#   company.html  the filed-financials dashboard
#   portfolio.html user holdings tracker
#   privacy.html  privacy policy
#   reports.html  the research index
#   reports/      full-length reports, self-contained
#   nav.js/.css   the top bar every page injects
#   gate.js       the sign-in walls (app pages and reports both load it)
#   brand-logo-*  light/dark brand mark for the nav
#   favicon-*     the same mark at tab size; nav.js picks one by the
#                 browser's colour scheme, favicon.ico is the fallback
#   analytics.js  Google Analytics 4 (gtag.js) and the consent banner
#   clarity-init.js + vendor/clarity  Microsoft Clarity (@microsoft/clarity)
#   logo.js       cached Logo.dev company logos (via Supabase)
set -euo pipefail

# Clarity ships as an npm package; install it when the build environment does
# not already have node_modules (Cloudflare Pages, a fresh clone).
if [ ! -f node_modules/@microsoft/clarity/index.js ]; then
  if command -v npm >/dev/null 2>&1; then
    npm ci --omit=dev
  else
    echo "npm is required to install @microsoft/clarity" >&2
    exit 1
  fi
fi

mkdir -p public/vendor/clarity/src
cp landing.html   public/index.html
cp dashboard.html public/company.html
cp news.html      public/news.html
cp earnings.html  public/earnings.html
cp portfolio.html public/portfolio.html
cp politicians.html public/politicians.html
cp privacy.html   public/privacy.html
cp reports.html   public/reports.html
# The report is a finished artefact -- its own stylesheet, its own fonts, its
# own A4 sheet -- so it is copied whole rather than folded into the app shell.
mkdir -p public/reports
for r in nvda spcx mu pltr; do
  cp "reports/$r.html" "reports/$r.pdf" "reports/$r-cover.png" public/reports/
done
cp nav.js nav.css public/
# Tells Cloudflare Pages to make the pages and the shared, unversioned
# scripts revalidate, so a deploy is visible without clearing a cache.
cp _headers public/
# gate.js is loaded by the app pages *and* by the reports, which reach it as
# ../gate.js from public/reports/. One copy at the root serves both.
cp gate.js public/
cp brand-logo-light.png brand-logo-dark.png public/
cp favicon.ico favicon-light.png favicon-dark.png public/
cp analytics.js public/
cp logo.js public/
cp clarity-init.js public/
cp node_modules/@microsoft/clarity/index.js      public/vendor/clarity/
cp node_modules/@microsoft/clarity/package.json  public/vendor/clarity/
cp node_modules/@microsoft/clarity/src/utils.js  public/vendor/clarity/src/

# ---- content-hashed asset names -------------------------------------------
# The shared scripts have stable names, so _headers had to make every page
# load revalidate each of them -- five conditional round trips per navigation,
# ~0.3s apiece on a warm connection. A content-hashed copy under /assets can
# be cached for a year instead: the name changes when the content does, so a
# deploy is still instantly visible, and a repeat visit skips the round trips.
#
# config.js stays unversioned (the env injection below edits it after this
# point), and clarity-init.js stays put because it imports ./vendor/clarity by
# relative path.
mkdir -p public/assets
for f in nav.js nav.css logo.js analytics.js gate.js; do
  base="${f%.*}"; ext="${f##*.}"
  h=$(md5sum "public/$f" | cut -c1-8)
  mv "public/$f" "public/assets/$base.$h.$ext"
  # Root pages reference "nav.js"; the reports reach gate.js as "../gate.js".
  # The two patterns are disjoint (the bare one requires the quote right
  # before the name), so both rewrites can run over every page.
  for page in public/*.html public/reports/*.html; do
    sed -i "s|\"$f\"|\"assets/$base.$h.$ext\"|g; s|\"\.\./$f\"|\"../assets/$base.$h.$ext\"|g" "$page"
  done
  echo "versioned $f -> assets/$base.$h.$ext"
done

if [ ! -f public/config.js ]; then
  echo "public/config.js is missing — both pages would have no data source." >&2
  exit 1
fi

# Optional: inject the public analytics IDs from the environment (Cloudflare
# Pages env vars, or a local `.env` already exported). Leaves config.js
# unchanged when unset, so a committed ID keeps working.
inject() {
  python3 - "$1" "$2" "$3" <<'PY'
import json, pathlib, re, sys
field, value, label = sys.argv[1], sys.argv[2], sys.argv[3]
path = pathlib.Path("public/config.js")
text = path.read_text(encoding="utf-8")
new, n = re.subn(
    field + r':\s*["\'][^"\']*["\']',
    field + ": " + json.dumps(value),
    text, count=1)
if n != 1:
    sys.exit(f"could not inject {label} into public/config.js")
path.write_text(new, encoding="utf-8")
print(f"{label}: injected ({len(value)} chars)")
PY
}

if [ -n "${CLARITY_PROJECT_ID:-}" ]; then
  inject clarityProjectId "$CLARITY_PROJECT_ID" CLARITY_PROJECT_ID
fi

if [ -n "${GA_MEASUREMENT_ID:-}" ]; then
  inject gaMeasurementId "$GA_MEASUREMENT_ID" GA_MEASUREMENT_ID
fi

echo "built public/: $(ls -1 public | tr '\n' ' ')"
