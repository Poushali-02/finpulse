"""Prompts for the two AI jobs, and the fixed templates used when the AI is unavailable or rejected.

The AI is never shown an amount, date or invoice number, and is told to use placeholders.
"""

from __future__ import annotations

import json

ALLOWED_FIELDS = frozenset({"customer", "invoice_no", "amount_owed", "due_date", "days_late"})
SIGN_OFF = "Accounts Receivable, Lion City Supplies Pte Ltd"

TIER_TONE = {
    7: "a friendly nudge: it may simply have been missed",
    30: "firm but polite: the invoice is now well overdue, ask for payment or a date this week",
    60: "a final notice: say that the account is seriously overdue and that we will also phone them",
}

REMINDER_SYSTEM = (
    "You draft short payment-reminder emails for the accounts-receivable team of Lion City Supplies "
    "Pte Ltd, a made-up company. Reply with JSON only, with no prose and no code fences, in exactly this "
    'shape: {"drafts": [{"id": "...", "subject": "...", "body": "..."}]}. '
    "Write one draft for every item you are given and copy its id exactly. "
    "STRICT RULES: never write any number, amount, date or invoice number yourself. Where one is needed, "
    "use exactly these placeholders and no other curly-brace text: {customer}, {invoice_no}, {amount_owed}, "
    "{due_date}, {days_late}. Each body is plain text of at most 90 words, starts with "
    f"'Dear {{customer}} accounts team,' and ends with the sign-off '{SIGN_OFF}'. "
    "Follow the tone given for each item."
)

SUMMARY_SYSTEM = (
    "You summarise a month-end receivables and payables check for a finance manager. Write one paragraph of "
    "at most 80 words using only the facts provided. Copy every amount and count exactly as written and do "
    "not calculate anything. Do not mention invoice or bill numbers. Reply with JSON only, with no code "
    'fences: {"summary": "..."}'
)


def reminder_user_prompt(items: list[dict]) -> str:
    payload = [
        {"id": i["key"], "tier_days_late": i["tier"], "customer": i["facts"]["customer"], "tone": TIER_TONE[i["tier"]]}
        for i in items
    ]
    return json.dumps({"items": payload}, indent=1)


def summary_user_prompt(facts: list[str]) -> str:
    return "Facts:\n" + "\n".join(f"- {f}" for f in facts)


# ---- fixed templates (same placeholder rules as the AI drafts) -----------------------------------

FALLBACK_REMINDER = {
    7: (
        "Friendly reminder: invoice {invoice_no}",
        "Dear {customer} accounts team,\n\nThis is a quick reminder that invoice {invoice_no} for {amount_owed}, "
        "due on {due_date}, is now {days_late} days late. If it has already been paid, please ignore this "
        f"note. Otherwise we would be grateful for payment soon.\n\n{SIGN_OFF}",
    ),
    30: (
        "Overdue invoice {invoice_no}: payment needed",
        "Dear {customer} accounts team,\n\nInvoice {invoice_no} for {amount_owed} was due on {due_date} and is "
        "now {days_late} days late. Please arrange payment this week, or tell us the date we can expect it.\n\n"
        f"{SIGN_OFF}",
    ),
    60: (
        "Final notice: invoice {invoice_no}",
        "Dear {customer} accounts team,\n\nInvoice {invoice_no} for {amount_owed}, due on {due_date}, is now "
        "{days_late} days late. This is a final notice and we will also be phoning you about the account. "
        f"Please arrange payment immediately.\n\n{SIGN_OFF}",
    ),
}
