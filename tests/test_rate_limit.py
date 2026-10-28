import io
import unittest

from workbench.observability import JsonLogger, Metrics, Observability
from workbench.reliability import ReliableCaller


class RateLimitTests(unittest.TestCase):

    def setUp(self):
        self.observer = Observability(
            JsonLogger(io.StringIO()),
            Metrics(),
        )
        self.caller = ReliableCaller(
            self.observer,
            base_delay=0,
            jitter_ratio=0,
            sleep=lambda seconds: None,
            failure_threshold=2,
        )

    def test_429_does_not_retry(self):
        calls = []

        def rate_limited(case_id, trace_id=None):
            calls.append(case_id)
            return 429, {"error": "too_many_requests"}

        result = self.caller.call(
            "policy",
            rate_limited,
            case_id="AUTO-D01",
            trace_id="rate_limit_test",
        )

        self.assertIsNone(result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            self.caller.health()["policy"]["state"],
            "closed",
        )
        self.assertEqual(
            self.caller.health()["policy"]["failure_count"],
            0,
        )

    def test_repeated_429_does_not_open_circuit(self):
        calls = []

        def rate_limited(case_id, trace_id=None):
            calls.append(case_id)
            return 429, {}

        for _ in range(5):
            self.caller.call(
                "policy",
                rate_limited,
                case_id="AUTO-D01",
                trace_id="rate_limit_test",
            )

        self.assertEqual(len(calls), 5)
        self.assertEqual(
            self.caller.health()["policy"]["state"],
            "closed",
        )

    def test_503_still_retries(self):
        calls = []

        def unavailable(case_id, trace_id=None):
            calls.append(case_id)
            return 503, {}

        self.caller.call(
            "policy",
            unavailable,
            case_id="AUTO-D01",
            trace_id="unavailable_test",
        )

        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
