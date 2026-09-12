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


class InvalidTaskDefinition(DomainError):
    """A task definition violated an invariant the benchmark depends on."""


class InvalidValidationReport(DomainError):
    """A runner result violated the contract the two languages share."""


class InvalidTransition(DomainError):
    """A run was asked to move to a state it cannot reach from its current one."""


class PreconditionViolated(DomainError):
    """A pure function was called with input it had already declared invalid."""


class InvalidFixture(DomainError):
    """A fixture tree contains something that cannot be hashed reproducibly."""
