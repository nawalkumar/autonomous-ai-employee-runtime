"""Repository for mock company entities (separate from runtime SQLiteStore)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.world.database import connect, init_company_schema, write_transaction
from app.world.models import Customer, Employee, Ticket, TicketPriority, TicketStatus
from app.world.seed import SEED_CUSTOMERS, SEED_EMPLOYEES, TICKET_SEQ_START


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class IdempotencyConflictError(ValueError):
    """Raised when an idempotency key is reused for a different request."""


def _request_hash(operation: str, payload: dict[str, Any]) -> str:
    blob = json.dumps({"operation": operation, **payload}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _load_idempotent(
    conn: sqlite3.Connection,
    key: str | None,
    operation: str,
    request_hash: str,
) -> dict[str, Any] | None:
    """Return the stored response for a key, or None if the key is unused."""
    if not key:
        return None
    row = conn.execute(
        "SELECT operation, request_hash, response_json "
        "FROM company_idempotency WHERE idempotency_key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    if row["operation"] != operation or row["request_hash"] != request_hash:
        raise IdempotencyConflictError(
            f"Idempotency key {key!r} was already used for a different request"
        )
    return json.loads(row["response_json"])


def _record_idempotent(
    conn: sqlite3.Connection,
    key: str | None,
    operation: str,
    request_hash: str,
    response: BaseModel,
) -> None:
    if not key:
        return
    conn.execute(
        """
        INSERT INTO company_idempotency
            (idempotency_key, operation, request_hash, response_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            key,
            operation,
            request_hash,
            response.model_dump_json(),
            utc_now().isoformat(),
        ),
    )


