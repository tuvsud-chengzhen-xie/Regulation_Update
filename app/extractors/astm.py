"""ASTM subcommittee extractor.

Each subcommittee jurisdiction page lists its standards as, for example:

    F2813-18(2023) Standard Specification for Glass Used as a Horizontal
    Surface in Desks and Tables

where 18 is the first publication year and (2023) the last reaffirmed year.
"See also WK....." lines and work-item ("WK") entries are ignored, per the
specification. The Standard ID is stored with an "ASTM" prefix.
"""

from __future__ import annotations

import re

from app.extractors.base import BaseExtractor, ExtractedStandard, ParseResult, clean_text
from app.extractors.html_utils import text_lines

# F2813-18(2023) / D6400-23 / C1036-21e1 / E2179-24a
ASTM_LINE_RE = re.compile(
    r"^(?P<ref>[A-Z]{1,2}\d{2,5}[A-Z]?(?:M)?\s*-\s*\d{2}[a-z]?(?:e\d)?(?:\s*\(\d{4}\))?(?:e\d)?)"
    r"\s*(?P<title>.*)$"
)
_IGNORE_RE = re.compile(r"^(see also|withdrawn|proposed new)", re.IGNORECASE)
# ASTM lists superseded documents in the same page with an explicit marker.
_WITHDRAWN_RE = re.compile(r"\(withdrawn\s*\d{4}\)", re.IGNORECASE)


class ASTMExtractor(BaseExtractor):
    body = "ASTM"
    extra_headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def parse(self, payload: bytes) -> ParseResult:
        result = ParseResult()
        for number, line in enumerate(text_lines(payload), start=1):
            if _IGNORE_RE.match(line) or line.startswith("WK") or _WITHDRAWN_RE.search(line):
                continue
            match = ASTM_LINE_RE.match(line)
            if not match:
                continue

            where = f"line {number}"
            reference = re.sub(r"\s*-\s*", "-", match.group("ref")).replace(" (", "(")
            title = clean_text(match.group("title"))
            if not title:
                # A bare designator with no title on a jurisdiction page is an
                # entry we cannot describe — report it instead of dropping it.
                result.fail(
                    "missing_title",
                    "Jurisdiction listing gave a designator with no standard title.",
                    raw=line,
                    standard_id=f"ASTM {reference}",
                    location=where,
                )
                continue
            if len(title) < 10:
                continue  # navigation chrome, not a standard

            # Drop the trailing "-See also WK..." fragment when it shares a line.
            title = re.split(r"\s*-?\s*See also\b", title, flags=re.IGNORECASE)[0].strip()

            year = publication_year(reference)
            if not year:
                result.warn(
                    "missing_publish_date",
                    "Neither an edition nor a reaffirmation year could be read from the "
                    "designator, so the internal registration stamp was used instead.",
                    raw=line,
                    standard_id=f"ASTM {reference}",
                    location=where,
                )

            result.add(
                ExtractedStandard(
                    standard_id=f"ASTM {reference}",
                    tc_code=self.source.tc_code,
                    title_en=title,
                    publish_date=year,
                    source_url=self.source.url,
                )
            )
        return result


def publication_year(reference: str) -> str | None:
    """Effective year: the reaffirmation year when present, else the edition year.

    F2813-18(2023) -> 2023 ; F3096-23 -> 2023 ; C1036-99 -> 1999
    """
    reaffirmed = re.search(r"\((\d{4})\)", reference)
    if reaffirmed:
        return reaffirmed.group(1)
    edition = re.search(r"-(\d{2})(?![\d])", reference)
    if edition:
        two_digit = int(edition.group(1))
        # ASTM two-digit years: 00-30 -> 2000s, 31-99 -> 1900s.
        return str(2000 + two_digit) if two_digit <= 30 else str(1900 + two_digit)
    return None
