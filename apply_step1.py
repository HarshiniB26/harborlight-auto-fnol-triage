
from pathlib import Path

service = Path("workbench/service.py")
source = service.read_text()

target = '''                self.observability.event(
                    "evidence_retrieval_completed",'''

replacement = '''                # Do not create drafts when required APIs fail.
                unavailable = [
                    name
                    for name, value in (
                        ("policy", policy),
                        ("claims", claims),
                    )
                    if not isinstance(value, dict)
                ]
                if unavailable:
                    self.observability.event(
                        "processing_dependency_unavailable",
                        level="WARNING",
                        trace_id=trace_id,
                        case_id=case_id,
                        dependencies=unavailable,
                    )
                    return {
                        "error": "dependency_unavailable",
                        "message": (
                            "Required insurance API unavailable; "
                            "case deferred for retry."
                        ),
                        "dependencies": unavailable,
                        "trace_id": trace_id,
                    }

''' + target

if source.count(target) != 1:
    raise RuntimeError(
        "Expected processing location not found. "
        "Check your service.py version."
    )

service.write_text(source.replace(target, replacement, 1))

test = Path("tests/test_batch_prepare.py")
tests = test.read_text()

tests = tests.replace(
    "test_dependency_outage_still_submits_safe_manual_review_fallback",
    "test_dependency_outage_defers_case_without_draft_or_review",
)

old = '''        self.assertEqual(result["status"], "completed")
        row = result["cases"][0]
        self.assertEqual(row["process"]["draft"]["routing"], "manual_review")
        self.assertEqual(row["review"]["status"], "pending")'''

new = '''        self.assertEqual(result["status"], "completed_with_errors")
        self.assertEqual(result["failed_case_ids"], ["AUTO-D01"])
        row = result["cases"][0]
        self.assertEqual(row["process"]["error"], "dependency_unavailable")
        self.assertNotIn("review", row)
        self.assertNotIn("AUTO-D01", broken.state_store.read())'''

if tests.count(old) != 1:
    raise RuntimeError("Expected batch test not found.")

test.write_text(tests.replace(old, new, 1))

print("Step 1 applied successfully.")
