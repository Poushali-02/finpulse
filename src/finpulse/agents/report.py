"""Renders outbox/report-<date>.md from the finished run."""

from __future__ import annotations

from pathlib import Path

from finpulse.format import sgd

TYPE_TITLES = {
    "reminder_email_7": "Reminder emails, 7+ days late",
    "reminder_email_30": "Reminder emails, 30+ days late",
    "reminder_email_60": "Reminder emails, 60+ days late",
    "phone_call": "Phone calls",
    "hold_bill": "Bills to hold",
    "review_duplicate": "Possible duplicate bills",
    "chase_approval": "Approval chases",
}


def render_report(result: dict) -> str:
    as_of = result["as_of"]
    f = result["findings"]
    proposals = result["proposals"]
    usage = result["usage"]
    overdue_total = sum((r["owed"] for r in f["overdue"]), start=0)
    lines = [
        f"# Month-end check, as of {as_of.isoformat()}",
        "",
        "> Unofficial concept, not affiliated with Peakflo. Made-up data (Lion City Supplies Pte Ltd, SGD). "
        "Nothing has been sent or changed: every action below waits for a person.",
        "",
        "## Summary",
        "",
        result["summary"],
        "",
        f"_Summary source: {result['summary_source']}._",
        "",
        "## What was found",
        "",
        "| Check | Count |",
        "|---|---|",
        f"| Overdue invoices (policy: reminders from {min(result['policy']['reminders']['days_late'])} days late) "
        f"| {len(f['overdue'])} ({sgd(overdue_total)} owed) |",
        f"| Customers to phone | {result['counts']['phone_calls']} |",
        f"| Bills to hold (price, quantity or no PO) | {result['counts']['holds']} |",
        f"| Possible duplicate bills | {result['counts']['duplicates']} |",
        f"| Bills stuck in approval | {result['counts']['stuck']} |",
        "",
        f"## Actions ({len(proposals)})",
        "",
    ]
    for action_type, title in TYPE_TITLES.items():
        group = [p for p in proposals if p["action_type"] == action_type]
        if not group:
            continue
        lines += [f"### {title} ({len(group)})", "", "| Action | Target | Detail | Drafted by |", "|---|---|---|---|"]
        for p in group:
            first = next((l for l in p["draft_text"].splitlines() if l.startswith("Subject:")), None)
            detail = (first or p["draft_text"].splitlines()[0]).replace("|", "/")[:110]
            number = f"#{p['action_id']}" + ("" if p["created"] else " (already pending)")
            lines.append(f"| {number} | {p['target_label']} | {detail} | {p['source']} |")
        lines.append("")
    lines += ["## Drafts the double-check discarded", ""]
    lines += [f"- {d}" for d in result["discarded"]] or ["- None."]
    lines += [
        "",
        "## Model use",
        "",
        f"- Model: {result['model'] or 'none (fixed templates only)'}",
        f"- Calls: {usage['calls']} (maximum {result['policy']['llm']['max_calls']})",
        f"- Tokens: {usage['prompt_tokens']} prompt + {usage['completion_tokens']} completion "
        f"= {usage['total_tokens']}",
        "",
        "## Next",
        "",
        "```",
        "uv run python -m finpulse.cli list",
        "uv run python -m finpulse.cli approve <id>",
        "uv run python -m finpulse.cli reject <id>",
        "```",
        "",
        "Approving a reminder email writes one `.eml` file into `outbox/`. Nothing is ever really sent.",
        "",
    ]
    return "\n".join(lines)


def write_report(outbox_dir: Path, result: dict) -> Path:
    outbox_dir.mkdir(parents=True, exist_ok=True)
    path = outbox_dir / f"report-{result['as_of'].isoformat()}.md"
    path.write_text(render_report(result), encoding="utf-8")
    return path
