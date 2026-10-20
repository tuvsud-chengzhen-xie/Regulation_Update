"""Extractor registry and the fetch context used by live extraction."""

from __future__ import annotations

from app.extractors.ali import ALIExtractor
from app.extractors.asme import ASMEExtractor
from app.extractors.astm import ASTMExtractor
from app.extractors.base import (
    BaseExtractor,
    ExtractedStandard,
    ExtractionIssue,
    ExtractorError,
    ParseResult,
)
from app.extractors.bifma import BIFMAExtractor
from app.extractors.cen import CENExtractor
from app.extractors.fetching import FetchContext
from app.extractors.iso import ISOExtractor
from app.extractors.wstda import WSTDAExtractor

EXTRACTORS: dict[str, type[BaseExtractor]] = {
    "CEN": CENExtractor,
    "ISO": ISOExtractor,
    "ASTM": ASTMExtractor,
    "ASME": ASMEExtractor,
    "BIFMA": BIFMAExtractor,
    "ALI": ALIExtractor,
    "WSTDA": WSTDAExtractor,
}

SUPPORTED_BODIES = sorted(EXTRACTORS)

#: Transport each body needs by default, for seeding and for the Sources view.
DEFAULT_FETCH_STRATEGY: dict[str, str] = {
    body: cls.fetch_strategy for body, cls in EXTRACTORS.items()
}

#: Hosts a source URL may point at. The server fetches whatever URL a source
#: holds, so administrators can only add catalogue pages of the bodies' sites.
ALLOWED_HOSTS: dict[str, set[str]] = {
    "CEN": {"standards.cencenelec.eu"},
    "ISO": {"www.iso.org", "iso.org"},
    "ASTM": {"www.astm.org", "astm.org"},
    "ASME": {"www.asme.org", "asme.org"},
    "BIFMA": {"www.madcad.com", "madcad.com", "www.bifma.org", "bifma.org"},
    "ALI": {"www.americanladderinstitute.org", "americanladderinstitute.org"},
    "WSTDA": {"wstda.com", "www.wstda.com"},
}


def get_extractor(source) -> BaseExtractor:
    try:
        extractor_cls = EXTRACTORS[source.body.upper()]
    except KeyError as exc:
        raise ExtractorError(f"no extractor registered for body '{source.body}'") from exc
    return extractor_cls(source)


def build_context() -> FetchContext:
    """One fetch context per run: a shared HTTP client and, if any source needs
    it, a single headless browser."""
    return FetchContext()


def extract_source(source, ctx: FetchContext) -> ParseResult:
    return get_extractor(source).extract(ctx)


__all__ = [
    "ALLOWED_HOSTS",
    "DEFAULT_FETCH_STRATEGY",
    "EXTRACTORS",
    "SUPPORTED_BODIES",
    "ExtractedStandard",
    "ExtractionIssue",
    "ExtractorError",
    "FetchContext",
    "ParseResult",
    "build_context",
    "extract_source",
    "get_extractor",
]
