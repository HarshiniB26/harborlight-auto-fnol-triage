# FNOL hardening specification and evidence

## SLO and error budget

Over a rolling 30-day period, at least 95% of FNOL processing requests will
produce either a cited triage draft or an explicit manual-review fallback
within 15 seconds. Time awaiting a human review decision is excluded because
it is outside the service's control.

The service-level indicators are the completed-request rate and the proportion
of requests completed within 15 seconds. A 95% objective leaves a 5% error
budget: no more than five of every 100 requests may fail or exceed 15 seconds.

If bad requests reach 3% of total traffic, 60% of the available error budget
has been consumed. Investigate dependency errors and latency and pause
nonessential releases. If bad requests reach 5%, the full error budget has
been consumed: freeze feature releases, route affected work to human review,
and restore reliability before resuming delivery.

## Failure-case mapping

| Case | Failure | Control and expected outcome |
|---|---|---|
| AUTO-D11 | Claims API fails once | Three-attempt bound; exponential backoff with jitter; second attempt succeeds |
| AUTO-D07 | Claims API remains unavailable | Bounded retries; breaker opens after repeated exhausted operations; manual_review fallback |
| Repeated record POST | Identical request is retried | Stable idempotency key replays the original response; exactly one record |
| AUTO-D09/D10 | Different intakes describe one logical loss | Logical-loss uniqueness returns the existing record; exactly one record |
| AUTO-D08 | Instruction embedded in estimate | Document is untrusted data; allowlisted fact extraction; command ignored |
| All policy reads | Restricted private_token | Removed immediately and prohibited from drafts, citations, state, and logs |
| AUTO-D04 | Required facts missing | Targeted questions and manual_review |
| AUTO-D05 | Multiple policy intervals match | Preserve null version and route manual_review; never choose arbitrarily |
| AUTO-D06 | No policy interval matches | Preserve null version and route manual_review; never deny |
| Record write | No approval or changed draft | Application refuses the write before calling the record API |

## Reliability design

`workbench/reliability.py` implements three total attempts with exponential
backoff, bounded jitter, and closed/open/half-open circuit states. Only HTTP
503/transport-unavailable reads are retried. A complete operation that exhausts
all three attempts counts as one breaker failure; two such operations open the
circuit for 30 seconds. Calls during the cooldown fail fast. A successful
half-open probe closes the circuit.

The claims and policy reads are safe to retry. Review submission is not retried
because the supplied review API has no idempotency key. Record creation uses a
stable idempotency key and the runtime stores the prior response.

## Security controls

- Documents are evidence, never executable instructions.
- Extraction accepts only explicitly recognized `Label: value` fields.
- The D08 instruction is skipped and produces a handler-visible warning.
- There is no LLM or prompt in this implementation, removing model rate-limit,
  model-deprecation, model-cost, and hallucination-chain risks.
- `private_token` is recursively removed immediately after a policy tool read.
- Safe logging records metadata and outcomes, never tool payloads or documents.
- An exact draft hash binds a human approval to the only draft that may be filed.
- Processing exposes no autonomous approval path. A decision is accepted only
  after a person explicitly reviews the pending draft in the private UI, enters
  their name, and approves or rejects that exact draft hash.
- `python tools/check_secrets.py` scans for likely live credentials.
- SQL values use bound parameters (`?` / `%s`); identifiers are static statements or an allowlisted role name (`fnol_app`), never concatenated from request input.
- The Postgres application login is dedicated least-privilege DML on the `fnol` schema only.

## Observability

Each HTTP action receives a `tr_...` trace ID. The same ID is propagated through
tool attempts and the deterministic processing result. Logs are JSON lines with
events such as `tool_call_started`, `tool_call_retry_scheduled`,
`circuit_opened`, `processing_completed`, and `duplicate_prevented`.

The `/metrics` endpoint exposes:

- `fnol_processing_total`
- `fnol_processing_completed_total`
- `fnol_processing_within_15s_total`
- `fnol_processing_error_total`
- `fnol_tool_call_total`
- `fnol_tool_retry_total`
- `fnol_tool_failure_total`
- `fnol_circuit_open_total`
- `fnol_circuit_rejected_total`
- `fnol_duplicate_prevented_total`
- completion rate, within-15-second rate, and p95 processing duration

The production CloudWatch alert is: page a human when the completion rate or
within-15-second rate remains below 95% for 15 consecutive minutes. Alert on
this user-visible symptom, not on one slow request or an arbitrary CPU value.

Local metrics and breaker state are process-local and reset on restart. The AWS
deployment will publish metrics and logs to CloudWatch for durable aggregation.

## Required demonstrations

| Scenario | Case | Expected evidence |
|---|---|---|
| Normal | AUTO-D01 | collision, standard, cited policy version 2 |
| Ambiguous | AUTO-D06 | no matching policy interval, null version, manual_review |
| Attack | AUTO-D08 | injected command shown, ignored warning, safe standard draft |
| Transient failure | AUTO-D11 | retry event followed by success |
| Persistent failure | AUTO-D07 | three attempts, manual_review, breaker opens and later fails fast |
| Idempotent replay | AUTO-D02 record POST repeated with the same key and payload | first request creates; retry returns HTTP 200 with the same record ID; one database row |
| Duplicate logical loss | AUTO-D09/D10 | first intake creates; different intake for the same loss resolves to the existing record |

Run:

```sh
python tools/validate_package.py
python -m unittest discover -s tests -v
python tools/check_secrets.py
python tools/hardening_demo.py
```
