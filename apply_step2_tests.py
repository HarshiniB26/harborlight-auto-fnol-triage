
from pathlib import Path

hardening = Path("tests/test_hardening.py")
workflow = Path("tests/test_workflow.py")

h = hardening.read_text()
w = workflow.read_text()

# 1. Update persistent API outage expectations.
old_h = '''        self.assertEqual(first["draft"]["routing"], "manual_review")
        self.assertEqual(second["draft"]["routing"], "manual_review")
        self.assertEqual(third["draft"]["routing"], "manual_review")'''

new_h = '''        for result in (first, second, third):
            self.assertEqual(
                result["error"],
                "dependency_unavailable",
            )
            self.assertNotIn("draft", result)

        # Technical outages must not create incomplete drafts.
        self.assertNotIn(
            "AUTO-D07",
            self.workbench.state_store.read(),
        )'''

# 2. Update workflow evaluation to handle expected dependency failures.
old_w = '''        drafts = [self.workbench.process(case["case_id"])["draft"] for case in cases]
        dataset = json.loads((ROOT / "data/dev/dataset.json").read_text())'''

new_w = '''        results = {
            case["case_id"]: self.workbench.process(case["case_id"])
            for case in cases
        }

        # D07 simulates a persistent dependency outage.
        self.assertEqual(
            results["AUTO-D07"]["error"],
            "dependency_unavailable",
        )
        self.assertNotIn("draft", results["AUTO-D07"])

        drafts = [
            result["draft"]
            for result in results.values()
            if "draft" in result
        ]

        dataset = json.loads((ROOT / "data/dev/dataset.json").read_text())'''

# 3. The evaluator expects all development cases.
# Evaluate only successfully processed cases and their matching gold entries.
old_eval = '''        report = evaluate(dataset, gold, drafts)'''

new_eval = '''        successful_ids = {draft["case_id"] for draft in drafts}

        filtered_dataset = dict(dataset)
        filtered_dataset["cases"] = [
            case for case in dataset["cases"]
            if case["case_id"] in successful_ids
        ]

        filtered_gold = {
            case_id: expected
            for case_id, expected in gold.items()
            if case_id in successful_ids
        } if isinstance(gold, dict) else [
            item for item in gold
            if item["case_id"] in successful_ids
        ]

        report = evaluate(
            filtered_dataset,
            filtered_gold,
            drafts,
        )'''

changes = [
    ("hardening outage assertions", h, old_h),
    ("workflow processing", w, old_w),
    ("workflow evaluation", w, old_eval),
]

for name, source, target in changes:
    if source.count(target) != 1:
        raise RuntimeError(
            f"STOP: Expected {name} block not found exactly once. "
            "No files changed."
        )

h = h.replace(old_h, new_h, 1)
w = w.replace(old_w, new_w, 1)
w = w.replace(old_eval, new_eval, 1)

hardening.write_text(h)
workflow.write_text(w)

print("Updated both tests for Step 1 dependency handling.")
