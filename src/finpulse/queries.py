"""All business logic: plain functions, parameterized SQL, Decimal math.

Nothing here knows about MCP, and nothing here uses the real clock: every
function takes `as_of`. The AI never does arithmetic; it only reads these results.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from psycopg.rows import dict_row

TOLERANCE = Decimal("0.02")  # price / quantity differences above 2% count as mismatches
STUCK_DAYS = 7               # a bill waiting longer than this is "stuck in approval"
HISTORY_DAYS = 183           # about 6 months of payment history
MAX_LIMIT = 20
MAX_FORECAST_DAYS = 90

BUCKET_LABELS = ["Not due yet", "1-30", "31-60", "61-90", "90+"]

# Every invoice with how much is still owed: amount - payments - credit notes.
_BALANCES = """
    SELECT i.id, i.invoice_no, i.customer_id, c.name AS customer, c.email AS customer_email,
           i.issue_date, i.due_date,
           i.amount,
           i.amount - COALESCE(p.paid, 0) - COALESCE(cr.credited, 0) AS owed
    FROM invoices i
    JOIN customers c ON c.id = i.customer_id
    LEFT JOIN (SELECT invoice_id, SUM(amount) AS paid FROM payments GROUP BY invoice_id) p
           ON p.invoice_id = i.id
    LEFT JOIN (SELECT invoice_id, SUM(amount) AS credited FROM credit_notes GROUP BY invoice_id) cr
           ON cr.invoice_id = i.id
