
from pathlib import Path

service = Path("workbench/service.py")
ui = Path("workbench/web/app.js")
tests = Path("tests/test_workflow.py")

s = service.read_text()
j = ui.read_text()
t = tests.read_text()

old_service = '''        payload["trace_id"] = trace_id
        return payload

    def file_record(self, case_id, trace_id=None):'''

new_service = '''        payload["trace_id"] = trace_id

        # A rejection is terminal: never create a claim.
        if decision != "approved":
            return payload

        # Approval has already been persisted by the runtime.
        # Claim creation is automatic and uses file_record's
        # stable idempotency key and exact-draft validation.
        try:
            filing = self.file_record(case_id, trace_id)
        except Exception as exc:
            self.observability.event(
                "automatic_record_creation_failed",
                trace_id=trace_id,
                case_id=case_id,
                error_type=type(exc).__name__,
            )
            filing = {
                "error": "record_creation_pending",
                "message": "Approval saved; claim creation requires retry.",
            }

        payload["record_creation"] = filing
        payload["record_creation_status"] = (
            "created"
            if filing.get("status") in ("created", "replayed", "existing")
            else "pending_retry"
        )
        return payload

    def file_record(self, case_id, trace_id=None):'''

old_button = '''        ${item.status === "approved" ? `<button class="button" id="create-record">Create claim record</button>` : ""}'''

new_button = '''        ${item.status === "approved" ? `<span class="decision-note">Approved — claim creation pending retry.</span>` : ""}'''

old_handler = '''  if (item.status === "approved") panel.querySelector("#create-record").addEventListener("click", () => act(`/api/cases/${encodeURIComponent(item.case_id)}/record`));'''

old_test = '''        created = self.workbench.file_record("AUTO-D01")
        self.assertEqual(created["status"], "created")'''

new_test = '''        self.assertEqual(
            approved["record_creation_status"], "created"
        )
        self.assertEqual(
            approved["record_creation"]["status"], "created"
        )
        created = self.workbench.file_record("AUTO-D01")
        self.assertEqual(created["status"], "existing")'''

checks = [
    ("service approval return", s, old_service),
    ("UI create button", j, old_button),
    ("UI create handler", j, old_handler),
    ("workflow approval test", t, old_test),
]

for name, content, expected in checks:
    if content.count(expected) != 1:
        raise SystemExit(
            f"STOP: {name} does not match exactly once. "
            "No files changed."
        )

# Back up original files before modification.
for path in (service, ui, tests):
    backup = path.with_suffix(path.suffix + ".step4.bak")
    if backup.exists():
        raise SystemExit(
            f"STOP: Backup already exists: {backup}"
        )

for path in (service, ui, tests):
    backup = path.with_suffix(path.suffix + ".step4.bak")
    backup.write_bytes(path.read_bytes())

service.write_text(
    s.replace(old_service, new_service, 1)
)
ui.write_text(
    j.replace(old_button, new_button, 1)
     .replace(old_handler, "", 1)
)
tests.write_text(
    t.replace(old_test, new_test, 1)
)

print("Step 4 immediate automatic filing applied.")
print("Original files backed up with .step4.bak suffix.")
