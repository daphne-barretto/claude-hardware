"""Safety guardrails: what the agent may claim, and what it refuses.

- **Untested properties.** No check simulates strength, drop or heat (`geometry.untested.UNTESTED`),
  so nothing the agent writes may say or imply how the design performs on them. `claims(text)`
  finds the sentences that do; `finish` refuses a summary with one, and the evaluation runs scan
  every chat message and question with it. `with_untested(items)` puts the untested items in
  every check report the agent sees, whichever service made it.
- **Certification.** No check covers certification or regulatory compliance either, so nothing
  the agent writes may say the design is certified, compliant, approved or safe for a use
  ("UL listed", "meets EN 71", "child-safe", "food-safe"), unless it says that is not checked.
  `claims` finds those too (property `certification`).
- **Refuse.** The agent refuses weapons, weapon components and parts whose clear purpose is to
  cause harm, and parts whose clear purpose is to defeat a safety protection or evade a legal or
  regulatory requirement. It says so plainly and finishes with outcome `refused`, which shows
  in the chat as a `refused` notice; after that it can't edit. When the API's own safeguards
  refuse a request before Claude answers, `platform_refused` spots it and the run ends the same
  way, with `PLATFORM_REFUSAL` instead of the raw API error.
- **Warn and proceed.** For legitimate work on a regulated or safety-critical product (the
  `SAFETY_CATEGORIES`), the agent calls `safety_notice` before its first edit. `safety_notice`
  makes the notice (requirements vary by region, the kinds of regulations that may apply, none
  checked), and `note_section` puts it in the build note.

```python
claims("The lid is strong enough for a 1 m drop.")  # [Claim("strength", ...), Claim("drop", ...)]
claims("Strength, drop and heat are untested.")      # []
claims("The case is UL listed.")                      # [Claim("certification", ...)]
safety_notice(["children"])  # {"text": "This looks like ...", "regulations": [...], ...}
```
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.agent.services import CheckItem


def untested() -> dict[str, tuple[str, str]]:
    """The untested checks by name: `(title, what is not covered)`."""
    from geometry.untested import UNTESTED  # not geometry.report, which loads the CAD kernel

    return UNTESTED


def with_untested(items: list[CheckItem]) -> list[CheckItem]:
    """`items` plus an `untested` item for each untested check the report doesn't list yet."""
    from app.agent.services import CheckItem

    listed = {i.check for i in items if i.status == "untested"}
    extra = [
        CheckItem(
            id=check,
            check=check,
            status="untested",
            summary=f"{title} is not tested: {covers} Review it by hand; never claim it passes.",
        )
        for check, (title, covers) in untested().items()
        if check not in listed
    ]
    return [*items, *extra]


# --- Refusals by the API's safeguards ---------------------------------------------------------

PLATFORM_REFUSAL = (
    "I can't help design that: it reads as a part meant to cause harm. "
    "I'm happy to keep working on the enclosure."
)


def platform_refused(message: object) -> bool:
    """Whether an SDK message says the API's safeguards refused the request. The API stops with
    `stop_reason: "refusal"`; the CLI reports it as an "API Error" saying the safeguards flagged
    the message."""
    if getattr(message, "stop_reason", None) == "refusal":
        return True
    texts = [getattr(message, "result", None) or "", *(getattr(message, "errors", None) or [])]
    texts += [getattr(b, "text", "") for b in getattr(message, "content", None) or []]
    return any("API Error" in t and "safeguards flagged" in t for t in texts)


# --- Safety notices: warn and proceed -------------------------------------------------------

# Kinds of regulated or safety-critical products: what to call one, and the kinds of
# regulations that may apply to it. General on purpose: a notice names kinds of rules, never a
# particular standard as met.
SAFETY_CATEGORIES: dict[str, tuple[str, str]] = {
    "children": (
        "a toy or a product for children",
        "toy and children's product safety rules (small parts, sharp edges and points, "
        "materials and coatings, labelling)",
    ),
    "medical": (
        "a medical device",
        "medical device regulations (device classification, biocompatibility of materials, "
        "quality management)",
    ),
    "electrical": (
        "a mains-voltage or high-energy battery product",
        "electrical safety standards for enclosures, battery safety and transport rules, and "
        "material flammability ratings",
    ),
    "pressure": (
        "a pressure vessel or pressurised part",
        "pressure equipment codes and directives",
    ),
    "food_contact": (
        "a part that touches food or drink",
        "food-contact material regulations",
    ),
    "ppe": (
        "personal protective equipment",
        "personal protective equipment standards and their certification schemes",
    ),
    "vehicle": (
        "a vehicle, aircraft or drone part",
        "vehicle type-approval, aviation airworthiness or drone operating rules",
    ),
    "other": (
        "a regulated or safety-critical product",
        "product safety rules for this kind of product",
    ),
}
MAX_NOTE_ITEMS = 6
MAX_NOTE_TEXT = 160
SAFETY_HEADING = "Safety notice"


