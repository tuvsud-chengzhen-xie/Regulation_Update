"""ISO extractor (TCs and SCs).

The ISO committee catalogue is a filterable page whose filter state is encoded
in the URL path: `/p/1/u/0/w/0/d/0` means published=1, under-development=0,
withdrawn=0, deleted=0 — exactly the "Published only" filter the specification
asks for. `_with_published_filter` guarantees it is applied even when a source
was configured with a bare catalogue URL.

The listing is rendered client-side and the site sits behind an interstitial
check, so this source uses the browser transport.

Publish date is the version year carried by the reference itself
(ISO 525:2000 -> 2000), per the worked example in the specification.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.extractors.base import (
    BaseExtractor,
    ExtractedStandard,
    ExtractorError,
    ParseResult,
    clean_text,
    decode,
)

# ISO 525:2000, ISO/TR 8124-8:2016, ISO 8124-1:2022/Amd 1:2025, ISO/IEC 17025:2017
# ISO 525:2000, ISO/R 603:1967, ISO/TR 8124-8:2016, ISO 8124-1:2022/Amd 1:2025,
# ISO 105-F01:2001 (letter-numbered parts), ISO 105-A05:1996/Cor 2:2005, and the
# IEC-numbered standards that sit under joint committees (IEC 80601-2-52:2026).
ISO_REF_RE = re.compile(
    r"^(?:ISO|IEC)(?:/[A-Z]{1,4})*\s+\d+(?:-[0-9A-Z]+)*(?::\d{4})?(?:/[A-Za-z0-9 .:-]+)?$"
)
# A cell that starts like a reference but does not match the full shape above.
ISO_REF_HINT_RE = re.compile(r"^(?:ISO|IEC)(?:/[A-Z]{1,4})*\s+\d", re.IGNORECASE)
# Committee identifiers (a TC page lists its own subcommittees). These are page
# furniture, not standards, so they are excluded rather than reported as lost.
COMMITTEE_RE = re.compile(r"^ISO(?:/IEC)?[/\s](?:TC|PC|JTC)\s*\d", re.IGNORECASE)
STAGE_RE = re.compile(r"^\d{2}\.\d{2}$")
# Withdrawn editions stay on the committee page, flagged inline as well.
WITHDRAWN_MARKER_RE = re.compile(r"\[\s*withdrawn\s*\]", re.IGNORECASE)


def stage_class(stage: str | None) -> str:
    """Classify an ISO harmonised stage code.

    00–50 are stages of development, 60 is publication, 90 is periodic review
    (still in force), 95 is withdrawal. The committee page carries all of them
    regardless of the catalogue filter, so the stage — not the page — decides
    what counts as a current standard.
    """
    if not stage:
        return "unknown"
    try:
        major = int(stage.split(".")[0])
    except ValueError:
        return "unknown"
    if major >= 95:
        return "withdrawn"
    if major >= 60:
        return "current"
    return "developing"

_FILTER_RE = re.compile(r"/p/\d/u/\d/w/\d/d/\d")


def _with_published_filter(url: str) -> str:
    """Force the catalogue filter to 'Published only'."""
    base = url.split("?")[0].rstrip("/")
    if _FILTER_RE.search(base):
        base = _FILTER_RE.sub("", base).rstrip("/")
    return f"{base}/p/1/u/0/w/0/d/0"


class ISOExtractor(BaseExtractor):
    body = "ISO"
    fetch_strategy = "browser"
    # The catalogue table is rendered client-side; an entry name is the exact
    # signal that the listing itself (not just the page chrome) has arrived.
    browser_wait_for = "table .entry-name"
    browser_wait_until = "domcontentloaded"
    extra_headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def target_url(self) -> str:
        return _with_published_filter(self.source.url)

    def parse(self, payload: bytes) -> ParseResult:
        html = decode(payload)
        soup = BeautifulSoup(html, "lxml")
        result = ParseResult()

        for number, row in enumerate(soup.find_all("tr"), start=1):
            cells = [clean_text(td.get_text(" ", strip=True)) or "" for td in row.find_all("td")]
            if not cells:
                continue

            where = f"row {number}"
            stage = next((c for c in cells if STAGE_RE.match(c)), None)
            state = stage_class(stage)

            # Withdrawn editions and work items still under development are
            # both correctly excluded, so neither is an extraction failure.
            if state == "withdrawn" or WITHDRAWN_MARKER_RE.search(cells[0]):
                continue
            if state == "developing":
                continue

            # The catalogue marks each entry up explicitly; fall back to
            # scanning the cell texts if ISO changes the markup.
            reference, marked_title = _from_markup(row)
            if reference is None:
                reference = next((c for c in cells if ISO_REF_RE.match(c)), None)

            if reference and COMMITTEE_RE.match(reference):
                continue  # a subcommittee listed on its parent TC's page

            if not reference or not ISO_REF_RE.match(reference):
                # A row that looks like a standard but whose reference does not
                # parse is a catalogue entry we are losing — say so.
                hint = reference or next((c for c in cells if ISO_REF_HINT_RE.match(c)), None)
                if hint:
                    result.fail(
                        "unrecognised_reference",
                        "Catalogue row looks like a published standard but its reference "
                        "did not match the expected ISO format.",
                        raw=" | ".join(cells),
                        standard_id=hint,
                        location=where,
                    )
                continue

            if state == "unknown":
                result.fail(
                    "missing_stage",
                    "Catalogue row carries no ISO stage code, so it could not be "
                    "confirmed as a published standard.",
                    raw=" | ".join(cells),
                    standard_id=reference,
                    location=where,
                )
                continue

            title = marked_title or _pick_title(cells, reference)
            if not title:
                result.warn(
                    "missing_title",
                    "Catalogue row has no readable English title.",
                    raw=" | ".join(cells),
                    standard_id=reference,
                    location=where,
                )

            publish_date = year_from_reference(reference)
            if not publish_date:
                result.warn(
                    "missing_publish_date",
                    "Reference carries no version year, so the internal registration "
                    "stamp was used instead.",
                    raw=reference,
                    standard_id=reference,
                    location=where,
                )

            result.add(
                ExtractedStandard(
                    standard_id=reference,
                    tc_code=self.source.tc_code,
                    title_en=title,
                    publish_date=publish_date,
                    source_url=self.source.url,
                    extras={"stage": stage} if stage else {},
                )
            )

        if not result.rows and "catalogue" not in html[:4000].lower():
            raise ExtractorError("ISO catalogue page did not contain a standards table")
        return result


def _from_markup(row) -> tuple[str | None, str | None]:
    """Read the reference and title from ISO's own per-entry markup.

    The catalogue wraps each entry as `.entry-name` (the reference) plus
    `.entry-description` (the title), which is far more reliable than picking
    the longest cell.
    """
    name = row.select_one(".entry-name")
    description = row.select_one(".entry-description")
    return (
        clean_text(name.get_text(" ", strip=True)) if name else None,
        clean_text(description.get_text(" ", strip=True)) if description else None,
    )


def _pick_title(cells: list[str], reference: str) -> str | None:
    """The title is the longest cell that is not the reference or a stage code."""
    candidates = [
        c
        for c in cells
        if c and c != reference and not STAGE_RE.match(c) and not c.isdigit() and len(c) > 8
    ]
    return max(candidates, key=len) if candidates else None


def year_from_reference(reference: str) -> str | None:
    """Latest 4-digit version year inside a reference (handles Amd/Cor suffixes)."""
    years = re.findall(r":(\d{4})", reference)
    if years:
        return max(years)
    match = re.search(r"\b(19|20)\d{2}\b", reference)
    return match.group(0) if match else None
