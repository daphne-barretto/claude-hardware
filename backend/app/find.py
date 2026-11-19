"""Find in model: resolve a query in words ("all M3 holes", "faces under 1° of draft") to face IDs.

The query is read against what the build knows about itself, never against a fixed list of parts:

- **feature type** from the mesh's feature tags (`boss_1` is a `boss`, `opening_Q4` an `opening`
  for component Q4), the face names inside a feature (`outer_wall_1` is an outer wall) and the
  part names;
- **size** from the design parameters that drive a feature (`Parameter.feature`), and fastener
  sizes such as M3 from the spec standards that name them (`standards.yaml`);
- **analysis values** from the build's analysis maps (wall thickness, draft), per face, and their
  flagged spots. A qualifier without a number ("thin walls", "low draft") uses the map's standard.

Anything the reader doesn't recognise makes the result `unclear`: it says what it did understand
and asks, with the closest words it knows.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from app.contracts.maps import AnalysisMap
from app.contracts.mesh import MeshInfo, validate_mesh
from app.contracts.session import FindMatch, FindResult, Parameter
from app.standards import StandardsFile

# Feature kinds the generators make holes with, and other names people use for feature kinds.
HOLE_KINDS = ("insert", "screw")
KIND_SYNONYMS: dict[str, tuple[str, ...]] = {
    "hole": HOLE_KINDS,
    "pocket": ("insert",),
    "cutout": ("opening",),
    "window": ("opening",),
    "standoff": ("boss",),
    "pin": ("peg",),
}
STOPWORDS = frozenset(
    "a all an and any are at by every face faces feature features find for from get has have "
    "highlight in is it list me model my of on or part parts select show surface surfaces than "
    "that the there these those to which with where area areas region regions section sections "
    "spot spots".split()
)
# Comparators: words -> (op, implied quantity).
Op = Literal["lt", "le", "gt", "ge", "eq"]
COMPARATORS: list[tuple[tuple[str, ...], Op, str | None]] = [
    (("no", "more", "than"), "le", None),
    (("no", "less", "than"), "ge", None),
    (("less", "than"), "lt", None),
    (("smaller", "than"), "lt", "size"),
    (("thinner", "than"), "lt", "wall_thickness"),
    (("lower", "than"), "lt", None),
    (("narrower", "than"), "lt", "size"),
    (("more", "than"), "gt", None),
    (("greater", "than"), "gt", None),
    (("larger", "than"), "gt", "size"),
    (("bigger", "than"), "gt", "size"),
    (("wider", "than"), "gt", "size"),
    (("thicker", "than"), "gt", "wall_thickness"),
    (("at", "most"), "le", None),
    (("at", "least"), "ge", None),
    (("up", "to"), "le", None),
    (("under",), "lt", None),
    (("below",), "lt", None),
    (("over",), "gt", None),
    (("above",), "gt", None),
    (("<",), "lt", None),
    (("≤",), "le", None),
    (("<=",), "le", None),
    ((">",), "gt", None),
    (("≥",), "ge", None),
    ((">=",), "ge", None),
]
OP_WORDS = {"lt": "under", "le": "at most", "gt": "over", "ge": "at least", "eq": "of"}
UNITS: dict[str, tuple[str, float]] = {
    "mm": ("mm", 1.0),
    "millimetre": ("mm", 1.0),
    "millimetres": ("mm", 1.0),
    "millimeter": ("mm", 1.0),
    "millimeters": ("mm", 1.0),
    "inch": ("mm", 25.4),
    "inches": ("mm", 25.4),
    '"': ("mm", 25.4),
    "deg": ("deg", 1.0),
    "degree": ("deg", 1.0),
    "degrees": ("deg", 1.0),
    "°": ("deg", 1.0),
}
QUANTITY_WORDS = {
    "draft": "draft",
    "drafted": "draft",
    "angle": "draft",
    "thickness": "wall_thickness",
    "thick": "wall_thickness",
    "thin": "wall_thickness",
    "radius": "radius",
    "diameter": "diameter",
    "size": "size",
    "wide": "size",
    "width": "size",
    "big": "size",
    "small": "size",
    "large": "size",
}
LOW_WORDS = frozenset("low no insufficient missing zero without not enough little too".split())
FLAG_WORDS = frozenset("flagged problem problems issue issues".split())
MAP_NAMES = {"wall_thickness": "wall thickness", "draft": "draft"}
EQ_TOLERANCE = 0.05
TOKEN = re.compile(r"\d+(?:\.\d+)?|[a-z][a-z0-9]*|<=|>=|[<>≤≥°\"]")
FASTENER = re.compile(r"^m(\d+(?:\.\d+)?)$")


@dataclass
class _Face:
    id: str
    part: str
    feature: str
    kind: str
    words: frozenset[str]
    index: int  # in the part's `faces`, the value of `_FACE_ID`


@dataclass
class _Condition:
    quantity: str  # wall_thickness, draft, radius, diameter or size
    op: Op
    value: float
    unit: str
    words: str  # how it was read

    def holds(self, v: float) -> bool:
        if self.op == "lt":
            return v < self.value
        if self.op == "le":
            return v <= self.value
        if self.op == "gt":
            return v > self.value
        if self.op == "ge":
            return v >= self.value
        return abs(v - self.value) <= EQ_TOLERANCE


@dataclass
class _Query:
    kinds: set[str] = field(default_factory=set)
    kind_words: list[str] = field(default_factory=list)
    face_words: set[str] = field(default_factory=set)
    parts: set[str] = field(default_factory=set)
    features: set[str] = field(default_factory=set)
    conditions: list[_Condition] = field(default_factory=list)
    flagged: set[str] = field(default_factory=set)  # flagged spot kinds, or "*" for all
    fastener: str | None = None
    unknown: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (
            self.kinds
            or self.face_words
            or self.parts
            or self.features
            or self.conditions
            or self.flagged
            or self.fastener
        )


def feature_kind(feature: str) -> str:
    """`boss_1` -> `boss`, `opening_Q4` -> `opening`, `base` -> `base`."""
    return feature.split("_", 1)[0].lower()


def _face_words(face_id: str, feature: str) -> frozenset[str]:
    local = face_id[len(feature) + 1 :] if face_id.startswith(feature + ".") else face_id
    local = local.split(".")[-1]
    return frozenset(w for w in re.split(r"[_\-]+", local.lower()) if w and not w.isdigit())


def _singular(word: str, known: Iterable[str]) -> str:
    known = set(known)
    if word in known:
        return word
    if word.endswith("es") and word[:-2] in known:
        return word[:-2]
    if word.endswith("s") and word[:-1] in known:
        return word[:-1]
    return word


def _faces(mesh: MeshInfo) -> list[_Face]:
    return [
        _Face(f.id, part.name, f.feature, feature_kind(f.feature), _face_words(f.id, f.feature), i)
        for part in mesh.parts.values()
        for i, f in enumerate(part.faces)
    ]


def _face_values(mesh: MeshInfo, amap: AnalysisMap) -> dict[str, tuple[float, float]]:
    """Each face's lowest and highest value on the map, for faces the analysis applies to."""
    out: dict[str, tuple[float, float]] = {}
    for pv in amap.parts:
        part = mesh.parts.get(pv.part)
        if part is None:
            continue
        seen: dict[int, list[float]] = {}
        for face_index, value in zip(part.face_index, pv.values, strict=False):
            if value is not None:
                seen.setdefault(face_index, []).append(value)
        for face_index, values in seen.items():
            if face_index < len(part.faces):
                out[part.faces[face_index].id] = (min(values), max(values))
    return out


