# Least-privilege and human-boundary audit

| Component | Required access | Explicitly excluded |
|---|---|---|
| Browser UI | Workbench `/api` only, including an explicit human review action | Runtime, state database, approval CLI |
| Workbench server | List/read cases; submit named human decision; create exact approved record | Autonomous approval, claim updates, adjudication, arbitrary files |
| Deterministic rules | Already-retrieved evidence values | Network, filesystem, database, tools |
| Human operator CLI | Administrative fallback to list and decide pending reviews | Draft generation and autonomous record creation |
| Supplied runtime | Supplied fixture and mock state | Public internet and unrelated systems |

The workbench exposes a review-decision method only for an explicit UI request;
case processing never calls it. It accepts a named person, a pending review,
and `approved` or `rejected`, after confirming the pending hash matches the
current draft. Before record creation it again checks that the review belongs
to the case, contains a readable draft, has the expected hash, and exactly
equals the current draft. A changed or unapproved draft is rejected before the
record endpoint is called. The record call uses a stable idempotency key and
cannot update an existing claim.

The rules never make coverage, liability, fraud, reserve, settlement, denial,
payment, or customer-commitment decisions. A repair discrepancy is only a
referral indicator. A policy mismatch remains uncertain and goes to a person.

For AWS, the Fargate application task role is limited to reading the FNOL
evidence bucket, retrieving the named application database secret, consuming
the named dataset-event queue, and opening ECS Exec channels. The separate
migration task can retrieve only the two named database secrets. Neither role
receives account-administrator, IAM-administration, or unrestricted S3
permissions. PostgreSQL uses a dedicated application role restricted to the
FNOL schema. Human IAM access and the application role remain separate.

The application database login is allowlisted to `fnol_app` only and receives
`USAGE` on schema `fnol` plus `SELECT`/`INSERT`/`UPDATE`/`DELETE` on that
schema's tables and sequences—no DDL and no cross-schema grants. Mutable SQL
in the runtimes binds values with parameters; local mock reset uses static
`DELETE` statements for a fixed table list rather than interpolating names.

Credential verification:

```sh
python tools/check_secrets.py
```

The supplied `SYNTHETIC-PRIVATE-*` values are deliberate restricted training
fixtures, not credentials. They remain in supplied fixtures but must never be
present in application output, persisted workbench state, or logs.
