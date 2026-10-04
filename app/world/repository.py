"""Repository for mock company entities (separate from runtime SQLiteStore)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.world.database import connect, init_company_schema
from app.world.models import Customer, Employee, Ticket, TicketPriority, TicketStatus
from app.world.seed import SEED_CUSTOMERS, SEED_EMPLOYEES, TICKET_SEQ_START


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


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

    def update_employee(self, employee_id: str, fields: dict[str, Any]) -> Employee | None:
        allowed = {"name", "email", "title", "department"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return self.get_employee(employee_id)

        employee = self.get_employee(employee_id)
        if employee is None:
            return None

        now = utc_now().isoformat()
        assignments = ", ".join(f"{col} = ?" for col in updates)
        values = list(updates.values()) + [now, employee_id]
        with connect(self.db_path) as conn:
            conn.execute(
                f"""
                UPDATE company_employees
                SET {assignments}, updated_at = ?
                WHERE employee_id = ?
                """,
                values,
            )
        return self.get_employee(employee_id)

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
    ) -> Ticket:
        if self.find_customers(customer_id=customer_id) == []:
            raise ValueError(f"Unknown customer_id: {customer_id}")

        now = utc_now()
        with connect(self.db_path) as conn:
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
        return Ticket(
            ticket_id=ticket_id,
            customer_id=customer_id,
            subject=subject,
            description=description,
            status=status,
            priority=priority,
            created_at=now,
            updated_at=now,
        )

    def get_ticket(self, ticket_id: str) -> Ticket | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM company_tickets WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()
        return Ticket.model_validate(dict(row)) if row else None

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
