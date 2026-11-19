"""Generate the fake books for "Lion City Supplies Pte Ltd" (made-up data, SGD).

Deterministic: a fixed random seed, and every date is an offset from AS_OF,
never the real clock. Running it twice gives identical data.

    uv run python scripts/generate_data.py

It resets the FinPulse tables, loads the data, and writes data/planted.json,
the answer key listing the problems hidden on purpose.
"""

from __future__ import annotations

import json
import random
import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from psycopg import sql

from finpulse.db import connect, get_as_of

SEED = 42
ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "db" / "schema.sql"
PLANTED_PATH = ROOT / "data" / "planted.json"

TABLES = [
    "customers", "invoices", "payments", "credit_notes",
    "suppliers", "purchase_orders", "po_lines", "goods_receipts",
    "receipt_lines", "bills", "bill_lines",
]

CENT = Decimal("0.01")

ADJECTIVES = [
    "Harbour", "Lantern", "Coral", "Saffron", "Monsoon", "Teak", "Orchid", "Jade",
    "Granite", "Indigo", "Pelican", "Kestrel", "Marigold", "Tidewater", "Cobalt",
    "Amber", "Juniper", "Sandalwood", "Falcon", "Willow", "Crimson", "Banyan",
    "Cedar", "Magnolia", "Quartz", "Lotus", "Ember", "Cypress", "Opal", "Starling",
    "Mariner", "Frangipani", "Basalt", "Sparrow", "Topaz", "Linden", "Garnet",
    "Heron", "Zephyr",
]
CUSTOMER_NOUNS = [
    "Foods", "Logistics", "Trading", "Hardware", "Interiors", "Cafes", "Print",
    "Medical", "Garments", "Electronics", "Packaging", "Cleaning", "Retail",
    "Catering", "Furnishings",
]
SUPPLIER_NOUNS = [
    "Plastics", "Steel", "Paper", "Chemicals", "Textiles", "Machinery",
    "Fasteners", "Lighting", "Wholesale", "Industrial",
]

# item -> base unit price (SGD)
ITEMS = {
    "A4 paper ream": "4.50",
    "Cardboard box (L)": "1.20",
    "Bubble wrap roll": "18.00",
    "Pallet wrap": "9.50",
    "Steel shelf bracket": "6.75",
    "LED panel 40W": "32.00",
    "Nitrile gloves (box)": "11.00",
    "Thermal labels (roll)": "7.20",
    "Packing tape (6 pack)": "13.50",
    "Safety boots": "48.00",
    "Hand pallet truck": "420.00",
    "Cable ties (100)": "3.10",
    "Stretch film": "15.00",
    "Barcode scanner": "95.00",
    "Cleaning solvent 5L": "22.00",
}

# zero-based customer positions used for planted problems
LATE_CUSTOMERS = (3, 11, 19)
PARTIAL_CUSTOMER = 5
CREDIT_CUSTOMER = 8
# days before AS_OF that the late customers' still-unpaid invoices were issued
# (Net 30, so they are 109, 88 and 67 days late)
LATE_ISSUE_AGO = (139, 118, 97)


def money(value) -> Decimal:
    return Decimal(value).quantize(CENT, ROUND_HALF_UP)


def days(n: int) -> timedelta:
    return timedelta(days=n)


