"""Persist the workbench draft document in PostgreSQL.

The document has the same shape as ``.state/workbench.json``. The file remains
the store when no database URL is configured.
"""

import json

from tools.state_store import canonical


def read_document(database_url):
    """Return the saved document, or an empty object when no row exists."""

    from tools.state_store import PostgresStore

    store = PostgresStore(database_url)
    with store.transaction() as tx:
        row = tx.fetchone("SELECT document FROM workbench_state WHERE id=?", (1,))
    if not row or not row.get("document"):
        return {}
    stored = json.loads(row["document"])
    if not isinstance(stored, dict):
        return {}
    return stored


def write_document(database_url, state):
    """Replace the single workbench document in one transaction."""

    from tools.state_store import PostgresStore

    store = PostgresStore(database_url)
    payload = json.dumps(state, indent=2)
    with store.transaction() as tx:
        tx.execute(
            """
            INSERT INTO workbench_state(id, document) VALUES(?, ?)
            ON CONFLICT(id) DO UPDATE SET document = EXCLUDED.document
            """,
            (1, payload),
        )
    return canonical(state)