"""


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def normalize_bill_no(bill_no: str) -> str:
    """'INV-1043' and 'inv1043' are the same number once punctuation and case are ignored."""
    return re.sub(r"[^a-z0-9]", "", bill_no.lower())


def _like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _rows(conn, query: str, params=None) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params or {})
        return cur.fetchall()


def bucket_for(days_late: int) -> str:
    if days_late <= 0:
        return "Not due yet"
    if days_late <= 30:
        return "1-30"
    if days_late <= 60:
        return "31-60"
    if days_late <= 90:
        return "61-90"
    return "90+"


# ---------------------------------------------------------------- receivables


def overdue_invoice_rows(conn, as_of: date, min_days_late: int = 1) -> list[dict]:
    """Every invoice with money still owed and at least `min_days_late` days late, worst first."""
    rows = _rows(
        conn,
        f"""
        WITH bal AS ({_BALANCES})
        SELECT id AS invoice_id, invoice_no, customer_id, customer, customer_email, due_date, amount, owed,
               %(as_of)s::date - due_date AS days_late
        FROM bal
        WHERE owed > 0 AND %(as_of)s::date - due_date >= %(min)s
        ORDER BY days_late DESC, owed DESC, invoice_no
        """,
        {"as_of": as_of, "min": min_days_late},
    )
    for r in rows:
        r["bucket"] = bucket_for(r["days_late"])
    return rows


def overdue_invoices(conn, as_of: date, min_days_late: int = 1, limit: int = 10) -> dict:
    limit = clamp(limit, 1, MAX_LIMIT)
    rows = overdue_invoice_rows(conn, as_of, min_days_late)
    buckets = []
    for label in BUCKET_LABELS:
        in_bucket = [r for r in rows if r["bucket"] == label]
        if in_bucket:
            buckets.append(
                {"label": label, "count": len(in_bucket), "total": sum((r["owed"] for r in in_bucket), Decimal("0.00"))}
            )
    per_customer: dict[str, dict] = {}
    for r in rows:
        entry = per_customer.setdefault(r["customer"], {"customer": r["customer"], "count": 0,
                                                        "total": Decimal("0.00"), "max_days_late": 0})
        entry["count"] += 1
        entry["total"] += r["owed"]
        entry["max_days_late"] = max(entry["max_days_late"], r["days_late"])
    by_customer = sorted(per_customer.values(), key=lambda e: (-e["total"], e["customer"]))
    return {
        "as_of": as_of,
        "min_days_late": min_days_late,
        "limit": limit,
        "by_customer": by_customer[:5],
        "customer_count": len(by_customer),
        "count": len(rows),
        "total": sum((r["owed"] for r in rows), Decimal("0.00")),
        "buckets": buckets,
        "rows": rows[:limit],
    }


def average_days_late(conn, as_of: date, customer_id: int | None = None) -> dict[int, Decimal]:
    """Average days late per customer over about the last 6 months, never below 0.

    Counts invoices that fell due in that window: settled ones count how late the
    last payment was; unpaid ones count how late they are today. Invoices not yet
    due are ignored. Customers with no such invoices are missing from the result.
    """
    rows = _rows(
        conn,
        f"""
        WITH bal AS ({_BALANCES})
        SELECT b.customer_id, b.due_date, b.owed,
               (SELECT MAX(p.paid_on) FROM payments p WHERE p.invoice_id = b.id) AS last_paid
        FROM bal b
        WHERE b.due_date BETWEEN %(start)s AND %(as_of)s
          AND (%(cid)s::int IS NULL OR b.customer_id = %(cid)s::int)
        """,
        {"start": as_of - timedelta(days=HISTORY_DAYS), "as_of": as_of, "cid": customer_id},
    )
    samples: dict[int, list[int]] = {}
    for r in rows:
        if r["owed"] > 0:
            late = (as_of - r["due_date"]).days
        elif r["last_paid"] is not None:
            late = (r["last_paid"] - r["due_date"]).days
        else:
            continue
        samples.setdefault(r["customer_id"], []).append(max(0, late))
    return {
        cid: (Decimal(sum(vals)) / len(vals)).quantize(Decimal("0.1"), ROUND_HALF_UP)
        for cid, vals in samples.items()
    }


def customer_summary(conn, as_of: date, customer_name: str) -> dict:
    name = (customer_name or "").strip()
    if not name:
        return {"status": "not_found", "query": name, "suggestions": []}
    matches = _rows(
        conn,
        "SELECT id, name FROM customers WHERE name ILIKE %s ESCAPE '\\' ORDER BY name",
        (_like_pattern(name),),
    )
    if not matches:
        first_word = name.split()[0]
        suggestions = _rows(
            conn,
            "SELECT name FROM customers WHERE name ILIKE %s ESCAPE '\\' ORDER BY name LIMIT 5",
            (_like_pattern(first_word[:4]),),
        )
        return {"status": "not_found", "query": name, "suggestions": [s["name"] for s in suggestions]}
    if len(matches) > 1:
        return {"status": "ambiguous", "query": name, "match_count": len(matches),
                "matches": [m["name"] for m in matches[:MAX_LIMIT]]}

    cust = matches[0]
    invoices = _rows(
        conn,
        f"""
        WITH bal AS ({_BALANCES})
        SELECT invoice_no, due_date, amount, owed, %(as_of)s::date - due_date AS days_late
        FROM bal WHERE customer_id = %(cid)s AND owed > 0
        ORDER BY days_late DESC, owed DESC, invoice_no
        """,
        {"as_of": as_of, "cid": cust["id"]},
    )
    last_payment = _rows(
        conn,
        """
        SELECT p.paid_on, p.amount, i.invoice_no
        FROM payments p JOIN invoices i ON i.id = p.invoice_id
        WHERE i.customer_id = %s ORDER BY p.paid_on DESC, p.id DESC LIMIT 1
        """,
        (cust["id"],),
    )
    terms = _rows(conn, "SELECT payment_terms_days FROM customers WHERE id = %s", (cust["id"],))[0]
    avg = average_days_late(conn, as_of, cust["id"]).get(cust["id"])
    return {
        "status": "ok",
        "as_of": as_of,
        "customer_id": cust["id"],
        "name": cust["name"],
        "payment_terms_days": terms["payment_terms_days"],
        "total_owed": sum((i["owed"] for i in invoices), Decimal("0.00")),
        "open_count": len(invoices),
        "overdue_count": sum(1 for i in invoices if i["days_late"] > 0),
        "average_days_late": avg,
        "invoices": invoices[:5],
        "last_payment": last_payment[0] if last_payment else None,
    }


# ----------------------------------------------------------------- cash forecast


def cash_forecast(conn, as_of: date, days: int = 14) -> dict:
    days = clamp(days, 1, MAX_FORECAST_DAYS)
    end = as_of + timedelta(days=days)
    averages = average_days_late(conn, as_of)
    open_invoices = _rows(
        conn,
        f"WITH bal AS ({_BALANCES}) SELECT * FROM bal WHERE owed > 0 ORDER BY due_date, invoice_no",
    )
    money_in, past_expected = [], []
    no_history = 0
    for inv in open_invoices:
        avg = averages.get(inv["customer_id"])
        if avg is None:
            no_history += 1
            avg = Decimal("0")
        expected = inv["due_date"] + timedelta(days=int(avg.to_integral_value(ROUND_CEILING)))
        inv["expected_on"] = expected
        if as_of <= expected <= end:
            money_in.append(inv)
        elif expected < as_of:
            past_expected.append(inv)
    money_in.sort(key=lambda r: (-r["owed"], r["invoice_no"]))

    money_out = _rows(
        conn,
        """
        SELECT b.bill_no, s.name AS supplier, b.due_date, b.amount
        FROM bills b JOIN suppliers s ON s.id = b.supplier_id
        WHERE b.status = 'approved' AND b.due_date <= %s
        ORDER BY b.amount DESC, b.bill_no
        """,
        (end,),
    )
    total_in = sum((r["owed"] for r in money_in), Decimal("0.00"))
    total_out = sum((r["amount"] for r in money_out), Decimal("0.00"))
    return {
        "as_of": as_of,
        "days": days,
        "end": end,
        "money_in": money_in,
        "total_in": total_in,
        "money_out": money_out,
        "total_out": total_out,
        "already_due_bills": sum(1 for b in money_out if b["due_date"] < as_of),
        "net": total_in - total_out,
        "past_expected_count": len(past_expected),
        "past_expected_total": sum((r["owed"] for r in past_expected), Decimal("0.00")),
        "customers_without_history": no_history,
    }


# ------------------------------------------------------------------ payables


def stuck_bills(conn, as_of: date, min_days: int = STUCK_DAYS) -> list[dict]:
    return _rows(
        conn,
        """
        SELECT b.bill_no, s.name AS supplier, b.amount,
               b.submitted_for_approval_on AS submitted_on,
               %(as_of)s::date - b.submitted_for_approval_on AS days_waiting
        FROM bills b JOIN suppliers s ON s.id = b.supplier_id
        WHERE b.status = 'pending_approval' AND %(as_of)s::date - b.submitted_for_approval_on > %(days)s
        ORDER BY days_waiting DESC, b.bill_no
        """,
        {"as_of": as_of, "days": min_days},
    )


def possible_duplicate_bills(conn) -> list[dict]:
    """Pairs of live bills with the same supplier and amount."""
    rows = _rows(
        conn,
        """
        SELECT a.bill_no AS bill_no, b.bill_no AS other_bill_no, s.name AS supplier, a.amount,
               a.status AS status, b.status AS other_status
        FROM bills a
        JOIN bills b ON b.supplier_id = a.supplier_id AND b.amount = a.amount AND b.id > a.id
        JOIN suppliers s ON s.id = a.supplier_id
        WHERE a.status <> 'rejected' AND b.status <> 'rejected'
        ORDER BY a.amount DESC, a.bill_no
        """,
    )
    for r in rows:
        r["same_number"] = normalize_bill_no(r["bill_no"]) == normalize_bill_no(r["other_bill_no"])
    return rows


def bills_without_po(conn) -> list[dict]:
    return _rows(
        conn,
        """
        SELECT b.bill_no, s.name AS supplier, b.amount, b.status
        FROM bills b JOIN suppliers s ON s.id = b.supplier_id
        WHERE b.po_id IS NULL AND b.status IN ('pending_approval', 'approved')
        ORDER BY b.amount DESC, b.bill_no
        """,
    )


def bills_needing_attention(conn, as_of: date, limit: int = 10) -> dict:
    limit = clamp(limit, 1, MAX_LIMIT)
    stuck = stuck_bills(conn, as_of)
    dups = possible_duplicate_bills(conn)
    no_po = bills_without_po(conn)
    return {
        "as_of": as_of,
        "limit": limit,
        "stuck": stuck[:limit],
        "stuck_count": len(stuck),
        "duplicates": dups[:limit],
        "duplicate_count": len(dups),
        "no_po": no_po[:limit],
        "no_po_count": len(no_po),
    }


def _find_bill(conn, bill_no: str):
    """Exact match first (ignoring case), then a match that ignores punctuation."""
    cols = """b.id, b.bill_no, b.supplier_id, s.name AS supplier, b.po_id, b.bill_date,
              b.due_date, b.amount, b.status"""
    base = f"SELECT {cols} FROM bills b JOIN suppliers s ON s.id = b.supplier_id"
    exact = _rows(conn, base + " WHERE lower(b.bill_no) = lower(%s)", (bill_no,))
    if exact:
        return exact
    return _rows(
        conn,
        base + " WHERE lower(regexp_replace(b.bill_no, '[^A-Za-z0-9]', '', 'g')) = %s ORDER BY b.bill_no",
        (normalize_bill_no(bill_no),),
    )


def check_bill(conn, as_of: date, bill_no: str, tolerance: Decimal = TOLERANCE) -> dict:
    bill_no = (bill_no or "").strip()
    found = _find_bill(conn, bill_no) if bill_no else []
    if not found:
        like = normalize_bill_no(bill_no)[:3] if bill_no else ""
        suggestions = []
        if like:
            suggestions = [
                r["bill_no"]
                for r in _rows(
                    conn,
                    "SELECT bill_no FROM bills WHERE bill_no ILIKE %s ESCAPE '\\' ORDER BY bill_no LIMIT 5",
                    (_like_pattern(bill_no[:3]),),
                )
            ]
        return {"status": "not_found", "query": bill_no, "suggestions": suggestions}
    if len(found) > 1:
        return {"status": "ambiguous", "query": bill_no, "matches": [b["bill_no"] for b in found]}

    bill = found[0]
    bill_lines = _rows(
        conn, "SELECT item, qty, unit_price FROM bill_lines WHERE bill_id = %s ORDER BY id", (bill["id"],)
    )
    items: dict[str, dict] = {
        ln["item"]: {"item": ln["item"], "billed_qty": ln["qty"], "billed_price": ln["unit_price"],
                     "ordered_qty": None, "po_price": None, "received_qty": 0}
        for ln in bill_lines
    }
    issues: list[str] = []
    po_no = None
    if bill["po_id"] is None:
        issues.append("no PO")
    else:
        po_no = _rows(conn, "SELECT po_no FROM purchase_orders WHERE id = %s", (bill["po_id"],))[0]["po_no"]
        for ln in _rows(conn, "SELECT item, qty, unit_price FROM po_lines WHERE po_id = %s ORDER BY id", (bill["po_id"],)):
            entry = items.setdefault(ln["item"], {"item": ln["item"], "billed_qty": 0, "billed_price": None,
                                                  "ordered_qty": None, "po_price": None, "received_qty": 0})
            entry["ordered_qty"], entry["po_price"] = ln["qty"], ln["unit_price"]
        for ln in _rows(
            conn,
            """
            SELECT rl.item, SUM(rl.qty_received) AS qty_received
            FROM receipt_lines rl JOIN goods_receipts gr ON gr.id = rl.receipt_id
            WHERE gr.po_id = %s GROUP BY rl.item
            """,
            (bill["po_id"],),
        ):
            if ln["item"] in items:
                items[ln["item"]]["received_qty"] = int(ln["qty_received"])
        for entry in items.values():
            entry["price_diff_pct"] = None
            if entry["po_price"] and entry["billed_price"] is not None:
                diff = (entry["billed_price"] - entry["po_price"]) / entry["po_price"]
                entry["price_diff_pct"] = (diff * 100).quantize(Decimal("0.1"), ROUND_HALF_UP)
                if diff > tolerance and "price mismatch" not in issues:
                    issues.append("price mismatch")
            if entry["billed_qty"] > entry["received_qty"] * (1 + tolerance) and "quantity mismatch" not in issues:
                issues.append("quantity mismatch")

    duplicates = _rows(
        conn,
        """
        SELECT bill_no, status FROM bills
        WHERE supplier_id = %s AND amount = %s AND id <> %s AND status <> 'rejected' ORDER BY bill_no
        """,
        (bill["supplier_id"], bill["amount"], bill["id"]),
    )
    if duplicates and bill["status"] != "rejected":
        issues.insert(0, "possible duplicate")
    return {
        "status": "ok",
        "as_of": as_of,
        "bill": bill,
        "po_no": po_no,
        "lines": list(items.values()),
        "issues": issues,
        "verdict": "OK" if not issues else ", ".join(issues),
        "duplicates": duplicates,
    }


# ------------------------------------------------------------ pending actions


def list_pending_actions(conn, as_of: date, limit: int = 10) -> dict:
    """Actions the month-end check proposed that no person has decided yet. Read-only."""
    limit = clamp(limit, 1, MAX_LIMIT)
    rows = _rows(
        conn,
        """
        SELECT a.id, a.action_type, a.target_type, a.draft_text,
               COALESCE(i.invoice_no, b.bill_no, c.name) AS target
        FROM actions a
        LEFT JOIN invoices  i ON a.target_type = 'invoice'  AND i.id = a.target_id
        LEFT JOIN bills     b ON a.target_type = 'bill'     AND b.id = a.target_id
        LEFT JOIN customers c ON a.target_type = 'customer' AND c.id = a.target_id
        WHERE a.status = 'pending'
        ORDER BY a.id
        """,
    )
    by_type: dict[str, int] = {}
    for r in rows:
        by_type[r["action_type"]] = by_type.get(r["action_type"], 0) + 1
        lines = [l for l in r["draft_text"].splitlines() if l.strip()]
        subject = next((l[len("Subject:"):].strip() for l in lines if l.startswith("Subject:")), None)
        r["summary"] = subject or (lines[0] if lines else "")
    return {"as_of": as_of, "limit": limit, "count": len(rows), "by_type": by_type, "rows": rows[:limit]}
