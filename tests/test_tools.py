"""Phase 3: the MCP tools. One test per tool, answer-key tests, and guardrails."""

import asyncio
import json
import subprocess
import sys
import threading

import psycopg
import pytest
from fastmcp import Client

from finpulse.db import connect_readonly
from finpulse.server import mcp

TOOL_NAMES = {"overdue_invoices", "customer_summary", "cash_forecast", "bills_needing_attention", "check_bill",
              "list_pending_actions"}


@pytest.fixture(autouse=True)
def _loaded(conn):
    """Make sure the database holds the generated books before every test."""


def call(name: str, **args) -> str:
    async def go():
        async with Client(mcp) as client:
            result = await client.call_tool(name, args)
            return result.content[0].text

    return asyncio.run(go())


def key(dataset):
    return {p["key"]: p for p in dataset["planted"]["problems"]}


def assert_standard_answer(text: str):
    assert text.startswith("[demo data] as of 2026-11-30")
    assert len(text.split()) <= 300


# ---------------------------------------------------------------- one per tool


def test_overdue_invoices(dataset):
    text = call("overdue_invoices")
    assert_standard_answer(text)
    assert "SGD" in text and "days late" in text and "By days late:" in text


def test_customer_summary(dataset):
    name = key(dataset)["late_customers"]["customers"][0]["customer_name"]
    text = call("customer_summary", customer_name=name)
    assert_standard_answer(text)
    assert name in text and "Total owed: SGD" in text and "Average days late" in text and "Last payment" in text


def test_cash_forecast():
    text = call("cash_forecast", days=14)
    assert_standard_answer(text)
    assert "Money in: SGD" in text and "Money out: SGD" in text and "Net: SGD" in text
    assert "estimate" in text


def test_bills_needing_attention():
    text = call("bills_needing_attention")
    assert_standard_answer(text)
    assert "Stuck in approval" in text and "Possible duplicates" in text and "no purchase order" in text


def test_check_bill():
    text = call("check_bill", bill_no="INV-2001")
    assert_standard_answer(text)
    assert "Verdict:" in text and "Three-way match" in text


# ------------------------------------------------------------- answer-key tests


def test_overdue_lists_the_three_late_customers_and_their_invoices(dataset):
    late = key(dataset)["late_customers"]["customers"]
    text = call("overdue_invoices", min_days_late=61, limit=20)
    for customer in late:
        assert customer["customer_name"] in text
        for inv in customer["overdue_invoices"]:
            assert inv["invoice_no"] in text, inv


def test_bills_needing_attention_catches_stuck_duplicate_and_no_po(dataset):
    k = key(dataset)
    text = call("bills_needing_attention")
    for bill in k["stuck_in_approval"]["bills"]:
        assert bill["bill_no"] in text
    for bill_no in k["duplicate_bill"]["bill_nos"]:
        assert bill_no in text
    assert k["bill_without_po"]["bill_no"] in text


@pytest.mark.parametrize(
    "problem, expected",
    [
        ("price_mismatch", "price mismatch"),
        ("quantity_mismatch", "quantity mismatch"),
        ("bill_without_po", "no PO"),
    ],
)
def test_check_bill_catches_planted_bill(dataset, problem, expected):
    bill_no = key(dataset)[problem]["bill_no"]
    text = call("check_bill", bill_no=bill_no)
    verdict = next(line for line in text.splitlines() if line.startswith("Verdict:"))
    assert expected in verdict, text


@pytest.mark.parametrize("bill_no", ["INV-1043", "INV1043", "inv-1043"])
def test_check_bill_flags_both_sides_of_the_duplicate(bill_no):
    verdict = next(l for l in call("check_bill", bill_no=bill_no).splitlines() if l.startswith("Verdict:"))
    assert "possible duplicate" in verdict


def test_check_bill_ok_for_a_clean_bill():
    # a paid bill from the generated set that no planted problem touches
    with connect_readonly() as conn:
        bill_no = conn.execute(
            "SELECT bill_no FROM bills WHERE status = 'paid' AND po_id IS NOT NULL ORDER BY id LIMIT 1"
        ).fetchone()[0]
    verdict = next(l for l in call("check_bill", bill_no=bill_no).splitlines() if l.startswith("Verdict:"))
    assert verdict == "Verdict: OK."


