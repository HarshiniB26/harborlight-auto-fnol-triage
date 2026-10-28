# FNOL Architecture

The active application uses deterministic Python rules.
No LLM calls are used.

## Active components

- workbench/server.py: Handler UI and API
- workbench/service.py: FNOL workflow
- workbench/draft.py: Draft preparation
- workbench/rules.py: Classification and routing
- workbench/reliability.py: Retries and circuit breakers
- tools/runtime.py: Mock insurance APIs and SQLite storage

## Workflow

1. Handler selects a case.
2. System prepares a deterministic FNOL draft.
3. Handler submits the draft for review.
4. Authorized reviewer approves the draft.
5. Approval automatically creates the FNOL record.

Reading a case must never create a record.

## Legacy implementation

The previous Harborlight implementation is preserved in
legacy_archive/ and is no longer part of the active application.
