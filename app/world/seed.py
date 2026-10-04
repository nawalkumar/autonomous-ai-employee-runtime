"""Deterministic seed data for the mock company world."""

from datetime import datetime, timezone

from app.world.models import Customer, Employee

SEED_EMPLOYEES: list[Employee] = [
    Employee(
        employee_id="E-17",
        name="Alex Morgan",
        email="alex.morgan@centralign.example",
        title="Software Engineer",
        department="Engineering",
        updated_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    ),
    Employee(
        employee_id="E-23",
        name="Priya Shah",
        email="priya.shah@centralign.example",
        title="Product Manager",
        department="Product",
        updated_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    ),
    Employee(
        employee_id="E-31",
        name="Daniel Kim",
        email="daniel.kim@centralign.example",
        title="Support Engineer",
        department="Support",
        updated_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    ),
]

SEED_CUSTOMERS: list[Customer] = [
    Customer(
        customer_id="C-1001",
        name="Acme Billing Contact",
        email="billing@acme.example",
        company="Acme Corp",
        created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    ),
    Customer(
        customer_id="C-1002",
        name="Globex Ops",
        email="ops@globex.example",
        company="Globex",
        created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    ),
    Customer(
        customer_id="C-1003",
        name="Initech Support",
        email="support@initech.example",
        company="Initech",
        created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    ),
]

TICKET_SEQ_START = 10001
