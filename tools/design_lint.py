#!/usr/bin/env python3
"""design_lint.py -- the ratchet that keeps the type scale from regrowing.

The pages once carried 26 distinct font sizes, 14 radii and five hard-coded
avatar palettes because nothing counted them. This counts, per file:

  * literal px font-sizes in CSS  (font-size:12.5px)   -- should be var(--fs-n)
  * literal px border-radius      (border-radius:9px)   -- should be var(--r*)
  * hex colours                   (#2563eb)             -- should be tokens
  * local toFixed() calls in page scripts                -- should be TA.fmt

and fails when any count is higher than the committed baseline. nav.css is
the one file allowed to hold literals, because that is where the tokens are
defined. Running with --update rewrites the baseline; do that only in the
same change that lowers a count, never to make a rise pass.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "tools" / "design_baseline.json"
PAGES = ["landing.html", "dashboard.html", "news.html", "earnings.html",
         "portfolio.html", "politicians.html", "reports.html", "privacy.html",
         "nav.js", "logo.js"]

RULES = {
    "font-size-px": re.compile(r"font-size\s*:\s*\d+(?:\.\d+)?px"),
    "radius-px": re.compile(r"border-radius\s*:\s*[^;}]*\d+(?:\.\d+)?px"),
    "hex-colour": re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b(?![0-9a-fA-F])"),
    "toFixed": re.compile(r"\.toFixed\("),
}
def count(path: Path) -> dict[str, int]:
    text = path.read_text(encoding="utf-8", errors="replace")
    out = {}
    for name, rx in RULES.items():
        out[name] = len(rx.findall(text))
    return out


def main(argv: list[str]) -> int:
    update = "--update" in argv
    current = {p: count(ROOT / p) for p in PAGES if (ROOT / p).exists()}
    if update or not BASELINE.exists():
        BASELINE.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        print(f"design lint: baseline {'rewritten' if update else 'created'} at {BASELINE.relative_to(ROOT)}")
        return 0
    base = json.loads(BASELINE.read_text())
    worse, better = [], []
    for page, counts in current.items():
        for rule, n in counts.items():
            b = base.get(page, {}).get(rule)
            if b is None:
                continue
            if n > b:
                worse.append(f"{page}: {rule} rose {b} -> {n}")
            elif n < b:
                better.append(f"{page}: {rule} fell {b} -> {n}")
    for line in better:
        print("  improved  " + line)
    if worse:
        print("design lint: the count of literals rose; use the tokens in nav.css")
        for line in worse:
            print("  FAIL  " + line)
        return 1
    print(f"design lint: ok ({len(current)} files, nothing rose)")
    if better:
        print("  counts fell; run `python tools/design_lint.py --update` to lock them in")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
