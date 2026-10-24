# Limitations

- Dates and rules are the fictional 2026 procedure. This is not an insurance determination.
- Documents must already be text files named by source id. There is no OCR and no external retrieval service.
- Hidden assessment cases work only when they follow the same tool paths, file layout, and procedure. New loss labels outside collision, theft, and glass become `unsupported` and `manual_review`.
- The circuit breaker is per case and tool because the mock's faults are per case. A real provider outage should use one breaker for that provider.
- Retry delay defaults to a short base so local evaluation stays responsive. Set `HARBORLIGHT_RETRY_BASE_SECONDS=1` for a production-shaped backoff.
- There is no production identity. The mock CLI is the human boundary.
- No final claim decision is in scope, and none is produced.
- An LLM was considered and rejected for this system. It would not improve policy selection, and it would weaken quote grounding and prompt-injection resistance.