def _feature_sizes(parameters: Sequence[Parameter]) -> dict[str, dict[str, float]]:
    """Feature -> its sizes in mm from the parameters that drive it: radius and diameter for
    a `*_radius` or `*_diameter` parameter, else `size`."""
    sizes: dict[str, dict[str, float]] = {}
    for p in parameters:
        if not p.feature or p.unit != "mm" or isinstance(p.value, str):
            continue
        v = float(p.value)
        entry = sizes.setdefault(p.feature, {})
        if p.name.endswith("radius"):
            entry.update(radius=v, diameter=2 * v)
        elif p.name.endswith("diameter"):
            entry.update(diameter=v, radius=v / 2)
        else:
            entry.setdefault("size", v)
    return sizes


def _fasteners(standards: StandardsFile) -> dict[str, str]:
    """Fastener size ("M3") -> the title of the spec standard that names it."""
    out: dict[str, str] = {}
    for s in standards.standards:
        if s.kind == "spec" and isinstance(s.value, str):
            for m in re.finditer(r"\bM(\d+(?:\.\d+)?)\b", s.value):
                out.setdefault(f"M{m.group(1)}", s.title)
    return out


def _fmt(v: float) -> str:
    return f"{v:g}"


def _amount(c: _Condition) -> str:
    return f"{_fmt(c.value)}°" if c.unit == "deg" else f"{_fmt(c.value)} {c.unit}"


