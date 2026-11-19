"""Phase 5: the month-end agent check. Recall, precision, safety, consistency.

Every test that needs a model uses a scripted stub: free, offline and deterministic.
"""

import ast
import asyncio
import inspect
import json
from pathlib import Path

import pytest
from fastmcp import Client

import generate_data
from finpulse import actions, approvals
from finpulse.agents import graph, verify
from finpulse.agents.llm import BudgetedLLM, LLMBudgetExceeded, LLMError, LLMResult, parse_json
from finpulse.server import mcp

SRC = Path(__file__).resolve().parent.parent / "src" / "finpulse"


# ------------------------------------------------------------------ stub models


class GoodLLM:
    """Follows the rules: placeholders only, summary copied from the facts."""

    def __init__(self):
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        if "payment-reminder" in system:
            drafts = [
                {"id": it["id"], "subject": "Reminder {invoice_no}",
                 "body": "Dear {customer} accounts team,\n\nPlease pay {amount_owed} for {invoice_no}, due "
                         "{due_date} and {days_late} days late.\n\nAccounts Receivable, Lion City Supplies Pte Ltd"}
                for it in json.loads(user)["items"]
            ]
            return LLMResult(json.dumps({"drafts": drafts}), 300, 600)
        facts = [l[2:] for l in user.splitlines() if l.startswith("- ")]
        return LLMResult(json.dumps({"summary": "; ".join(facts) + "."}), 100, 80)


class HallucinatingLLM(GoodLLM):
    """Writes its own amount into every draft and the summary."""

    def complete(self, system, user):
        self.calls += 1
        if "payment-reminder" in system:
            drafts = [{"id": it["id"], "subject": "Pay up", "body": "Dear {customer}, pay SGD 1.00 now."}
                      for it in json.loads(user)["items"]]
            return LLMResult(json.dumps({"drafts": drafts}), 1, 1)
        return LLMResult(json.dumps({"summary": "We are owed SGD 5,000.00 in total."}), 1, 1)


class GarbageLLM(GoodLLM):
    def complete(self, system, user):
        self.calls += 1
        return LLMResult("sorry, I cannot do that", 1, 1)


class BrokenLLM(GoodLLM):
    def complete(self, system, user):
        self.calls += 1
        raise LLMError("model call failed with HTTP 500")


CLIENTS = {"no model": None, "good": GoodLLM, "hallucinating": HallucinatingLLM,
           "garbage": GarbageLLM, "broken": BrokenLLM}


# --------------------------------------------------------------------- helpers


@pytest.fixture
def fresh(conn, dataset):
    """The generated books with an empty actions table."""
    generate_data.load(conn, dataset)
    return conn


@pytest.fixture
def as_of(dataset):
    return dataset["as_of"]


def key(dataset):
    return {p["key"]: p for p in dataset["planted"]["problems"]}


def run(conn, as_of, tmp_path, client=None):
    return graph.run_month_end(conn, as_of, client, tmp_path / "outbox")


def make_client(name):
    factory = CLIENTS[name]
    return factory() if factory else None


def action_map(conn):
    """{(action_type, target label)} for every stored action."""
    return {(a["action_type"], a["target"]) for a in actions.list_actions(conn)}


def expected_overdue(conn, as_of, min_days=7):
    """Independent SQL, not using queries.py: invoices with money owed that are min_days+ late."""
    return conn.execute(
        """
        SELECT i.invoice_no, i.customer_id,
               %(as_of)s::date - i.due_date AS days_late
        FROM invoices i
        WHERE i.amount - COALESCE((SELECT SUM(amount) FROM payments p WHERE p.invoice_id = i.id), 0)
                       - COALESCE((SELECT SUM(amount) FROM credit_notes c WHERE c.invoice_id = i.id), 0) > 0
          AND %(as_of)s::date - i.due_date >= %(min)s
        ORDER BY i.invoice_no
        """,
        {"as_of": as_of, "min": min_days},
    ).fetchall()


def tier(days_late):
    return 60 if days_late >= 60 else 30 if days_late >= 30 else 7


