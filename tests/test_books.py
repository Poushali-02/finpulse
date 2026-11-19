"""Phase 1: prove the generated books add up."""

from decimal import Decimal

import generate_data
from psycopg import sql

ZERO = Decimal("0.00")


def test_invoice_balances(conn):
    rows = conn.execute(
        """
        SELECT i.invoice_no, i.amount,
               COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.invoice_id = i.id), 0),
               COALESCE((SELECT SUM(c.amount) FROM credit_notes c WHERE c.invoice_id = i.id), 0)
        FROM invoices i
        """
    ).fetchall()
    assert len(rows) > 0
    for invoice_no, amount, paid, credited in rows:
        owed = amount - paid - credited
        assert owed >= ZERO, f"{invoice_no}: payments + credits exceed the invoice"
        assert paid + credited + owed == amount, invoice_no


def test_invoice_status_matches_payments(conn):
    rows = conn.execute(
        """
        SELECT i.invoice_no, i.status, i.amount,
               COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.invoice_id = i.id), 0),
               COALESCE((SELECT SUM(c.amount) FROM credit_notes c WHERE c.invoice_id = i.id), 0)
        FROM invoices i
        """
    ).fetchall()
    for invoice_no, status, amount, paid, credited in rows:
        if paid + credited >= amount:
            expected = "paid"
        elif paid > 0:
            expected = "partially_paid"
        else:
            expected = "open"
        assert status == expected, invoice_no


def test_bill_amount_equals_sum_of_lines(conn):
    rows = conn.execute(
        """
        SELECT b.bill_no, b.amount, COALESCE(SUM(l.qty * l.unit_price), 0)
        FROM bills b LEFT JOIN bill_lines l ON l.bill_id = b.id
        GROUP BY b.id, b.bill_no, b.amount
        """
    ).fetchall()
    assert rows
    for bill_no, amount, line_sum in rows:
        assert amount == line_sum, bill_no


def test_po_total_equals_sum_of_lines(conn):
    rows = conn.execute(
        """
        SELECT p.po_no, p.total, COALESCE(SUM(l.qty * l.unit_price), 0)
        FROM purchase_orders p LEFT JOIN po_lines l ON l.po_id = p.id
        GROUP BY p.id, p.po_no, p.total
        """
    ).fetchall()
    assert rows
    for po_no, total, line_sum in rows:
        assert total == line_sum, po_no


def test_sizes_and_emails(conn):
    count = lambda t: conn.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(t))).fetchone()[0]
    assert count("customers") == 25
    assert count("suppliers") == 12
    assert 290 <= count("invoices") <= 320
    assert count("purchase_orders") == 120
    bad = conn.execute(
        """
        SELECT email FROM customers WHERE email NOT LIKE '%@example.com'
        UNION ALL SELECT email FROM suppliers WHERE email NOT LIKE '%@example.com'
        """
    ).fetchall()
    assert bad == []


def test_dataset_is_deterministic(dataset):
    assert generate_data.build_dataset() == dataset
    assert generate_data.planted_json(generate_data.build_dataset()) == generate_data.planted_json(dataset)


def test_database_load_is_identical_twice(conn, dataset):
    def dump():
        return [
            conn.execute(sql.SQL("SELECT * FROM {} ORDER BY id").format(sql.Identifier(t))).fetchall()
            for t in generate_data.TABLES
        ]

    first = dump()
    generate_data.load(conn, dataset)
    assert dump() == first


def test_planted_file_is_up_to_date(dataset):
    on_disk = generate_data.PLANTED_PATH.read_text(encoding="utf-8")
    assert on_disk == generate_data.planted_json(dataset), "run scripts/generate_data.py again"
