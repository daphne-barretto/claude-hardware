"""Parameter store: named design values with a lock, who set them, and when.

A value set by hand (a person typing a dimension) becomes locked: it is a
constraint the agent must design around. The store refuses any change to a
locked value, by anyone, unless an approval is passed with it. Unlocking is a
change too and needs an approval.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

ParameterValue = float | int | str | bool


class Approval(BaseModel):
    """Explicit consent from a person to change a locked value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    by: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class Parameter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    value: ParameterValue
    locked: bool
    set_by: str = Field(min_length=1)
    set_at: datetime
    approval: Approval | None = None


class LockedParameterError(PermissionError):
    """A change to a locked parameter was attempted without an approval."""

    def __init__(self, parameter: Parameter) -> None:
        super().__init__(
            f"parameter {parameter.name!r} is locked (set by {parameter.set_by} "
            f"at {parameter.set_at.isoformat()}); changing it needs an approval"
        )
        self.parameter = parameter


def _now() -> datetime:
    return datetime.now(UTC)


class ParameterStore:
    def __init__(
        self,
        parameters: list[Parameter] | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._clock = clock
        self._parameters: dict[str, Parameter] = {p.name: p for p in parameters or []}

    def __contains__(self, name: object) -> bool:
        return name in self._parameters

    def __iter__(self) -> Iterator[Parameter]:
        return iter(self._parameters.values())

    def __len__(self) -> int:
        return len(self._parameters)

    def get(self, name: str) -> Parameter:
        return self._parameters[name]

    def value(self, name: str) -> ParameterValue:
        return self._parameters[name].value

    def set(
        self,
        name: str,
        value: ParameterValue,
        *,
        by: str,
        by_hand: bool = False,
        approval: Approval | None = None,
    ) -> Parameter:
        """Set a value. `by_hand=True` (a person typed it) locks the parameter.

        Raises LockedParameterError if the parameter is locked and no approval
        is given. A locked parameter stays locked after an approved change.
        """
        current = self._parameters.get(name)
        if current is not None and current.locked and approval is None:
            raise LockedParameterError(current)
        parameter = Parameter(
            name=name,
            value=value,
            locked=by_hand or (current is not None and current.locked),
            set_by=by,
            set_at=self._clock(),
            approval=approval,
        )
        self._parameters[name] = parameter
        return parameter

    def unlock(self, name: str, *, approval: Approval) -> Parameter:
        """Release a lock, returning control of the value to the agent."""
        if approval is None:
            raise LockedParameterError(self._parameters[name])
        current = self._parameters[name]
        parameter = current.model_copy(
            update={
                "locked": False,
                "set_by": approval.by,
                "set_at": self._clock(),
                "approval": approval,
            }
        )
        self._parameters[name] = parameter
        return parameter

    def restore(self, parameter: Parameter, *, replacing: Parameter) -> Parameter:
        """Put back an earlier record of a value, undoing the change that replaced it. No new
        approval is recorded: the store holds that earlier record again, as it was.

        Only the holder of the current record can do this: `replacing` must be it. Raises
        LockedParameterError otherwise (someone changed the value since)."""
        current = self._parameters.get(parameter.name)
        if current != replacing:
            raise LockedParameterError(current or parameter)
        self._parameters[parameter.name] = parameter
        return parameter

    def locked(self) -> list[Parameter]:
        return [p for p in self._parameters.values() if p.locked]

    def to_json(self) -> list[dict]:
        return [p.model_dump(mode="json") for p in self._parameters.values()]

    @classmethod
    def from_json(cls, data: list[dict], clock: Callable[[], datetime] = _now) -> ParameterStore:
        return cls([Parameter.model_validate(item) for item in data], clock=clock)
