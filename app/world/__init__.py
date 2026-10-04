"""Deterministic mock company world."""

from app.world.models import Customer, Employee, Ticket
from app.world.repository import CompanyRepository

__all__ = ["CompanyRepository", "Customer", "Employee", "Ticket"]
