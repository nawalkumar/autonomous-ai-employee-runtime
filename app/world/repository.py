"""Repository for mock company entities (separate from runtime SQLiteStore)."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, get_args

from pydantic import BaseModel

from app.world.database import (
    connect,
    init_company_schema,
    migrate_company_schema,
    write_transaction,
)
from app.world.models import (
    ASSIGNABLE_DEPARTMENT,
    OPEN_TICKET_STATUSES,
    TICKET_TRANSITIONS,
    Customer,
    Employee,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.world.seed import SEED_CUSTOMERS, SEED_EMPLOYEES, TICKET_SEQ_START


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class IdempotencyConflictError(ValueError):
    """Raised when an idempotency key is reused for a different request."""


class InvalidEmployeeUpdateError(ValueError):
    """Raised for an employee update that cannot be applied exactly as requested."""


EMPLOYEE_UPDATE_FIELDS = frozenset({"name", "email", "title", "department"})


def validate_employee_update(fields: Any) -> dict[str, str]:
    """Return the update unchanged, or raise if any part of it would be ignored.

    The whole update is rejected when a field is unsupported or a value is not
    a non-empty string, so a request can never succeed while silently dropping
    part of what was asked.
    """
    if not isinstance(fields, dict) or not fields:
        raise InvalidEmployeeUpdateError(
            "Employee update requires a non-empty mapping of fields "
            f"(supported: {', '.join(sorted(EMPLOYEE_UPDATE_FIELDS))})"
        )
    unknown = sorted(str(key) for key in fields if key not in EMPLOYEE_UPDATE_FIELDS)
    if unknown:
        raise InvalidEmployeeUpdateError(
            f"Unsupported employee field(s): {', '.join(unknown)} "
            f"(supported: {', '.join(sorted(EMPLOYEE_UPDATE_FIELDS))})"
        )
    blank = sorted(
        key for key, value in fields.items() if not isinstance(value, str) or not value.strip()
    )
    if blank:
        raise InvalidEmployeeUpdateError(
            f"Employee field value(s) must be non-empty strings: {', '.join(blank)}"
        )
    return dict(fields)


class TicketUpdateError(ValueError):
    """An unsupported ticket field or value, or a status change that is not allowed."""


class RecordNotFoundError(LookupError):
    """A referenced company record does not exist."""


class BusinessRuleError(ValueError):
    """A valid request that the company's rules do not allow in the current state."""


TICKET_UPDATE_FIELDS = frozenset({"status", "priority"})
_TICKET_FIELD_VALUES = {
    "status": frozenset(get_args(TicketStatus)),
    "priority": frozenset(get_args(TicketPriority)),
}
CUSTOMER_EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
MAX_TEXT_LENGTH = 200
MAX_TICKET_PAGE = 100


def validate_ticket_update(fields: Any) -> dict[str, str]:
    """Return the update unchanged, or raise if any part of it is not supported."""
    if not isinstance(fields, dict) or not fields:
        raise TicketUpdateError(
            "Ticket update requires a non-empty mapping of fields "
            f"(supported: {', '.join(sorted(TICKET_UPDATE_FIELDS))})"
        )
    unknown = sorted(str(key) for key in fields if key not in TICKET_UPDATE_FIELDS)
    if unknown:
        raise TicketUpdateError(
            f"Unsupported ticket field(s): {', '.join(unknown)} "
            f"(supported: {', '.join(sorted(TICKET_UPDATE_FIELDS))})"
        )
    for key, value in fields.items():
        if value not in _TICKET_FIELD_VALUES[key]:
            raise TicketUpdateError(
                f"Invalid ticket {key} {value!r} "
                f"(allowed: {', '.join(sorted(_TICKET_FIELD_VALUES[key]))})"
            )
    return dict(fields)


