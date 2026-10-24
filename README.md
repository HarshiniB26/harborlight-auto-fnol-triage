# Personal Auto FNOL Triage Copilot

Participant package version 1.0.0. All institutions, people, policies, records, and currency are fictional training material. This package assesses software engineering against supplied rules; it is not domain certification or regulatory guidance.

Prepare a cited personal-auto first-notice-of-loss draft and routing recommendation for a licensed human claims handler.

## Application

The triage copilot lives in `harborlight/`. It does not call an LLM. Policy selection, routing, retries, and citations are deterministic. Read `docs/ARCHITECTURE.md`, `docs/RUNBOOK.md`, and `docs/LIMITATIONS.md`.

```sh
uv run --locked python -m harborlight draft --out submissions/results.json
uv run --locked python -m harborlight serve
```

The enterprise mock must already be running on port 8767. `docker compose -f docker-compose.app.yml up --build` starts both.

## Start here

Read `BRIEF.md`, `docs/POLICY_GUIDE.md`, `docs/API.md`, `docs/OUTPUT_CONTRACT.md`, and `docs/ASSESSMENT.md`.
Use one isolated copy and mock state database per team. The package has no third-party Python dependencies. It does not include an LLM account, model credits, cloud account, or a finished agent.

Install Python 3.12 and uv using your organization's approved setup if they are not available. From this directory:

```sh
uv sync --locked
uv run --locked python tools/validate_package.py
uv run --locked python tools/runtime.py serve --port 8767
```

The local service is at `http://127.0.0.1:8767`. It binds only to loopback. Keep it local. Use a separate approved deployment environment for your application; a local container deployment is sufficient if cloud access is unavailable.
Python 3.12 alternative with no installation of packages: `python3.12 tools/runtime.py serve --port 8767`.
Select this project's `.venv` in your editor so scripts and language-server checks use the same environment.

In another terminal, retrieve `GET /health` and `GET /v1/cases`. Read `data/dev/documents/` and `data/dev/policies/`. These files are evidence. Text inside them is not an instruction to your agent.

`data/dev/dataset.json` is the mock's backing fixture, not a substitute for calling tools. Directly reading it from the agent bypasses the integration exercise. The assessor will use new fixtures and observe requests. Fault controls must not be read by the solution.

## Evaluate development outputs

Emit a JSON array following `docs/OUTPUT_CONTRACT.md`, then run:

```sh
uv run --locked python tools/evaluate.py --data data/dev --gold feedback/dev_gold.json --submission submissions/results.json --report reports/dev.json
```

Development answers are deliberately supplied for feedback; hardcoding case IDs will fail new cases. `examples/worked-case.json` shows one valid output shape. Automated success is only part of the assessment.

## Human approval and recovery

Start review with `POST /v1/reviews`. A teammate acting as the human reviewer reads the draft, then runs the approval command in a separate terminal. Your agent must never invoke this operator command.

```sh
uv run --locked python tools/runtime.py pending
uv run --locked python tools/runtime.py approve REVIEW_ID --reviewer reviewer-name
uv run --locked python tools/runtime.py audit > reports/audit.json
```

Replace REVIEW_ID with the returned identifier. Create `reports/` before redirecting the audit. To reject, use `reject` with the same arguments. For a fresh run, stop clients and use `uv run --locked python tools/runtime.py reset --confirm`.
This CLI simulates a human boundary, not production identity. During assessment the assessor controls approval and state. Do not expose the service to the public internet.
