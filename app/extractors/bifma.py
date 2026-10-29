"""BIFMA extractor (MadCAD storefront listing).

Listing entries look like:

    BIFMA G.81-2026   Furniture Surfaces: Design and Durability for ...

or, when the storefront splits the reference and the edition year across
elements, as an ID line followed by a title line and a year. Both shapes are
handled. Result: "BIFMA G.81-2026".

A reaffirmed edition is listed as "BIFMA X5.1-2017 (R2022) Office Chairs";
the reaffirmation is part of the version, so the result is
"BIFMA X5.1-2017(R2022)" with 2022 as its publish year.
"""

from __future__ import annotations

import re

from app.extractors.base import BaseExtractor, ExtractedStandard, ParseResult, clean_text
from app.extractors.html_utils import entry_block, text_lines

# "BIFMA G.81", "BIFMA X5.1", "ANSI/BIFMA e3-2019".
# The trailing look-ahead stops "BIFMA PCR for Seating" being read as "BIFMA PC".
ID_RE = re.compile(
    r"^(?:ANSI/)?BIFMA\s+([A-Z]{1,2}[0-9]*(?:\.[0-9A-Z]+)*)(?![A-Za-z])", re.IGNORECASE
)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
REAFFIRMED_RE = re.compile(r"\(\s*R\s*((?:19|20)\d{2})\s*\)", re.IGNORECASE)
# The storefront also sells companion documents that are not standards and
# carry no edition year of their own.
NON_STANDARD_RE = re.compile(r"guidance manual|\bPCR\b|price list|poster", re.IGNORECASE)


class BIFMAExtractor(BaseExtractor):
    body = "BIFMA"
    fetch_strategy = "browser"
    browser_wait_for = "a[href*='BIFMA'], .product, table"
    browser_wait_until = "domcontentloaded"
    extra_headers = {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"}

    def parse(self, payload: bytes) -> ParseResult:
        lines = text_lines(payload)
        result = ParseResult()

        for index, line in enumerate(lines):
            match = ID_RE.match(line)
            if not match:
                continue
            if NON_STANDARD_RE.search(line):
                continue  # companion document, not a standard
            designator = match.group(1)
            remainder = line[match.end() :].strip(" -–—:")
            block = entry_block(lines, index, ID_RE)
            where = f"line {index + 1}"

            year = _first_year(remainder) or _first_year(" ".join(block))
            reaffirmed = REAFFIRMED_RE.search(remainder) or REAFFIRMED_RE.search(" ".join(block))
            title = _strip_year(remainder) or _title_from(block)

            if not year:
                # The edition year IS the version half of "Standard ID +
                # Version"; without it the entry cannot be reconciled at all.
                result.fail(
                    "missing_edition_year",
                    "Storefront entry gave no edition year, so no "
                    "'Standard ID + Version' could be formed.",
                    raw=" ".join([line, *block]),
                    standard_id=f"BIFMA {designator}",
                    location=where,
                )
                continue

            reference = f"BIFMA {designator}-{year}"
            if reaffirmed:
                reference += f"(R{reaffirmed.group(1)})"

            if not title:
                result.warn(
                    "missing_title",
                    "Storefront entry has no readable standard title.",
                    raw=line,
                    standard_id=reference,
                    location=where,
                )

            result.add(
                ExtractedStandard(
                    standard_id=reference,
                    tc_code=self.source.tc_code,
                    title_en=title,
                    publish_date=reaffirmed.group(1) if reaffirmed else year,
                    source_url=self.source.url,
                )
            )
        return result


def _first_year(text: str | None) -> str | None:
    if not text:
        return None
    years = YEAR_RE.findall(text)
    return YEAR_RE.search(text).group(0) if years else None


def _strip_year(text: str) -> str | None:
    cleaned = YEAR_RE.sub("", REAFFIRMED_RE.sub("", text)).strip(" -–—,:")
    return clean_text(cleaned) if cleaned and len(cleaned) > 5 else None


def _title_from(block: list[str]) -> str | None:
    for candidate in block:
        stripped = _strip_year(candidate)
        if stripped:
            return stripped
    return None
