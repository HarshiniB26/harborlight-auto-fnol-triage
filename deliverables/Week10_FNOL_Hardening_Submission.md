# Week 10 FNOL hardening submission

The submission PDF is intentionally plain. Its evidence pages contain the exact
commands executed while building the document and their captured stdout/stderr.

- PDF: `Week10_FNOL_Hardening_Submission.pdf`
- Page count: 25
- Evidence date: 2026-10-26 UTC

## Evidence index

- Page 3: 1. SLO and error budget - Service objective and error budget
- Page 4: 2. Failure-case mapping - Deliberate failures mapped to controls
- Page 5: 3. Retry, backoff, and circuit breaker - Retry loop and exponential backoff
- Page 6: 3. Retry, backoff, and circuit breaker - Retry limits and calculated delay
- Page 7: 3. Retry, backoff, and circuit breaker - Circuit state transitions and fail-fast behavior
- Page 8: 3. Retry, backoff, and circuit breaker - Transient dependency failure: actual recovery run
- Page 9: 3. Retry, backoff, and circuit breaker - Persistent dependency failure: actual fallback run
- Page 10: 4. Idempotency - Stable idempotency key generation
- Page 11: 4. Idempotency - Request header and runtime replay behavior
- Page 12: 4. Idempotency - Identical record request: actual replay run
- Page 13: 4. Logical-loss duplicate prevention - Different intakes for one loss: actual test run
- Page 14: 5. Prompt-injection handling - Injected document text and actual safe result
- Page 15: 5. Prompt-injection handling - Untrusted-text isolation in code
- Page 16: 6. Least privilege and human boundary - Least-privilege audit
- Page 17: 6. Least privilege and human boundary - Human approval enforced in code and test
- Page 18: 7. Restricted values and credential scan - Private-token removal and repository scan
- Page 19: 8. Logging and tracing - Structured JSON logging implementation
- Page 20: 8. Logging and tracing - Actual multi-step trace
- Page 21: 9. Metrics and alert threshold - SLO metrics implementation and live snapshot
- Page 22: 10. Runbook - Runbook: trigger and diagnosis
- Page 23: 10. Runbook - Runbook: mitigation and recovery
- Page 24: 11. Required scenarios - Normal, ambiguous, and attack: raw test events
- Page 25: Final verification - Package, tests, coverage, gold evaluation, and credential scan

## Final verification commands

```sh
uv run --locked python tools/validate_package.py
uv run --with pytest --with pytest-cov python -m pytest tests/ --cov=workbench --cov-report=term-missing --cov-fail-under=98
python tools/check_secrets.py
```
