"""Turns query results into short plain-text answers. No arithmetic on money here
beyond display: every total comes from queries.py."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

MAX_WORDS = 300


def sgd(amount: Decimal) -> str:
    return f"SGD {amount:,.2f}"


def header(as_of: date) -> str:
    return f"[demo data] as of {as_of.isoformat()}"


def _words(lines: list[str]) -> int:
    return sum(len(line.split()) for line in lines)


def assemble(as_of: date, fixed: list[str], detail: list[str], footer: list[str] | None = None) -> str:
    """Header + fixed lines + as many detail lines as fit in MAX_WORDS, + footer."""
    footer = footer or []
    head = [header(as_of)] + fixed
    budget = MAX_WORDS - _words(head) - _words(footer) - 12
    kept: list[str] = []
    for line in detail:
        if _words(kept) + len(line.split()) > budget:
            break
        kept.append(line)
    out = head + kept
    if len(kept) < len(detail):
        out.append(f"(+{len(detail) - len(kept)} more not shown; ask again with a smaller limit.)")
    return "\n".join(out + footer)


def not_found_customer(as_of: date, result: dict) -> str:
    lines = [header(as_of), f"No customer matches '{result['query']}'."]
    if result["suggestions"]:
        lines.append("Similar names: " + "; ".join(result["suggestions"]) + ".")
    lines.append("Try part of the company name, for example its first word.")
    return "\n".join(lines)


def format_overdue(r: dict) -> str:
    if r["count"] == 0:
        return "\n".join([
            header(r["as_of"]),
            f"No invoices are {r['min_days_late']} or more days late.",
            "Try a smaller min_days_late (use 0 to include invoices not yet due).",
        ])
    fixed = [f"{r['count']} unpaid invoices {r['min_days_late']}+ days late, {sgd(r['total'])} owed in total."]
    fixed.append("By days late: " + "; ".join(f"{b['label']}: {b['count']} ({sgd(b['total'])})" for b in r["buckets"]) + ".")
    shown = r["by_customer"]
    fixed.append(
        f"By customer ({r['customer_count']} in total" + (f", top {len(shown)}" if r["customer_count"] > len(shown) else "") + "): "
        + "; ".join(f"{c['customer']}: {sgd(c['total'])} over {c['count']} invoices" for c in shown) + "."
    )
    fixed.append(f"Worst {len(r['rows'])} invoices:")
    detail = [
        f"- {x['invoice_no']} | {x['customer']} | {sgd(x['owed'])} owed | {x['days_late']} days late"
        for x in r["rows"]
    ]
    return assemble(r["as_of"], fixed, detail)


def format_customer_summary(as_of: date, r: dict) -> str:
    if r["status"] == "not_found":
        return not_found_customer(as_of, r)
    if r["status"] == "ambiguous":
        more = r["match_count"] - len(r["matches"])
        return "\n".join([
            header(as_of),
            f"'{r['query']}' matches {r['match_count']} customers:",
            *[f"- {name}" for name in r["matches"]],
            *([f"(+{more} more not shown.)"] if more else []),
            "Which one do you mean? Ask again with the full name.",
        ])
    avg = r["average_days_late"]
    avg_text = "no payment history in the last 6 months" if avg is None else f"{avg} days"
    fixed = [
        f"{r['name']} (terms: Net {r['payment_terms_days']}).",
        f"Total owed: {sgd(r['total_owed'])} across {r['open_count']} open invoices ({r['overdue_count']} overdue).",
        f"Average days late, last 6 months: {avg_text}.",
    ]
    lp = r["last_payment"]
    fixed.append(
        f"Last payment: {sgd(lp['amount'])} on {lp['paid_on'].isoformat()} for {lp['invoice_no']}."
        if lp else "Last payment: none on record."
    )
    detail = []
    if r["invoices"]:
        fixed.append(f"Oldest {len(r['invoices'])} open invoices:")
        detail = [
            f"- {i['invoice_no']} | {sgd(i['owed'])} owed | due {i['due_date'].isoformat()} | "
            + (f"{i['days_late']} days late" if i["days_late"] > 0 else "not due yet")
            for i in r["invoices"]
        ]
    return assemble(as_of, fixed, detail)


def format_forecast(r: dict) -> str:
    fixed = [
        f"Cash forecast, next {r['days']} days ({r['as_of'].isoformat()} to {r['end'].isoformat()}):",
        f"Money in: {sgd(r['total_in'])} from {len(r['money_in'])} invoices.",
        f"Money out: {sgd(r['total_out'])} for {len(r['money_out'])} approved bills"
        + (f" ({r['already_due_bills']} already due)." if r["already_due_bills"] else "."),
        f"Net: {sgd(r['net'])}.",
    ]
    detail = []
    if r["money_in"]:
        detail.append("Largest expected in:")
        detail += [f"- {x['invoice_no']} | {x['customer']} | {sgd(x['owed'])} | expected {x['expected_on'].isoformat()}"
                   for x in r["money_in"][:3]]
    if r["money_out"]:
        detail.append("Largest going out:")
        detail += [f"- {x['bill_no']} | {x['supplier']} | {sgd(x['amount'])} | due {x['due_date'].isoformat()}"
                   for x in r["money_out"][:3]]
    footer = [
        "Method (an estimate): an open invoice is expected on its due date plus that customer's "
        "average days late over the last 6 months (0 if no history). Money out is approved, unpaid bills due by the end date."
    ]
    if r["past_expected_count"]:
        footer.append(
            f"Not counted: {r['past_expected_count']} overdue invoices ({sgd(r['past_expected_total'])}) "
            "already past their expected date. See overdue_invoices."
        )
    return assemble(r["as_of"], fixed, detail, footer)


def format_attention(r: dict) -> str:
    if not (r["stuck_count"] or r["duplicate_count"] or r["no_po_count"]):
        return "\n".join([
            header(r["as_of"]),
            "No supplier bills need attention right now.",
            "Use check_bill with a bill number to run a full three-way match on one bill.",
        ])
    fixed, detail = [], []
    if r["stuck"]:
        fixed.append(f"Stuck in approval over 7 days: {r['stuck_count']}")
        detail += [f"- {b['bill_no']} | {b['supplier']} | {sgd(b['amount'])} | waiting {b['days_waiting']} days" for b in r["stuck"]]
    if r["duplicates"]:
        detail.append(f"Possible duplicates (same supplier and amount): {r['duplicate_count']}")
        detail += [
            f"- {d['bill_no']} and {d['other_bill_no']} | {d['supplier']} | {sgd(d['amount'])}"
            + (" | same number apart from punctuation" if d["same_number"] else "")
            for d in r["duplicates"]
        ]
    if r["no_po"]:
        detail.append(f"Bills with no purchase order: {r['no_po_count']}")
        detail += [f"- {b['bill_no']} | {b['supplier']} | {sgd(b['amount'])} | {b['status']}" for b in r["no_po"]]
    footer = ["Next step: check_bill with a bill number gives the line-by-line match."]
    return assemble(r["as_of"], fixed, detail, footer)


def format_pending_actions(r: dict) -> str:
    if r["count"] == 0:
        return "\n".join([
            header(r["as_of"]),
            "Nothing is waiting for approval.",
            "A person runs the month-end check with `python -m finpulse.cli run-month-end`; its proposals then show up here.",
        ])
    fixed = [
        f"{r['count']} actions are waiting for a person to approve or reject them: "
        + ", ".join(f"{n} {t.replace('_', ' ')}" for t, n in sorted(r["by_type"].items())) + ".",
        f"First {len(r['rows'])}:",
    ]
    detail = [
        f"- #{x['id']} | {x['action_type'].replace('_', ' ')} | {x['target']} | {x['summary'][:90]}"
        for x in r["rows"]
    ]
    footer = ["Nothing is sent or changed until someone approves it at the command line "
              "(`python -m finpulse.cli approve <id>`). Claude cannot approve."]
    return assemble(r["as_of"], fixed, detail, footer)


def format_check_bill(as_of: date, r: dict) -> str:
    if r["status"] == "not_found":
        lines = [header(as_of), f"No bill numbered '{r['query']}' found."]
        if r["suggestions"]:
            lines.append("Bills starting similarly: " + ", ".join(r["suggestions"]) + ".")
        lines.append("Use bills_needing_attention to see bill numbers that need a look.")
        return "\n".join(lines)
    if r["status"] == "ambiguous":
        return "\n".join([
            header(as_of),
            f"'{r['query']}' matches several bills: " + ", ".join(r["matches"]) + ".",
            "Ask again with the exact bill number.",
        ])
    b = r["bill"]
    fixed = [
        f"Bill {b['bill_no']} | {b['supplier']} | {sgd(b['amount'])} | {b['status'].replace('_', ' ')} | "
        f"PO: {r['po_no'] or 'none'}",
        f"Verdict: {r['verdict']}.",
    ]
    if r["duplicates"]:
        fixed.append("Same supplier and amount as: " + ", ".join(f"{d['bill_no']} ({d['status'].replace('_', ' ')})" for d in r["duplicates"]) + ".")
    detail = []
    if b["po_id"] is not None:
        fixed.append("Three-way match (ordered / received / billed):")
        for ln in r["lines"]:
            ordered = "not on PO" if ln["ordered_qty"] is None else str(ln["ordered_qty"])
            po_price = "n/a" if ln["po_price"] is None else sgd(ln["po_price"])
            billed_price = "n/a" if ln["billed_price"] is None else sgd(ln["billed_price"])
            pct = ln.get("price_diff_pct")
            pct_text = "" if not pct else f" ({pct:+}%)"
            detail.append(
                f"- {ln['item']}: qty {ordered} / {ln['received_qty']} / {ln['billed_qty']}; "
                f"price {po_price} PO vs {billed_price} billed{pct_text}"
            )
    footer = ["Read-only: this tool cannot pay, approve or change a bill."]
    return assemble(as_of, fixed, detail, footer)
