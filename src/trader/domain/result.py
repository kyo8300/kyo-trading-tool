"""`Result` type: `Ok[T] | Err[E]` (N-6).

Every operation that can fail returns a `Result` instead of raising, so
callers must explicitly handle the failure case. Both variants are frozen
dataclasses so a `Result` value can never be mutated after creation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeGuard


@dataclass(frozen=True, slots=True)
class Ok[T]:
    """A successful result carrying a value of type `T`."""

    value: T

    def is_ok(self) -> bool:
        return True

    def is_err(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class Err[E]:
    """A failed result carrying an error of type `E`."""

    error: E

    def is_ok(self) -> bool:
        return False

    def is_err(self) -> bool:
        return True


type Result[T, E] = Ok[T] | Err[E]


def is_ok[T, E](result: Result[T, E]) -> TypeGuard[Ok[T]]:
    """Narrow `result` to `Ok[T]` for type checkers."""
    return isinstance(result, Ok)


def is_err[T, E](result: Result[T, E]) -> TypeGuard[Err[E]]:
    """Narrow `result` to `Err[E]` for type checkers."""
    return isinstance(result, Err)