# ---------------------------------------------------------------------- recall


@pytest.mark.parametrize("client_name", list(CLIENTS))
def test_recall_every_planted_agent_target_is_found(fresh, dataset, as_of, tmp_path, client_name):
    """100% of agent_target problems, whatever the model does (or doesn't) do."""
    k = key(dataset)
    run(fresh, as_of, tmp_path, make_client(client_name))
    found = action_map(fresh)

    # 1: the three late customers get a phone call and a 60-day reminder for each overdue invoice
    for customer in k["late_customers"]["customers"]:
        assert ("phone_call", customer["customer_name"]) in found
        for inv in customer["overdue_invoices"]:
            assert ("reminder_email_60", inv["invoice_no"]) in found
    # 4: duplicate bill: a human review of the pending copy
    assert ("review_duplicate", "INV1043") in found
    # 5, 6, 7: held bills
    for problem in ("price_mismatch", "quantity_mismatch", "bill_without_po"):
        assert ("hold_bill", k[problem]["bill_no"]) in found
    # 8: stuck bills
    for bill in k["stuck_in_approval"]["bills"]:
        assert ("chase_approval", bill["bill_no"]) in found

    kinds = {p["kind"] for p in dataset["planted"]["problems"]}
    assert kinds == {"agent_target", "data_case"}


# ------------------------------------------------------------------- precision


@pytest.mark.parametrize("client_name", ["no model", "good"])
def test_precision_no_actions_for_healthy_items(fresh, dataset, as_of, tmp_path, client_name):
    k = key(dataset)
    run(fresh, as_of, tmp_path, make_client(client_name))
    rows = actions.list_actions(fresh)

    overdue = {inv: d for inv, _, d in expected_overdue(fresh, as_of)}
    reminders = {a["target"]: a["action_type"] for a in rows if a["action_type"].startswith("reminder_email")}
    assert set(reminders) == set(overdue), "reminders go to exactly the invoices 7+ days late"
    for inv, action_type in reminders.items():
        assert action_type == f"reminder_email_{tier(overdue[inv])}"

    # the partial-payment and credit-note invoices are data cases: no action
    targets = {a["target"] for a in rows}
    assert k["partial_payment"]["invoice_no"] not in targets
    assert k["credit_note"]["invoice_no"] not in targets

    # customers: only the 3 late ones are phoned
    late_names = {c["customer_name"] for c in k["late_customers"]["customers"]}
    assert {a["target"] for a in rows if a["action_type"] == "phone_call"} == late_names

    # bills: holds, duplicate review and chases touch only the planted bills
    planted_bills = {k["price_mismatch"]["bill_no"], k["quantity_mismatch"]["bill_no"],
                     k["bill_without_po"]["bill_no"]}
    assert {a["target"] for a in rows if a["action_type"] == "hold_bill"} == planted_bills
    assert {a["target"] for a in rows if a["action_type"] == "review_duplicate"} == {"INV1043"}
    assert {a["target"] for a in rows if a["action_type"] == "chase_approval"} == {
        b["bill_no"] for b in k["stuck_in_approval"]["bills"]}
    assert len(rows) == len(overdue) + 3 + 3 + 1 + 2


# ---------------------------------------------------------------------- safety


def test_everything_the_agent_creates_is_pending_and_nothing_is_emailed(fresh, as_of, tmp_path):
    result = run(fresh, as_of, tmp_path, GoodLLM())
    rows = actions.list_actions(fresh)
    assert rows and {a["status"] for a in rows} == {"pending"}
    assert all(a["decided_at"] is None and a["decided_by"] is None for a in rows)
    outbox = tmp_path / "outbox"
    assert not list(outbox.glob("*.eml")), "no .eml without an approval"
    assert [p.name for p in outbox.iterdir()] == [result["report_path"].name]


def test_create_pending_cannot_take_a_status():
    assert "status" not in inspect.signature(actions.create_pending).parameters


