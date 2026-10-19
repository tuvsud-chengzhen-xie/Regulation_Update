"""ASME extractor.

`find-codes-standards` is a Vue front end over a public JSON catalogue at
`/api/products?type=Codes-Standards`. Filtering by Designator is done here
against the `FullDesignator` field, which is equivalent to the UI flow
"Designator -> B107 -> All B107 -> Apply" but does not depend on undocumented
query-string parameters.

Only the latest edition of each designator is captured ("the latest version of
year"), so a new edition naturally withdraws the previous one via the standard
update rule. The Standard ID is stored with an "ASME" prefix.
"""

from __future__ import annotations

import json

from app.extractors.base import (
    BaseExtractor,
    ExtractedStandard,
    ExtractorError,
    ParseResult,
    clean_text,
    decode,
)

API_URL = "https://www.asme.org/api/products"
PER_PAGE = 100
MAX_PAGES = 25


class ASMEExtractor(BaseExtractor):
    body = "ASME"
    extra_headers = {"Accept": "application/json"}

    @property
    def designator(self) -> str:
        return (self.source.options or {}).get("designator") or self.source.tc_code

    def fetch(self, ctx) -> bytes:
        """Page through the catalogue and return one merged JSON document."""
        results: list[dict] = []
        page = 1
        while page <= MAX_PAGES:
            query = (
                f"{API_URL}?type=Codes-Standards&page={page}"
                f"&perPage={PER_PAGE}&sortBy=&sortByDir=asc"
            )
            payload = ctx.http_get(query, body=self.body, headers=self.extra_headers)
            document = json.loads(decode(payload))
            results.extend(document.get("results") or [])
            if page >= int(document.get("pages") or 1):
                break
            page += 1
        return json.dumps({"results": results}).encode("utf-8")

    def parse(self, payload: bytes) -> ParseResult:
        try:
            document = json.loads(decode(payload))
        except json.JSONDecodeError as exc:
            raise ExtractorError(f"ASME catalogue was not valid JSON: {exc}") from exc

        wanted = self.designator
        result = ParseResult()
        for index, item in enumerate(document.get("results") or [], start=1):
            group = item.get("WebsiteProductGrouping") or item
            designator = _match_designator(group.get("FullDesignator") or [], wanted)
            if not designator:
                continue

            title = clean_text(group.get("DisplayTitle"))
            where = f"catalogue entry {index}"
            year = _latest_year(group)
            if not year:
                # Without an edition year there is no "Standard ID + Version"
                # to compare against, so the entry cannot enter the database.
                result.fail(
                    "missing_edition_year",
                    "Catalogue entry carries no edition year, so no "
                    "'Standard ID + Version' could be formed.",
                    raw=f"{designator} — {title or '(no title)'}",
                    standard_id=f"ASME {designator}",
                    location=where,
                )
                continue

            if not title:
                result.warn(
                    "missing_title",
                    "Catalogue entry has no display title.",
                    raw=designator,
                    standard_id=f"ASME {designator}-{year}",
                    location=where,
                )

            result.add(
                ExtractedStandard(
                    standard_id=f"ASME {designator}-{year}",
                    tc_code=self.source.tc_code,
                    title_en=title,
                    publish_date=str(year),
                    source_url=self.source.url,
                    extras={"designator": designator},
                )
            )
        return result


def _match_designator(designators: list[str], wanted: str) -> str | None:
    """Return the display form of a designator that falls under `wanted`.

    "B94" matches B94.54 and B94.9; "PASE" matches PASE only; a fully qualified
    designator such as "A112.18.1/CSA B125.1" matches itself.
    """
    normalised = []
    for value in designators:
        text = clean_text(value)
        if not text:
            continue
        if text.upper().startswith("ASME "):
            text = text[5:].strip()
        normalised.append(text)
    if not normalised:
        return None

    wanted = wanted.strip()
    for text in sorted(normalised, key=len):
        if text == wanted or text.startswith((f"{wanted}.", f"{wanted}/", f"{wanted} ", f"{wanted}-")):
            return text
    return None


def _latest_year(group: dict) -> int | None:
    years: list[int] = []
    for value in (group.get("DesignatorYears") or []) + (group.get("Editions") or []):
        try:
            years.append(int(str(value)[:4]))
        except (TypeError, ValueError):
            continue
    return max(years) if years else None
