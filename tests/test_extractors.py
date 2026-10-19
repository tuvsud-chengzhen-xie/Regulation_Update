"""Every worked example in the specification must come out of the extractors."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.extractors.base import dedupe
from app.extractors.astm import publication_year
from app.extractors.iso import _with_published_filter, year_from_reference
from app.extractors.registry import get_extractor


def source(body, tc_code, sample, url="https://example.invalid/list", **options):
    return SimpleNamespace(
        body=body,
        tc_code=tc_code,
        sample=sample,
        url=url,
        options=options,
        id_prefix=None,
        fetch_strategy=None,
    )


def parse(src):
    """Run the real parser over a bundled sample payload."""
    extractor = get_extractor(src)
    result = extractor.parse(extractor.sample_bytes(src.sample))
    result.rows = dedupe(result.rows)
    return result


def extract(src):
    return {row.standard_id: row for row in parse(src).rows}


def test_cen_matches_specification_example():
    rows = extract(source("CEN", "CEN/TC 207", "cen_tc207.xml"))
    row = rows["EN 16122:2012"]
    assert row.tc_code == "CEN/TC 207"
    assert row.publish_date == "2012-07-18"
    assert row.title_en.startswith("Domestic and non-domestic storage furniture")
    assert len(rows) > 50


def test_iso_uses_version_year_and_drops_withdrawn_stages():
    rows = extract(source("ISO", "ISO/TC 181", "iso_tc181.html"))
    assert rows["ISO 8124-1:2022"].publish_date == "2022"
    assert rows["ISO 8124-1:2022"].tc_code == "ISO/TC 181"

    abrasives = extract(source("ISO", "ISO/TC 29/SC 5", "iso_tc29sc5.html"))
    assert "ISO 525:2020" in abrasives
    # Stage 95.99 is withdrawn and must never enter the database.
    assert "ISO 21949:1998" not in abrasives


def test_iso_published_filter_is_always_applied():
    assert _with_published_filter("https://www.iso.org/committee/47502/x/catalogue/") == (
        "https://www.iso.org/committee/47502/x/catalogue/p/1/u/0/w/0/d/0"
    )
    # An already-filtered URL is not doubled up.
    assert _with_published_filter(
        "https://www.iso.org/committee/47502/x/catalogue/p/1/u/0/w/0/d/0"
    ).endswith("/catalogue/p/1/u/0/w/0/d/0")


def test_astm_matches_specification_example():
    rows = extract(source("ASTM", "F15.42", "astm_f1542.html"))
    row = rows["ASTM F2813-18(2023)"]
    assert row.tc_code == "F15.42"
    assert row.publish_date == "2023"
    assert "Horizontal Surface" in row.title_en
    # "See also WK..." and withdrawn entries are ignored.
    assert not any(key.startswith("ASTM WK") for key in rows)


def test_astm_withdrawn_entries_are_skipped():
    rows = extract(source("ASTM", "F15.22", "astm_f1522.html"))
    assert "ASTM F963-23" in rows
    assert "ASTM F834-84(2008)" not in rows


@pytest.mark.parametrize(
    ("reference", "expected"),
    [("F2813-18(2023)", "2023"), ("F3096-23", "2023"), ("C1036-99", "1999"), ("D6400-12", "2012")],
)
def test_astm_publication_year(reference, expected):
    assert publication_year(reference) == expected


def test_asme_matches_specification_example():
    rows = extract(source("ASME", "PASE", "asme_products.json", designator="PASE"))
    row = rows["ASME PASE-2024"]
    assert row.tc_code == "PASE"
    assert row.publish_date == "2024"
    assert row.title_en == "Safety Standard for Portable Automotive Service Equipment"


def test_asme_designator_group_captures_all_children():
    rows = extract(source("ASME", "B107", "asme_products.json", designator="B107"))
    assert len(rows) > 5
    assert all(key.startswith("ASME B107.") for key in rows)


def test_bifma_matches_specification_example():
    rows = extract(source("BIFMA", "BIFMA", "bifma_store.html"))
    row = rows["BIFMA G.81-2026"]
    assert row.publish_date == "2026"
    assert row.title_en.startswith("Furniture Surfaces")


def test_ali_matches_specification_example():
    rows = extract(source("ALI", "ALI", "ali_a14.html"))
    row = rows["ANSI/ALI A14.1-2018"]
    assert row.title_en == "Portable Wood Ladder"
    assert row.publish_date == "2018"


def test_wstda_matches_specification_example():
    rows = extract(source("WSTDA", "WSTDA", "wstda_products.html"))
    row = rows["WSTDA-WS-1-2015"]
    assert row.title_en == "Synthetic Web Slings"
    assert row.publish_date == "2015"


def test_year_from_reference_handles_amendments():
    assert year_from_reference("ISO 8124-1:2022/Amd 1:2025") == "2025"
    assert year_from_reference("ISO 525:2000") == "2000"


def test_iso_reference_shapes_that_were_silently_lost():
    """Shapes a live run proved the original pattern dropped.

    ISO/TC 38/SC 1 captured 2 standards instead of 107 because letter-numbered
    parts never matched; joint committees publish IEC-numbered standards; and a
    TC page lists its own subcommittees, which are not standards at all.
    """
    from app.extractors.iso import COMMITTEE_RE, ISO_REF_RE

    for reference in (
        "ISO 525:2020",
        "ISO/R 603:1967",
        "ISO/TR 8124-8:2016",
        "ISO 8124-1:2022/Amd 1:2025",
        "ISO 105-F01:2001",              # letter-numbered part
        "ISO 105-A05:1996/Cor 2:2005",   # letter-numbered part + corrigendum
        "IEC 80601-2-52:2026",           # IEC-numbered, joint committee
    ):
        assert ISO_REF_RE.match(reference), reference

    for committee in ("ISO/TC 173/SC 1", "ISO/TC 38", "ISO/IEC JTC 1"):
        assert COMMITTEE_RE.match(committee), committee
    assert not COMMITTEE_RE.match("ISO 525:2020")


def test_iso_stage_decides_what_is_current():
    """The committee page carries every stage regardless of the URL filter."""
    from app.extractors.iso import stage_class

    assert stage_class("60.60") == "current"      # published
    assert stage_class("90.93") == "current"      # under periodic review, in force
    assert stage_class("95.99") == "withdrawn"
    assert stage_class("20.00") == "developing"   # approved work item
    assert stage_class("40.60") == "developing"   # draft international standard
    assert stage_class(None) == "unknown"


def test_a_listing_entry_without_an_edition_year_is_reported():
    """It must be reported, never dropped: a dropped entry looks withdrawn."""
    result = parse(source("ALI", "ALI", "ali_a14.html"))
    lost = [i for i in result.issues if i.scope == "record"]
    assert lost, "the ALI sample lists an errata entry with no edition year"
    assert lost[0].reason == "missing_edition_year"
    assert lost[0].raw          # the user can see what the site said
    assert lost[0].location     # and where on the page it was
