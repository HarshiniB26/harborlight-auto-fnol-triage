# Package architecture and reproducibility

Version 1.0.0; scenario dates fixed in 2026. Synthetic data is authored deterministically; no random generation or model sampling was used. No real customer data. Expected deadlines and boundary cases are hand specified; the private baseline calculates dates independently.
Python 3.12 with standard library only; uv project and lockfile included. REST was selected for ease of inspection; adding MCP is optional and must not change semantics.
State uses SQLite transactions, approval bound to the draft hash, request idempotency and logical-record uniqueness. Local operator commands simulate human decisions. Tool timeout simulation is an immediate 503, not a sleeping connection.
Source documents, mock fixtures, labels and baseline are versioned together. Do not edit fixtures during an assessment. Make any approved correction as a new version and publish it consistently.
