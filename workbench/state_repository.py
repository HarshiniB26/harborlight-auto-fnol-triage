"""Persistence boundary for FNOL workflow state.

The JSON implementation is temporary. A PostgreSQL implementation
can replace it without changing the workflow orchestration.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Protocol


WorkflowState = dict[str, dict[str, Any]]


class StateRepository(Protocol):
    """Interface for loading and saving workflow state."""

    def load(self) -> WorkflowState:
        ...

    def save(self, state: WorkflowState) -> None:
        ...


class JsonStateRepository:
    """Single-process JSON implementation for local development."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> WorkflowState:
        if not self.path.is_file():
            return {}

        with self.path.open("r", encoding="utf-8") as handle:
            stored = json.load(handle)

        if not isinstance(stored, dict):
            return {}

        # Preserve compatibility with the current flat state format.
        # Ignore obsolete entries under the "cases" key.
        return {
            case_id: entry
            for case_id, entry in stored.items()
            if case_id != "cases"
            and isinstance(entry, dict)
            and isinstance(entry.get("draft"), dict)
        }

    def save(self, state: WorkflowState) -> None:
        """Replace the state file atomically.

        The temporary file is created in the same directory so
        os.replace can perform an atomic rename.
        """

        self.path.parent.mkdir(parents=True, exist_ok=True)

        temporary_path = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=self.path.name + ".",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                os.chmod(temporary_path, 0o600)

                json.dump(state, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())

            os.replace(temporary_path, self.path)
            temporary_path = None

        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