def validate_new_customer(name: Any, company: Any, email: Any) -> tuple[str, str, str]:
    """Normalised (name, company, email) or InvalidCustomerError."""
    values = []
    for label, value in (("name", name), ("company", company), ("email", email)):
        if not isinstance(value, str) or not value.strip():
            raise InvalidCustomerError(f"Customer {label} must be a non-empty string")
        if len(value.strip()) > MAX_TEXT_LENGTH:
            raise InvalidCustomerError(f"Customer {label} exceeds {MAX_TEXT_LENGTH} characters")
        values.append(value.strip())
    name, company, email = values
    email = email.lower()
    if not CUSTOMER_EMAIL_PATTERN.fullmatch(email):
        raise InvalidCustomerError(f"Customer email is not a valid address: {email!r}")
    return name, company, email


class InvalidCustomerError(ValueError):
    """Customer fields that do not satisfy the customer data model."""


class TicketChange(BaseModel):
    ticket: Ticket
    previous: Ticket


class EmployeeChange(BaseModel):
    employee: Employee
    previous: Employee


def _ticket_order_sql() -> str:
    # Numeric order of T-<n> identifiers, ties broken by the full identifier.
    return "CAST(SUBSTR(ticket_id, 3) AS INTEGER), ticket_id"


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


def _placeholders(values: frozenset[str]) -> str:
    return ", ".join("?" for _ in values)