def _plural(word: str) -> str:
    if word.endswith(("s", "x", "ch", "sh")):
        return word + "es"
    return word + "s"


class _Reader:
    def __init__(
        self,
        faces: list[_Face],
        maps: dict[str, AnalysisMap],
        sizes: dict[str, dict[str, float]],
        standards: StandardsFile,
    ):
        self.faces = faces
        self.maps = maps
        self.sizes = sizes
        self.standards = standards
        self.part_names = {f.part.lower(): f.part for f in faces}
        self.kinds = {f.kind for f in faces} - set(self.part_names)
        self.features = {f.feature for f in faces}
        self.refs: dict[str, str] = {}  # component or feature suffix -> feature
        for feat in self.features:
            if "_" in feat:
                suffix = feat.split("_", 1)[1]
                if not suffix.isdigit():
                    self.refs.setdefault(suffix.lower(), feat)
        self.face_vocab = set().union(*(f.words for f in faces)) if faces else set()
        self.fasteners = _fasteners(standards)

    def vocabulary(self) -> list[str]:
        words = set(self.kinds) | set(self.part_names) | self.face_vocab
        words |= {w for w, kinds in KIND_SYNONYMS.items() if set(kinds) & self.kinds}
        words |= {"draft", "thickness", "thin", "thick", "radius", "diameter", "flagged"}
        words |= set(self.fasteners.keys())
        return sorted(w.lower() for w in words)

    def read(self, text: str) -> _Query:
        q = _Query()
        tokens = TOKEN.findall(text.lower().replace("’", "'"))
        used = [False] * len(tokens)
        quantities: list[tuple[int, str]] = []

        # Comparisons: <comparator> <number> [unit], "between a and b", or a bare "<number> <unit>".
        i = 0
        while i < len(tokens):
            match = None
            for words, op, implied in COMPARATORS:
                if tuple(tokens[i : i + len(words)]) == words:
                    match = (len(words), op, implied)
                    break
            if match and i + match[0] < len(tokens) and _is_number(tokens[i + match[0]]):
                n, op, implied = match
                j = i + n
                value = float(tokens[j])
                unit, scale, k = self._unit(tokens, j + 1)
                for t in range(i, k):
                    used[t] = True
                if implied:
                    quantities.append((i, implied))
                q.conditions.append(_Condition("", op, value * scale, unit, ""))
                i = k
                continue
            if tokens[i] == "between" and i + 3 < len(tokens) and tokens[i + 2] == "and":
                if _is_number(tokens[i + 1]) and _is_number(tokens[i + 3]):
                    unit, scale, k = self._unit(tokens, i + 4)
                    lo, hi = float(tokens[i + 1]) * scale, float(tokens[i + 3]) * scale
                    q.conditions.append(_Condition("", "ge", min(lo, hi), unit, ""))
                    q.conditions.append(_Condition("", "le", max(lo, hi), unit, ""))
                    for t in range(i, k):
                        used[t] = True
                    i = k
                    continue
            if _is_number(tokens[i]) and i + 1 < len(tokens) and tokens[i + 1] in UNITS:
                unit, scale, k = self._unit(tokens, i + 1)
                q.conditions.append(_Condition("", "eq", float(tokens[i]) * scale, unit, ""))
                for t in range(i, k):
                    used[t] = True
                i = k
                continue
            i += 1

        # Words: fastener sizes, parts, feature kinds and numbers, components, face names,
        # quantities and qualifiers.
        for i, tok in enumerate(tokens):
            if used[i]:
                continue
            fastener = FASTENER.match(tok)
            if fastener:
                q.fastener = f"M{fastener.group(1)}"
                used[i] = True
                continue
            if tok in self.part_names:
                q.parts.add(self.part_names[tok])
                used[i] = True
                continue
            if tok in self.refs:
                q.features.add(self.refs[tok])
                used[i] = True
                continue
            word = _singular(tok, set(self.kinds) | set(KIND_SYNONYMS) | self.face_vocab)
            if word in self.kinds or word in KIND_SYNONYMS:
                kinds = {word} if word in self.kinds else set(KIND_SYNONYMS[word])
                nxt = i + 1
                if nxt < len(tokens) and tokens[nxt].isdigit():
                    feature = next(
                        (f for f in self.features if f.lower() == f"{word}_{tokens[nxt]}"), None
                    )
                    if feature:
                        q.features.add(feature)
                        used[i] = used[nxt] = True
                        continue
                q.kinds |= kinds
                q.kind_words.append(word)
                used[i] = True
                continue
            if word == "wall" and i + 1 < len(tokens) and tokens[i + 1] == "thickness":
                used[i] = True  # "wall thickness" is the quantity, not wall faces
                continue
            if word in self.face_vocab and word not in QUANTITY_WORDS:
                q.face_words.add(word)
                used[i] = True
                continue
            if tok in QUANTITY_WORDS:
                quantities.append((i, QUANTITY_WORDS[tok]))
                used[i] = True
                continue
            if tok in FLAG_WORDS:
                q.flagged.add("*")
                used[i] = True
                continue

        self._apply_quantities(q, tokens, used, quantities)

        for i, tok in enumerate(tokens):
            if not used[i] and tok not in STOPWORDS and tok not in LOW_WORDS:
                q.unknown.append(tok)
        return q

    def _unit(self, tokens: list[str], j: int) -> tuple[str, float, int]:
        if j < len(tokens) and tokens[j] in UNITS:
            unit, scale = UNITS[tokens[j]]
            return unit, scale, j + 1
        return "", 1.0, j

    def _apply_quantities(
        self, q: _Query, tokens: list[str], used: list[bool], quantities: list[tuple[int, str]]
    ) -> None:
        named = [name for _, name in quantities]
        low = any(t in LOW_WORDS for t in tokens)
        # Numbers: give each comparison its quantity.
        for c in q.conditions:
            quantity = self._quantity_for(c, named, q)
            if quantity is None:
                q.problems.append(
                    f"{OP_WORDS[c.op]} {_fmt(c.value)}{(' ' + c.unit) if c.unit else ''}: "
                    "is that a wall thickness, a draft angle or a feature size?"
                )
                continue
            want = "deg" if quantity == "draft" else "mm"
            if c.unit and c.unit != want:
                q.problems.append(
                    f"{MAP_NAMES.get(quantity, quantity)} is measured in {want}, not {c.unit}"
                )
                continue
            c.quantity, c.unit = quantity, want
        # Qualifiers without a number: judged against the map's standard.
        if not q.conditions:
            tok_set = set(tokens)
            if "draft" in named or "drafted" in tok_set:
                if "draft" in self.maps:
                    q.conditions.append(self._standard_condition("draft", "lt"))
                else:
                    q.problems.append("this build has no draft map")
            if "thin" in tok_set and "wall_thickness" in named:
                if "wall_thickness" in self.maps:
                    q.conditions.append(self._standard_condition("wall_thickness", "lt"))
                else:
                    q.problems.append("this build has no wall thickness map")
            elif "thick" in tok_set and "wall_thickness" in named:
                q.flagged.add("thick")
            elif "thickness" in tok_set and not low:
                q.problems.append("thickness compared with what? For example under 2.5 mm")
        for c in q.conditions:
            if c.quantity in ("wall_thickness", "draft") and c.quantity not in self.maps:
                q.problems.append(f"this build has no {MAP_NAMES[c.quantity]} map")

    def _quantity_for(self, c: _Condition, named: list[str], q: _Query) -> str | None:
        if c.unit == "deg":
            return "draft"
        for name in named:
            if name in ("wall_thickness", "draft", "radius", "diameter"):
                return name
            if name == "size":
                return "diameter" if self._candidates_have("diameter", q) else "size"
        if c.unit == "mm" or not c.unit:
            if self._candidates_have("diameter", q):
                return "diameter"
            if self._candidates_have("size", q):
                return "size"
        return None

    def _candidates_have(self, measure: str, q: _Query) -> bool:
        if not (q.kinds or q.features or q.fastener):
            return False
        return any(measure in self.sizes.get(f.feature, {}) for f in self._filtered(q))

    def _standard_condition(self, quantity: str, op: Op) -> _Condition:
        amap = self.maps[quantity]
        standard = self.standards.get(amap.standard)
        value = float(standard.value) if not isinstance(standard.value, str) else 0.0
        # The standard by its title, as people read it (QA fix brief 2): "the minimum draft angle".
        title = standard.title[:1].lower() + standard.title[1:]
        c = _Condition(quantity, op, value, amap.unit, f"the {title}")
        return c

    def _filtered(self, q: _Query) -> list[_Face]:
        kinds = _kinds_of(q)
        out = []
        for f in self.faces:
            if q.parts and f.part not in q.parts:
                continue
            if (kinds or q.features) and not (f.kind in kinds or f.feature in q.features):
                continue
            if q.face_words and not q.face_words <= f.words:
                continue
            out.append(f)
        return out

    def describe(self, q: _Query) -> str:
        nouns = [_plural(n) for n in q.kind_words] or (["holes"] if q.fastener else [])
        subject = " or ".join(nouns)
        if q.face_words:
            words = " ".join(sorted(q.face_words))
            subject = f"{words} faces of {subject}" if subject else f"{words} faces"
        subject = subject or "faces"
        if q.fastener:
            subject = f"{q.fastener} {subject}"
        if q.features:
            subject += f" of {', '.join(sorted(q.features))}"
        if q.parts:
            subject += f" on {', '.join(sorted(q.parts))}"
        bits = [subject]
        conditions = [c for c in q.conditions if c.quantity]
        for n, c in enumerate(conditions):
            what = MAP_NAMES.get(c.quantity, c.quantity)
            nxt = conditions[n + 1] if n + 1 < len(conditions) else None
            if c.op == "ge" and nxt and nxt.op == "le" and nxt.quantity == c.quantity:
                bits.append(f"with {what} between {_fmt(c.value)} and {_amount(nxt)}")
                continue
            if n and c.op == "le" and conditions[n - 1].op == "ge":
                continue
            bits.append(f"with {what} {OP_WORDS[c.op]} {c.words or _amount(c)}")
        if q.flagged:
            kinds_text = (
                "flagged" if "*" in q.flagged else f"flagged {', '.join(sorted(q.flagged))}"
            )
            bits.append(f"in {kinds_text} spots")
        text = " ".join(bits)
        if q.fastener:
            standard = self.fasteners.get(q.fastener)
            if standard:
                text += f" ({standard})"
        return text[0].upper() + text[1:]

    def match(
        self, q: _Query, face_values: dict[str, dict[str, tuple[float, float]]]
    ) -> list[FindMatch]:
        flagged_faces: set[str] = set()
        for amap in self.maps.values():
            for spot in amap.flagged:
                if "*" in q.flagged or spot.kind in q.flagged:
                    flagged_faces.update(spot.location.faces)
        out = []
        for f in self._filtered(q):
            if q.flagged and f.id not in flagged_faces:
                continue
            value: float | None = None
            unit: str | None = None
            ok = True
            for c in q.conditions:
                v = self._value(f, c, face_values)
                if v is None or not c.holds(v):
                    ok = False
                    break
                if value is None:
                    value, unit = round(v, 4), c.unit
            if ok:
                out.append(
                    FindMatch(face=f.id, part=f.part, feature=f.feature, value=value, unit=unit)  # type: ignore[arg-type]
                )
        return out

    def _value(
        self, f: _Face, c: _Condition, face_values: dict[str, dict[str, tuple[float, float]]]
    ) -> float | None:
        if c.quantity in face_values:
            lo_hi = face_values[c.quantity].get(f.id)
            if lo_hi is None:
                return None
            # The face's extreme in the direction asked: its highest for "over", else lowest.
            return (
                lo_hi[0] if c.op in ("lt", "le") else lo_hi[1] if c.op in ("gt", "ge") else lo_hi[0]
            )
        return self.sizes.get(f.feature, {}).get(c.quantity)


