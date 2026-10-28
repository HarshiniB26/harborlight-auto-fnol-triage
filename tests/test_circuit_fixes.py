import io
import unittest

from workbench.observability import JsonLogger, Metrics, Observability
from workbench.reliability import ReliableCaller


class Clock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        return self.value

    def sleep(self, amount):
        self.value += amount


class CircuitFixTests(unittest.TestCase):

    def setUp(self):
        self.clock = Clock()
        self.caller = ReliableCaller(
            Observability(JsonLogger(io.StringIO()), Metrics()),
            failure_threshold=2,
            reset_timeout=10,
            base_delay=0,
            jitter_ratio=0,
            sleep=self.clock.sleep,
            clock=self.clock,
        )

    def call(self, operation):
        return self.caller.call(
            "policy",
            operation,
            case_id="AUTO-D01",
            trace_id="test",
        )

    def open_circuit(self):
        for _ in range(2):
            self.call(lambda *_args, **_kw: (503, {}))

        self.assertEqual(
            self.caller.health()["policy"]["state"], "open"
        )

    def test_half_open_non_retryable_failure_reopens(self):
        self.open_circuit()
        self.clock.value += 10
        calls = []

        def fail(*_args, **_kwargs):
            calls.append(1)
            return 400, {}

        self.assertIsNone(self.call(fail))
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            self.caller.health()["policy"]["state"], "open"
        )

        self.assertIsNone(self.call(fail))
        self.assertEqual(len(calls), 1)

    def test_half_open_503_uses_one_attempt(self):
        self.open_circuit()
        self.clock.value += 10
        calls = []

        def fail(*_args, **_kwargs):
            calls.append(1)
            return 503, {}

        self.assertIsNone(self.call(fail))
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            self.caller.health()["policy"]["state"], "open"
        )

    def test_temporary_http_errors_retry(self):
        for status in (503, 502, 504):
            calls = []

            def operation(*_args, **_kwargs):
                calls.append(1)
                if len(calls) == 1:
                    return status, {}
                return 200, {"ok": True}

            self.assertEqual(
                self.call(operation), {"ok": True}
            )
            self.assertEqual(len(calls), 2)

    def test_timeout_retries(self):
        calls = []

        def operation(*_args, **_kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError("connection timeout")
            return 200, {"ok": True}

        self.assertEqual(
            self.call(operation), {"ok": True}
        )
        self.assertEqual(len(calls), 2)

    def test_successful_probe_closes_circuit(self):
        self.open_circuit()
        self.clock.value += 10

        result = self.call(
            lambda *_args, **_kw: (200, {"ok": True})
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            self.caller.health()["policy"]["state"], "closed"
        )


if __name__ == "__main__":
    unittest.main()
