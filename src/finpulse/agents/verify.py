"""The double-check: every number in AI-written text must match the database.

Pure Python. A draft that fails is discarded by the graph and the reason is logged.
"""

from __future__ import annotations

import re
import string
from decimal import Decimal, InvalidOperation

from finpulse.agents.prompts import ALLOWED_FIELDS

DOC_NO = re.compile(r"\b(?:SI|INV)-?\d+\b", re.IGNORECASE)
AMOUNT = re.compile(r"(?:SGD|S\$|\$)\s?(\d[\d,]*(?:\.\d+)?)", re.IGNORECASE)
DAYS = re.compile(r"\b(\d+)\s+days?\s+(?:late|overdue|past due)\b", re.IGNORECASE)


def render_template(template: str, values: dict[str, str]) -> str:
    """Fill {placeholders}. Raises ValueError for any placeholder that is not allowed."""
    for _, field, _, _ in string.Formatter().parse(template):
        if field is not None and field not in ALLOWED_FIELDS:
            raise ValueError(f"unknown placeholder {{{field}}}")
    return template.format_map(values)


def known_document_numbers(conn) -> set[str]:
    rows = conn.execute("SELECT invoice_no FROM invoices UNION SELECT bill_no FROM bills").fetchall()
    return {r[0].upper() for r in rows}


def verify_text(
    text: str,
    *,
    known_docs: set[str],
    allowed_docs: set[str] | None = None,
    allowed_amounts: set[Decimal] | None = None,
    allowed_days: set[int] | None = None,
) -> list[str]:
    """Return the problems found (empty list = the text passes).

    - every invoice or bill number must exist in the database (and, if given, be one of allowed_docs)
    - every SGD amount must equal one of allowed_amounts exactly
    - every "N days late" must equal one of allowed_days
    """
    problems = []
    for match in DOC_NO.findall(text):
        number = match.upper()
        if number not in known_docs:
            problems.append(f"{match} does not exist in the database")
        elif allowed_docs is not None and number not in {d.upper() for d in allowed_docs}:
            problems.append(f"{match} is not the document this draft is about")
    if allowed_amounts is not None:
        allowed = {a.quantize(Decimal("0.01")) for a in allowed_amounts}
        for raw in AMOUNT.findall(text):
            try:
                value = Decimal(raw.replace(",", ""))
            except InvalidOperation:
                problems.append(f"unreadable amount {raw!r}")
                continue
            if value not in allowed:
                problems.append(f"amount {raw} does not match the database")
    if allowed_days is not None:
        for raw in DAYS.findall(text):
            if int(raw) not in allowed_days:
                problems.append(f"{raw} days late does not match the database")
    return problems
