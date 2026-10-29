"""American Ladder Institute (ANSI/ALI A14 series) extractor.

Listing entries look like:

    ANSI/ALI A14.1-2018   Portable Wood Ladder

The reference already carries the edition year, which is also used as the
publish date. A reaffirmed edition ("A14.3-2008 (R2018)") keeps the
reaffirmation in its version and uses that year as the publish date.

The page also lists errata sheets ("ANSI/ALI A14.1 Errata") without a year;
when the page lists exactly one edition of that standard, the errata is
recorded against it ("ANSI/ALI A14.1-2018 Errata").
"""

from __future__ import annotations

import re

from app.extractors.base import BaseExtractor, ExtractedStandard, ParseResult, clean_text
from app.extractors.html_utils import entry_block, text_lines

# The A14 series is always numbered (A14.1, A14.2, ...); requiring the
# sub-number keeps page headings such as "ANSI/ALI A14 Ladder Safety
# Standards" out of the results.
ID_RE = re.compile(
    r"^(?:ANSI[\s/-]*)?(ALI\s*A14\.[0-9A-Z]+(?:\.[0-9A-Z]+)*)\s*[-–—]?\s*((?:19|20)\d{2})?",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
REAFFIRMED_RE = re.compile(r"\(\s*R\s*((?:19|20)\d{2})\s*\)", re.IGNORECASE)
# The live page renders some years as two runs of digits: "Portable Wood Ladders - 20 26".
SPLIT_YEAR_RE = re.compile(r"(?<![\d.])(19|20)\s(\d{2})(?![\d.])")
ERRATA_RE = re.compile(r"\berrata\b", re.IGNORECASE)
_TRIM = " -–—:•·|"


class ALIExtractor(BaseExtractor):
    body = "ALI"
    fetch_strategy = "browser"
    browser_wait_for = "main, .content, article"
    browser_wait_until = "domcontentloaded"
    extra_headers = {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"}

    def parse(self, payload: bytes) -> ParseResult:
        lines = text_lines(payload)
        result = ParseResult()
        yearless_errata: list[tuple[str, str, str, list[str]]] = []
        editions: dict[str, set[str]] = {}

        for index, line in enumerate(lines):
            match = ID_RE.match(line)
            if not match:
                continue
            designator = re.sub(r"\s+", " ", match.group(1)).upper()
            year = match.group(2)
            remainder = SPLIT_YEAR_RE.sub(r"\1\2", line[match.end() :]).strip(_TRIM)
            block = entry_block(lines, index, ID_RE)
            where = f"line {index + 1}"
            if not year:
                found = YEAR_RE.search(remainder) or YEAR_RE.search(
                    SPLIT_YEAR_RE.sub(r"\1\2", " ".join(block))
                )
                year = found.group(0) if found else None
            reaffirmed = REAFFIRMED_RE.search(remainder)
            is_errata = bool(ERRATA_RE.search(line))

            if not year:
                if is_errata:
                    yearless_errata.append((designator, line, where, block))
                    continue
                result.fail(
                    "missing_edition_year",
                    "A14 listing gave no edition year, so no "
                    "'Standard ID + Version' could be formed.",
                    raw=" ".join([line, *block]),
                    standard_id=f"ANSI/{designator}",
                    location=where,
                )
                continue

            version = f"{year}(R{reaffirmed.group(1)})" if reaffirmed else year
            reference = f"ANSI/{designator}-{version}"
            if is_errata:
                reference += " Errata"
            else:
                editions.setdefault(designator, set()).add(version)

            title = _clean_title(ERRATA_RE.sub("", remainder)) if is_errata else (
                _clean_title(remainder) or _title_from(block)
            )
            if not title and not is_errata:
                result.warn(
                    "missing_title",
                    "A14 listing entry has no readable standard title.",
                    raw=line,
                    standard_id=reference,
                    location=where,
                )

            result.add(
                ExtractedStandard(
                    standard_id=reference,
                    tc_code=self.source.tc_code,
                    title_en=title or ("Errata" if is_errata else None),
                    publish_date=reaffirmed.group(1) if reaffirmed else year,
                    source_url=self.source.url,
                )
            )

        for designator, line, where, block in yearless_errata:
            listed = editions.get(designator, set())
            if len(listed) != 1:
                result.fail(
                    "missing_edition_year",
                    "Errata entry gives no edition year and the page does not list exactly "
                    "one edition of this standard, so it could not be attributed.",
                    raw=" ".join([line, *block]),
                    standard_id=f"ANSI/{designator} Errata",
                    location=where,
                )
                continue
            version = next(iter(listed))
            reaffirmed = REAFFIRMED_RE.search(version)
            reference = f"ANSI/{designator}-{version} Errata"
            result.warn(
                "edition_inferred",
                f"Errata entry gives no edition year; attributed to ANSI/{designator}-{version}, "
                "the only edition of that standard on the page.",
                raw=line,
                standard_id=reference,
                location=where,
            )
            result.add(
                ExtractedStandard(
                    standard_id=reference,
                    tc_code=self.source.tc_code,
                    title_en="Errata",
                    publish_date=reaffirmed.group(1) if reaffirmed else version[:4],
                    source_url=self.source.url,
                )
            )
        return result


def _clean_title(text: str) -> str | None:
    return clean_text(YEAR_RE.sub("", REAFFIRMED_RE.sub("", text)).strip(_TRIM))


def _title_from(block: list[str]) -> str | None:
    for candidate in block:
        stripped = _clean_title(candidate)
        if stripped and len(stripped) > 5:
            return stripped
    return None
