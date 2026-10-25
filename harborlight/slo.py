"""Draft SLO for the FNOL copilot.

99% of draft requests finish, including a fail-closed manual_review route, within
2 seconds. One draft in a hundred may miss that target. That 1% is the error
budget. A claims timeout that still returns manual_review inside 2 seconds has
met the SLO. A hang, a crash, or a draft slower than 2 seconds spends budget.

If the in-process ratio drafts_within_slo_total / drafts_total falls below 99%,
stop workflow changes and fix the dependency path. Page a human when that ratio
falls below 95%. A single retry is not an alert.
"""

SLO_NAME = "fnol_draft_latency"
TARGET_RATIO = 0.99
LATENCY_MS = 2000
ERROR_BUDGET_RATIO = 1 - TARGET_RATIO
ALERT_RATIO = 0.95
ALERT_WINDOW = "rolling session of draft requests; page when the ratio stays under 95%"


def within_target(duration_ms: float) -> bool:
    return duration_ms <= LATENCY_MS


def budget_action(ratio: float) -> str:
    if ratio < ALERT_RATIO:
        return "page_human"
    if ratio < TARGET_RATIO:
        return "stop_risky_changes"
    return "budget_available"
