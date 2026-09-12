"""Errors raised by the domain layer.

Every domain error derives from `DomainError`, so a caller can distinguish "the
model rejected this as invalid" from an arbitrary `ValueError` raised somewhere
inside the standard library. Services translate these into run outcomes; they
are never allowed to escape as-is to the CLI.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for every error the domain raises."""


class InvalidIdentifier(DomainError):
    """An identifier was empty, too long, or contained disallowed characters."""
