"""Command line for the month-end check and for deciding its actions.

    uv run python -m finpulse.cli run-month-end [--no-ai]
    uv run python -m finpulse.cli list [--all]
    uv run python -m finpulse.cli approve <id>
    uv run python -m finpulse.cli reject <id>

Approve and reject are deliberately only here, never MCP tools.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from finpulse import actions, approvals
from finpulse.agents.graph import load_policy, run_month_end
from finpulse.agents.llm import llm_from_env
from finpulse.db import connect, get_as_of

ROOT = Path(__file__).resolve().parents[2]


def outbox_dir() -> Path:
    return Path(os.environ.get("FINPULSE_OUTBOX") or ROOT / "outbox")


def cmd_run(args) -> int:
    as_of, policy = get_as_of(), load_policy()
    client = None if args.no_ai else llm_from_env(policy["llm"]["temperature"])
    if client is None:
        why = "--no-ai was given" if args.no_ai else "OPENROUTER_API_KEY or FINPULSE_MODEL is not set"
        print(f"Note: no AI model used ({why}); reminders use fixed templates.")
    with connect() as conn:
        result = run_month_end(conn, as_of, client, outbox_dir(), policy, model=getattr(client, "model", None))
    created = sum(1 for p in result["proposals"] if p["created"])
    existing = len(result["proposals"]) - created
    c, u = result["counts"], result["usage"]
    print(f"Month-end check as of {as_of.isoformat()} (made-up data)")
    print(f"  Overdue invoices: {len(result['findings']['overdue'])}  |  reminders: {c['reminders']}  |  "
          f"phone calls: {c['phone_calls']}  |  bills to hold: {c['holds']}  |  "
          f"duplicates: {c['duplicates']}  |  stuck in approval: {c['stuck']}")
    print(f"  Actions: {created} new pending" + (f", {existing} already pending from an earlier run" if existing else ""))
    for line in result["discarded"]:
        print(f"  Discarded: {line}")
    print(f"  Report: {result['report_path']}")
    print(f"  LLM: {u['calls']} calls (max {policy['llm']['max_calls']}), "
          f"{u['prompt_tokens']} prompt + {u['completion_tokens']} completion = {u['total_tokens']} tokens")
    print("  Nothing has been sent. Review with: uv run python -m finpulse.cli list")
    return 0


def cmd_list(args) -> int:
    with connect() as conn:
        rows = actions.list_actions(conn, None if args.all else "pending")
    if not rows:
        print("No pending actions." if not args.all else "No actions.")
        return 0
    for r in rows:
        lines = [l for l in r["draft_text"].splitlines() if l.strip()]
        subject = next((l[len("Subject:"):].strip() for l in lines if l.startswith("Subject:")), None)
        text = subject or lines[0]
        print(f"#{r['id']:<4} {r['status']:<9} {r['action_type']:<18} {r['target']:<28} {text[:80]}")
    print(f"{len(rows)} shown.")
    return 0


def cmd_decide(args, decision: str) -> int:
    try:
        with connect() as conn:
            result = approvals.decide(conn, get_as_of(), args.id, decision, outbox_dir())
    except approvals.ApprovalError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Action #{result['id']} {result['status']}.")
    if result["file"]:
        print(f"Wrote {result['file']} (not sent: this demo never sends email).")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="finpulse.cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run-month-end", help="find problems, apply policy, store pending actions, write the report")
    run.add_argument("--no-ai", action="store_true", help="use fixed templates only, no model call")
    lst = sub.add_parser("list", help="show pending actions")
    lst.add_argument("--all", action="store_true", help="include approved and rejected")
    for name in ("approve", "reject"):
        p = sub.add_parser(name, help=f"{name} one action")
        p.add_argument("id", type=int)
    args = parser.parse_args(argv)
    if args.command == "run-month-end":
        return cmd_run(args)
    if args.command == "list":
        return cmd_list(args)
    return cmd_decide(args, "approved" if args.command == "approve" else "rejected")


if __name__ == "__main__":
    sys.exit(main())
