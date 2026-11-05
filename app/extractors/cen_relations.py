"""Supersession relations of a CEN publication, read from its project page.

The CEN export lists references only; "Supersedes / Superseded by" is on each
project page, whose URL carries a checksum (cs=) that only CEN's own standards
search hands out. So the reference is searched once in the browser, and the
project page it links to is read over plain HTTPS.
"""

from __future__ import annotations

import html as html_lib
from dataclasses import dataclass, field
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.config import settings
from app.extractors.base import clean_text, decode, normalize_standard_id

SEARCH_URL = "https://standards.cencenelec.eu/ords/f?p=CEN:105::RESET::::"
BASE_URL = "https://standards.cencenelec.eu/ords/"


@dataclass(slots=True)
class CENRelations:
    reference: str
    project_url: str
    supersedes: list[str] = field(default_factory=list)
    superseded_by: list[str] = field(default_factory=list)


def lookup(ctx, reference: str) -> CENRelations | None:
    """Relations of one reference, or None when CEN's search does not find it."""
    url = find_project_url(ctx, reference)
    if url is None:
        return None
    supersedes, superseded_by = parse_relations(ctx.http_get(url, body="CEN"))
    return CENRelations(reference, url, supersedes, superseded_by)


def find_project_url(ctx, reference: str) -> str | None:
    def search(page) -> str:
        page.fill("#STAND_REF", reference)
        with page.expect_navigation(
            wait_until="domcontentloaded", timeout=settings.browser_timeout_seconds * 1000
        ):
            page.press("#STAND_REF", "Enter")
        return page.content()

    return project_link(ctx.browser_interact(SEARCH_URL, search, body="CEN"), reference)


def project_link(search_html: str, reference: str) -> str | None:
    """The search result linking exactly this reference to its project page."""
    wanted = normalize_standard_id(reference)
    soup = BeautifulSoup(search_html, "lxml")
    for anchor in soup.find_all("a", href=True):
        href = html_lib.unescape(anchor["href"])
        if "CEN:110" not in href or "cs=" not in href:
            continue
        if normalize_standard_id(anchor.get_text(" ", strip=True)) == wanted:
            return urljoin(BASE_URL, href)
    return None


def parse_relations(payload: bytes | str) -> tuple[list[str], list[str]]:
    """(supersedes, superseded_by) from a project page's Relations table."""
    text = payload if isinstance(payload, str) else decode(payload)
    soup = BeautifulSoup(text, "lxml")
    found: dict[str, list[str]] = {"supersedes": [], "superseded by": []}
    for header in soup.find_all("th"):
        label = (clean_text(header.get_text(" ", strip=True)) or "").lower()
        if label not in found:
            continue
        cell = header.find_next_sibling("td")
        if cell is None:
            continue
        refs = [clean_text(a.get_text(" ", strip=True)) for a in cell.find_all("a")]
        found[label] = [ref for ref in refs if ref]
    return found["supersedes"], found["superseded by"]
