import unittest

from harborlight.resilience import (
    CircuitBreaker,
    UpstreamUnavailable,
    call_with_retries,
)
from harborlight.triage import route, select_policy

POLICIES = [
    {"version": "1", "effective_from": "2026-01-01T00:00:00Z", "effective_to": "2026-09-01T00:00:00Z", "wording_source": "AUTO-WORDING-V1", "endorsements": []},
    {"version": "2", "effective_from": "2026-09-01T00:00:00Z", "effective_to": "2027-01-01T00:00:00Z", "wording_source": "AUTO-WORDING-V2", "endorsements": ["AUTO-END-GLASS-01"]},
]


class PolicySelectionTest(unittest.TestCase):
    def test_inclusive_start_exclusive_end(self):
        self.assertEqual(select_policy(POLICIES, "2026-09-01T00:00:00Z")[0]["version"], "2")
        self.assertEqual(select_policy(POLICIES, "2026-08-31T23:59:59Z")[0]["version"], "1")
        self.assertEqual(select_policy(POLICIES, "2027-01-01T00:00:00Z"), [])

    def test_routing_precedence(self):
        self.assertEqual(route("collision", missing=[], conflicts=[], policy_ok=True, claims_ok=True, injury=False, non_drivable=False, referral=None), "standard")
        self.assertEqual(route("collision", missing=[], conflicts=[], policy_ok=True, claims_ok=True, injury=True, non_drivable=False, referral=None), "urgent")
        self.assertEqual(route("theft", missing=[], conflicts=[], policy_ok=True, claims_ok=True, injury=False, non_drivable=False, referral=None), "specialist")
        self.assertEqual(route("collision", missing=["driver_name"], conflicts=[], policy_ok=True, claims_ok=True, injury=True, non_drivable=False, referral=None), "manual_review")
        self.assertEqual(route("unsupported", missing=[], conflicts=[], policy_ok=True, claims_ok=True, injury=False, non_drivable=False, referral=None), "manual_review")


class RetryTest(unittest.TestCase):
    def test_retries_then_succeeds(self):
        calls = {"n": 0}

        def flaky(_attempt):
            calls["n"] += 1
            if calls["n"] < 2:
                from harborlight.resilience import TransientUpstreamError
                raise TransientUpstreamError("timeout")
            return "ok"

        result = call_with_retries(flaky, max_attempts=3, base_delay=0, breaker=CircuitBreaker(), sleep=lambda _s: None)
        self.assertEqual(result, "ok")
        self.assertEqual(calls["n"], 2)

    def test_stops_at_three(self):
        from harborlight.resilience import TransientUpstreamError

        def always(_attempt):
            raise TransientUpstreamError("timeout")

        with self.assertRaises(UpstreamUnavailable):
            call_with_retries(always, max_attempts=3, base_delay=0, breaker=CircuitBreaker(failure_threshold=99), sleep=lambda _s: None)


if __name__ == "__main__":
    unittest.main()