def _kinds_of(q: _Query) -> set[str]:
    return set(q.kinds) or (set(HOLE_KINDS) if q.fastener else set())


def _is_number(tok: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:\.\d+)?", tok))


def find_in_model(
    query: str,
    mesh: MeshInfo,
    parameters: Sequence[Parameter],
    maps: dict[str, AnalysisMap] | Iterable[AnalysisMap],
    standards: StandardsFile,
) -> FindResult:
    """Resolve `query` to faces of `mesh`, using its feature tags, the design `parameters`, the
    build's analysis `maps` and the `standards`."""
    if not isinstance(maps, dict):
        maps = {m.type: m for m in maps}
    reader = _Reader(_faces(mesh), maps, _feature_sizes(parameters), standards)
    q = reader.read(query)
    face_values = {t: _face_values(mesh, m) for t, m in maps.items()}

    understood = reader.describe(q) if not q.empty else "Nothing I can search for"
    if q.unknown or q.problems or q.empty:
        return FindResult(
            query=query,
            status="unclear",
            understood=understood,
            question=_question(reader, q),
        )
    if q.fastener and q.fastener not in reader.fasteners:
        known = ", ".join(f"{k} ({v})" for k, v in sorted(reader.fasteners.items()))
        return FindResult(
            query=query,
            status="none",
            understood=f"{understood}. The constraints only use {known}"
            if known
            else f"{understood}. The constraints name no fastener size",
        )
    matches = reader.match(q, face_values)
    missing = sorted(_kinds_of(q) - {f.kind for f in reader.faces})
    if not matches and missing:
        understood += f". This build has no {' or '.join(_plural(k) for k in missing)}"
    return FindResult(
        query=query, status="found" if matches else "none", understood=understood, matches=matches
    )


