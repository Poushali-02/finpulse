# FinPulse evals

Questions a finance manager might ask Claude, the tool(s) each should trigger, and the result.
Everything runs on made-up data (Lion City Supplies Pte Ltd), as of 2026-11-30.

**How these were run (2026-11-19):** each question was sent on its own to headless Claude
(`claude -p`) with only the FinPulse tools allowed and no file or shell access, so the only way
to answer was through the MCP tools. Pass means Claude chose the expected tool(s) with sensible
arguments and the answer matched the tool output. To repeat them by hand, connect the server
(see the README) and ask the questions in Claude Code or Claude Desktop.

| # | Question | Expected tool(s) | Tools Claude called | Result |
|---|---|---|---|---|
| 1 | Which customers are more than 60 days late? | `overdue_invoices` (min_days_late=61) | `overdue_invoices(min_days_late=61, limit=20)` | Pass. Found the 3 planted late customers and all 9 invoices. |
| 2 | How much cash do we expect in the next 2 weeks? | `cash_forecast` (days=14) | `cash_forecast(days=14)` | Pass. Quoted money in, out and net, and said it is an estimate. |
| 3 | Can we pay bill INV-1043? (the duplicate) | `check_bill` | `check_bill("INV-1043")` | Pass. Said to hold it: possible duplicate of INV1043. |
| 4 | Is there anything wrong with our supplier bills? | `bills_needing_attention` | `bills_needing_attention()` | Pass. Listed the 2 stuck bills, the duplicate pair and the bill with no PO. |
| 5 | Tell me about Lotus Trading. | `customer_summary` | `customer_summary("Lotus Trading")` | Pass. |
| 6 | Compare the payment behaviour of Harbour Cleaning and Amber Retail. | `customer_summary` x2 | `customer_summary` for each name | Pass. |
| 7 | Tell me about Zzyzx Holdings. (does not exist) | `customer_summary`, fails politely | `customer_summary("Zzyzx Holdings")`, then `("Zzyzx")` | Pass. Said it found no such customer and asked for another spelling. |
| 8 | Pay all approved bills. (should refuse) | none | none | Pass. Explained the tools are read-only and offered the checks it can do. |
| 9 | What's waiting for my approval? | `list_pending_actions` | `list_pending_actions()` | Pass (3 of 4 runs). Listed the 26 pending actions by type and said Claude cannot approve. In one run Claude made no tool call and answered about something unrelated; see the note below. |

Question 9 was run after seeding the database with `uv run python -m finpulse.cli run-month-end --no-ai`
(26 pending actions). The one miss made no tool call at all and answered about something unrelated from
the surrounding session context, so it is not clear it was a docstring problem. The server was connected
and all 6 tools were listed in the later runs. Worth re-running by hand in a clean Claude session.

## What the evals changed

- First run of question 1: Claude added up per-customer totals itself. The totals were right, but
  the rule is that the AI never does arithmetic. `overdue_invoices` now returns totals per customer
  (top 5), and the server instructions say not to recalculate amounts.
- Claude keeps pointing out that the demo date (2026-11-30) differs from the real date. The server
  instructions now say to treat the "as of" date as today. This helped a little (question 6 used it
  without comment) but not every time. It is harmless, and the `[demo data] as of` line stays visible.
