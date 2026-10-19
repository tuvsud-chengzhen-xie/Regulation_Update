"""American Ladder Institute (ANSI/ALI A14 series) extractor.

Listing entries look like:

    ANSI/ALI A14.1-2018   Portable Wood Ladder

The reference already carries the edition year, which is also used as the
publish date.
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


class ALIExtractor(BaseExtractor):
    body = "ALI"
    fetch_strategy = "browser"
    browser_wait_for = "main, .content, article"
    browser_wait_until = "domcontentloaded"
    extra_headers = {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"}

    def parse(self, payload: bytes) -> ParseResult:
        lines = text_lines(payload)
        result = ParseResult()

        for index, line in enumerate(lines):
            match = ID_RE.match(line)
            if not match:
                continue
            designator = re.sub(r"\s+", " ", match.group(1)).upper()
            year = match.group(2)
            remainder = line[match.end() :].strip(" -–—:")
            block = entry_block(lines, index, ID_RE)
            where = f"line {index + 1}"
            if not year:
                found = YEAR_RE.search(remainder) or YEAR_RE.search(" ".join(block))
                year = found.group(0) if found else None

            if not year:
                result.fail(
                    "missing_edition_year",
                    "A14 listing gave no edition year, so no "
                    "'Standard ID + Version' could be formed.",
                    raw=" ".join([line, *block]),
                    standard_id=f"ANSI/{designator}",
                    location=where,
                )
                continue

            title = clean_text(YEAR_RE.sub("", remainder).strip(" -–—:")) or _title_from(block)
            if not title:
                result.warn(
                    "missing_title",
                    "A14 listing entry has no readable standard title.",
                    raw=line,
                    standard_id=f"ANSI/{designator}-{year}",
                    location=where,
                )

            result.add(
                ExtractedStandard(
                    standard_id=f"ANSI/{designator}-{year}",
                    tc_code=self.source.tc_code,
                    title_en=title,
                    publish_date=year,
                    source_url=self.source.url,
                )
            )
        return result


def _title_from(block: list[str]) -> str | None:
    for candidate in block:
        stripped = clean_text(YEAR_RE.sub("", candidate).strip(" -–—:"))
        if stripped and len(stripped) > 5:
            return stripped
    return None
