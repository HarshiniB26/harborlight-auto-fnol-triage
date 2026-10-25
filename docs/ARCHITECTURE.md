# Architecture

Harborlight FNOL triage is a deterministic workflow. An LLM is not used.

The supplied procedure names the loss types, the policy-interval rule, the routing precedence, the retry limit, and the human boundary. Those are checks, not generation. A model would be free to paraphrase a quote, follow an instruction hidden in an estimate, or write a coverage conclusion. The evaluator rejects all three. Model calls also add a dependency the exercise does not provision.

## Flow

1. `GET /v1/cases` on the enterprise mock returns case ids and document source ids. The workflow does not read `dataset.json` and does not read fault configuration.
2. Document and policy files under the data directory are evidence. Instruction-like text is recorded as ignored and is never treated as an operator command.
3. `GET /v1/cases/{id}/policy` and `GET /v1/cases/{id}/claims` run with at most three attempts, exponential backoff, and jitter. A per-case circuit opens after repeated failure so a later step does not wait on a dependency already known to be down for that case.
4. Policy version is the single interval that contains the incident timestamp, inclusive at the start and exclusive at the end. Zero or several matches leave `selected_policy_version` null and cite only `AUTO-TRIAGE-01`.
5. Routing follows the procedure: manual review, then urgent, then specialist, then standard. Injury and non-drivable flags stay in the narrative when manual review wins.
6. The draft is the evaluation contract plus an `indicators` object for the handler. No secret field is copied.
7. `POST /v1/reviews` submits that exact draft. Approval is a separate operator command (`tools/runtime.py approve`). This process never approves.
8. `POST /v1/records` sends the same draft with an idempotency key. A 409 `duplicate_case` is an existing record, not a new claim.

## SLO

Defined in `harborlight/slo.py`. 99% of draft requests finish within 2 seconds, including a fail-closed `manual_review` when claims or policy cannot be read. The error budget is the other 1%. A timeout that still returns a route inside 2 seconds has met the target. A hang, a crash, or a draft slower than 2 seconds spends budget. Below 99%, stop workflow changes. Below 95%, page a human. Counters: `drafts_within_slo_total`, `drafts_over_slo_total`, and `draft_success_within_slo_ratio`.

## What is intentionally not an agent

Retrieval is by source id from the case payload and the policy version selected by timestamp. The next step depends on those tool results: no unique policy, a failed claims read, or a duplicate ledger row changes routing and whether creation is allowed. That is the required tool-assisted branch, implemented in code so the branch is testable.