class CompanyRepository:
    """CRUD over company_* tables in the shared SQLite database file."""

    def __init__(self, db_path: str | Path, *, seed: bool = True) -> None:
        self.db_path = Path(db_path)
        self._ensure_initialized(seed=seed)

    def _ensure_initialized(self, *, seed: bool) -> None:
        with connect(self.db_path) as conn:
            init_company_schema(conn)
            if seed:
                self._seed(conn)

    def _seed(self, conn: Any) -> None:
        for employee in SEED_EMPLOYEES:
            conn.execute(
                """
                INSERT OR IGNORE INTO company_employees
                    (employee_id, name, email, title, department, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    employee.employee_id,
                    employee.name,
                    employee.email,
                    employee.title,
                    employee.department,
                    employee.updated_at.isoformat(),
                ),
            )
        for customer in SEED_CUSTOMERS:
            conn.execute(
                """
                INSERT OR IGNORE INTO company_customers
                    (customer_id, name, email, company, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    customer.customer_id,
                    customer.name,
                    customer.email,
                    customer.company,
                    customer.created_at.isoformat(),
                ),
            )
        conn.execute(
            """
            INSERT OR IGNORE INTO company_ticket_seq (name, next_value)
            VALUES ('ticket', ?)
            """,
            (TICKET_SEQ_START,),
        )

    def get_employee(self, employee_id: str) -> Employee | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM company_employees WHERE employee_id = ?",
                (employee_id,),
            ).fetchone()
        return Employee.model_validate(dict(row)) if row else None

    def update_employee(
        self,
        employee_id: str,
        fields: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> Employee | None:
        employee, _ = self.update_employee_idempotent(
            employee_id, fields, idempotency_key=idempotency_key
        )
        return employee

    def update_employee_idempotent(
        self,
        employee_id: str,
        fields: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> tuple[Employee | None, bool]:
        """Apply an update at most once per key. Returns (employee, replayed)."""
        allowed = {"name", "email", "title", "department"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        request_hash = _request_hash(
            "update_employee", {"employee_id": employee_id, "fields": updates}
        )

        with write_transaction(self.db_path) as conn:
            prior = _load_idempotent(
                conn, idempotency_key, "update_employee", request_hash
            )
            if prior is not None:
                return Employee.model_validate(prior), True

            row = conn.execute(
                "SELECT * FROM company_employees WHERE employee_id = ?",
                (employee_id,),
            ).fetchone()
            if row is None:
                return None, False
            if not updates:
                return Employee.model_validate(dict(row)), False

            now = utc_now().isoformat()
            assignments = ", ".join(f"{col} = ?" for col in updates)
            values = list(updates.values()) + [now, employee_id]
            conn.execute(
                f"""
                UPDATE company_employees
                SET {assignments}, updated_at = ?
                WHERE employee_id = ?
                """,
                values,
            )
            updated = Employee.model_validate(
                dict(
                    conn.execute(
                        "SELECT * FROM company_employees WHERE employee_id = ?",
                        (employee_id,),
                    ).fetchone()
                )
            )
            _record_idempotent(
                conn, idempotency_key, "update_employee", request_hash, updated
            )
        return updated, False

    def find_customers(
        self,
        *,
        customer_id: str | None = None,
        company: str | None = None,
        email: str | None = None,
    ) -> list[Customer]:
        clauses: list[str] = []
        params: list[str] = []
        if customer_id:
            clauses.append("customer_id = ?")
            params.append(customer_id)
        if company:
            clauses.append("LOWER(company) = LOWER(?)")
            params.append(company)
        if email:
            clauses.append("LOWER(email) = LOWER(?)")
            params.append(email)
        if not clauses:
            return []

        sql = "SELECT * FROM company_customers WHERE " + " AND ".join(clauses)
        with connect(self.db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Customer.model_validate(dict(row)) for row in rows]

    def create_ticket(
        self,
        *,
        customer_id: str,
        subject: str,
        description: str,
        priority: TicketPriority = "medium",
        status: TicketStatus = "open",
        idempotency_key: str | None = None,
    ) -> Ticket:
        ticket, _ = self.create_ticket_idempotent(
            customer_id=customer_id,
            subject=subject,
            description=description,
            priority=priority,
            status=status,
            idempotency_key=idempotency_key,
        )
        return ticket

    def create_ticket_idempotent(
        self,
        *,
        customer_id: str,
        subject: str,
        description: str,
        priority: TicketPriority = "medium",
        status: TicketStatus = "open",
        idempotency_key: str | None = None,
    ) -> tuple[Ticket, bool]:
        """Create a ticket at most once per key. Returns (ticket, replayed)."""
        request_hash = _request_hash(
            "create_ticket",
            {
                "customer_id": customer_id,
                "subject": subject,
                "description": description,
                "priority": priority,
                "status": status,
            },
        )
        now = utc_now()
        with write_transaction(self.db_path) as conn:
            prior = _load_idempotent(conn, idempotency_key, "create_ticket", request_hash)
            if prior is not None:
                return Ticket.model_validate(prior), True

            exists = conn.execute(
                "SELECT 1 FROM company_customers WHERE customer_id = ?",
                (customer_id,),
            ).fetchone()
            if exists is None:
                raise ValueError(f"Unknown customer_id: {customer_id}")

            row = conn.execute(
                "SELECT next_value FROM company_ticket_seq WHERE name = 'ticket'"
            ).fetchone()
            if row is None:
                raise RuntimeError("Ticket sequence not initialized")
            next_value = int(row["next_value"])
            ticket_id = f"T-{next_value}"
            conn.execute(
                "UPDATE company_ticket_seq SET next_value = ? WHERE name = 'ticket'",
                (next_value + 1,),
            )
            conn.execute(
                """
                INSERT INTO company_tickets
                    (ticket_id, customer_id, subject, description, status, priority,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_id,
                    customer_id,
                    subject,
                    description,
                    status,
                    priority,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            ticket = Ticket(
                ticket_id=ticket_id,
                customer_id=customer_id,
                subject=subject,
                description=description,
                status=status,
                priority=priority,
                created_at=now,
                updated_at=now,
            )
            _record_idempotent(conn, idempotency_key, "create_ticket", request_hash, ticket)
        return ticket, False

    def get_idempotency_record(self, idempotency_key: str) -> dict[str, Any] | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM company_idempotency WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return dict(row) if row else None

    def get_ticket(self, ticket_id: str) -> Ticket | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM company_tickets WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()
        return Ticket.model_validate(dict(row)) if row else None

    def delete_ticket(self, ticket_id: str) -> bool:
        """Remove a ticket by id. Used for independence tests / controlled demos."""
        with connect(self.db_path) as conn:
            cur = conn.execute(
                "DELETE FROM company_tickets WHERE ticket_id = ?",
                (ticket_id,),
            )
            return cur.rowcount > 0

    def list_tickets(self) -> list[Ticket]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM company_tickets ORDER BY ticket_id ASC"
            ).fetchall()
        return [Ticket.model_validate(dict(row)) for row in rows]

    def count_employees(self) -> int:
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM company_employees").fetchone()
        return int(row["c"])

    def count_customers(self) -> int:
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM company_customers").fetchone()
        return int(row["c"])
