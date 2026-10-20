"""CEN/CENELEC extractor.

The source URL in the specification is the "XLS export" link of a CEN/TC work
programme. It actually returns SpreadsheetML 2003 (an XML workbook), not a
binary .xls, so the parser handles SpreadsheetML first and falls back to real
Excel workbooks and delimited text for robustness.

Source columns: Reference | Date | Title
"""

from __future__ import annotations

import csv
import io
import re
from xml.etree import ElementTree as ET

from app.extractors.base import (
    BaseExtractor,
    ExtractedStandard,
    ExtractorError,
    ParseResult,
    clean_text,
    decode,
)

SS_NS = "{urn:schemas-microsoft-com:office:spreadsheet}"
_HEADER_TOKENS = {"reference", "date", "title"}
# EN 16121:2023, EN 16121:2023/AC:2025, EN 1335-1:2020+A1:2022, CEN/TS 18231:2026,
# CEN/TR 14073-1:2004, EN ISO 8124-1:2022, CEN ISO/TS 16976-1:2015, CWA 17145-1:2017,
# CEN/CLC/TR 17602:2021, HD 1000:1988
CEN_REF_RE = re.compile(
    r"^(?:PR)?(?:[A-Z]{2,5}(?:/[A-Z]{2,5})*\s+)+(?:GUIDE\s+)?\d[\w.\-]*\s*:\s*\d{4}\b",
    re.IGNORECASE,
)


class CENExtractor(BaseExtractor):
    body = "CEN"

    def parse(self, payload: bytes) -> ParseResult:
        rows = self._rows(payload)
        if not rows:
            raise ExtractorError("CEN export contained no rows")

        result = ParseResult()
        for number, row in enumerate(rows, start=1):
            cells = [clean_text(c) or "" for c in row]
            if not any(cells):
                continue  # spacer row
            if {c.lower() for c in cells[:3]} & _HEADER_TOKENS:
                continue  # header row

            where = f"row {number}"
            if len(cells) < 2:
                result.fail(
                    "malformed_row",
                    "Export row has fewer columns than Reference | Date | Title.",
                    raw=" | ".join(cells),
                    location=where,
                )
                continue

            reference, date, title = (cells + ["", "", ""])[:3]
            if not reference:
                result.fail(
                    "missing_reference",
                    "Export row carries a title or date but no standard reference.",
                    raw=" | ".join(cells),
                    location=where,
                )
                continue
            if not CEN_REF_RE.match(reference):
                # Never let an unrecognised cell become a "standard": it would
                # be added as Current and later withdrawn as noise.
                result.fail(
                    "unrecognised_reference",
                    "The Reference column does not hold a CEN deliverable reference "
                    "(e.g. EN 16121:2023, EN 16121:2023/AC:2025, CEN/TS 18231:2026).",
                    raw=" | ".join(cells),
                    standard_id=reference,
                    location=where,
                )
                continue

            publish_date = _normalise_date(date)
            if not publish_date:
                result.warn(
                    "missing_publish_date",
                    "CEN normally publishes a date for every reference; none could be "
                    "read here, so the internal registration stamp was used instead.",
                    raw=" | ".join(cells),
                    standard_id=reference,
                    location=where,
                )
            if not title:
                result.warn(
                    "missing_title",
                    "Export row has no English title.",
                    raw=" | ".join(cells),
                    standard_id=reference,
                    location=where,
                )

            result.add(
                ExtractedStandard(
                    standard_id=reference,
                    tc_code=self.source.tc_code,
                    title_en=title or None,
                    # CEN publishes a real ISO-8601 date; keep it verbatim.
                    publish_date=publish_date,
                    source_url=self.source.url,
                )
            )
        return result

    # --- payload shapes ----------------------------------------------------
    def _rows(self, payload: bytes) -> list[list[str]]:
        head = payload[:512].lstrip()
        lowered = head[:256].lower()
        if lowered.startswith((b"<!doctype html", b"<html")) or b"<html" in lowered:
            # CEN answers a stale or altered export link with an HTML page and
            # HTTP 200; parsing that as delimited text would invent "standards".
            raise ExtractorError(
                "CEN returned an HTML page instead of the XLS export. The export link "
                "(including its cs= checksum) may have expired: copy a fresh 'XLS export' "
                "link from the committee page and update this source."
            )
        if head.startswith(b"<?xml") or b"urn:schemas-microsoft-com:office:spreadsheet" in head:
            return _spreadsheetml_rows(payload)
        if head.startswith(b"PK"):  # .xlsx
            return _xlsx_rows(payload)
        if head.startswith(b"\xd0\xcf\x11\xe0"):  # legacy .xls (OLE2)
            return _xls_rows(payload)
        return _delimited_rows(payload)


def _spreadsheetml_rows(payload: bytes) -> list[list[str]]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:  # pragma: no cover - malformed source
        raise ExtractorError(f"could not parse CEN SpreadsheetML: {exc}") from exc

    rows: list[list[str]] = []
    for row in root.iter(f"{SS_NS}Row"):
        cells: list[str] = []
        for cell in row.findall(f"{SS_NS}Cell"):
            # Honour ss:Index so that skipped (empty) columns keep alignment.
            index = cell.get(f"{SS_NS}Index")
            if index:
                while len(cells) < int(index) - 1:
                    cells.append("")
            data = cell.find(f"{SS_NS}Data")
            cells.append("".join(data.itertext()) if data is not None else "")
        rows.append(cells)
    return rows


def _xlsx_rows(payload: bytes) -> list[list[str]]:
    import openpyxl

    workbook = openpyxl.load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
    sheet = workbook.worksheets[0]
    return [["" if v is None else str(v) for v in row] for row in sheet.iter_rows(values_only=True)]


def _xls_rows(payload: bytes) -> list[list[str]]:
    import xlrd

    book = xlrd.open_workbook(file_contents=payload)
    sheet = book.sheet_by_index(0)
    return [[str(sheet.cell_value(r, c)) for c in range(sheet.ncols)] for r in range(sheet.nrows)]


def _delimited_rows(payload: bytes) -> list[list[str]]:
    text = decode(payload)
    delimiter = "\t" if text.count("\t") >= text.count(",") else ","
    return [row for row in csv.reader(io.StringIO(text), delimiter=delimiter) if row]


_DATE_RE = re.compile(r"(\d{4})[-/](\d{2})[-/](\d{2})")


def _normalise_date(value: str | None) -> str | None:
    """CEN dates arrive as 2012-07-18, sometimes tab-padded or as 18/07/2012."""
    text = clean_text(value)
    if not text:
        return None
    match = _DATE_RE.search(text)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    match = re.search(r"(\d{2})[-/](\d{2})[-/](\d{4})", text)
    if match:
        return f"{match.group(3)}-{match.group(2)}-{match.group(1)}"
    match = re.search(r"\b(19|20)\d{2}\b", text)
    return match.group(0) if match else None
