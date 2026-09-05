"""Unit tests for alpha.py story-first selection (no DB/FMP)."""

from __future__ import annotations

import datetime as dt
import unittest

import alpha


def _digest(**overrides) -> dict:
    base = {
        "symbol": "BE", "name": "Bloom Energy", "industry": "Electrical Equipment",
        "price": 12.5, "cap": 2.5e9, "pe": 28.0, "year_high": 18.0,
        "avg200": 13.0, "dd_1y": 0.30, "ext_pct": -0.04,
        "pe_own_pct": 40.0, "pe_hist": [], "ins_trades": [],
        "street_rows": [], "target_range": None, "monthly_raw": [],
        "rev_q": [], "cg_amt": 0.0, "cg_members": 0,
        "cg_lead_person": None, "cg_lead_amt": 0.0,
        "cg_sell_amt": 0.0, "cg_marks": [],
        "ins_cluster_buy": False, "ins_cluster_sell": False,
        "ins_buyers": 0, "ins_buy_amt": 0.0,
        "ins_sellers": 0, "ins_sell_amt": 0.0,
        "neg7": 0, "severe7": 0, "stories7": 0,
        "upside": 0.25, "updown30": 1,
        "rev_up": 6, "rev_comps": 8,
        "earn_days": 30, "season": 0.2, "season_month": 9,
        "chg1d": None, "move3d": None, "news3": 0, "big_news": 0,
        "since_earn": None, "earn_surprise": None,
        "has_long": False, "long_to": None, "dd_stats": None,
    }
    base.update(overrides)
    return base


def _score(d: dict) -> dict:
    ctx = alpha.build_ctx([d])
    return alpha.score(d, ctx)


class CongressCaptionTests(unittest.TestCase):
    def test_named_congress_lead(self):
        d = _digest(cg_amt=6.8e6, cg_lead_person="Nancy Pelosi", cg_lead_amt=6.8e6)
        self.assertEqual(alpha._congress_who(d), "Nancy Pelosi")

    def test_generic_congress_without_name(self):
        d = _digest(cg_amt=2e6)
        self.assertEqual(alpha._congress_who(d), "Members of Congress")


class SeedMiningTests(unittest.TestCase):
    def test_congress_seed(self):
        d = _digest(cg_amt=2e6, cg_lead_person="Nancy Pelosi")
        seeds = alpha.mine_seeds(d)
        self.assertTrue(seeds)
        self.assertEqual(seeds[0]["type"], alpha.SEED_CONGRESS)

    def test_insider_cluster_seed(self):
        d = _digest(ins_cluster_buy=True, ins_buyers=3, ins_buy_amt=3e6)
        seeds = alpha.mine_seeds(d)
        self.assertEqual(seeds[0]["type"], alpha.SEED_INSIDER)

    def test_rare_dip_needs_revenue_support(self):
        d = _digest(
            dd_stats={"dd": 0.22, "rarity": 92.0, "high": 18.0, "years": 8.0},
            rev_up=2, rev_comps=8,
        )
        types = [s["type"] for s in alpha.mine_seeds(d)]
        self.assertNotIn(alpha.SEED_DIP, types)

    def test_rare_dip_with_stable_revenue(self):
        d = _digest(
            dd_stats={"dd": 0.22, "rarity": 92.0, "high": 18.0, "years": 8.0},
            rev_up=6, rev_comps=8,
        )
        types = [s["type"] for s in alpha.mine_seeds(d)]
        self.assertIn(alpha.SEED_DIP, types)


class GateTests(unittest.TestCase):
    def test_soft_cap_for_congress_seed(self):
        d = _digest(cap=2e9, cg_amt=2e6, cg_lead_person="Nancy Pelosi")
        self.assertTrue(alpha.seed_eligible(d))
        self.assertEqual(alpha._cap_floor(d), alpha.SEED_MIN_CAP)
        self.assertEqual(alpha.buy_gates(d), [])

    def test_hard_cap_without_seed(self):
        d = _digest(cap=2e9)
        self.assertIn("size", alpha.buy_gates(d))

    def test_mega_cap_always_passes(self):
        d = _digest(cap=50e9)
        self.assertEqual(alpha.buy_gates(d), [])


class StorySelectionTests(unittest.TestCase):
    def test_support_families_fund_and_hist(self):
        d = _digest(upside=0.25, rev_up=7, rev_comps=8, dd_1y=0.15)
        ctx = alpha.build_ctx([d, _digest(symbol="OTHER", upside=0.05)])
        s = alpha.score(d, ctx)
        support = alpha.support_families(d, s)
        self.assertIn("fund", support)
        self.assertIn("hist", support)

    def test_claim_matches_congress_seed(self):
        d = _digest(cg_amt=2e6, cg_lead_person="Nancy Pelosi")
        s = _score(d)
        have = {"revenue": True, "drawdown": False, "pe": True,
                "ma": False, "seasonality": False, "targets": True}
        claims = alpha._claims(d, s, "BUY", have)
        congress = [c for c in claims if c.get("seed") == alpha.SEED_CONGRESS]
        self.assertTrue(congress)
        self.assertTrue(alpha._claim_matches_seed(congress[0], alpha.SEED_CONGRESS))
        hist = [c for c in claims if c.get("fam") == "hist"]
        if hist:
            self.assertFalse(
                alpha._claim_matches_seed(hist[0], alpha.SEED_CONGRESS))

    def test_pick_lead_respects_seed(self):
        d = _digest(
            cg_amt=5e6, cg_lead_person="Nancy Pelosi",
            dd_stats={"dd": 0.18, "rarity": 95.0, "high": 18.0, "years": 10.0},
            rev_up=7, rev_comps=8,
        )
        s = _score(d)
        have = {"drawdown": True, "revenue": True, "pe": True,
                "ma": False, "seasonality": False, "targets": True}
        claims = alpha._claims(d, s, "BUY", have)
        lead = alpha._pick_lead(claims, {}, alpha.SEED_CONGRESS)
        self.assertIsNotNone(lead)
        self.assertEqual(lead.get("seed"), alpha.SEED_CONGRESS)


if __name__ == "__main__":
    unittest.main()