class NoticeError(ValueError):
    """Why `safety_notice` can't make the notice."""


def safety_notice(
    categories: list[str], regulations: list[str] | None = None, region: str | None = None
) -> dict:
    """The safety notice for a regulated or safety-critical product: `categories` are keys of
    `SAFETY_CATEGORIES`; `regulations` adds kinds of rules that may apply (the agent's, e.g. for
    a region the user named); `region` is the region the user named, never a guess. Returns the
    `notice` event's fields. Raises `NoticeError` for an unknown category or text that claims
    certification or untested performance."""
    keys = list(dict.fromkeys(categories or []))
    if not keys:
        raise NoticeError(f"Name at least one category: {', '.join(SAFETY_CATEGORIES)}.")
    unknown = [k for k in keys if k not in SAFETY_CATEGORIES]
    if unknown:
        raise NoticeError(
            f"Unknown categories {unknown}; choose from {', '.join(SAFETY_CATEGORIES)}."
        )
    extra = [r.strip() for r in regulations or [] if r and r.strip()]
    region = (region or "").strip() or None
    if len(extra) > MAX_NOTE_ITEMS:
        raise NoticeError(f"List at most {MAX_NOTE_ITEMS} regulations.")
    for text in [*extra, region or ""]:
        if len(text) > MAX_NOTE_TEXT:
            raise NoticeError(f"Keep each item under {MAX_NOTE_TEXT} characters: {text[:40]!r}…")
        if (why := claim_error(text)) is not None:
            raise NoticeError(why)
    labels = [SAFETY_CATEGORIES[k][0] for k in keys]
    kinds = list(dict.fromkeys([*(SAFETY_CATEGORIES[k][1] for k in keys), *extra]))
    what = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]
    where = (
        f"Requirements vary by region; for {region}, the regulations listed may apply."
        if region
        else "Requirements vary by region, and yours isn't known; the regulations listed may apply."
    )
    text = (
        f"This looks like {what}. {where} None of them were checked: the checks here cover the "
        "geometry only. Have the design reviewed against the rules where it will be made, sold "
        "or used."
    )
    return {"text": text, "categories": labels, "regulations": kinds, "region": region}


def _field(notice: Any, name: str) -> Any:
    return notice.get(name) if isinstance(notice, dict) else getattr(notice, name, None)


def note_section(notices: list[Any]) -> str:
    """The build note's safety section for a session's safety notices (events or dicts), or ""
    when it has none. Each distinct notice once, then every kind of regulation listed."""
    safety = [n for n in notices if _field(n, "kind") in (None, "safety")]
    if not safety:
        return ""
    texts = list(dict.fromkeys(_field(n, "text") for n in safety))
    kinds = list(dict.fromkeys(r for n in safety for r in _field(n, "regulations") or []))
    lines = [f"## {SAFETY_HEADING}", "", *(f"{t}\n" for t in texts)]
    if kinds:
        lines += ["Regulations that may apply (none were checked):", ""]
        lines += [f"- {k}" for k in kinds]
        lines.append("")
    return "\n".join(lines)


def with_safety_section(note: str, notices: list[Any]) -> str:
    """`note` (a build note) with the safety section before its file list (or at the end)."""
    section = note_section(notices)
    if not section:
        return note
    marker = "\n## Files\n"
    at = note.find(marker)
    if at < 0:
        return note.rstrip("\n") + "\n\n" + section
    return note[: at + 1] + section + "\n" + note[at + 1 :]


# --- Claims about untested properties ------------------------------------------------------------