def make_email(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", ".", name.lower().replace(" pte ltd", "")).strip(".")
    return f"ap.{slug}@example.com"


def invoice_status(amount: Decimal, paid: Decimal, credited: Decimal) -> str:
    if paid + credited >= amount:
        return "paid"
    if paid > 0:
        return "partially_paid"
    return "open"


def normal_payment_date(rng, issue: date, due: date, as_of: date) -> date | None:
    """Mostly on time, some a little late. Healthy customers are never 7+ days late and unpaid."""
    delay = rng.randint(-5, 3) if rng.random() < 0.75 else rng.randint(4, 15)
    pay = max(issue + days(1), due + days(delay))
    if pay > as_of:
        if (as_of - due).days >= 7:
            return max(issue + days(1), as_of - days(rng.randint(0, 2)))
        return None
    return pay


def late_payment_date(rng, due: date, as_of: date) -> date | None:
    pay = due + days(rng.randint(40, 55))
    return pay if pay <= as_of else None


def bill_status(bill_date: date, due: date, as_of: date) -> tuple[str, date]:
    """Healthy bills: old ones paid, recent ones awaiting approval for 5 days or less."""
    if due < as_of - days(5):
        return "paid", min(bill_date + days(1), as_of)
    if bill_date >= as_of - days(5):
        return "pending_approval", bill_date
    return "approved", min(bill_date + days(1), as_of)


def make_po(rng, as_of: date, supplier: dict, ordered_on: date) -> dict:
    lines = []
    for item in rng.sample(sorted(ITEMS), rng.randint(1, 4)):
        price = money(Decimal(ITEMS[item]) * Decimal(rng.randint(90, 110)) / 100)
        lines.append({"item": item, "qty": rng.randint(5, 200), "unit_price": price})
    received_on = min(ordered_on + days(rng.randint(3, 9)), as_of)
    bill_date = min(received_on + days(rng.randint(1, 4)), as_of)
    return {
        "supplier": supplier,
        "ordered_on": ordered_on,
        "lines": lines,
        "received_on": received_on,
        "receipt_qty": {ln["item"]: ln["qty"] for ln in lines},
        "bill_date": bill_date,
        "bill_lines": [dict(ln) for ln in lines],
    }


def lines_total(lines) -> Decimal:
    return sum((ln["qty"] * ln["unit_price"] for ln in lines), Decimal("0.00"))


def build_dataset(seed: int = SEED, as_of: date | None = None) -> dict:
    """Pure function: builds every table row in memory. No database, no clock."""
    rng = random.Random(seed)
    as_of = as_of or get_as_of()

    # ---- customers and suppliers --------------------------------------
    adjectives = rng.sample(ADJECTIVES, 37)
    customers = []
    for i in range(25):
        name = f"{adjectives[i]} {rng.choice(CUSTOMER_NOUNS)} Pte Ltd"
        terms = rng.choice([14, 30, 30, 30, 45, 60])
        if i in LATE_CUSTOMERS + (PARTIAL_CUSTOMER, CREDIT_CUSTOMER):
            terms = 30
        customers.append(
            {"id": i + 1, "name": name, "email": make_email(name), "payment_terms_days": terms}
        )
    suppliers = []
    for i in range(12):
        name = f"{adjectives[25 + i]} {rng.choice(SUPPLIER_NOUNS)} Pte Ltd"
        suppliers.append(
            {"id": i + 1, "name": name, "email": make_email(name),
             "payment_terms_days": rng.choice([30, 30, 45])}
        )

    # ---- invoices ------------------------------------------------------
    specs = []

    def add_spec(cust, issue, amount, tag="normal", payments=(), credits=()):
        specs.append({
            "cust": cust, "issue": issue, "amount": amount, "tag": tag,
            "due": issue + days(cust["payment_terms_days"]),
            "payments": list(payments), "credits": list(credits),
        })

    for cust in customers:
        count = rng.randint(11, 13)
        i = cust["id"] - 1
        if i in LATE_CUSTOMERS:
            for ago in LATE_ISSUE_AGO:  # forced: still unpaid, 61+ days late
                add_spec(cust, as_of - days(ago), money(Decimal(rng.randint(50000, 2500000)) / 100), "late")
            for _ in range(count - len(LATE_ISSUE_AGO)):
                ago = rng.randint(143, 182) if rng.random() < 0.45 else rng.randint(3, 86)
                issue = as_of - days(ago)
                due = issue + days(cust["payment_terms_days"])
                amount = money(Decimal(rng.randint(50000, 2500000)) / 100)
                pay = late_payment_date(rng, due, as_of)
                add_spec(cust, issue, amount, "normal", [(pay, amount)] if pay else [])
        else:
            for _ in range(count):
                issue = as_of - days(rng.randint(3, 182))
                due = issue + days(cust["payment_terms_days"])
                amount = money(Decimal(rng.randint(50000, 2500000)) / 100)
                pay = normal_payment_date(rng, issue, due, as_of)
                add_spec(cust, issue, amount, "normal", [(pay, amount)] if pay else [])

    # planted 2: partially paid, not yet due
    add_spec(customers[PARTIAL_CUSTOMER], as_of - days(20), money("8000.00"), "partial",
             payments=[(as_of - days(10), money("3200.00"))])
    # planted 3: reduced by a credit note, not yet due
    add_spec(customers[CREDIT_CUSTOMER], as_of - days(18), money("12000.00"), "credit",
             credits=[(as_of - days(12), money("1200.00"), "Damaged goods discount")])

    specs.sort(key=lambda s: (s["issue"], s["cust"]["id"], s["amount"], s["tag"]))

    invoices, payments, credit_notes = [], [], []
    for n, s in enumerate(specs, start=1):
        s["invoice_no"] = f"SI-{n:04d}"
        paid = sum((a for _, a in s["payments"]), Decimal("0.00"))
        credited = sum((c[1] for c in s["credits"]), Decimal("0.00"))
        invoices.append({
            "id": n, "invoice_no": s["invoice_no"], "customer_id": s["cust"]["id"],
            "issue_date": s["issue"], "due_date": s["due"], "amount": s["amount"],
            "status": invoice_status(s["amount"], paid, credited),
        })
        for paid_on, amt in s["payments"]:
            payments.append({"id": len(payments) + 1, "invoice_id": n, "paid_on": paid_on, "amount": amt})
        for issued_on, amt, reason in s["credits"]:
            credit_notes.append({"id": len(credit_notes) + 1, "invoice_id": n, "amount": amt,
                                 "reason": reason, "issued_on": issued_on})

    # ---- purchase orders, receipts, bills ------------------------------
    pos = []
    for _ in range(120):
        supplier = suppliers[rng.randrange(12)]
        ordered_on = as_of - days(rng.randint(8, 190))
        pos.append(make_po(rng, as_of, supplier, ordered_on))

    def set_dates(po, ordered_ago, received_ago, bill_ago):
        po["ordered_on"] = as_of - days(ordered_ago)
        po["received_on"] = as_of - days(received_ago)
        po["bill_date"] = as_of - days(bill_ago)

    # planted 4: duplicate bill (original below, copy added after the loop)
    set_dates(pos[0], 40, 31, 28)
    # planted 5: bill unit price higher than the PO price (+12%)
    set_dates(pos[1], 10, 6, 3)
    price_line = pos[1]["bill_lines"][0]
    po_price = price_line["unit_price"]
    price_line["unit_price"] = money(po_price * Decimal("1.12"))
    # planted 6: bill charges more than was received (received 70% of ordered)
    set_dates(pos[2], 11, 7, 3)
    qty = rng.randint(40, 100)
    pos[2]["lines"][0]["qty"] = qty
    pos[2]["bill_lines"][0]["qty"] = qty
    received_qty = int(qty * 0.7)
    pos[2]["receipt_qty"][pos[2]["lines"][0]["item"]] = received_qty
    # planted 8: two bills stuck in approval (12 and 9 days)
    set_dates(pos[3], 30, 24, 12)
    set_dates(pos[4], 27, 21, 9)

    bills = []
    for idx, po in enumerate(pos):
        terms = po["supplier"]["payment_terms_days"]
        due = po["bill_date"] + days(terms)
        status, submitted = bill_status(po["bill_date"], due, as_of)
        if idx in (1, 2):
            status, submitted = "pending_approval", as_of - days(2)
        if idx in (3, 4):
            status, submitted = "pending_approval", po["bill_date"]
        bills.append({
            "bill_no": None, "supplier": po["supplier"], "po_idx": idx,
            "bill_date": po["bill_date"], "due_date": due, "status": status,
            "submitted": submitted, "lines": po["bill_lines"],
        })
    bills[0]["bill_no"] = "INV-1043"
    duplicate = {
        "bill_no": "INV1043", "supplier": pos[0]["supplier"], "po_idx": 0,
        "bill_date": as_of - days(3),
        "due_date": as_of - days(3) + days(pos[0]["supplier"]["payment_terms_days"]),
        "status": "pending_approval", "submitted": as_of - days(3),
        "lines": [dict(ln) for ln in pos[0]["bill_lines"]],
    }
    bills.append(duplicate)
    # planted 7: bill with no PO
    no_po_lines = []
    for item in rng.sample(sorted(ITEMS), 2):
        no_po_lines.append({
            "item": item, "qty": rng.randint(5, 60),
            "unit_price": money(Decimal(ITEMS[item]) * Decimal(rng.randint(90, 110)) / 100),
        })
    no_po_supplier = suppliers[5]
    no_po_bill_date = as_of - days(4)
    bills.append({
        "bill_no": None, "supplier": no_po_supplier, "po_idx": None,
        "bill_date": no_po_bill_date,
        "due_date": no_po_bill_date + days(no_po_supplier["payment_terms_days"]),
        "status": "pending_approval", "submitted": as_of - days(3), "lines": no_po_lines,
    })
    counter = 2001
    for b in bills:
        if b["bill_no"] is None:
            b["bill_no"] = f"INV-{counter}"
            counter += 1

    # ---- rows for the purchase side ------------------------------------
    purchase_orders, po_lines, goods_receipts, receipt_lines = [], [], [], []
    for idx, po in enumerate(pos):
        po_id = idx + 1
        purchase_orders.append({
            "id": po_id, "po_no": f"PO-{3001 + idx}", "supplier_id": po["supplier"]["id"],
            "ordered_on": po["ordered_on"], "total": lines_total(po["lines"]),
        })
        for ln in po["lines"]:
            po_lines.append({"id": len(po_lines) + 1, "po_id": po_id, "item": ln["item"],
                             "qty": ln["qty"], "unit_price": ln["unit_price"]})
        goods_receipts.append({"id": po_id, "po_id": po_id, "received_on": po["received_on"]})
        for ln in po["lines"]:
            receipt_lines.append({"id": len(receipt_lines) + 1, "receipt_id": po_id,
                                  "item": ln["item"], "qty_received": po["receipt_qty"][ln["item"]]})

    bill_rows, bill_line_rows = [], []
    for n, b in enumerate(bills, start=1):
        b["id"] = n
        bill_rows.append({
            "id": n, "bill_no": b["bill_no"], "supplier_id": b["supplier"]["id"],
            "po_id": None if b["po_idx"] is None else b["po_idx"] + 1,
            "bill_date": b["bill_date"], "due_date": b["due_date"],
            "amount": lines_total(b["lines"]), "status": b["status"],
            "submitted_for_approval_on": b["submitted"],
        })
        for ln in b["lines"]:
            bill_line_rows.append({"id": len(bill_line_rows) + 1, "bill_id": n, "item": ln["item"],
                                   "qty": ln["qty"], "unit_price": ln["unit_price"]})

    # ---- the answer key ------------------------------------------------
    def spec_for(tag):
        return next(s for s in specs if s["tag"] == tag)

    late_detail = []
    for i in LATE_CUSTOMERS:
        c = customers[i]
        late_detail.append({
            "customer_id": c["id"], "customer_name": c["name"],
            "overdue_invoices": [
                {"invoice_no": s["invoice_no"], "days_late": (as_of - s["due"]).days}
                for s in specs if s["cust"]["id"] == c["id"] and s["tag"] == "late"
            ],
        })
    partial, credit = spec_for("partial"), spec_for("credit")
    po_item_price = pos[1]["lines"][0]["unit_price"]
    planted = {
        "seed": seed,
        "as_of": as_of.isoformat(),
        "problems": [
            {"number": 1, "key": "late_customers", "kind": "agent_target",
             "tool": "overdue_invoices", "customers": late_detail},
            {"number": 2, "key": "partial_payment", "kind": "data_case",
             "invoice_no": partial["invoice_no"], "amount": str(partial["amount"]),
             "paid": str(partial["payments"][0][1])},
            {"number": 3, "key": "credit_note", "kind": "data_case",
             "invoice_no": credit["invoice_no"], "amount": str(credit["amount"]),
             "credit": str(credit["credits"][0][1])},
            {"number": 4, "key": "duplicate_bill", "kind": "agent_target",
             "bill_nos": ["INV-1043", "INV1043"], "duplicate_of": "INV-1043",
             "supplier_id": pos[0]["supplier"]["id"],
             "amount": str(lines_total(pos[0]["bill_lines"]))},
            {"number": 5, "key": "price_mismatch", "kind": "agent_target",
             "bill_no": bills[1]["bill_no"], "item": price_line["item"],
             "po_unit_price": str(po_item_price), "billed_unit_price": str(price_line["unit_price"])},
            {"number": 6, "key": "quantity_mismatch", "kind": "agent_target",
             "bill_no": bills[2]["bill_no"], "item": pos[2]["lines"][0]["item"],
             "qty_received": received_qty, "qty_billed": qty},
            {"number": 7, "key": "bill_without_po", "kind": "agent_target",
             "bill_no": bills[-1]["bill_no"]},
            {"number": 8, "key": "stuck_in_approval", "kind": "agent_target",
             "bills": [
                 {"bill_no": bills[i]["bill_no"], "days_waiting": (as_of - bills[i]["submitted"]).days}
                 for i in (3, 4)
             ]},
        ],
    }

    return {
        "as_of": as_of,
        "tables": {
            "customers": customers, "invoices": invoices, "payments": payments,
            "credit_notes": credit_notes, "suppliers": suppliers,
            "purchase_orders": purchase_orders, "po_lines": po_lines,
            "goods_receipts": goods_receipts, "receipt_lines": receipt_lines,
            "bills": bill_rows, "bill_lines": bill_line_rows,
        },
        "planted": planted,
    }


def load(conn, dataset: dict) -> None:
    """Reset the tables from db/schema.sql and insert the dataset."""
    conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    for table in TABLES:
        rows = dataset["tables"][table]
        columns = list(rows[0])
        insert = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(", ").join(sql.Identifier(c) for c in columns),
            sql.SQL(", ").join(sql.Placeholder() for _ in columns),
        )
        with conn.cursor() as cur:
            cur.executemany(insert, [tuple(r[c] for c in columns) for r in rows])
        conn.execute(
            sql.SQL("SELECT setval(pg_get_serial_sequence({}, 'id'), (SELECT max(id) FROM {}))").format(
                sql.Literal(table), sql.Identifier(table)
            )
        )
    conn.commit()


def planted_json(dataset: dict) -> str:
    return json.dumps(dataset["planted"], indent=2, sort_keys=True) + "\n"


def main() -> None:
    dataset = build_dataset()
    with connect() as conn:
        load(conn, dataset)
    PLANTED_PATH.parent.mkdir(exist_ok=True)
    PLANTED_PATH.write_text(planted_json(dataset), encoding="utf-8")
    counts = {t: len(rows) for t, rows in dataset["tables"].items()}
    print(f"Loaded as of {dataset['as_of']}: {counts}")
    print(f"Wrote {PLANTED_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
