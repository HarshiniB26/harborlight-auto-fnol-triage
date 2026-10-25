"""Routing and policy-interval rules. These do not read the mock fixture."""
import unittest

from workbench.rules import determine_route, select_policy

INTERVALS = [
    {"version": "1", "effective_from": "2026-01-01T00:00:00Z", "effective_to": "2026-09-01T00:00:00Z",
     "wording_source": "AUTO-WORDING-V1", "endorsements": []},
    {"version": "2", "effective_from": "2026-09-01T00:00:00Z", "effective_to": "2027-01-01T00:00:00Z",
     "wording_source": "AUTO-WORDING-V2", "endorsements": ["AUTO-END-GLASS-01"]},
]


class RuleTests(unittest.TestCase):
    def decision(self, classification="collision", **overrides):
        """Build a routing decision with safe defaults for focused tests."""

        values = {
            "missing_fields": [],
            "conflicts": [],
            "assertions": [],
            "policy_available": True,
            "claims_available": True,
        }
        values.update(overrides)
        return determine_route(classification, **values)

    def test_half_open_interval_selects_version_one_at_its_last_second(self):
        selected, issue = select_policy(INTERVALS, "2026-08-31T23:59:59Z")
        self.assertIsNone(issue)
        self.assertEqual(selected["version"], "1")

    def test_exclusive_end_matches_no_interval(self):
        selected, issue = select_policy(INTERVALS, "2027-01-01T00:00:00Z")
        self.assertIsNone(selected)
        self.assertIn("No policy interval", issue)

    def test_overlap_does_not_pick_a_version(self):
        overlap = [
            {"version": "1", "effective_from": "2026-01-01T00:00:00Z", "effective_to": "2026-12-01T00:00:00Z"},
            {"version": "2", "effective_from": "2026-06-01T00:00:00Z", "effective_to": "2027-01-01T00:00:00Z"},
        ]
        selected, issue = select_policy(overlap, "2026-10-01T12:00:00Z")
        self.assertIsNone(selected)
        self.assertIn("Multiple policy intervals", issue)

    def test_manual_review_wins_and_injury_stays_visible(self):
        decision = self.decision(
            missing_fields=["driver_name"],
            assertions=[{"field": "injury_reported", "value": True}],
        )
        self.assertEqual(decision["routing"], "manual_review")
        self.assertTrue(decision["injury_reported"])

    def test_injury_is_urgent_when_nothing_forces_manual_review(self):
        decision = self.decision(
            assertions=[{"field": "injury_reported", "value": True}],
        )
        self.assertEqual(decision["routing"], "urgent")

    def test_theft_and_repair_conflict_are_specialist(self):
        self.assertEqual(self.decision("theft")["routing"], "specialist")
        referral = [{"field": "referral_indicator", "value": "repair_amount_conflict"}]
        self.assertEqual(self.decision(assertions=referral)["routing"], "specialist")

    def test_otherwise_standard(self):
        self.assertEqual(self.decision("glass")["routing"], "standard")


if __name__ == "__main__":
    unittest.main()