# Certification and compliance: never checked, so never claimed. Acronyms of standards bodies,
# regulators and marks match in capitals only ("CE", not "ce").
REGIME_TERMS = (
    r"(?-i:(?:EN|IEC|ISO|UL|ASTM|ANSI|ASME|NSF|BS|DIN|CSA|JIS|SAE|FCC)[ -]?\d[\w./:-]*"
    r"|UL|CE|UKCA|FCC|RoHS|REACH|FDA|CPSIA|CPSC|LFGB|FAA|EASA|DOT|ATEX|PED|NIOSH|TÜV|TUV)"
    r"|(?:\w+[ -]){0,2}?(?:safety|regulatory|legal|toy|food[- ]contact|medical[- ]device"
    r"|electrical|fire|building|pressure[- ]equipment|aviation|PPE)"
    r" (?:standards?|regulations?|rules|requirements|codes?|directives?|laws?)"
    r"|regulations?|regulatory requirements|directives?|the law"
)
_REGIME = rf"(?:{REGIME_TERMS})"
CERT_TERMS = (
    # "UL listed", "CE marked", "FDA approved", "EN 71 compliant"
    rf"{_REGIME}[- ]?(?:compliant|certified|approved|cleared|listed|marked|conformant)"
    # "meets EN 71", "complies with the relevant safety standards"
    r"|(?:meets?|meeting|satisf(?:y|ies|ying)|pass(?:es|ed|ing)?|compl(?:y|ies|ying) with"
    r"|compliant with|conform(?:s|ing)? (?:to|with)|in (?:compliance|conformity|line) with"
    r"|certified (?:to|under|for|by|against)|approved (?:by|for|under)|fulfill?s?"
    r"|adheres? to|(?:designed|built|made) to)"
    rf" (?:[\w/]+[ -]){{0,4}}?{_REGIME}"
    r"|certified|certifiable|homologated|(?:fully|regulatory|code|legally) compliant"
    r"|(?:ensures?|achieves?|guarantees?|for|full|regulatory|legal) compliance"
    r"|(?:child|kid|baby|infant|toddler|food|dishwasher|microwave|skin|patient|finger|touch)"
    r"[- ]?(?:safe|proof)|childproof"
    r"|safe (?:for|around|with) (?:\w+ ){0,2}?(?:children|kids|babies|infants|toddlers|food"
    r"|drinks?|patients|medical use|mains)"
    r"|safe to (?:touch|eat|drink|use|handle|wear|mouth|chew)"
    r"|(?:pass(?:es)?|ready for|clears?) (?:\w+ ){0,2}?(?:certification|inspection"
    r"|type approval|homologation)"
    r"|(?:is|are|it's|they're|now) (?:\w+ )?safe(?=\s*(?:[.!;]|$))"
)
# What excuses a certification word: it is not checked, may apply, or still has to happen.
CERT_EXCUSES = (
    r"not (?:been )?(?:certified|approved|listed|compliant|assessed|evaluated)"
    r"|(?:isn't|aren't|wasn't|weren't|never) (?:\w+ )?(?:certified|approved|listed|compliant)"
    r"|uncertified|non-?compliant|weren't checked|none (?:of (?:them|it) )?(?:was|were|is|are)"
    r" checked|nothing (?:was|is) checked"
    r"|(?:may|might|could|can) (?:\w+ )?(?:apply|be required|be needed|need)"
    r"|vary|varies|depends? on"
    r"|(?:needs?|must|has|have|would have|should|before it can|to get(?: it)?|get it|until it is)"
    r" (?:to )?(?:be )?(?:\w+ )?(?:certified|approved|listed|assessed|tested)"
    r"|needs? (?:\w+ )?(?:certification|approval|assessment)"
)

# Words that put a sentence on an untested property. "heat-set" (the inserts) is not about heat.
PROPERTY_TERMS = {
    "strength": r"strength|strong(?:er|est)?|sturdy|sturdier|robust|rigid(?:ity)?|stiff(?:er|ness)?"
    r"|durab(?:le|ility)|tough(?:er|ness)?|break(?:s|ing)?|crack(?:s|ing)?|load(?:s|ing)?"
    r"|stiffens?|stiffened|stiffening|stress(?:es)?|hold(?:s)? up|snap(?:s)? off"
    # Giving way under a load ("the lid won't flex"). Not a flex cable or a bend in the wall.
    r"|flex(?:es|ing|ible)?(?! (?:cable|pcb|circuit|connector|ribbon)s?\b)"
    r"|(?<!the )(?<!a )(?<!each )(?<!this )(?<!that )bend(?:s|ing)?"
    r"(?! (?:radius|radii|line|relief)\b)|sag(?:s|ging)?|buckl(?:e|es|ing)"
    r"|deflect(?:s|ion|ing)?|give way|gives way",
    # "drop" and "fall" are also a change of a dimension ("the wall drops to 2.0 mm", "a drop
    # from 2.4 to 2.0 mm", "falls within the limit"): only the falling kind.
    "drop": r"drop-?(?:proof|tests?|tested|testing)|drop (?:resistance|performance|height|impact)"
    r"|(?:if|when|once|gets?|being|be|is|was|were) dropped|drops? (?:on|onto|off) "
    r"|(?:if|when|once) (?:it|they|the \w+) drops?"
    r"|(?:a|any|the) (?:\d[\d.]*\s*m |hard |accidental |short |long )?(?:drop|fall)s?"
    r"(?! (?:in|of|to|by)\b| from (?:\d|about|around)\b)"
    r"|falls?(?! (?:to|by|within|under|below|inside|outside|short|back|in line|into place)\b)"
    r"|falling|fell|impacts?|shocks?|knocks?(?!-?outs?\b)|knocked|jolts?|bangs?",
    "heat": r"(?<!-)heat(?!-set| set| sets)|thermal(?:ly)?|temperatures?|hot(?:ter)?|overheat\w*"
    r"|warm(?:s|er|ing)?|melt(?:s|ing)?|deform(?:s|ing)?|cool(?:s|er|ing)?|dissipat\w*"
    r"|air ?flow|ventilat\w*",
}

