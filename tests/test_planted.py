"""Phase 1: the answer key (planted.json) is true in the database, and nothing else is broken."""

from decimal import Decimal

import pytest

AS_OF_SQL = "%(as_of)s::date"


@pytest.fixture(scope="module")
def key(dataset):
    return {p["key"]: p for p in dataset["planted"]["problems"]}


@pytest.fixture(scope="module")
def as_of(dataset):
    return dataset["as_of"]


def test_every_problem_has_a_kind(dataset):
    problems = dataset["planted"]["problems"]
    assert [p["number"] for p in problems] == list(range(1, 9))
    assert {p["kind"] for p in problems} == {"agent_target", "data_case"}
    assert {p["key"] for p in problems if p["kind"] == "data_case"} == {"partial_payment", "credit_note"}


def test_three_late_customers_have_open_invoices_61_plus_days_late(conn, key, as_of):
    late_ids = {c["customer_id"] for c in key["late_customers"]["customers"]}
    assert len(late_ids) == 3
    rows = conn.execute(
        """
        SELECT i.customer_id, COUNT(*)
        FROM invoices i
        WHERE i.status <> 'paid' AND %(as_of)s::date - i.due_date >= 61
        GROUP BY i.customer_id
        """,
        {"as_of": as_of},
    ).fetchall()
    assert {cid for cid, _ in rows} == late_ids
    assert all(n >= 3 for _, n in rows)


def test_healthy_customers_have_no_invoice_7_plus_days_late(conn, key, as_of):
    late_ids = [c["customer_id"] for c in key["late_customers"]["customers"]]
    rows = conn.execute(
        """
        SELECT invoice_no FROM invoices
        WHERE status <> 'paid' AND %(as_of)s::date - due_date >= 7
          AND customer_id <> ALL(%(late)s)
        """,
        {"as_of": as_of, "late": late_ids},
    ).fetchall()
    assert rows == []


def test_partial_payment_and_credit_note_invoices(conn, key, as_of):
    partial = key["partial_payment"]
    status, due = conn.execute(
        "SELECT status, due_date FROM invoices WHERE invoice_no = %s", (partial["invoice_no"],)
    ).fetchone()
    assert status == "partially_paid" and due > as_of
    credit = key["credit_note"]
    status, due, credited = conn.execute(
        """
        SELECT i.status, i.due_date, SUM(c.amount)
        FROM invoices i JOIN credit_notes c ON c.invoice_id = i.id
        WHERE i.invoice_no = %s GROUP BY i.id
        """,
        (credit["invoice_no"],),
    ).fetchone()
    assert status == "open" and due > as_of and credited == Decimal(credit["credit"])
    assert conn.execute("SELECT COUNT(*) FROM credit_notes").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM invoices WHERE status = 'partially_paid'").fetchone()[0] == 1


def test_duplicate_bill(conn, key):
    dup = key["duplicate_bill"]
    rows = conn.execute(
        """
        SELECT bill_no, supplier_id, amount FROM bills
        WHERE bill_no = ANY(%s) ORDER BY bill_no
        """,
        (dup["bill_nos"],),
    ).fetchall()
    assert len(rows) == 2
    assert rows[0][1] == rows[1][1] == dup["supplier_id"]
    assert rows[0][2] == rows[1][2] == Decimal(dup["amount"])
    # exactly one duplicate group: same supplier, same amount, same number once punctuation is stripped
    groups = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT supplier_id, amount, lower(regexp_replace(bill_no, '[^A-Za-z0-9]', '', 'g'))
            FROM bills GROUP BY 1, 2, 3 HAVING COUNT(*) > 1
        ) g
        """
    ).fetchone()[0]
    assert groups == 1


def test_price_mismatch_is_over_2_percent_and_unique(conn, key):
    p = key["price_mismatch"]
    assert Decimal(p["billed_unit_price"]) > Decimal(p["po_unit_price"]) * Decimal("1.02")
    rows = conn.execute(
        """
        SELECT b.bill_no FROM bills b
        JOIN po_lines pl ON pl.po_id = b.po_id
        JOIN bill_lines bl ON bl.bill_id = b.id AND bl.item = pl.item
        WHERE bl.unit_price <> pl.unit_price
        """
    ).fetchall()
    assert rows == [(p["bill_no"],)]


def test_quantity_mismatch_is_over_2_percent_and_unique(conn, key):
    q = key["quantity_mismatch"]
    assert q["qty_billed"] > q["qty_received"] * 1.02
    rows = conn.execute(
        """
        SELECT b.bill_no FROM bills b
        JOIN goods_receipts gr ON gr.po_id = b.po_id
        JOIN receipt_lines rl ON rl.receipt_id = gr.id
        JOIN bill_lines bl ON bl.bill_id = b.id AND bl.item = rl.item
        WHERE bl.qty <> rl.qty_received
        """
    ).fetchall()
    assert rows == [(q["bill_no"],)]


def test_exactly_one_bill_without_po(conn, key):
    rows = conn.execute("SELECT bill_no FROM bills WHERE po_id IS NULL").fetchall()
    assert rows == [(key["bill_without_po"]["bill_no"],)]


def test_exactly_two_bills_stuck_in_approval(conn, key, as_of):
    rows = conn.execute(
        """
        SELECT bill_no FROM bills
        WHERE status = 'pending_approval' AND %(as_of)s::date - submitted_for_approval_on > 7
        ORDER BY bill_no
        """,
        {"as_of": as_of},
    ).fetchall()
    assert sorted(r[0] for r in rows) == sorted(b["bill_no"] for b in key["stuck_in_approval"]["bills"])
    assert all(b["days_waiting"] > 7 for b in key["stuck_in_approval"]["bills"])
