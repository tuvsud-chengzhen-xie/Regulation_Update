"""Reading a "Standard ID + Version" into its parts.

The update rule compares full references, so `EN 16121:2023` and
`EN 16121:2023/AC:2025` are two separate rows. To report *what kind* of change
a new row is, and to link a withdrawn row to the one that replaced it, each
reference is split into:

* family     — the reference without edition or suffixes (``EN 16121``)
* edition    — the edition year (``2023``)
* kind       — Standard, Amendment, Corrigendum, Technical Report, ...
* suffixes   — amendments/corrigenda (``A1:2019``, ``AC:2025``)

``+`` joins a suffix into a consolidated document (``EN 1335-1:2020+A1:2022``
is the standard incorporating A1); ``/`` makes the suffix a document of its own
(``EN 16121:2023/AC:2025`` is the corrigendum itself).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.extractors.base import normalize_standard_id

KIND_STANDARD = "Standard"
KIND_AMENDMENT = "Amendment"
KIND_CORRIGENDUM = "Corrigendum"
KIND_TS = "Technical Specification"
KIND_TR = "Technical Report"
KIND_PAS = "Publicly Available Specification"
KIND_WORKSHOP = "Workshop Agreement"
KIND_GUIDE = "Guide"

_SUFFIX_RE = re.compile(
    r"\s*([+/])\s*(AC|COR\s*\d+|A\d+|AMD\s*\d+|DAM\s*\d+)\s*[:\-]\s*(\d{4}|\d{2})\b"
)
_COLON_EDITION_RE = re.compile(r"^(?P<family>.+?)\s*:\s*(?P<edition>\d{4})$")
# ASTM F2813-18(2023), ASME B94.11M-2018, WSTDA-WS-1-2015, ANSI/ALI A14.1-2018
_DASH_EDITION_RE = re.compile(
    r"^(?P<family>.+?)-(?P<edition>\d{4}|\d{2})[A-Z]?(?:\(\d{4}\))?(?:E\d+)?$"
)
_PREFIX_KINDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(^|[\s/])(TS)\b"), KIND_TS),
    (re.compile(r"(^|[\s/])(TR)\b"), KIND_TR),
    (re.compile(r"(^|[\s/])(PAS)\b"), KIND_PAS),
    (re.compile(r"^(CWA|IWA)\b"), KIND_WORKSHOP),
    (re.compile(r"(^|[\s/])GUIDE\b"), KIND_GUIDE),
)
_COLON_STYLE_BODIES = {"CEN", "ISO"}


@dataclass(frozen=True, slots=True)
class ReferenceInfo:
    reference: str
    family: str
    edition: str | None
    kind: str
    amendments: tuple[str, ...] = ()
    corrigenda: tuple[str, ...] = ()
    #: For a stand-alone amendment/corrigendum: the document it modifies.
    amends: str | None = None

    @property
    def is_standalone_modification(self) -> bool:
        return self.amends is not None


def describe_reference(standard_id: str, body: str | None = None) -> ReferenceInfo:
    text = normalize_standard_id(standard_id)
    matches = list(_SUFFIX_RE.finditer(text))
    head = text[: matches[0].start()].strip() if matches else text

    amendments: list[str] = []
    corrigenda: list[str] = []
    standalone: re.Match[str] | None = None
    for match in matches:
        label = re.sub(r"\s+", " ", match.group(2))
        token = f"{label}:{match.group(3)}"
        if label == "AC" or label.startswith("COR"):
            corrigenda.append(token)
        else:
            amendments.append(token)
        if match.group(1) == "/":
            standalone = match

    family, edition = _split_edition(head, body)
    kind = _base_kind(family)
    amends = None
    if standalone is not None:
        label = standalone.group(2)
        kind = KIND_CORRIGENDUM if label == "AC" or label.startswith("COR") else KIND_AMENDMENT
        amends = text[: standalone.start()].strip()

    return ReferenceInfo(
        reference=text,
        family=family,
        edition=edition,
        kind=kind,
        amendments=tuple(amendments),
        corrigenda=tuple(corrigenda),
        amends=amends,
    )


def _split_edition(head: str, body: str | None) -> tuple[str, str | None]:
    match = _COLON_EDITION_RE.match(head)
    if match:
        return match.group("family").strip(), match.group("edition")
    if ":" not in head and (body or "").upper() not in _COLON_STYLE_BODIES:
        match = _DASH_EDITION_RE.match(head)
        if match:
            return match.group("family").strip(), match.group("edition")
    return head, None


def _base_kind(family: str) -> str:
    for pattern, kind in _PREFIX_KINDS:
        if pattern.search(family):
            return kind
    return KIND_STANDARD
