"""Single-process ownership of local JSON workflow state.

Temporary protection until durable shared persistence is available.
Supported on macOS and Linux.
"""

import fcntl
import os
from pathlib import Path


def acquire_state_lock(state_path):
    """Prevent two server processes from using the same JSON state."""

    lock_path = Path(str(state_path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR,
        0o600,
    )

    handle = os.fdopen(fd, "a+")

    try:
        fcntl.flock(
            handle.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )
    except BlockingIOError:
        handle.close()
        raise RuntimeError(
            "Another FNOL server is already using this state file. "
            "Stop the other server or configure a separate state path."
        )
    except Exception:
        handle.close()
        raise

    return handle