def test_every_non_planted_bill_is_ok_or_planted(dataset):
    """No false alarms: only the planted bills get a verdict other than OK."""
    k = key(dataset)
    planted = {"INV-1043", "INV1043", k["price_mismatch"]["bill_no"], k["quantity_mismatch"]["bill_no"],
               k["bill_without_po"]["bill_no"]}
    with connect_readonly() as conn:
        bill_nos = [r[0] for r in conn.execute("SELECT bill_no FROM bills ORDER BY id").fetchall()]
    flagged = set()
    for bill_no in bill_nos:
        text = call("check_bill", bill_no=bill_no)
        if "Verdict: OK." not in text:
            flagged.add(bill_no)
    assert flagged == planted


# ------------------------------------------------------------------- guardrails


def test_limit_is_capped_at_20():
    text = call("overdue_invoices", min_days_late=0, limit=500)
    assert sum(1 for line in text.splitlines() if line.startswith("- ")) <= 20
    text = call("overdue_invoices", limit=0)  # too small is raised to 1, not an error
    assert sum(1 for line in text.splitlines() if line.startswith("- ")) == 1


def test_unknown_customer_gets_a_helpful_message():
    text = call("customer_summary", customer_name="Zzyzx Nonexistent")
    assert_standard_answer(text)
    assert "No customer matches" in text and "Try" in text


def test_ambiguous_customer_asks_which_one():
    text = call("customer_summary", customer_name="Pte")
    assert "Which one do you mean?" in text


def test_unknown_bill_gets_a_helpful_message():
    text = call("check_bill", bill_no="NOPE-0")
    assert "No bill numbered" in text and "bills_needing_attention" in text


def test_no_result_says_what_to_try_next():
    text = call("overdue_invoices", min_days_late=100000)
    assert "Try a smaller min_days_late" in text


def test_percent_and_underscore_in_a_name_do_not_act_as_wildcards():
    assert "No customer matches" in call("customer_summary", customer_name="%")
    assert "No customer matches" in call("customer_summary", customer_name="_")


def test_empty_customer_name_is_handled():
    assert "No customer matches" in call("customer_summary", customer_name="   ")


def test_six_tools_all_read_only_with_simple_inputs():
    async def tools():
        async with Client(mcp) as client:
            return await client.list_tools()

    listed = asyncio.run(tools())
    assert {t.name for t in listed} == TOOL_NAMES
    for t in listed:
        assert t.description and len(t.description) > 80
        assert t.annotations.read_only_hint is True
        for prop in t.input_schema.get("properties", {}).values():
            assert prop.get("type") in {"integer", "string"}, (t.name, prop)


def test_the_database_connection_refuses_writes():
    with connect_readonly() as conn:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("DELETE FROM payments")


def test_nothing_but_json_rpc_on_stdout():
    """Over stdio, stdout must carry only protocol messages. Anything else breaks the connection."""
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "overdue_invoices", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "check_bill", "arguments": {"bill_no": "NOPE"}}},
    ]
    proc = subprocess.Popen(
        [sys.executable, "-m", "finpulse.server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    watchdog = threading.Timer(60, proc.kill)
    watchdog.start()
    lines = []
    try:
        proc.stdin.write("\n".join(json.dumps(m) for m in messages) + "\n")
        proc.stdin.flush()
        # keep stdin open until both tool answers have arrived, then close it
        while not {2, 3} <= {json.loads(l).get("id") for l in lines}:
            line = proc.stdout.readline()
            assert line, "server exited early"
            if line.strip():
                lines.append(line)
        proc.stdin.close()
        lines += [l for l in proc.stdout.read().splitlines() if l.strip()]
    finally:
        watchdog.cancel()
        proc.kill()
        proc.wait()
    for line in lines:
        assert json.loads(line)["jsonrpc"] == "2.0", line  # nothing but protocol messages