def _question(reader: _Reader, q: _Query) -> str:
    vocab = reader.vocabulary()
    parts = []
    if q.unknown:
        words = ", ".join(f"“{w}”" for w in q.unknown)
        suggestions: list[str] = []
        for w in q.unknown:
            for s in difflib.get_close_matches(w, vocab, n=2, cutoff=0.7):
                if s not in suggestions:
                    suggestions.append(s)
        ask = f"I don't know {words} on this model."
        if suggestions:
            ask += f" Did you mean {' or '.join(f'“{s}”' for s in suggestions)}?"
        parts.append(ask)
    parts.extend(p[0].upper() + p[1:] + ("" if p.endswith("?") else ".") for p in q.problems)
    if q.empty and not q.unknown and not q.problems:
        parts.append("Which faces do you want?")
    examples = sorted(_plural(k) for k in reader.kinds)
    hint = "Try a feature type"
    if examples:
        hint += f" ({', '.join(examples)})"
    hint += ", a size (over 3 mm), or an analysis value (under 1° of draft, thinner than 2 mm)."
    parts.append(hint)
    return " ".join(parts)


def find_in_build(
    query: str,
    mesh_gltf: str | bytes | dict,
    parameters: Sequence[Parameter],
    maps: dict[str, dict],
    standards: StandardsFile,
) -> FindResult:
    """`find_in_model` on a build's served documents: its glTF and its analysis maps' JSON."""
    mesh = validate_mesh(mesh_gltf.encode() if isinstance(mesh_gltf, str) else mesh_gltf)
    parsed = {t: AnalysisMap.model_validate(doc) for t, doc in maps.items()}
    return find_in_model(query, mesh, parameters, parsed, standards)
