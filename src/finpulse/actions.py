"""Writing and reading the `actions` table.

The month-end agent imports only `create_pending`. The INSERT below hard-codes the status
'pending', so there is no way to create an approved or rejected action from here.
Deciding an action lives in approvals.py, which only the command line imports.
"""

from __future__ import annotations

from datetime import datetime

from psycopg.rows import dict_row


def create_pending(
    conn, action_type: str, target_type: str, target_id: int, draft_text: str, created_at: datetime
) -> tuple[int, bool]:
    """Insert a pending action unless the same one is already pending or approved.

    Returns (action_id, created). Re-running the month-end check therefore never piles up copies.
    """
    existing = conn.execute(
        """
        SELECT id FROM actions
        WHERE action_type = %s AND target_type = %s AND target_id = %s
          AND status IN ('pending', 'approved')
        ORDER BY id LIMIT 1
        """,
        (action_type, target_type, target_id),
    ).fetchone()
    if existing:
        return existing[0], False
    new_id = conn.execute(
        """
        INSERT INTO actions (action_type, target_type, target_id, draft_text, status, created_at)
        VALUES (%s, %s, %s, %s, 'pending', %s)
        RETURNING id
        """,
        (action_type, target_type, target_id, draft_text, created_at),
    ).fetchone()[0]
    return new_id, True


def list_actions(conn, status: str | None = None) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT a.id, a.action_type, a.target_type, a.target_id, a.status, a.draft_text,
                   a.created_at, a.decided_at, a.decided_by,
                   COALESCE(i.invoice_no, b.bill_no, c.name) AS target
            FROM actions a
            LEFT JOIN invoices  i ON a.target_type = 'invoice'  AND i.id = a.target_id
            LEFT JOIN bills     b ON a.target_type = 'bill'     AND b.id = a.target_id
            LEFT JOIN customers c ON a.target_type = 'customer' AND c.id = a.target_id
            WHERE (%(status)s::text IS NULL OR a.status = %(status)s::text)
            ORDER BY a.id
            """,
            {"status": status},
        )
        return cur.fetchall()
