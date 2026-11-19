"""The rule catalogue: every kind of team standard the app can enforce, one per check.

A user can only make a rule of a kind listed here (`GET /api/standards/kinds`), so every
standard in every set has a check that enforces it (CLAUDE.md: a standard without a check is a
bug). A new kind of rule needs a new check in code (`geometry.checks`) and an entry here;
`tests/test_standards_editor.py` keeps the two in step.

Each kind fixes the rule's ID (the ID the check and the design code read the value by), its
comparison (`min`, `max`, `tolerance` or `spec`), its unit and the range of values the check
can sensibly judge. Ranges are physical sanity bounds, not team choices: a team's values live in
its sets (`standards.yaml` for the built-in set). This module never imports the CAD kernel.
"""

from __future__ import annotations

from app.contracts.session import CheckKindDimension as KindDimension
from app.contracts.session import CheckKindInfo as CheckKind
from app.standards import Standard, StandardsFile


def _kind(**fields) -> CheckKind:
    return CheckKind.model_validate(fields)


# One entry per standard check in `geometry.checks` (not the board or assembly checks, which
# every design with a board gets and no set can turn off).
KINDS: tuple[CheckKind, ...] = (
    _kind(
        check="wall_thickness",
        standard_id="min_wall",
        title="Minimum wall thickness",
        kind="min",
        unit="mm",
        min=0.2,
        max=50.0,
        phrase="Walls at least {value}",
        description="The thinnest wall of every part, measured by rays cast into the material "
        "from sample points on every face.",
        applies_to="any",
        value_label="Minimum wall",
    ),
    _kind(
        check="draft_angle",
        standard_id="min_draft",
        title="Minimum draft angle",
        kind="min",
        unit="deg",
        min=0.0,
        max=45.0,
        phrase="Draft at least {value} along the mold pull",
        description="The angle of every face that is not horizontal against the mold pull along Z.",
        applies_to="any",
        value_label="Minimum draft",
    ),
    _kind(
        check="overall_height",
        standard_id="max_height",
        title="Overall height limit",
        kind="max",
        unit="mm",
        min=1.0,
        max=5000.0,
        phrase="No taller than {value}",
        description="The Z extent of the closed design, all parts together.",
        applies_to="any",
        value_label="Maximum height",
    ),
    _kind(
        check="component_clearance",
        standard_id="min_component_clearance",
        title="Minimum clearance to components",
        kind="min",
        unit="mm",
        min=0.0,
        max=50.0,
        phrase="At least {value} from every board component",
        description="The exact distance from each board component's body to every part.",
        applies_to="board",
        value_label="Minimum clearance",
    ),
    _kind(
        check="fit_tolerance",
        standard_id="fit_tolerance",
        title="Fit tolerance",
        kind="tolerance",
        unit="mm",
        min=0.01,
        max=5.0,
        phrase="Mating features fit to ±{value}",
        description="Lid to base at the parting plane, and the walls of every opening against "
        "the size the board requires.",
        applies_to="board",
        value_label="Tolerance (±)",
    ),
    _kind(
        check="object_fit",
        standard_id="object_fit",
        title="Fit to the object",
        kind="max",
        unit="mm",
        min=0.01,
        max=50.0,
        phrase="Takes up none of the object and comes within {value} of it",
        description="Every part against the object it is made to fit: no overlap, and the "
        "closest part within the gap.",
        applies_to="object",
        value_label="Largest gap",
    ),
    _kind(
        check="material",
        standard_id="material",
        title="Material",
        kind="spec",
        phrase="Made of {value}",
        description="The material each part is set up for, which the molding rules assume.",
        applies_to="any",
        value_label="Material",
    ),
    _kind(
        check="insert_pockets",
        standard_id="heat_set_inserts",
        title="Heat-set inserts",
        kind="spec",
        phrase="Fasteners go into {value}",
        description="Every boss has a pocket that takes the insert, clear and deep enough, "
        "and the insert in it is seated within the fit tolerance.",
        applies_to="board",
        value_label="Insert",
        material=True,
        dimensions=[
            KindDimension(
                name="hole_diameter",
                label="Pocket diameter",
                unit="mm",
                min=0.5,
                max=30.0,
                description="The pocket the insert is pressed into",
            ),
            KindDimension(
                name="insert_length",
                label="Insert length",
                unit="mm",
                min=0.5,
                max=50.0,
                description="The pocket is at least this deep",
            ),
            KindDimension(
                name="screw_clearance",
                label="Screw clearance hole",
                unit="mm",
                min=0.5,
                max=30.0,
                description="The clearance hole for the screw in the mating part",
            ),
        ],
    ),
)

BY_CHECK = {k.check: k for k in KINDS}
BY_STANDARD = {k.standard_id: k for k in KINDS}


def kinds(built_in: StandardsFile | None = None) -> list[CheckKind]:
    """The catalogue, each kind with the built-in set's rule of that kind as its suggestion."""
    out = []
    for k in KINDS:
        rule = None
        if built_in is not None:
            rule = next((s for s in built_in.standards if s.check == k.check), None)
        suggested = summary(rule) if rule else None
        out.append(k.model_copy(update={"suggested": suggested}))
    return out


def summary(standard: Standard):
    """A rule as the API lists it (`StandardSummary`)."""
    from app.contracts.session import StandardSummary

    return StandardSummary(
        id=standard.id,
        title=standard.title,
        description=standard.description,
        check=standard.check,
        kind=standard.kind,
        value=standard.value,
        unit=standard.unit,
        applies_to=standard.applies_to,
        enabled=standard.enabled,
        aliases=list(standard.aliases),
        dimensions=dict(standard.dimensions) if standard.dimensions else None,
        material=standard.material,
        label=plain(standard),
    )


def _number(value: float) -> str:
    """At least one decimal, as the team writes sizes: 2.0, 0.2, 15.1."""
    return f"{value:.1f}" if float(value).is_integer() else f"{value:g}"


def value_text(standard: Standard) -> str:
    """The rule's value with its unit: "2 mm", "1°", "molded ABS"."""
    if standard.kind == "spec" or isinstance(standard.value, str):
        return str(standard.value)
    unit = "°" if standard.unit == "deg" else f" {standard.unit}"
    return f"{_number(float(standard.value))}{unit}"


def plain(standard: Standard) -> str:
    """The rule in plain language, as the editor's rows show it ("Walls at least 2 mm")."""
    kind = BY_CHECK.get(standard.check)
    if kind is None:
        return f"{standard.title}: {value_text(standard)}"
    return kind.phrase.replace("{value}", value_text(standard))


__all__ = [
    "BY_CHECK",
    "BY_STANDARD",
    "KINDS",
    "CheckKind",
    "KindDimension",
    "kinds",
    "plain",
    "summary",
    "value_text",
]
