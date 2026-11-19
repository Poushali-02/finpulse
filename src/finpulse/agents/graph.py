"""The month-end check as a fixed LangGraph graph: find -> check_rules -> double_check.

1. find: pure Python. Calls queries.py directly (not through MCP), so it cannot miss what the tools find.
2. check_rules: pure Python applies policy.yaml and decides every action. The AI only writes the wording
   of reminder emails and a one-paragraph summary, and never sees an amount, date or invoice number.
3. double_check: every number in AI text must match the database, or that draft is discarded.

Nothing here can approve anything: this module only imports actions.create_pending.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import TypedDict

import yaml
from langgraph.graph import END, START, StateGraph
from psycopg.rows import dict_row

from finpulse import actions, queries
from finpulse.agents import prompts, verify
from finpulse.agents.llm import BudgetedLLM, LLMError, parse_json
from finpulse.agents.report import write_report
from finpulse.format import sgd

log = logging.getLogger("finpulse.agents")

POLICY_PATH = Path(__file__).with_name("policy.yaml")


def load_policy(path: Path = POLICY_PATH) -> dict:
    policy = yaml.safe_load(path.read_text(encoding="utf-8"))
    policy["reminders"]["days_late"] = sorted(int(d) for d in policy["reminders"]["days_late"])
    policy["phone_call"]["days_late"] = int(policy["phone_call"]["days_late"])
    policy["bills"]["hold_tolerance"] = Decimal(str(policy["bills"]["hold_tolerance"]))
    policy["bills"]["chase_approval_after_days"] = int(policy["bills"]["chase_approval_after_days"])
    policy["llm"]["max_calls"] = int(policy["llm"]["max_calls"])
    return policy


class MonthEndState(TypedDict, total=False):
    as_of: date
    findings: dict
    proposals: list[dict]
    discarded: list[str]
    summary: str
    summary_source: str
    summary_facts: list[str]
    counts: dict


def _select(conn, query: str, params=None) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params or {})
        return cur.fetchall()


def _tier(days_late: int, thresholds: list[int]) -> int:
    return max(t for t in thresholds if days_late >= t)


# ---------------------------------------------------------------------------------------- node 1


def _find(conn, policy: dict, state: MonthEndState) -> dict:
    as_of = state["as_of"]
    bills = _select(
        conn,
        """
        SELECT b.id, b.bill_no, s.name AS supplier, b.status, b.amount
        FROM bills b JOIN suppliers s ON s.id = b.supplier_id
        WHERE b.status IN ('pending_approval', 'approved') ORDER BY b.id
        """,
    )
    tolerance = policy["bills"]["hold_tolerance"]
    findings = {
        "overdue": queries.overdue_invoice_rows(conn, as_of, min(policy["reminders"]["days_late"])),
        "bill_checks": [queries.check_bill(conn, as_of, b["bill_no"], tolerance) for b in bills],
        "stuck": queries.stuck_bills(conn, as_of, policy["bills"]["chase_approval_after_days"]),
        "duplicates": queries.possible_duplicate_bills(conn),
        "bill_ids": {r["bill_no"]: r["id"] for r in _select(conn, "SELECT id, bill_no FROM bills")},
    }
    return {"findings": findings, "discarded": []}


# ---------------------------------------------------------------------------------------- node 2


def _proposal(key, action_type, target_type, target_id, target_label, kind, **extra) -> dict:
    return {"key": key, "action_type": action_type, "target_type": target_type, "target_id": target_id,
            "target_label": target_label, "kind": kind, "source": "rules", **extra}


def _hold_reasons(check: dict, tolerance: Decimal) -> tuple[list[str], set[Decimal]]:
    reasons, amounts = [], set()
    if "no PO" in check["issues"]:
        reasons.append("no purchase order")
    for ln in check["lines"]:
        pct = ln.get("price_diff_pct")
        if pct is not None and pct > tolerance * 100:
            reasons.append(f"{ln['item']}: billed {sgd(ln['billed_price'])} vs PO {sgd(ln['po_price'])} (+{pct}%)")
            amounts |= {ln["billed_price"], ln["po_price"]}
        if check["bill"]["po_id"] is not None and ln["billed_qty"] > ln["received_qty"] * (1 + tolerance):
            reasons.append(f"{ln['item']}: billed {ln['billed_qty']} but only {ln['received_qty']} received")
    return reasons, amounts


def _build_proposals(findings: dict, policy: dict) -> list[dict]:
    proposals = []
    thresholds = policy["reminders"]["days_late"]
    phone_days = policy["phone_call"]["days_late"]
    tolerance = policy["bills"]["hold_tolerance"]
    policy_days = set(thresholds) | {phone_days}

    # reminders: one per overdue invoice, at the highest tier reached
    for r in findings["overdue"]:
        tier = _tier(r["days_late"], thresholds)
        proposals.append(_proposal(
            f"invoice:{r['invoice_no']}:reminder_{tier}", f"reminder_email_{tier}", "invoice", r["invoice_id"],
            r["invoice_no"], "email", tier=tier, to=r["customer_email"],
            facts={"customer": r["customer"], "invoice_no": r["invoice_no"], "amount_owed": sgd(r["owed"]),
                   "due_date": r["due_date"].isoformat(), "days_late": str(r["days_late"])},
            allowed_docs={r["invoice_no"]}, allowed_amounts={r["owed"]},
            allowed_days={r["days_late"], tier} | policy_days,
        ))

    # phone calls: one per customer with an invoice at or past the threshold
    by_customer: dict[int, list[dict]] = {}
    for r in findings["overdue"]:
        if r["days_late"] >= phone_days:
            by_customer.setdefault(r["customer_id"], []).append(r)
    for customer_id, rows in sorted(by_customer.items(), key=lambda kv: (-sum(x["owed"] for x in kv[1]), kv[0])):
        total = sum((x["owed"] for x in rows), Decimal("0.00"))
        oldest = max(x["days_late"] for x in rows)
        numbers = ", ".join(x["invoice_no"] for x in rows)
        text = (f"PHONE CALL: ring {rows[0]['customer']} ({rows[0]['customer_email']}) about {len(rows)} invoices "
                f"more than {phone_days} days late ({numbers}), {sgd(total)} owed in total. "
                f"The oldest is {oldest} days late.")
        proposals.append(_proposal(
            f"customer:{customer_id}:phone_call", "phone_call", "customer", customer_id, rows[0]["customer"],
            "note", draft_text=text, allowed_docs={x["invoice_no"] for x in rows},
            allowed_amounts={total} | {x["owed"] for x in rows}, allowed_days={oldest} | policy_days,
        ))

    # bills to hold: price or quantity more than the tolerance off, or no PO
    for check in findings["bill_checks"]:
        if check["status"] != "ok":
            continue
        if not ({"price mismatch", "quantity mismatch", "no PO"} & set(check["issues"])):
            continue
        if "no PO" in check["issues"] and not policy["bills"]["hold_if_no_po"]:
            continue
        b = check["bill"]
        reasons, amounts = _hold_reasons(check, tolerance)
        text = (f"HOLD bill {b['bill_no']} ({b['supplier']}, {sgd(b['amount'])}): " + "; ".join(reasons)
                + ". Do not pay until the supplier corrects it.")
        proposals.append(_proposal(
            f"bill:{b['bill_no']}:hold", "hold_bill", "bill", b["id"], b["bill_no"], "note", draft_text=text,
            allowed_docs={b["bill_no"]}, allowed_amounts={b["amount"]} | amounts, allowed_days=None,
        ))

    # duplicates: propose a human review of the pending copy (or the later bill if both are pending)
    for d in findings["duplicates"]:
        a_no, b_no = d["bill_no"], d["other_bill_no"]
        pick, other = (a_no, b_no) if (d["status"] == "pending_approval" and d["other_status"] != "pending_approval") else (b_no, a_no)
        pick_status = d["status"] if pick == a_no else d["other_status"]
        other_status = d["other_status"] if pick == a_no else d["status"]
        text = (f"REVIEW DUPLICATE: bill {pick} ({pick_status.replace('_', ' ')}) looks like a duplicate of "
                f"{other} ({other_status.replace('_', ' ')}): same supplier, {d['supplier']}, and the same amount, "
                f"{sgd(d['amount'])}. A person must decide whether to reject {pick}; nothing is rejected automatically.")
        proposals.append(_proposal(
            f"bill:{pick}:review_duplicate", "review_duplicate", "bill", findings["bill_ids"][pick], pick, "note",
            draft_text=text, allowed_docs={a_no, b_no}, allowed_amounts={d["amount"]}, allowed_days=None,
        ))

    # stuck in approval
    for s in findings["stuck"]:
        text = (f"CHASE APPROVER: bill {s['bill_no']} ({s['supplier']}, {sgd(s['amount'])}) has waited "
                f"{s['days_waiting']} days for approval since {s['submitted_on'].isoformat()}. "
                "Please approve or reject it.")
        proposals.append(_proposal(
            f"bill:{s['bill_no']}:chase_approval", "chase_approval", "bill", findings["bill_ids"][s["bill_no"]],
            s["bill_no"], "note", draft_text=text, allowed_docs={s["bill_no"]}, allowed_amounts={s["amount"]},
            allowed_days=None,
        ))
    return proposals


def _ask_for_reminder_drafts(llm: BudgetedLLM, emails: list[dict]) -> dict[str, tuple[str, str]]:
    """One call for every reminder; one retry if the JSON is unusable. Returns id -> (subject, body)."""
    last_error: LLMError | None = None
    for _ in range(2):
        try:
            data = parse_json(llm.complete(prompts.REMINDER_SYSTEM, prompts.reminder_user_prompt(emails)).text)
            drafts = {}
            for entry in data.get("drafts", []):
                if isinstance(entry, dict) and all(isinstance(entry.get(k), str) for k in ("id", "subject", "body")):
                    drafts[entry["id"]] = (entry["subject"], entry["body"])
            if not drafts:
                raise LLMError("the model returned no usable drafts")
            return drafts
        except LLMError as exc:
            last_error = exc
            log.warning("reminder drafts: %s", exc)
    raise last_error  # type: ignore[misc]


def _summary_facts(proposals: list[dict], findings: dict, as_of: date) -> tuple[list[str], dict]:
    counts = {
        "reminders": sum(1 for p in proposals if p["kind"] == "email"),
        "phone_calls": sum(1 for p in proposals if p["action_type"] == "phone_call"),
        "holds": sum(1 for p in proposals if p["action_type"] == "hold_bill"),
        "duplicates": sum(1 for p in proposals if p["action_type"] == "review_duplicate"),
        "stuck": sum(1 for p in proposals if p["action_type"] == "chase_approval"),
    }
    total = sum((r["owed"] for r in findings["overdue"]), Decimal("0.00"))
    facts = [
        f"Check date: {as_of.isoformat()}",
        f"{len(findings['overdue'])} overdue invoices, {sgd(total)} owed in total",
        f"{counts['reminders']} reminder emails drafted",
        f"{counts['phone_calls']} customers flagged for a phone call",
        f"{counts['holds']} bills to hold",
        f"{counts['duplicates']} possible duplicate bills",
        f"{counts['stuck']} bills stuck in approval",
        "Every action waits for a person to approve it",
    ]
    return facts, counts


def _fallback_summary(facts: list[str]) -> str:
    return "Month-end check. " + "; ".join(facts[1:]) + "."


def _check_rules(llm: BudgetedLLM | None, policy: dict, state: MonthEndState) -> dict:
    as_of, findings = state["as_of"], state["findings"]
    discarded = list(state["discarded"])
    proposals = _build_proposals(findings, policy)
    emails = [p for p in proposals if p["kind"] == "email"]

    drafts: dict[str, tuple[str, str]] = {}
    if llm and emails:
        try:
            drafts = _ask_for_reminder_drafts(llm, emails)
        except LLMError as exc:
            discarded.append(f"All AI reminder drafts unavailable ({exc}); fixed templates used.")
    for p in emails:
        if p["key"] in drafts:
            p["template"], p["source"] = drafts[p["key"]], "AI"
        else:
            if llm and drafts:
                discarded.append(f"No AI draft returned for {p['target_label']}; fixed template used.")
            p["template"], p["source"] = prompts.FALLBACK_REMINDER[p["tier"]], "template"

    facts, counts = _summary_facts(proposals, findings, as_of)
    summary, source = _fallback_summary(facts), "built-in template (no AI)"
    if llm:
        try:
            text = parse_json(llm.complete(prompts.SUMMARY_SYSTEM, prompts.summary_user_prompt(facts)).text)["summary"]
            if not isinstance(text, str) or not text.strip():
                raise LLMError("empty summary")
            summary, source = text.strip(), "AI, checked against the database"
        except (LLMError, KeyError) as exc:
            discarded.append(f"AI summary unavailable ({exc}); built-in summary used.")
    return {"proposals": proposals, "discarded": discarded, "summary": summary, "summary_source": source,
            "summary_facts": facts, "counts": counts}


# ---------------------------------------------------------------------------------------- node 3


def _render_email(p: dict, subject_template: str, body_template: str) -> str:
    values = p["facts"]
    subject = verify.render_template(subject_template, values).replace("\n", " ").strip()
    body = verify.render_template(body_template, values).strip()
    return f"To: {p['to']}\nSubject: {subject}\n\n{body}\n"


def _double_check(conn, state: MonthEndState) -> dict:
    known = verify.known_document_numbers(conn)
    discarded = list(state["discarded"])
    for p in state["proposals"]:
        if p["kind"] == "email":
            subject_t, body_t = p["template"]
            try:
                text = _render_email(p, subject_t, body_t)
                problems = verify.verify_text(text, known_docs=known, allowed_docs=p["allowed_docs"],
                                              allowed_amounts=p["allowed_amounts"], allowed_days=p["allowed_days"])
            except ValueError as exc:
                text, problems = "", [str(exc)]
            if problems and p["source"] == "AI":
                discarded.append(f"AI draft for {p['target_label']} discarded: " + "; ".join(problems) + ". Fixed template used.")
                p["source"] = "template (AI draft rejected)"
                text = _render_email(p, *prompts.FALLBACK_REMINDER[p["tier"]])
                problems = verify.verify_text(text, known_docs=known, allowed_docs=p["allowed_docs"],
                                              allowed_amounts=p["allowed_amounts"], allowed_days=p["allowed_days"])
            if problems:  # a fixed template failing is a bug, not something to paper over
                raise ValueError(f"fixed draft for {p['target_label']} failed the double-check: {problems}")
            p["draft_text"] = text
        else:
            problems = verify.verify_text(p["draft_text"], known_docs=known, allowed_docs=p["allowed_docs"],
                                          allowed_amounts=p["allowed_amounts"], allowed_days=p["allowed_days"])
            if problems:
                raise ValueError(f"note for {p['target_label']} failed the double-check: {problems}")

    facts_amounts = {Decimal(a.replace(",", "")) for f in state["summary_facts"] for a in verify.AMOUNT.findall(f)}
    summary, source = state["summary"], state["summary_source"]
    problems = verify.verify_text(summary, known_docs=known, allowed_docs=set(), allowed_amounts=facts_amounts)
    if problems:
        discarded.append("AI summary discarded: " + "; ".join(problems) + ". Built-in summary used.")
        summary, source = _fallback_summary(state["summary_facts"]), "built-in template (AI summary rejected)"
    return {"proposals": state["proposals"], "discarded": discarded, "summary": summary, "summary_source": source}


# ------------------------------------------------------------------------------------------ graph


def build_graph(conn, llm: BudgetedLLM | None, policy: dict):
    graph = StateGraph(MonthEndState)
    graph.add_node("find", lambda state: _find(conn, policy, state))
    graph.add_node("check_rules", lambda state: _check_rules(llm, policy, state))
    graph.add_node("double_check", lambda state: _double_check(conn, state))
    graph.add_edge(START, "find")
    graph.add_edge("find", "check_rules")
    graph.add_edge("check_rules", "double_check")
    graph.add_edge("double_check", END)
    return graph.compile()


def run_month_end(conn, as_of: date, client, outbox_dir: Path, policy: dict | None = None, model: str | None = None) -> dict:
    """Run the check, store pending actions, write the report. `client` is any object with
    `.complete(system, user)`, or None for fixed templates only."""
    policy = policy or load_policy()
    llm = BudgetedLLM(client, policy["llm"]["max_calls"]) if client else None
    state = build_graph(conn, llm, policy).invoke({"as_of": as_of})

    created_at = datetime.combine(as_of, time(9, 0), tzinfo=timezone.utc)
    for p in state["proposals"]:
        p["action_id"], p["created"] = actions.create_pending(
            conn, p["action_type"], p["target_type"], p["target_id"], p["draft_text"], created_at
        )
    conn.commit()

    result = {
        "as_of": as_of,
        "policy": policy,
        "findings": state["findings"],
        "proposals": state["proposals"],
        "discarded": state["discarded"],
        "summary": state["summary"],
        "summary_source": state["summary_source"],
        "counts": state["counts"],
        "usage": llm.usage if llm else {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "model": model if client else None,
    }
    result["report_path"] = write_report(outbox_dir, result)
    return result