def _select_ticket(conn: sqlite3.Connection, ticket_id: str) -> Ticket | None:
    row = conn.execute("SELECT * FROM company_tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
    return Ticket.model_validate(dict(row)) if row else None


def _select_employee(conn: sqlite3.Connection, employee_id: str) -> Employee | None:
    row = conn.execute(
        "SELECT * FROM company_employees WHERE employee_id = ?", (employee_id,)
    ).fetchone()
    return Employee.model_validate(dict(row)) if row else None


_TICKET_WRITABLE = frozenset({"status", "priority", "assignee_id"})


def _update_ticket_columns(conn: sqlite3.Connection, ticket_id: str, values: dict[str, Any]) -> None:
    columns = sorted(values)
    assert set(columns) <= _TICKET_WRITABLE
    assignments = ", ".join(f"{column} = ?" for column in columns)
    conn.execute(
        f"UPDATE company_tickets SET {assignments}, updated_at = ? WHERE ticket_id = ?",
        [*(values[c] for c in columns), utc_now().isoformat(), ticket_id],
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
        migrate_company_schema(self.db_path)

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
        """Apply an update at most once per key. Returns (employee, replayed).

        Raises InvalidEmployeeUpdateError before any write for an update that
        validate_employee_update rejects.
        """
        updates = validate_employee_update(fields)
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

    def list_employees(self) -> list[Employee]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM company_employees ORDER BY employee_id ASC"
            ).fetchall()
        return [Employee.model_validate(dict(row)) for row in rows]

    def list_customers(self) -> list[Customer]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM company_customers ORDER BY customer_id ASC"
            ).fetchall()
        return [Customer.model_validate(dict(row)) for row in rows]

    # --- v0.3.0 operations -----------------------------------------------------------

    def get_customer(self, customer_id: str) -> Customer | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM company_customers WHERE customer_id = ?", (customer_id,)
            ).fetchone()
        return Customer.model_validate(dict(row)) if row else None

    def customers_with_email(self, email: str) -> list[Customer]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM company_customers WHERE LOWER(email) = LOWER(?) "
                "ORDER BY customer_id",
                (email,),
            ).fetchall()
        return [Customer.model_validate(dict(row)) for row in rows]

    def open_tickets_assigned_to(self, employee_id: str) -> list[str]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT ticket_id FROM company_tickets
                WHERE assignee_id = ? AND status IN ({_placeholders(OPEN_TICKET_STATUSES)})
                ORDER BY {_ticket_order_sql()}
                """,
                (employee_id, *sorted(OPEN_TICKET_STATUSES)),
            ).fetchall()
        return [row["ticket_id"] for row in rows]

    def query_tickets(
        self,
        *,
        status: str | None = None,
        priority: str | None = None,
        customer_id: str | None = None,
        assignee_id: str | None = None,
        unassigned: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Ticket], int]:
        """Filtered, stably ordered page of tickets and the total match count.

        Filters are fixed columns compared by equality with bound parameters.
        """
        if not 1 <= limit <= MAX_TICKET_PAGE or offset < 0:
            raise ValueError(f"limit must be 1-{MAX_TICKET_PAGE} and offset >= 0")
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("status", status),
            ("priority", priority),
            ("customer_id", customer_id),
            ("assignee_id", assignee_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if unassigned:
            clauses.append("assignee_id IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with connect(self.db_path) as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM company_tickets {where}", params
            ).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM company_tickets {where} "
                f"ORDER BY {_ticket_order_sql()} LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [Ticket.model_validate(dict(row)) for row in rows], int(total)

    def update_ticket_idempotent(
        self,
        ticket_id: str,
        fields: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> tuple[Ticket | None, Ticket | None, bool]:
        """Change status and/or priority at most once per key.

        Returns (ticket, previous, replayed); (None, None, False) if the ticket
        does not exist. Raises TicketUpdateError for an invalid field, value or
        status transition, before anything is written.
        """
        updates = validate_ticket_update(fields)
        request_hash = _request_hash("update_ticket", {"ticket_id": ticket_id, "fields": updates})
        with write_transaction(self.db_path) as conn:
            prior = _load_idempotent(conn, idempotency_key, "update_ticket", request_hash)
            if prior is not None:
                change = TicketChange.model_validate(prior)
                return change.ticket, change.previous, True
            previous = _select_ticket(conn, ticket_id)
            if previous is None:
                return None, None, False
            target = updates.get("status", previous.status)
            if target != previous.status and target not in TICKET_TRANSITIONS[previous.status]:
                allowed = ", ".join(sorted(TICKET_TRANSITIONS[previous.status])) or "none"
                raise TicketUpdateError(
                    f"Ticket {ticket_id} cannot change from {previous.status} to {target} "
                    f"(allowed from {previous.status}: {allowed})"
                )
            changes = {k: v for k, v in updates.items() if getattr(previous, k) != v}
            if changes:
                _update_ticket_columns(conn, ticket_id, changes)
            ticket = _select_ticket(conn, ticket_id)
            assert ticket is not None
            _record_idempotent(
                conn, idempotency_key, "update_ticket", request_hash,
                TicketChange(ticket=ticket, previous=previous),
            )
        return ticket, previous, False

    def assign_ticket_idempotent(
        self,
        ticket_id: str,
        employee_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> tuple[Ticket, Ticket, bool]:
        """Assign an open ticket to an active Support employee, at most once per key.

        Raises RecordNotFoundError or BusinessRuleError before anything is written.
        """
        request_hash = _request_hash(
            "assign_ticket", {"ticket_id": ticket_id, "employee_id": employee_id}
        )
        with write_transaction(self.db_path) as conn:
            prior = _load_idempotent(conn, idempotency_key, "assign_ticket", request_hash)
            if prior is not None:
                change = TicketChange.model_validate(prior)
                return change.ticket, change.previous, True
            previous = _select_ticket(conn, ticket_id)
            if previous is None:
                raise RecordNotFoundError(f"Ticket not found: {ticket_id}")
            if previous.status not in OPEN_TICKET_STATUSES:
                raise BusinessRuleError(
                    f"Ticket {ticket_id} is {previous.status}; only open or in-progress "
                    "tickets can be assigned"
                )
            employee = _select_employee(conn, employee_id)
            if employee is None:
                raise RecordNotFoundError(f"Employee not found: {employee_id}")
            if employee.status != "active":
                raise BusinessRuleError(f"Employee {employee_id} is inactive and cannot be assigned")
            if employee.department != ASSIGNABLE_DEPARTMENT:
                raise BusinessRuleError(
                    f"Employee {employee_id} is in {employee.department}; tickets can only be "
                    f"assigned to {ASSIGNABLE_DEPARTMENT}"
                )
            if previous.assignee_id != employee_id:
                _update_ticket_columns(conn, ticket_id, {"assignee_id": employee_id})
            ticket = _select_ticket(conn, ticket_id)
            assert ticket is not None
            _record_idempotent(
                conn, idempotency_key, "assign_ticket", request_hash,
                TicketChange(ticket=ticket, previous=previous),
            )
        return ticket, previous, False

    def create_customer_idempotent(
        self,
        *,
        name: str,
        company: str,
        email: str,
        idempotency_key: str | None = None,
    ) -> tuple[Customer, bool]:
        """Create a customer with the next C-<n> identifier, at most once per key.

        Emails are unique case-insensitively; a duplicate raises BusinessRuleError.
        """
        name, company, email = validate_new_customer(name, company, email)
        request_hash = _request_hash(
            "create_customer", {"name": name, "company": company, "email": email}
        )
        with write_transaction(self.db_path) as conn:
            prior = _load_idempotent(conn, idempotency_key, "create_customer", request_hash)
            if prior is not None:
                return Customer.model_validate(prior), True
            existing = conn.execute(
                "SELECT customer_id FROM company_customers WHERE LOWER(email) = LOWER(?)",
                (email,),
            ).fetchone()
            if existing is not None:
                raise BusinessRuleError(
                    f"A customer with email {email} already exists ({existing['customer_id']})"
                )
            row = conn.execute(
                "SELECT next_value FROM company_ticket_seq WHERE name = 'customer'"
            ).fetchone()
            if row is None:
                raise RuntimeError("Customer sequence not initialized")
            number = int(row["next_value"])
            while conn.execute(
                "SELECT 1 FROM company_customers WHERE customer_id = ?", (f"C-{number}",)
            ).fetchone():
                number += 1
            customer = Customer(
                customer_id=f"C-{number}", name=name, email=email, company=company
            )
            conn.execute(
                "UPDATE company_ticket_seq SET next_value = ? WHERE name = 'customer'",
                (number + 1,),
            )
            conn.execute(
                "INSERT INTO company_customers (customer_id, name, email, company, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    customer.customer_id,
                    customer.name,
                    customer.email,
                    customer.company,
                    customer.created_at.isoformat(),
                ),
            )
            _record_idempotent(conn, idempotency_key, "create_customer", request_hash, customer)
        return customer, False

    def deactivate_employee_idempotent(
        self,
        employee_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> tuple[Employee | None, Employee | None, bool]:
        """Mark an employee inactive; refuses while open tickets are assigned to them.

        Deactivating an already inactive employee changes nothing and succeeds.
        Returns (employee, previous, replayed); (None, None, False) if not found.
        """
        request_hash = _request_hash("deactivate_employee", {"employee_id": employee_id})
        with write_transaction(self.db_path) as conn:
            prior = _load_idempotent(conn, idempotency_key, "deactivate_employee", request_hash)
            if prior is not None:
                change = EmployeeChange.model_validate(prior)
                return change.employee, change.previous, True
            previous = _select_employee(conn, employee_id)
            if previous is None:
                return None, None, False
            if previous.status != "inactive":
                blocking = [
                    row["ticket_id"]
                    for row in conn.execute(
                        f"""
                        SELECT ticket_id FROM company_tickets
                        WHERE assignee_id = ?
                          AND status IN ({_placeholders(OPEN_TICKET_STATUSES)})
                        ORDER BY {_ticket_order_sql()}
                        """,
                        (employee_id, *sorted(OPEN_TICKET_STATUSES)),
                    )
                ]
                if blocking:
                    raise BusinessRuleError(
                        f"Employee {employee_id} still has {len(blocking)} open ticket(s) "
                        f"assigned ({', '.join(blocking)}); reassign them before deactivating"
                    )
                conn.execute(
                    "UPDATE company_employees SET status = 'inactive', updated_at = ? "
                    "WHERE employee_id = ?",
                    (utc_now().isoformat(), employee_id),
                )
            employee = _select_employee(conn, employee_id)
            assert employee is not None
            _record_idempotent(
                conn, idempotency_key, "deactivate_employee", request_hash,
                EmployeeChange(employee=employee, previous=previous),
            )
        return employee, previous, False

    def count_employees(self) -> int:
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM company_employees").fetchone()
        return int(row["c"])

    def count_customers(self) -> int:
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM company_customers").fetchone()
        return int(row["c"])
