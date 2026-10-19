"""Web Sling & Tie Down Association extractor.

Product listing entries look like:

    WSTDA-WS-1   Synthetic Web Slings      Current Edition: 2015

The edition year is appended to the reference: "WSTDA-WS-1-2015".
"""

from __future__ import annotations

import re

from app.extractors.base import BaseExtractor, ExtractedStandard, ParseResult, clean_text
from app.extractors.html_utils import entry_block, text_lines

ID_RE = re.compile(r"^(WSTDA[-\s]?[A-Z]{1,3}[-\s]?[0-9]+(?:\.[0-9A-Z]+)*)", re.IGNORECASE)
EDITION_RE = re.compile(r"(?:current\s+edition|edition|revised)\D{0,12}((?:19|20)\d{2})", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


class WSTDAExtractor(BaseExtractor):
    body = "WSTDA"
    fetch_strategy = "browser"
    browser_wait_for = "main, .content, table"
    browser_wait_until = "domcontentloaded"
    extra_headers = {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"}

    def parse(self, payload: bytes) -> ParseResult:
        lines = text_lines(payload)
        result = ParseResult()

        for index, line in enumerate(lines):
            match = ID_RE.match(line)
            if not match:
                continue
            reference = re.sub(r"[\s]+", "-", match.group(1).upper())
            remainder = line[match.end() :].strip(" -–—:")
            block = entry_block(lines, index, ID_RE)
            context = " ".join([remainder, *block])
            where = f"line {index + 1}"

            edition = EDITION_RE.search(context) or YEAR_RE.search(context)
            if not edition:
                result.fail(
                    "missing_edition_year",
                    "Product listing gave no current edition year, so no "
                    "'Standard ID + Version' could be formed.",
                    raw=" ".join([line, *block]),
                    standard_id=reference,
                    location=where,
                )
                continue
            year = edition.group(1) if edition.lastindex else edition.group(0)

            title = _title(remainder) or _title(block[0] if block else None)
            if not title:
                result.warn(
                    "missing_title",
                    "Product listing entry has no readable standard title.",
                    raw=line,
                    standard_id=f"{reference}-{year}",
                    location=where,
                )

            result.add(
                ExtractedStandard(
                    standard_id=f"{reference}-{year}",
                    tc_code=self.source.tc_code,
                    title_en=title,
                    publish_date=year,
                    source_url=self.source.url,
                )
            )
        return result


def _title(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = EDITION_RE.sub("", text)
    cleaned = YEAR_RE.sub("", cleaned).strip(" -–—,:")
    cleaned = clean_text(cleaned)
    return cleaned if cleaned and len(cleaned) > 4 else None

