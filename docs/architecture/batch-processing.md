# Batch processing and dataset-diff architecture

Handoff document for operators and future agents merging **ECS / Fargate** into
the pilot stack. Companion to [`system-architecture.md`](system-architecture.md).

## Purpose

- **On-demand:** prepare many FNOL cases into cited drafts and submit each for
  human review (`prepare_queue`).
- **AWS incremental:** when `dataset.json` is uploaded to the evidence bucket,
  compute a case-ID diff and prepare **only added** IDs.

## Non-goals

- No batch mock `/v1` endpoints (still one review / record per case).
- No dedicated always-on Fargate “diff worker” service (cost).
- No automatic approve/file (human boundary unchanged).
- No content-hash detection for same-ID fixture edits (ID diff only).
- No local filesystem watcher for `data/dev/dataset.json`.

## Flows

### On-demand (local and deployed)

1. `GET /v1/cases` via workbench `list_cases` (or explicit `case_ids`).
2. For each ID: `process(..., require_dependencies=True)` then `submit_review`.
3. Return per-case summary + job `trace_id`.

CLI: `tools/batch_prepare.py` → `POST /api/queue/prepare`.

Local does **not** maintain a `last_seen` cursor.

### AWS dataset diff (S3 → SQS → co-located poller)

```text
PUT s3://evidence/dataset.json
  → S3 ObjectCreated:* (suffix dataset.json)
  → SQS DatasetDiffQueue
  → workbench DatasetDiffPoller (daemon thread)
  → list_cases → apply_dataset_diff
  → prepare_queue(added IDs only)
  → update Postgres last_seen cursor
  → DeleteMessage (when ack)
```

Poller starts only when `DATASET_DIFF_QUEUE_URL` is set (see `workbench/server.py`).

## Key symbols

| Symbol | Location | Role |
|---|---|---|
| `Workbench.prepare_queue` | `workbench/service.py` | Batch process + submit |
| `Workbench.process(..., require_dependencies=)` | `workbench/service.py` | UI: soft fallback; batch: no draft if tools down |
| `Workbench.apply_dataset_diff` | `workbench/service.py` | AWS ID diff + prepare added |
| `QUEUE_META_KEY` / `fnol-queue-meta` | `workbench/storage.py` | Postgres cursor row |
| `DatasetDiffPoller` | `workbench/dataset_diff_poller.py` | SQS long-poll loop |
| `POST /api/queue/prepare` | `workbench/server.py` | HTTP entry for on-demand batch |
| `tools/batch_prepare.py` | `tools/` | CLI client |

## Cursor (`last_seen_case_ids`)

- **AWS / Postgres only** (`supports_queue_cursor` when state store is
  `PostgresStateStore`).
- Stored as `fnol.workbench_state` row `case_id = fnol-queue-meta`,
  `entry.last_seen_case_ids`.
- Advance only for **handled** IDs (successful process+submit or `draft_locked`).
- Never advance for `dependency_unavailable` or failed submit (so a later
  `dataset.json` upload re-selects them as added).
- Removals: drop from cursor; `PostgresStateStore.delete_cases` cleans UI rows.

## Failure behavior (batch)

| Case | Behavior |
|---|---|
| Transient 503 | Existing `ReliableCaller` retries |
| Exhausted / circuit open (batch) | No draft, no review; ID not in `last_seen` |
| UI single `process()` | Unchanged manual_review fallback (AUTO-D07) |
| SQS: cannot list cases / all adds fail | Do not delete message (visibility retry) |
| SQS: partial success | Ack OK if failed IDs omitted from `last_seen` |

## Observability

Same JSON logs / `X-Trace-ID` / `tr_…` conventions.

Batch events: `queue_prepare_started`, `queue_prepare_completed`,
`dataset_diff_started`, `dataset_diff_completed`,
`processing_deferred_dependency`, plus existing per-case process/review events
under the job `trace_id`.

## Environment

| Variable | Purpose |
|---|---|
| `DATASET_DIFF_QUEUE_URL` | Enable co-located SQS poller |
| `RUNTIME_API_URL` | Mock `/v1` base URL |
| `STORAGE_BACKEND=aws` | Postgres + S3 evidence |
| `EVIDENCE_BUCKET` / `EVIDENCE_PREFIX` | Evidence objects |
| `DB_SECRET_ARN` / `DATABASE_URL` | Postgres |

## CloudFormation resources (phase 1)

Added in `deployment/cloudformation.yaml`:

- `DatasetDiffQueue` + `DatasetDiffDeadLetterQueue` (maxReceiveCount 5)
- `DatasetDiffQueuePolicy` (S3 `SendMessage` from evidence bucket)
- `EvidenceBucket` `NotificationConfiguration` → queue, suffix `dataset.json`
- Instance role SQS receive/delete/get attributes
- Outputs: `DatasetDiffQueueUrl`, `DatasetDiffQueueArn`, `DatasetDiffDeadLetterQueueUrl`

`deployment/deploy.sh` passes `DATASET_DIFF_QUEUE_URL` into the workbench
container.

**Do not duplicate** these queues/notifications when merging ECS — reuse them.

## ECS merge checklist (for a future agent)

1. Keep SQS + S3 notification resources; map outputs into the ECS stack.
2. Set `DATASET_DIFF_QUEUE_URL` on the **workbench** task definition only.
3. Grant the **task role** (not a second service role) `sqs:ReceiveMessage`,
   `DeleteMessage`, `GetQueueAttributes` on the diff queue (+ existing S3/DB).
4. **Do not** add a dedicated Fargate diff-worker service.
5. Prefer workbench `desiredCount: 1` while the poller is co-located (multiple
   tasks ⇒ multiple pollers; `draft_locked` helps but cursor races remain).
6. If autoscaling min is `0`, the poller is off while scaled to zero — messages
   wait in SQS until a task starts (or use on-demand `batch_prepare`).
7. Point operators at this doc and `system-architecture.md`.

## Locking note

`process()` holds the workbench lock across tool I/O. A full-queue prepare can
briefly block other mutating UI actions; acceptable at pilot scale (~12 cases).
