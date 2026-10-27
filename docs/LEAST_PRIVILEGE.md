# Least-privilege and human-boundary audit

| Component | Required access | Explicitly excluded |
|---|---|---|
| Browser UI | Workbench `/api` only | Runtime, state database, approval CLI |
| Workbench server | List/read cases; submit review; create exact approved record | Human approve/reject, claim updates, adjudication, arbitrary files |
| Deterministic rules | Already-retrieved evidence values | Network, filesystem, database, tools |
| Human operator CLI | List and approve/reject pending reviews | Draft generation and autonomous record creation |
| Supplied runtime | Supplied fixture and mock state | Public internet and unrelated systems |

The workbench has no approval method. Before record creation it checks that a
review is approved, belongs to the case, contains a readable draft, has the
expected hash, and exactly equals the current draft. A changed or unapproved
draft is rejected before the record endpoint is called. The record call uses a
stable idempotency key and cannot update an existing claim.

The rules never make coverage, liability, fraud, reserve, settlement, denial,
payment, or customer-commitment decisions. A repair discrepancy is only a
referral indicator. A policy mismatch remains uncertain and goes to a person.

For AWS, the EC2 task/instance role will be limited to reading the FNOL evidence
bucket, retrieving the named database secret, and writing application telemetry.
It will not receive account-administrator, IAM-administration, or unrestricted
S3 permissions. PostgreSQL will use a dedicated application role restricted to
the FNOL schema. Human sign-in and the application role remain separate.

Credential verification:

```sh
python tools/check_secrets.py
```

The supplied `SYNTHETIC-PRIVATE-*` values are deliberate restricted training
fixtures, not credentials. They remain in supplied fixtures but must never be
present in application output, persisted workbench state, or logs.

The AWS controls that implement this boundary are described in
[security.md](security.md). IAM lets the instance read the dataset object and
the database secrets. It does not grant handler permissions. Handlers still
cannot approve a review from the workbench.
