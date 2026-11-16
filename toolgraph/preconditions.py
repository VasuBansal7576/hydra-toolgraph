"""Execute explicit developer-defined state guards without reusing stale observations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence, TypeVar

T = TypeVar("T")


class PreconditionError(RuntimeError):
    """A declared state condition could not be established before execution."""


@dataclass(frozen=True)
class Precondition:
    """A developer-supplied predicate and optional authorized repair operation."""

    name: str
    check: Callable[[], bool]
    repair: Callable[[], object] | None = None


def execute_guarded(
    action: Callable[[], T],
    preconditions: Sequence[Precondition],
    *,
    emit: Callable[[dict], None] | None = None,
) -> T:
    """Recheck current state, repair unmet guards, then invoke the action once.

    Guards are explicit contracts, not inferred from trace co-occurrence.
    Repairs may have side effects and require the caller's authorization.
    Predicates and event callbacks must not mutate the guarded state.
    This is not a transaction: concurrent state changes remain possible.
    """
    guards = tuple(preconditions)
    names = [guard.name for guard in guards]
    if any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError("Each precondition needs a nonempty name")
    if len(names) != len(set(names)):
        raise ValueError("Precondition names must be unique")

    def observe(guard: Precondition, phase: str) -> bool:
        value = guard.check()
        if type(value) is not bool:
            raise PreconditionError(f"{guard.name}: predicate did not return a bool")
        if emit is not None:
            emit({"precondition": guard.name, "phase": phase, "satisfied": value})
        return value

    for guard in guards:
        if observe(guard, "before"):
            continue
        if guard.repair is None:
            raise PreconditionError(f"{guard.name}: unmet condition has no repair")
        guard.repair()
        if not observe(guard, "after_repair"):
            raise PreconditionError(f"{guard.name}: repair did not establish condition")
    if len(guards) > 1:
        for guard in guards:
            if not observe(guard, "before_action"):
                raise PreconditionError(
                    f"{guard.name}: a later repair invalidated condition"
                )
    return action()
