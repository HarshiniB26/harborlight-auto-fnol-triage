# Engineering constraints

These constraints are release gates for this repository. They may only be
changed through an explicit team decision; implementation work must not weaken
them to make a check pass.

## Test coverage

- Every statement and every branch in `workbench/` and `tools/` must be tested.
- Required combined line-and-branch coverage: **100%**.
- Do not use coverage omissions, exclusion pragmas, skipped tests, or reduced
  thresholds to satisfy this gate.
- Run the gate from the repository root with:

  ```bash
  uv run coverage erase
  uv run coverage run -m unittest discover -s tests
  uv run coverage report
  ```

The configuration in `pyproject.toml` enables branch measurement, fixes the
measured source packages, and fails the command when coverage is below 100%.