# Words that judge how the design performs. A property word alone ("a load path", "the thermal
# pad") is no claim; with one of these it is ("strong" is both).
CLAIM_TERMS = (
    r"survives?|surviving|withstands?|withstanding|handles?|handling|resists?|resistant"
    r"|tolerates?|rated|proof|safe(?:ly)?|enough|sufficient(?:ly)?|adequate(?:ly)?|fine|ok|okay"
    r"|pass(?:es|ed)?|meets?|tested|verified|validated|proven|guaranteed|ensures?"
    r"|won't|will not|doesn't|does not|shouldn't|should not|no risk|unlikely|less likely"
    r"|holds? up|improves?|improved|better (?:at|for|in|under|against)|reinforc\w*"
    r"|protects?|protected|resistance|prevents?|avoids?"
    r"|keeps? (?:it|them|the \w+(?: \w+)?) from"
    # "the vents keep the board cool", "airflow keeps temperatures down", "let heat escape"
    r"|keep(?:s|ing)? (?:\w+ ){0,3}?(?:cool|cooler|down|safe)"
    r"|let(?:s|ting)? (?:the )?heat (?:escape|out)|dissipat\w*"
    r"|for (?:added |more |extra |better )?(?:strength|rigidity|stiffness|durability"
    r"|impact|drop|heat|cooling)"
    r"|strong(?:er|est)?|sturdy|sturdier|robust|rigid(?:er)?|stiff(?:er|ens?|ened)?|durable"
    r"|tough(?:er)?"
    # Implied: "will absorb a knock", "can take a knock", "plenty of airflow", "better
    # ventilation". With a property word these say how much of it the design has or can take.
    r"|absorbs?|absorbing|absorbed|cushion(?:s|ed|ing)?|shrugs? off|tak(?:e|es|ing) (?:a|any)"
    r"|plenty|ample|lots of|more|extra|better|good|great|stops? (?:\w+ ){0,3}?from"
)

# A phrase that says a property is untested (or must be reviewed) excuses only the part of the
# clause it belongs to, see `_excused`.
UNTESTED_TERMS = (
    r"untested|not tested|not been tested|(?:isn't|aren't|wasn't|weren't|is not|are not) tested"
    r"|not simulated|(?:isn't|aren't|is not|are not) simulated|unverified|not verified"
    r"|no check|not checked|(?:isn't|aren't|is not|are not) checked|by hand|can't say"
    r"|cannot say|can't tell|cannot tell|unknown|not covered|needs? (?:a )?(?:physical )?test"
)

_PROPERTY = {p: re.compile(rf"\b(?:{t})\b", re.IGNORECASE) for p, t in PROPERTY_TERMS.items()}
_CLAIM = re.compile(rf"\b(?:{CLAIM_TERMS})\b", re.IGNORECASE)
_UNTESTED = re.compile(rf"\b(?:{UNTESTED_TERMS})\b", re.IGNORECASE)
_CERT = re.compile(rf"(?<![\w-])(?:{CERT_TERMS})(?![\w-])", re.IGNORECASE)
_CERT_EXCUSED = re.compile(rf"\b(?:{CERT_EXCUSES})\b", re.IGNORECASE)
# Sentences, and clauses joined by a comma, a dash, parentheses or "but": "It is strong enough,
# but heat is untested" and "It is strong enough (drop is untested)" still claim strength.
_SENTENCE = re.compile(
    r"(?<=[.!?;:])\s+|\n+|,\s+|\s*[\u2014\u2013]\s*|\s+-\s+|\s*[()]\s*"
    r"|\s+(?:but|though|although|while|whereas|yet)\s+",
    re.IGNORECASE,
)
# Parts of a clause joined by "and", "or" or "so": "It is strong enough and heat is untested"
# still claims strength.
_PART = re.compile(r"\s+(?:and|or|so|and so|plus)\s+", re.IGNORECASE)
# An untested phrase that introduces what is untested ("it is untested whether it survives a
# drop and stays strong") excuses the rest of the clause.
_UNTESTED_SCOPE = re.compile(
    rf"\b(?:{UNTESTED_TERMS})\b\s*,?\s*(?:whether|if|how|that|what)\b", re.IGNORECASE
)


