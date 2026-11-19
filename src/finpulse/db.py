"""Database connection and the fixed "today" date (AS_OF)."""

from __future__ import annotations

import os
from datetime import date

import psycopg
from dotenv import load_dotenv

load_dotenv()

DEFAULT_AS_OF = "2026-11-30"


def get_as_of() -> date:
    """The date every calculation treats as today. Never the real clock."""
    return date.fromisoformat(os.environ.get("AS_OF", DEFAULT_AS_OF))


def connect(**kwargs) -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"], **kwargs)


def connect_readonly() -> psycopg.Connection:
    """Connection for the MCP server: Postgres itself refuses any write."""
    return psycopg.connect(
        os.environ["DATABASE_URL"],
        options="-c default_transaction_read_only=on",
    )
