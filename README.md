Unofficial concept. Not affiliated with Peakflo. Runs on made-up data.

# FinPulse

An MCP server plus a month-end agent check that lets a finance team ask Claude about money coming in and money going out. It runs on the invented books of "Lion City Supplies Pte Ltd", a Singapore distributor, in SGD. Every figure is labelled `[demo data]`.

1. **MCP server (read-only).** Ask Claude "who's late on payments?", "can we pay bill INV-1043?" or "how much cash comes in next week?". Claude calls FinPulse's tools, and the tools answer from a PostgreSQL database. All the arithmetic is SQL and Python `Decimal`; the AI never adds anything up.
2. **Month-end agent check.** A small LangGraph workflow finds the problems, applies company rules, double-checks the numbers, and drafts actions such as reminder emails. A person approves every action. Nothing is ever really sent.

## Finance words

| Word | Meaning in plain English |
|---|---|
| **Invoice** | A bill the company sends to a customer: "you owe us SGD 5,000". |
| **Payment terms ("Net 30")** | The customer has 30 days from the invoice date to pay. That deadline is the **due date**. |
| **Days late / aging** | How many days past the due date an invoice is still unpaid. Grouped into buckets: not due yet, 1-30, 31-60, 61-90, 90+. |
| **Partial payment** | The customer paid part of the invoice. The rest is still owed. |
| **Credit note** | A discount or refund that reduces what a customer owes on an invoice. |
| **Bill** | An invoice the company *receives* from a supplier. |
| **Purchase order (PO)** | What the company ordered from a supplier: items, quantities and prices. |
| **Goods receipt** | What actually arrived. |
| **Three-way match** | Before paying a bill, check that the PO (ordered), the goods receipt (received) and the bill (charged) agree. |
| **Accounts receivable (AR)** | Money customers owe the company. |
| **Accounts payable (AP)** | Money the company owes suppliers. |

## The six tools

All are read-only. Each answer starts with `[demo data] as of 2026-11-30`, shows amounts like `SGD 12,400.00`, and stays under about 300 words.

| Tool | Question it answers |
|---|---|
| `overdue_invoices` | Who's late? Totals per aging bucket and per customer, then the worst invoices. |
| `customer_summary` | What's the story with this customer? Total owed, average days late, last payment. |
| `cash_forecast` | How much comes in and goes out soon? An estimate, with the method stated. |
| `bills_needing_attention` | Bills stuck in approval, possible duplicates, bills with no PO. |
| `check_bill` | Can we pay this bill? A three-way match with a verdict. |
| `list_pending_actions` | What is waiting for a person's approval? |

The server connects to Postgres with `default_transaction_read_only=on`, so the database itself refuses writes. Logs go to stderr, never stdout.

## The month-end check and approval flow

```
find  ->  check rules  ->  double-check  ->  pending actions + report
```

1. **Find.** Pure Python over the same query functions the tools use.
2. **Check rules.** Pure Python applies [`policy.yaml`](src/finpulse/agents/policy.yaml): reminders at 7 / 30 / 60 days late, a phone-call flag at 60+, a hold on any bill whose price or quantity is more than 2% off or that has no PO, a person-only review of duplicates, and a chase for bills stuck in approval over 7 days. The AI is used only to word the reminder emails and a one-paragraph summary. It never sees an amount, date or invoice number: it writes placeholders, and the numbers are filled in from the database.
3. **Double-check.** Every invoice or bill number in AI text must exist in the database, and every amount and "days late" must match it exactly. A draft that fails is discarded, the reason is logged, and a fixed template is used instead. There is a hard cap of 3 model calls per run, and a token count is printed at the end.

Approvals are enforced in code, not by convention:

```
uv run python -m finpulse.cli run-month-end     # writes outbox/report-<date>.md and pending actions
uv run python -m finpulse.cli list              # what is waiting
uv run python -m finpulse.cli approve <id>      # a reminder becomes one .eml file in outbox/
uv run python -m finpulse.cli reject <id>
```

- Approve and reject are deliberately **not** MCP tools. Claude can show what is waiting (`list_pending_actions`), but only a person at the command line can decide.
- The agent code can only create `pending` actions, and a test checks that it cannot import the approval code.
- "Sending" an email means writing a `.eml` file with Python's standard library. There is no SMTP code anywhere.

## Setup

```
docker compose up -d
uv sync && cp .env.example .env && uv run python scripts/generate_data.py
claude mcp add finpulse -- uv run --directory <full path to this folder> python -m finpulse.server
```

For Claude Desktop, add the same command to `%APPDATA%\Claude\claude_desktop_config.json`. To use a real model for the month-end wording, set `FINPULSE_MODEL` and either `OPENROUTER_API_KEY` or `GEMINI_API_KEY` in `.env`; without them the check uses fixed templates (`--no-ai` forces this).

## Tests and evals

- **75 tests pass** (`uv run pytest`). They cover:
  - the books adding up and the generator giving identical data twice;
  - an answer key (`data/planted.json`) of problems hidden on purpose, each found by the right tool;
  - the month-end check finding 100% of the problems it should act on, with no actions for healthy invoices or bills, the same result over 3 runs, and nothing reaching `outbox/` as an email without an approval.
- **Evals** ([`evals/tasks.md`](evals/tasks.md)): 9 questions with the tool each should trigger. They were run through headless Claude with only the FinPulse tools allowed. 8 of 8 passed, and question 9 passed in 3 of 4 runs.
- **Not done:** the month-end wording has been tested with scripted stub models (including ones that invent amounts, which the double-check rejects), but not yet with a real model through OpenRouter.

## Limits

Made-up data only. No dashboards or UI, invoice scanning, voice calls or real email. The cash forecast is an estimate based on each customer's past lateness.
