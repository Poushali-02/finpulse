"""Deciding an action. Only the command line (cli.py) imports this module; the agent never does.

Approving a reminder email "sends" it by writing one .eml file into outbox/. There is no SMTP
code anywhere. Approving any other kind of action only records the decision.
"""

from __future__ import annotations

import email
import getpass
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

SENDER = "Lion City Supplies Accounts Receivable <accounts.receivable@example.com>"


class ApprovalError(Exception):
    """The action does not exist or has already been decided."""


def eml_path(outbox_dir: Path, action_id: int) -> Path:
    return outbox_dir / f"action-{action_id:04d}.eml"


def build_eml(draft_text: str, as_of: date) -> bytes:
    draft = email.message_from_string(draft_text)
    message = EmailMessage()
    message["From"] = SENDER
    message["To"] = draft["To"]
    message["Subject"] = draft["Subject"]
    message["Date"] = format_datetime(datetime.combine(as_of, time(9, 30), tzinfo=timezone(timedelta(hours=8))))
    message["X-FinPulse"] = "made-up demo data; written to a file, never sent"
    message.set_content(draft.get_payload())
    return message.as_bytes()


def decide(conn, as_of: date, action_id: int, decision: str, outbox_dir: Path, decided_by: str | None = None) -> dict:
    """Approve or reject one pending action. Returns {"id", "status", "file"}."""
    if decision not in ("approved", "rejected"):
        raise ValueError(f"unknown decision {decision!r}")
    row = conn.execute(
        "SELECT action_type, status, draft_text FROM actions WHERE id = %s FOR UPDATE", (action_id,)
    ).fetchone()
    if row is None:
        raise ApprovalError(f"No action #{action_id}.")
    action_type, status, draft_text = row
    if status != "pending":
        raise ApprovalError(f"Action #{action_id} is already {status}.")

    conn.execute(
        "UPDATE actions SET status = %s, decided_at = %s, decided_by = %s WHERE id = %s AND status = 'pending'",
        (decision, datetime.combine(as_of, time(9, 30), tzinfo=timezone.utc), decided_by or getpass.getuser(), action_id),
    )
    path = None
    try:
        if decision == "approved" and action_type.startswith("reminder_email"):
            outbox_dir.mkdir(parents=True, exist_ok=True)
            path = eml_path(outbox_dir, action_id)
            with open(path, "xb") as f:  # "x": refuses to overwrite, so at most one file per action
                f.write(build_eml(draft_text, as_of))
        conn.commit()
    except BaseException:
        conn.rollback()
        if path is not None and path.exists():
            path.unlink()
        raise
    return {"id": action_id, "status": decision, "action_type": action_type, "file": path}
