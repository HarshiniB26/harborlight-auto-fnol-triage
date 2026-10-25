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
