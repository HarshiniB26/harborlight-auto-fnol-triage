# Runbook: hostile evidence or instruction-override attempt

## Trigger

A trace contains the handler warning `Instruction-like text in the evidence was
ignored`, or a reviewer reports text attempting to override human approval,
request payment, decide coverage, or reveal restricted values.

## Check first

1. Record the case ID and trace ID; do not copy the full document into chat or logs.
2. Confirm the draft is still marked as a draft and identify its current route.
3. Check the trace for `processing_completed` and verify no `record_created`
   event occurred without the separate human approval event.
4. Check the mock audit for unsupported or unapproved write attempts.
5. Search output and logs for `private_token` and `SYNTHETIC-PRIVATE-`.

## Confirm the cause

Open the cited source in the controlled evidence store and confirm whether the
instruction-like text came from an attachment. Compare extracted facts and
citations with recognized labels. Verify the text was not treated as authority,
did not alter routing outside supplied rules, and did not invoke a write tool.

## Immediate mitigation

- Do not approve or file the affected draft until a licensed handler reviews it.
- Keep or move the case to `manual_review` if the evidence is ambiguous.
- If any forbidden write occurred, disable record creation, preserve audit data,
  and notify the project security/client contact.
- If a restricted value leaked, stop output/log forwarding, restrict access to
  the affected artifact, rotate any real credential involved, and remove the
  unsafe artifact according to the client's incident process.
- Add the new attack wording to the adversarial regression tests without adding
  special-case business answers.

## Recovery

Run the attack and human-boundary tests, the credential scan, and all 12 case
evaluations. Restore filing only after the injection is ignored, logs are clean,
and unapproved/changed drafts are blocked.

# Runbook: operational alarms

## Completion or latency SLO

**Means:** Fewer than 95% of started case preparations completed successfully or
within 15 seconds during the 15-minute evaluation window.
**First check:** Search the application log group for `processing_failed`,
`runtime_unavailable`, `tool_call_failed`, and `circuit_opened`, then correlate
events using `trace_id`.

## Runtime unavailable or ECS task deficit

**Means:** The Workbench could not reach its Runtime container, or ECS has fewer
running tasks than the service desires.
**First check:** Open the ECS service events and stopped-task reason, then inspect
the newest `runtime` and `workbench` CloudWatch streams.

## Queue age or dead-letter queue

**Means:** A dataset update has remained unhandled for 15 minutes or exhausted
five receive attempts.
**First check:** Inspect the SQS receive count and matching `dataset_diff_*`
events. Confirm the S3 evidence exists before replaying an event.