def _python_files(folder):
    return sorted(p for p in folder.rglob("*.py"))


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            found.add(base)
            found |= {f"{base}.{a.name}" for a in node.names}
    return found


def test_agent_code_never_imports_the_approval_code():
    forbidden = {"finpulse.approvals", "finpulse.cli"}
    for path in _python_files(SRC / "agents"):
        imports = _imports(path)
        assert not (imports & forbidden), f"{path.name} imports approval code"
        assert not any(i.endswith(("approvals", "cli")) for i in imports), path.name


def test_agent_code_only_uses_create_pending_from_actions():
    for path in _python_files(SRC / "agents"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        used = {n.attr for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "actions"}
        assert used <= {"create_pending"}, f"{path.name} uses actions.{used - {'create_pending'}}"
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "finpulse.actions":
                assert {a.name for a in node.names} <= {"create_pending"}


def test_only_the_cli_imports_approvals():
    for path in _python_files(SRC):
        if path.name in {"cli.py", "approvals.py"}:
            continue
        imports = _imports(path)
        assert not any("approvals" in i for i in imports), f"{path.name} imports approvals"


def test_the_mcp_server_cannot_reach_the_agent_or_approval_code():
    imports = _imports(SRC / "server.py")
    for banned in ("approvals", "cli", "agents", "actions"):
        assert not any(i.split(".")[-1] == banned or f".{banned}" in i for i in imports), banned
    tool_names = {t.name for t in asyncio.run(_tools())}
    assert not {n for n in tool_names if any(w in n for w in ("approve", "reject", "send", "pay", "delete"))}


async def _tools():
    async with Client(mcp) as client:
        return await client.list_tools()


def test_approving_a_reminder_creates_exactly_one_file(fresh, as_of, tmp_path):
    run(fresh, as_of, tmp_path)
    outbox = tmp_path / "outbox"
    reminder = next(a for a in actions.list_actions(fresh) if a["action_type"] == "reminder_email_60")
    result = approvals.decide(fresh, as_of, reminder["id"], "approved", outbox, decided_by="tester")
    files = sorted(outbox.glob("*.eml"))
    assert files == [result["file"]] and len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert reminder["target"] in text and "ap." in text and "@example.com" in text
    row = next(a for a in actions.list_actions(fresh) if a["id"] == reminder["id"])
    assert row["status"] == "approved" and row["decided_by"] == "tester"
    # the same action cannot be decided twice, and no second file appears
    with pytest.raises(approvals.ApprovalError):
        approvals.decide(fresh, as_of, reminder["id"], "approved", outbox)
    assert len(list(outbox.glob("*.eml"))) == 1


def test_rejecting_or_approving_a_note_writes_no_file(fresh, as_of, tmp_path):
    run(fresh, as_of, tmp_path)
    outbox = tmp_path / "outbox"
    rows = actions.list_actions(fresh)
    email = next(a for a in rows if a["action_type"].startswith("reminder_email"))
    note = next(a for a in rows if a["action_type"] == "phone_call")
    approvals.decide(fresh, as_of, email["id"], "rejected", outbox)
    approvals.decide(fresh, as_of, note["id"], "approved", outbox)
    assert not list(outbox.glob("*.eml"))
    statuses = {a["id"]: a["status"] for a in actions.list_actions(fresh)}
    assert statuses[email["id"]] == "rejected" and statuses[note["id"]] == "approved"


def test_deciding_an_unknown_action_fails_cleanly(fresh, as_of, tmp_path):
    with pytest.raises(approvals.ApprovalError):
        approvals.decide(fresh, as_of, 99999, "approved", tmp_path / "outbox")


def test_pending_actions_tool_shows_them_but_cannot_decide(fresh, as_of, tmp_path):
    run(fresh, as_of, tmp_path)

    async def ask():
        async with Client(mcp) as client:
            return (await client.call_tool("list_pending_actions", {"limit": 20})).content[0].text

    text = asyncio.run(ask())
    assert text.startswith("[demo data] as of 2026-11-30")
    assert "26 actions are waiting" in text and "Claude cannot approve" in text
    assert len(text.split()) <= 300
    first = actions.list_actions(fresh)[0]
    approvals.decide(fresh, as_of, first["id"], "rejected", tmp_path / "outbox")
    assert "25 actions are waiting" in asyncio.run(ask())


def test_pending_actions_tool_when_nothing_is_waiting(fresh):
    async def ask():
        async with Client(mcp) as client:
            return (await client.call_tool("list_pending_actions", {})).content[0].text

    assert "Nothing is waiting for approval" in asyncio.run(ask())


# ----------------------------------------------------------------- consistency


def test_three_runs_find_the_same_issues(fresh, as_of, tmp_path):
    keysets, counts = [], []
    for _ in range(3):
        result = run(fresh, as_of, tmp_path, GoodLLM())
        keysets.append({p["key"] for p in result["proposals"]})
        counts.append(len(actions.list_actions(fresh)))
    assert keysets[0] == keysets[1] == keysets[2] and keysets[0]
    assert counts[0] == counts[1] == counts[2], "re-running must not pile up duplicate actions"
    assert all(len(r) > 0 for r in keysets)


def test_a_rerun_after_a_rejection_proposes_it_again(fresh, as_of, tmp_path):
    run(fresh, as_of, tmp_path)
    target = actions.list_actions(fresh)[0]
    approvals.decide(fresh, as_of, target["id"], "rejected", tmp_path / "outbox")
    run(fresh, as_of, tmp_path)
    again = [a for a in actions.list_actions(fresh)
             if a["action_type"] == target["action_type"] and a["target"] == target["target"]]
    assert sorted(a["status"] for a in again) == ["pending", "rejected"]


def test_same_data_same_drafts(fresh, dataset, as_of, tmp_path):
    run(fresh, as_of, tmp_path)
    first = [(a["action_type"], a["target"], a["draft_text"]) for a in actions.list_actions(fresh)]
    generate_data.load(fresh, dataset)
    run(fresh, as_of, tmp_path)
    assert first == [(a["action_type"], a["target"], a["draft_text"]) for a in actions.list_actions(fresh)]


# ----------------------------------------------- the double-check and the budget


def test_good_model_drafts_are_used_and_numbers_come_from_the_database(fresh, as_of, tmp_path):
    result = run(fresh, as_of, tmp_path, GoodLLM())
    emails = [p for p in result["proposals"] if p["kind"] == "email"]
    assert emails and all(p["source"] == "AI" for p in emails)
    assert result["discarded"] == []
    one = emails[0]
    assert one["facts"]["amount_owed"] in one["draft_text"] and one["facts"]["invoice_no"] in one["draft_text"]
    assert result["usage"]["calls"] == 2 and result["summary_source"].startswith("AI")


def test_invented_amounts_are_discarded_and_logged(fresh, as_of, tmp_path):
    result = run(fresh, as_of, tmp_path, HallucinatingLLM())
    emails = [p for p in result["proposals"] if p["kind"] == "email"]
    assert emails and all(p["source"] == "template (AI draft rejected)" for p in emails)
    assert all("SGD 1.00" not in p["draft_text"] for p in emails)
    assert any("does not match the database" in d for d in result["discarded"])
    assert "AI summary discarded" in " ".join(result["discarded"])
    assert "5,000.00" not in result["summary"]
    report = result["report_path"].read_text(encoding="utf-8")
    assert "discarded" in report.lower() and "SGD 5,000.00" not in report.split("## Summary")[1].split("## What")[0]


def test_garbage_model_still_gives_a_full_run_within_the_call_limit(fresh, as_of, tmp_path):
    llm = GarbageLLM()
    result = run(fresh, as_of, tmp_path, llm)
    assert llm.calls <= 3 and result["usage"]["calls"] == llm.calls
    assert len(result["proposals"]) == 26
    assert any("unavailable" in d for d in result["discarded"])


def test_broken_model_falls_back_to_templates(fresh, as_of, tmp_path):
    result = run(fresh, as_of, tmp_path, BrokenLLM())
    assert len(result["proposals"]) == 26
    assert all(p["source"] == "template" for p in result["proposals"] if p["kind"] == "email")


def test_budget_refuses_a_fourth_call():
    llm = BudgetedLLM(GoodLLM(), max_calls=3)
    for _ in range(3):
        llm.complete("s", "u")
    with pytest.raises(LLMBudgetExceeded):
        llm.complete("s", "u")
    assert llm.usage["calls"] == 3


def test_token_counts_are_added_up():
    llm = BudgetedLLM(GoodLLM(), max_calls=3)
    llm.complete("payment-reminder", json.dumps({"items": []}))
    llm.complete("s", "- a")
    assert llm.usage == {"calls": 2, "prompt_tokens": 400, "completion_tokens": 680, "total_tokens": 1080}


def test_llm_provider_comes_from_the_environment(monkeypatch):
    from finpulse.agents.llm import GEMINI_URL, OPENROUTER_URL, llm_from_env

    for name in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "FINPULSE_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert llm_from_env() is None
    monkeypatch.setenv("FINPULSE_MODEL", "some-model")
    assert llm_from_env() is None                       # a model alone is not enough
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    client = llm_from_env()
    assert client.provider == "gemini" and client.url == GEMINI_URL and client.model == "some-model"
    monkeypatch.setenv("OPENROUTER_API_KEY", "o-test")
    assert llm_from_env().url == OPENROUTER_URL         # OpenRouter wins if both are set


def test_policy_comes_from_the_file():
    policy = graph.load_policy()
    assert policy["reminders"]["days_late"] == [7, 30, 60]
    assert policy["phone_call"]["days_late"] == 60
    assert str(policy["bills"]["hold_tolerance"]) == "0.02"
    assert policy["llm"]["max_calls"] == 3 and policy["llm"]["temperature"] == 0


def test_the_report_lists_actions_and_says_nothing_was_sent(fresh, as_of, tmp_path):
    result = run(fresh, as_of, tmp_path)
    report = result["report_path"].read_text(encoding="utf-8")
    assert report.startswith("# Month-end check, as of 2026-11-30")
    assert "Unofficial concept" in report and "Nothing has been sent" in report
    assert "## Actions (26)" in report and "INV1043" in report


# ------------------------------------------------------------ verify_text units


KNOWN = {"SI-0001", "SI-0002", "INV-1043"}


def test_verify_text_accepts_matching_numbers():
    from decimal import Decimal
    text = "Invoice SI-0001 for SGD 1,250.50 is 40 days late."
    assert verify.verify_text(text, known_docs=KNOWN, allowed_docs={"SI-0001"},
                              allowed_amounts={Decimal("1250.50")}, allowed_days={40}) == []


def test_verify_text_rejects_each_kind_of_mistake():
    from decimal import Decimal
    kwargs = dict(known_docs=KNOWN, allowed_docs={"SI-0001"}, allowed_amounts={Decimal("1250.50")}, allowed_days={40})
    assert verify.verify_text("Invoice SI-9999", **kwargs)                     # does not exist
    assert verify.verify_text("Invoice SI-0002", **kwargs)                     # exists, wrong document
    assert verify.verify_text("SGD 1,250.51", **kwargs)                        # amount off by a cent
    assert verify.verify_text("S$99", **kwargs)                                # other currency notation
    assert verify.verify_text("It is 41 days late", **kwargs)                  # wrong days
    assert verify.verify_text("SI0001", **kwargs)                              # no dash: not a real number


def test_render_template_rejects_unknown_placeholders():
    with pytest.raises(ValueError):
        verify.render_template("Pay {total_due}", {})
    assert verify.render_template("Hi {customer}", {"customer": "A"}) == "Hi A"


def test_parse_json_tolerates_fences_and_prose():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Here you go: {"a": 1} thanks') == {"a": 1}
    with pytest.raises(LLMError):
        parse_json("no json here")
    with pytest.raises(LLMError):
        parse_json("[1, 2]")
