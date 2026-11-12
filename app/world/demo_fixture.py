"""Deterministic demo-company fixture for a separate, disposable database.

    uv run python -m app.world.demo_fixture --database data/demo.db
    uv run python -m app.world.demo_fixture --database data/demo.db --reset

The fixture extends the default seed (E-17, E-23, E-31, C-1001..C-1003, which
are left exactly as they are) to 20 employees, 10 customers and 60 tickets.
All names, emails and companies are fictional; emails use the reserved
``.example`` domain. There is no randomness: every run produces identical rows.

Safety: the seeder only writes to a database file that does not exist yet, is
empty, or already carries this fixture's marker table. Any other database
(including a normal ``data/runtime.db``) is refused, with or without --reset.
``--reset`` deletes and recreates only a marked demo database. There is no HTTP
endpoint for seeding or resetting.

Controlled scenarios (identifiers are stable):

- High-priority unassigned open tickets: T-10001..T-10004.
- Globex (C-1002) open tickets with low/medium priority, for escalation: T-10005..T-10008.
- Resolved tickets eligible for closing: T-10009..T-10012.
- E-44 Rina Patel (Support, active) is an offboarding candidate with open
  tickets T-10013..T-10015; deactivation is refused until they are reassigned.
- Duplicate-looking open tickets for review-only workflows: T-10016 and T-10017.
- Eligible assignees (active Support): E-31, E-40, E-41, E-42, E-43, E-44.
  Ineligible: E-45 (Support, inactive; only a closed ticket, T-10018) and every
  employee outside Support.
- Customers with several tickets: every customer has at least five.

Expected totals: 20 employees (Support 7 including inactive E-45, Engineering 4,
Product 3, Finance 3, Operations 3), 10 customers, 60 tickets (open 22,
in_progress 11, resolved 16, closed 11; low 15, medium 18, high 14, urgent 13).
The next new ticket is T-10061 and the next new customer C-1011.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.world.database import write_transaction
from app.world.repository import CompanyRepository

FIXTURE_NAME = "centralign-demo-company"
FIXTURE_VERSION = "1"
MARKER_TABLE = "demo_fixture_meta"
BASE_TIME = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)

# (employee_id, name, title, department, status); the three default employees
# (E-17, E-23, E-31) come from the default seed unchanged.
EXTRA_EMPLOYEES: tuple[tuple[str, str, str, str, str], ...] = (
    ("E-40", "Lena Ortiz", "Senior Support Engineer", "Support", "active"),
    ("E-41", "Omar Haddad", "Support Engineer", "Support", "active"),
    ("E-42", "Grace Liu", "Support Engineer", "Support", "active"),
    ("E-43", "Tomasz Nowak", "Support Lead", "Support", "active"),
    ("E-44", "Rina Patel", "Support Engineer", "Support", "active"),
    ("E-45", "Felix Wagner", "Support Engineer", "Support", "inactive"),
    ("E-50", "Sam Rivera", "Backend Engineer", "Engineering", "active"),
    ("E-51", "Mei Tanaka", "Frontend Engineer", "Engineering", "active"),
    ("E-52", "Jonah Brooks", "Engineering Manager", "Engineering", "active"),
    ("E-60", "Clara Jensen", "Product Designer", "Product", "active"),
    ("E-61", "Diego Alvarez", "Product Analyst", "Product", "active"),
    ("E-70", "Hannah Weiss", "Finance Manager", "Finance", "active"),
    ("E-71", "Kwame Mensah", "Accountant", "Finance", "active"),
    ("E-72", "Sofia Rossi", "Billing Specialist", "Finance", "active"),
    ("E-80", "Liam Oconnor", "Operations Manager", "Operations", "active"),
    ("E-81", "Aisha Bello", "Office Coordinator", "Operations", "active"),
    ("E-82", "Noah Fischer", "IT Administrator", "Operations", "active"),
)

# (customer_id, contact name, email, company); C-1001..C-1003 come from the default seed.
EXTRA_CUSTOMERS: tuple[tuple[str, str, str, str], ...] = (
    ("C-1004", "Northwind Accounts", "accounts@northwind.example", "Northwind Traders"),
    ("C-1005", "Bluebird Ops", "ops@bluebird.example", "Bluebird Analytics"),
    ("C-1006", "Copperline Purchasing", "purchasing@copperline.example", "Copperline Foods"),
    ("C-1007", "Harbor IT Desk", "itdesk@harborhealth.example", "Harbor Health Clinics"),
    ("C-1008", "Juniper Admin", "admin@juniper.example", "Juniper Schools"),
    ("C-1009", "Meridian Dispatch", "dispatch@meridian.example", "Meridian Freight"),
    ("C-1010", "Orchid Support", "support@orchid.example", "Orchid Retail"),
)

CUSTOMER_IDS = tuple(f"C-{n}" for n in range(1001, 1011))
COMPANIES = {
    "C-1001": "Acme Corp",
    "C-1002": "Globex",
    "C-1003": "Initech",
    **{c[0]: c[3] for c in EXTRA_CUSTOMERS},
}
ELIGIBLE_ASSIGNEES = ("E-31", "E-40", "E-41", "E-42", "E-43")

# (number, customer_id, subject, priority, status, assignee_id): controlled scenarios.
SCENARIO_TICKETS: tuple[tuple[int, str, str, str, str, str | None], ...] = (
    (10001, "C-1001", "Payment portal returns error 500", "urgent", "open", None),
    (10002, "C-1005", "Dashboard export missing rows", "high", "open", None),
    (10003, "C-1007", "Patient portal login loop", "urgent", "open", None),
    (10004, "C-1009", "Shipment tracking API timeout", "high", "open", None),
    (10005, "C-1002", "Late invoice reminder emails", "medium", "open", None),
    (10006, "C-1002", "Duplicate charge on October invoice", "low", "open", "E-41"),
    (10007, "C-1002", "Cannot download tax receipts", "medium", "in_progress", "E-42"),
    (10008, "C-1002", "Account manager contact outdated", "low", "open", None),
    (10009, "C-1001", "Refund confirmation not received", "medium", "resolved", "E-40"),
    (10010, "C-1003", "Password reset email delayed", "low", "resolved", "E-41"),
    (10011, "C-1006", "Wrong unit price on order", "high", "resolved", "E-43"),
    (10012, "C-1008", "Calendar sync stopped", "medium", "resolved", "E-42"),
    (10013, "C-1004", "Bulk import fails on CSV header", "high", "open", "E-44"),
    (10014, "C-1010", "Gift card balance incorrect", "medium", "in_progress", "E-44"),
    (10015, "C-1001", "SSO certificate expiring soon", "urgent", "open", "E-44"),
    (10016, "C-1004", "Invoice PDF does not load", "medium", "open", None),
    (10017, "C-1004", "Invoice PDF not loading", "medium", "open", None),
    (10018, "C-1003", "Legacy API key rotation", "low", "closed", "E-45"),
)
FILLER_SUBJECTS = (
    "Order confirmation email missing",
    "Report totals do not match",
    "User cannot be added to team",
    "Slow page load on reports",
    "Shipping address not saved",
    "Two-factor code not accepted",
    "Usage graph shows gaps",
    "Webhook retries failing",
    "Language setting resets",
    "Invoice due date incorrect",
    "Search returns stale results",
    "Contract renewal question",
)
STATUSES = ("open", "in_progress", "resolved", "closed")
PRIORITIES = ("low", "medium", "high", "urgent")

SCENARIOS: dict[str, Any] = {
    "high_priority_unassigned": ["T-10001", "T-10002", "T-10003", "T-10004"],
    "escalation_customer": "C-1002",
    "escalation_candidates": ["T-10005", "T-10006", "T-10007", "T-10008"],
    "resolved_closable": ["T-10009", "T-10010", "T-10011", "T-10012"],
    "offboarding_employee": "E-44",
    "offboarding_open_tickets": ["T-10013", "T-10014", "T-10015"],
    "duplicate_looking": ["T-10016", "T-10017"],
    "eligible_assignees": ["E-31", "E-40", "E-41", "E-42", "E-43", "E-44"],
    "inactive_employee": "E-45",
    "non_support_employee": "E-17",
}
EXPECTED_COUNTS = {"employees": 20, "customers": 10, "tickets": 60}
NEXT_TICKET_NUMBER = 10061
NEXT_CUSTOMER_NUMBER = 1011


class FixtureSafetyError(RuntimeError):
    """The target is not a disposable demo database; nothing was changed."""


@dataclass
class FixtureSummary:
    database: str
    created: bool
    counts: dict[str, int] = field(default_factory=dict)
    scenarios: dict[str, Any] = field(default_factory=lambda: dict(SCENARIOS))


def _filler_ticket(number: int) -> tuple[int, str, str, str, str, str | None]:
    i = number - 10000
    customer = CUSTOMER_IDS[i % 10]
    status = STATUSES[(i * 7) % 4]
    if customer == SCENARIOS["escalation_customer"] and status in ("open", "in_progress"):
        status = "resolved"  # keep the Globex escalation set exactly T-10005..T-10008
    priority = PRIORITIES[(i * 3) % 4]
    assignee = None if i % 6 == 0 else ELIGIBLE_ASSIGNEES[i % 5]
    if assignee is None and status in ("open", "in_progress") and priority in ("high", "urgent"):
        assignee = ELIGIBLE_ASSIGNEES[i % 5]  # keep the unassigned high-priority set exact
    subject = f"{FILLER_SUBJECTS[(i * 5) % len(FILLER_SUBJECTS)]} (ref {1000 + i})"
    return number, customer, subject, priority, status, assignee


def fixture_tickets() -> list[dict[str, Any]]:
    """All 60 fixture tickets, deterministic."""
    rows = list(SCENARIO_TICKETS) + [_filler_ticket(n) for n in range(10019, NEXT_TICKET_NUMBER)]
    tickets = []
    for number, customer, subject, priority, status, assignee in rows:
        created = BASE_TIME + timedelta(hours=5 * (number - 10000))
        updated = created if status == "open" else created + timedelta(days=2)
        tickets.append(
            {
                "ticket_id": f"T-{number}",
                "customer_id": customer,
                "subject": subject,
                "description": f"{COMPANIES[customer]} reported: {subject.lower()}.",
                "status": status,
                "priority": priority,
                "assignee_id": assignee,
                "created_at": created.isoformat(),
                "updated_at": updated.isoformat(),
            }
        )
    return tickets


def _classify(db_path: Path) -> str:
    """'absent', 'empty', 'demo' or 'other'."""
    # Directories (and other non-files) are never seedable. On Windows a directory
    # often reports st_size == 0, which previously looked like an absent DB file
    # and fell through to SQLite ("unable to open database file").
    if db_path.exists() and not db_path.is_file():
        return "other"
    if not db_path.exists() or db_path.stat().st_size == 0:
        return "absent"
    try:
        with closing(sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables:
                return "empty"
            if MARKER_TABLE not in tables:
                return "other"
            row = conn.execute(
                f"SELECT value FROM {MARKER_TABLE} WHERE key = 'fixture'"
            ).fetchone()
            return "demo" if row and row[0] == FIXTURE_NAME else "other"
    except sqlite3.DatabaseError:
        return "other"


def _counts(db_path: Path) -> dict[str, int]:
    with closing(sqlite3.connect(db_path)) as conn:
        return {
            "employees": conn.execute("SELECT COUNT(*) FROM company_employees").fetchone()[0],
            "customers": conn.execute("SELECT COUNT(*) FROM company_customers").fetchone()[0],
            "tickets": conn.execute("SELECT COUNT(*) FROM company_tickets").fetchone()[0],
        }


def _remove(db_path: Path) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def seed_demo_company(db_path: str | Path, *, reset: bool = False) -> FixtureSummary:
    """Create the fixture in a disposable database; idempotent for an existing one."""
    path = Path(db_path).expanduser()
    if path.exists() and not path.is_file():
        raise FixtureSafetyError(
            f"{path} is not a SQLite database file path "
            "(directories and special files are refused); nothing was changed."
        )
    kind = _classify(path)
    if kind == "other":
        raise FixtureSafetyError(
            f"{path} is not a disposable demo database (no {MARKER_TABLE} marker); "
            "refusing to seed or reset it. Use a new file path."
        )
    if kind == "demo" and not reset:
        return FixtureSummary(str(path), created=False, counts=_counts(path))
    if kind == "demo":
        _remove(path)

    CompanyRepository(path)  # default seed + migrations, exactly as for a runtime database
    with write_transaction(path) as conn:
        conn.executemany(
            "INSERT INTO company_employees "
            "(employee_id, name, email, title, department, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    eid, name, f"{name.lower().replace(' ', '.')}@centralign.example",
                    title, dept, status, BASE_TIME.isoformat(),
                )
                for eid, name, title, dept, status in EXTRA_EMPLOYEES
            ],
        )
        conn.executemany(
            "INSERT INTO company_customers (customer_id, name, email, company, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [(cid, name, email, company, BASE_TIME.isoformat()) for cid, name, email, company in EXTRA_CUSTOMERS],
        )
        conn.executemany(
            "INSERT INTO company_tickets (ticket_id, customer_id, subject, description, status, "
            "priority, assignee_id, created_at, updated_at) VALUES "
            "(:ticket_id, :customer_id, :subject, :description, :status, :priority, "
            ":assignee_id, :created_at, :updated_at)",
            fixture_tickets(),
        )
        conn.execute(
            "UPDATE company_ticket_seq SET next_value = ? WHERE name = 'ticket'",
            (NEXT_TICKET_NUMBER,),
        )
        conn.execute(
            "UPDATE company_ticket_seq SET next_value = ? WHERE name = 'customer'",
            (NEXT_CUSTOMER_NUMBER,),
        )
        conn.execute(f"CREATE TABLE {MARKER_TABLE} (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.executemany(
            f"INSERT INTO {MARKER_TABLE} (key, value) VALUES (?, ?)",
            [("fixture", FIXTURE_NAME), ("version", FIXTURE_VERSION)],
        )
    return FixtureSummary(str(path), created=True, counts=_counts(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.world.demo_fixture",
        description="Seed the deterministic demo company into a disposable database.",
    )
    parser.add_argument("--database", required=True, help="path of the demo SQLite file")
    parser.add_argument(
        "--reset", action="store_true", help="recreate an existing demo database (marked ones only)"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    try:
        summary = seed_demo_company(args.database, reset=args.reset)
    except FixtureSafetyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(asdict(summary), indent=2))
    else:
        state = "created" if summary.created else "already seeded (unchanged)"
        print(f"Demo company {state}: {summary.database}")
        for name, count in summary.counts.items():
            print(f"  {name}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