# What separates clauses, kept so `_clauses` can tell a comma from the rest.
_SEPARATOR = re.compile(f"({_SENTENCE.pattern})", re.IGNORECASE)
_COMMA = re.compile(r",\s+")
# A clause that starts like a new statement, not the rest of the one before it.
_NEW_STATEMENT = re.compile(
    r"(?:and|or|so|plus|then|but|though|although|while|whereas|yet|however)\b", re.IGNORECASE
)


CERTIFICATION = "certification"


@dataclass(frozen=True)
class Claim:
    property: str  # strength, drop, heat or certification
    sentence: str


def claims(text: str) -> list[Claim]:
    """The sentences of `text` that claim (or imply) strength, drop or heat performance, or that
    the design is certified, compliant or safe for a use (`certification`). A phrase saying a
    property is untested excuses only its own part of the sentence, not the rest."""
    out = []
    for sentence in _clauses(text or ""):
        sentence = sentence.strip()
        if not sentence:
            continue
        found: set[str] = set()
        for part in _parts(sentence):
            if _UNTESTED.search(part):
                continue
            if _CLAIM.search(part):
                found |= {p for p, pattern in _PROPERTY.items() if pattern.search(part)}
            if _CERT.search(part) and not _CERT_EXCUSED.search(part):
                found.add(CERTIFICATION)
        out += [Claim(p, sentence) for p in (*_PROPERTY, CERTIFICATION) if p in found]
    return out


def _clauses(text: str) -> list[str]:
    """`text` split into clauses (`_SENTENCE`), except that commas after an untested phrase that
    introduces what is untested don't end it: in "it is untested whether the lid, after the
    change, survives a drop" the drop is still what is untested. A comma clause that starts a new
    statement ("..., and it won't flex") or any other separator ends it as usual."""
    pieces = _SEPARATOR.split(text)
    out = [pieces[0]]
    for sep, piece in zip(pieces[1::2], pieces[2::2], strict=True):
        if (
            _COMMA.fullmatch(sep)
            and _UNTESTED_SCOPE.search(out[-1])
            and not _NEW_STATEMENT.match(piece)
        ):
            out[-1] = f"{out[-1]}{sep}{piece}"
        else:
            out.append(piece)
    return out


def _parts(clause: str) -> list[str]:
    """`clause` split at "and"/"or"/"so", except after an untested phrase that introduces what is
    untested ("not tested whether ..."), which keeps the rest of the clause with it."""
    scope = _UNTESTED_SCOPE.search(clause)
    head, tail = (clause[: scope.start()], clause[scope.start() :]) if scope else (clause, "")
    parts = _PART.split(head) if head.strip() else []
    if tail:
        if parts:
            parts[-1] = f"{parts[-1]} {tail}"
        else:
            parts = [tail]
    return parts


def claim_error(text: str) -> str | None:
    """Why `text` may not be said, or None."""
    found = claims(text)
    if not found:
        return None
    reasons = []
    performance = [c for c in found if c.property != CERTIFICATION]
    if performance:
        props = sorted({c.property for c in performance})
        reasons.append(
            f"{', '.join(props).capitalize()} {'is' if len(props) == 1 else 'are'} not tested by "
            f"any check, so don't say or imply how the design performs on "
            f"{'it' if len(props) == 1 else 'them'}: {performance[0].sentence!r}. Say it is "
            "untested and needs review by hand."
        )
    cert = next((c for c in found if c.property == CERTIFICATION), None)
    if cert is not None:
        reasons.append(
            "No check covers certification or regulatory compliance, so don't say or imply the "
            f"design is certified, compliant, approved or safe for a use: {cert.sentence!r}. Say "
            "which kinds of regulations may apply and that none were checked."
        )
    return " ".join(reasons)


__all__ = [
    "CERTIFICATION",
    "PLATFORM_REFUSAL",
    "SAFETY_CATEGORIES",
    "Claim",
    "NoticeError",
    "claim_error",
    "claims",
    "note_section",
    "platform_refused",
    "safety_notice",
    "untested",
    "with_safety_section",
    "with_untested",
]
