"""Check-result contract: what the check runner produces and the checks UI shows.

See `contracts/README.md` ("Check results"). `validate_check_report()` also cross-checks the
report against `standards.yaml` (every standard has a result, and a result that names a standard
uses that standard's check and limit) and, given a mesh, that every location resolves.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import ConfigDict, Field, ValidationError, model_validator

from app.contracts.common import (
    BoardRef,
    Contract,
    ContractError,
    Location,
    StableId,
    check_location,
)
from app.contracts.mesh import MeshInfo
from app.standards import StandardsFile

CHECKS_CONTRACT = "check-results/1"

Status = Literal["pass", "fail", "untested", "not_applicable"]
STATUSES = ("pass", "fail", "untested", "not_applicable")
Unit = Literal["mm", "deg", "mm2", "mm3"]


class Measured(Contract):
    """A measured value. Text for spec checks (e.g. the material found)."""

    value: float | str
    unit: Unit | None = None


class Required(Contract):
    """The limit a result is judged against, in the same shape as a standard."""

    kind: Literal["min", "max", "tolerance", "spec"]
    value: float | str
    unit: Unit | None = None


class CheckResult(Contract):
    id: StableId = Field(
        description="Unique in the report. The check name, or `<check>:<subject>` when a check "
        "runs once per subject (e.g. `opening_alignment:Q4-sensor_window`)."
    )
    check: StableId = Field(description="Name of the check that produced the result")
    title: str = Field(min_length=1, description="Short label for the checks strip")
    standard: StableId | None = Field(
        default=None, description="ID of the standard in `standards.yaml`, null for board checks"
    )
    component: str | None = Field(default=None, description="Board reference designator")
    opening: str | None = Field(default=None, description="Board opening ID")
    parts: list[StableId] | None = Field(
        default=None,
        min_length=2,
        max_length=2,
        description='The two assembly parts the result is between (e.g. `["base", "lid"]`, '
        '`["lid", "heat_set_insert_1"]`, `["lid", "board"]`), null for a result about '
        "one part or none. `board` is the board, drawn from the board data rather than the mesh.",
    )
    status: Status
    measured: Measured | None = None
    required: Required | None = None
    summary: str = Field(min_length=1, description="One sentence: what was found")
    covers: str = Field(min_length=1, description="What the check covers (and what it doesn't)")
    location: Location | None = None
    reason: str | None = Field(
        default=None,
        min_length=1,
        description="Why the standard was not checked: it does not apply to this design "
        "(`applies_to`; required when the status is `not_applicable`), or it is turned off in "
        "its set (an `untested` result naming a standard). Never set on a pass or fail",
    )

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if self.status == "not_applicable" and self.reason is None:
            raise ValueError(f"{self.id}: a not_applicable result needs a reason")
        if self.status in ("pass", "fail") and self.reason is not None:
            raise ValueError(f"{self.id}: a {self.status} result has no reason")
        if self.status == "untested" and self.reason is not None and self.standard is None:
            raise ValueError(f"{self.id}: only a standard turned off in its set has a reason")
        if self.status == "not_applicable" and self.standard is None:
            raise ValueError(f"{self.id}: only a standard can be not applicable")
        if self.status in ("pass", "fail"):
            if self.measured is None or self.required is None:
                raise ValueError(f"{self.id}: a {self.status} result needs measured and required")
            if self.location is None:
                raise ValueError(f"{self.id}: a {self.status} result needs a location")
        if not (self.id == self.check or self.id.startswith(self.check + ":")):
            raise ValueError(f"{self.id}: id must be the check name or start with '<check>:'")
        return self


class CheckSummary(Contract):
    passed: int = Field(ge=0, alias="pass")
    failed: int = Field(ge=0, alias="fail")
    untested: int = Field(ge=0)
    not_applicable: int = Field(
        default=0, ge=0, description="Standards that do not apply to this design"
    )

    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)


class CheckReport(Contract):
    contract: Literal["check-results/1"]
    build_id: StableId
    board: BoardRef | None = Field(
        description="The board revision the design was checked against; null for a design "
        "without a board"
    )
    results: list[CheckResult] = Field(min_length=1)
    summary: CheckSummary

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        ids = [r.id for r in self.results]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate result ids")
        counts = {s: sum(r.status == s for r in self.results) for s in STATUSES}
        got = {
            "pass": self.summary.passed,
            "fail": self.summary.failed,
            "untested": self.summary.untested,
            "not_applicable": self.summary.not_applicable,
        }
        if counts != got:
            raise ValueError(f"summary {got} does not match the results {counts}")
        return self


def validate_check_report(
    data: dict, standards: StandardsFile, mesh: MeshInfo | None = None
) -> CheckReport:
    """Validate a check report; raises `ContractError`."""
    try:
        report = CheckReport.model_validate(data)
    except ValidationError as error:
        raise ContractError(f"check results: {error}") from error
    by_standard = {r.standard for r in report.results if r.standard}
    for standard in standards.standards:
        if standard.id not in by_standard:
            raise ContractError(f"no result for standard {standard.id!r}")
    for result in report.results:
        if result.standard is not None:
            try:
                standard = standards.get(result.standard)
            except KeyError:
                raise ContractError(f"{result.id}: unknown standard {result.standard!r}") from None
            if result.check != standard.check:
                raise ContractError(
                    f"{result.id}: standard {standard.id} is enforced by {standard.check!r}, "
                    f"not {result.check!r}"
                )
            if result.status == "untested" and standard.enabled:
                raise ContractError(f"{result.id}: standard {standard.id} is on but not run")
            if result.status in ("pass", "fail") and not standard.enabled:
                raise ContractError(f"{result.id}: standard {standard.id} is turned off")
            if result.status == "not_applicable" and standard.applies(report.board is not None):
                raise ContractError(f"{result.id}: standard {standard.id} applies to this design")
            req = result.required
            if req is not None and (
                req.kind != standard.kind
                or req.value != standard.value
                or req.unit != standard.unit
            ):
                raise ContractError(f"{result.id}: required does not match standard {standard.id}")
        if mesh is not None and result.location is not None:
            check_location(result.location, mesh, result.id)
    return report
