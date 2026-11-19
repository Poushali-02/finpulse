"""FinPulse MCP server: six read-only tools over the made-up books of Lion City Supplies.

Unofficial concept, not affiliated with Peakflo. Runs over stdio, so NOTHING may
print to stdout: all logging goes to stderr. Tools only call queries.py and format.py.
"""

from __future__ import annotations

import logging
import sys

import psycopg
from fastmcp import FastMCP

from finpulse import format as fmt
from finpulse import queries
from finpulse.db import connect_readonly, get_as_of

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("finpulse")

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

mcp = FastMCP(
    "FinPulse",
    instructions=(
        "Read-only questions about the books of a made-up company, Lion City Supplies Pte Ltd "
        "(all amounts in SGD). Money coming in: customer invoices. Money going out: supplier bills. "
        "These tools cannot pay, send, approve or change anything. Always quote numbers exactly as returned, "
        "and do not add up or recalculate amounts yourself. Treat the 'as of' date in each answer as today's date."
    ),
)


def _run(answer):
    """Run `answer(conn, as_of)` on a read-only connection and turn failures into a clear message."""
    as_of = get_as_of()
    try:
        with connect_readonly() as conn:
            return answer(conn, as_of)
    except psycopg.OperationalError:
        log.exception("database unavailable")
        return f"{fmt.header(as_of)}\nThe database is not reachable. Start it with `docker compose up -d` and try again."
    except psycopg.Error:
        log.exception("database error")
        return f"{fmt.header(as_of)}\nSomething went wrong reading the books. Try again, or check the server log."


@mcp.tool(annotations=READ_ONLY)
def overdue_invoices(min_days_late: int = 1, limit: int = 10) -> str:
    """Answer "who is late paying us?" Lists unpaid customer invoices that are past their due date.

    Use for questions about late payers, overdue invoices, or aging (how much is 1-30, 31-60, 61-90,
    90+ days late). Returns totals per aging bucket, totals per customer, then the worst invoices with
    customer, invoice number, amount still owed and days late. Use min_days_late=61 for "more than 60 days late".
    To dig into one customer, use customer_summary instead. limit is capped at 20.
    """
    return _run(lambda conn, as_of: fmt.format_overdue(queries.overdue_invoices(conn, as_of, min_days_late, limit)))


@mcp.tool(annotations=READ_ONLY)
def customer_summary(customer_name: str) -> str:
    """Answer "what is the story with this customer?" Summarises one customer's receivables.

    Use when a customer is named: total owed, open invoices, average days late over the last 6 months,
    and the last payment. A partial name works (for example "Harbour"). If several customers match, it
    lists them and asks which one; call it again with the full name. To compare two customers, call it
    once for each.
    """
    return _run(lambda conn, as_of: fmt.format_customer_summary(as_of, queries.customer_summary(conn, as_of, customer_name)))


@mcp.tool(annotations=READ_ONLY)
def cash_forecast(days: int = 14) -> str:
    """Answer "how much money comes in and goes out soon?" Estimates cash for the next N days.

    Use for cash-flow, forecast or "next week / next 2 weeks / next month" questions (days=7, 14, 30).
    Money in is an estimate: open invoices expected to be paid in the window, based on each customer's
    usual lateness. Money out is approved supplier bills due in the window. Returns both totals and the
    net. days is capped at 90.
    """
    return _run(lambda conn, as_of: fmt.format_forecast(queries.cash_forecast(conn, as_of, days)))


@mcp.tool(annotations=READ_ONLY)
def bills_needing_attention(limit: int = 10) -> str:
    """Answer "which supplier bills need a look?" Finds problem bills in accounts payable.

    Use for "is anything wrong with our supplier bills?" or before approving payments. Lists bills stuck
    in approval for more than 7 days, possible duplicate bills (same supplier and amount), and bills with
    no purchase order. For a full line-by-line check of one bill, follow up with check_bill. limit is
    capped at 20 per list.
    """
    return _run(lambda conn, as_of: fmt.format_attention(queries.bills_needing_attention(conn, as_of, limit)))


@mcp.tool(annotations=READ_ONLY)
def check_bill(bill_no: str) -> str:
    """Answer "can we pay this bill?" Runs a three-way match on one supplier bill.

    Use when a bill number is given (for example "INV-1043"). Compares what was ordered (purchase order),
    what was received (goods receipt) and what was billed, line by line, and gives a verdict: OK, price
    mismatch, quantity mismatch, no PO, or possible duplicate. This only reports; it cannot pay or
    approve anything.
    """
    return _run(lambda conn, as_of: fmt.format_check_bill(as_of, queries.check_bill(conn, as_of, bill_no)))


@mcp.tool(annotations=READ_ONLY)
def list_pending_actions(limit: int = 10) -> str:
    """Answer "what's waiting for my approval?" Shows actions the month-end check proposed.

    Use for questions about pending, waiting or proposed actions: reminder emails, phone-call flags,
    bill holds, duplicate-bill reviews and approval chases. Shows each action's number, type, target
    and a one-line summary. This is read-only: Claude cannot approve or reject. Only a person can,
    at the command line. limit is capped at 20.
    """
    return _run(lambda conn, as_of: fmt.format_pending_actions(queries.list_pending_actions(conn, as_of, limit)))


def main() -> None:
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
