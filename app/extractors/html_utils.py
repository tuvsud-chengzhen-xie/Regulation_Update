"""Shared HTML helpers for the website-scraping extractors."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.extractors.base import decode


def text_lines(payload: bytes) -> list[str]:
    """Visible text of a page as clean, non-empty lines."""
    soup = BeautifulSoup(decode(payload), "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    raw = soup.get_text("\n")
    lines = []
    for line in raw.split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return lines


def entry_block(lines: list[str], index: int, id_re, max_span: int = 8) -> list[str]:
    """The lines that belong to the listing entry starting at `index`.

    Listings wrap a description over several lines and put the edition year
    last, so a fixed two- or three-line lookahead silently loses entries with
    long descriptions. Reading up to the *next* entry's identifier keeps the
    whole block without ever borrowing the following entry's year.
    """
    block: list[str] = []
    for line in lines[index + 1 : index + 1 + max_span]:
        if id_re.match(line):
            break
        block.append(line)
    return block
