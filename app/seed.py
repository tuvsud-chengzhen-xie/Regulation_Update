"""Bootstrap data: the administrator account and the monitored sources.

The source list is taken from `Database Format.xlsx` (the CEN/TC and ISO/TC
tables) and from the worked examples in the monitoring specification. Every
source is fetched live; each one uses the transport its site requires.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.extractors.registry import DEFAULT_FETCH_STRATEGY
from app.models import (
    FETCH_HTTP,
    POLICY_SOURCE_DATE,
    POLICY_YEAR_THEN_STAMP,
    ROLE_ADMIN,
    Source,
    User,
)

CEN_EXPORT = (
    "https://standards.cencenelec.eu/ords/f?p=205:75:::::"
    "FSP_ORG_ID,FSP_LANG_ID,FSP_EXPORT:{org},25,XLS&cs={cs}"
)

# (body, tc_code, title, url, publish-date policy, options)
# The transport is not listed here: it comes from the extractor for that body
# (browser for ISO/BIFMA/ALI/WSTDA, plain HTTP for the rest) and can be
# overridden per source afterwards.
SOURCES: list[tuple[str, str, str, str, str, dict]] = [
    # --- CEN: downloadable export, carries a real publication date ----------
    ("CEN", "CEN/TC 207", "Furniture",
     CEN_EXPORT.format(org="6188", cs="1C0F77BD6F199770C140923329CEA88A6"),
     POLICY_SOURCE_DATE, {}),
    ("CEN", "CEN/TC 52", "Safety of Toys",
     CEN_EXPORT.format(org="6036", cs="1ED4FE4666E18868F7986A60EB2721E84"),
     POLICY_SOURCE_DATE, {}),
    ("CEN", "CEN/TC 93", "Ladders",
     CEN_EXPORT.format(org="6076", cs="1D2AFC52C44682CDB2BE8010620F523ED"),
     POLICY_SOURCE_DATE, {}),
    ("CEN", "CEN/TC 281", "Appliances, solid fuels and firelighters for barbecuing",
     CEN_EXPORT.format(org="6262", cs="1F00ACE53FA98C8D302C2A593EF38D5A5"),
     POLICY_SOURCE_DATE, {}),
    ("CEN", "CEN/TC 33", "Doors, windows, shutters, building hardware and curtain walling",
     CEN_EXPORT.format(org="6017", cs="18B2533733E873C58DDFFB8AE9DC9005E"),
     POLICY_SOURCE_DATE, {}),
    ("CEN", "CEN/TC 293", "Assistive products and accessibility",
     CEN_EXPORT.format(org="6274", cs="15C6A144634C8A2F566473D855E33D683"),
     POLICY_SOURCE_DATE, {}),
    ("CEN", "CEN/TC 129", "Glass in building",
     CEN_EXPORT.format(org="6111", cs="14AF0A7A25DB08332A84ED07D26C60085"),
     POLICY_SOURCE_DATE, {}),
    ("CEN", "CEN/TC 194", "Utensils in contact with food",
     CEN_EXPORT.format(org="6175", cs="10120F26AC66AC919DA5D9BB2DAC1EE2C"),
     POLICY_SOURCE_DATE, {}),

    # --- ISO: filterable catalogue, "Published" filter encoded in the URL ---
    ("ISO", "ISO/TC 136", "Furniture",
     "https://www.iso.org/committee/52448/x/catalogue/p/1/u/0/w/0/d/0",
     POLICY_YEAR_THEN_STAMP, {}),
    ("ISO", "ISO/TC 181", "Safety of toys",
     "https://www.iso.org/committee/54044/x/catalogue/p/1/u/0/w/0/d/0",
     POLICY_YEAR_THEN_STAMP, {}),
    ("ISO", "ISO/TC 29/SC 5", "Grinding wheels and abrasives",
     "https://www.iso.org/committee/47502/x/catalogue/p/1/u/0/w/0/d/0",
     POLICY_YEAR_THEN_STAMP, {}),
    ("ISO", "ISO/TC 61/SC 4", "Burning behaviour",
     "https://www.iso.org/contents/data/committee/04/93/49306/x/catalogue/p/1/u/0/w/0/d/0",
     POLICY_YEAR_THEN_STAMP, {}),
    ("ISO", "ISO/TC 38/SC 2", "Cleansing, finishing and water resistance tests",
     "https://www.iso.org/committee/48198/x/catalogue/p/1/u/0/w/0/d/0",
     POLICY_YEAR_THEN_STAMP, {}),
    ("ISO", "ISO/TC 38/SC 1", "Tests for coloured textiles and colorants",
     "https://www.iso.org/committee/48172/x/catalogue/p/1/u/0/w/0/d/0",
     POLICY_YEAR_THEN_STAMP, {}),
    ("ISO", "ISO/TC 173/SC 1", "Wheelchairs",
     "https://www.iso.org/committee/53792/x/catalogue/p/1/u/0/w/0/d/0",
     POLICY_YEAR_THEN_STAMP, {}),
    ("ISO", "ISO/TC 173", "Assistive products",
     "https://www.iso.org/committee/53782/x/catalogue/p/1/u/0/w/0/d/0",
     POLICY_YEAR_THEN_STAMP, {}),

    # --- ASTM subcommittees -------------------------------------------------
    ("ASTM", "F15.42", "Furniture Safety",
     "https://www.astm.org/membership-participation/technical-committees/committee-f15/"
     "subcommittee-f15/jurisdiction-f1542",
     POLICY_YEAR_THEN_STAMP, {}),
    ("ASTM", "F15.22", "Toy Safety",
     "https://www.astm.org/membership-participation/technical-committees/committee-f15/"
     "subcommittee-f15/jurisdiction-f1522",
     POLICY_YEAR_THEN_STAMP, {}),
    ("ASTM", "F15.24", "Jewelry",
     "https://www.astm.org/membership-participation/technical-committees/committee-f15/"
     "subcommittee-f15/jurisdiction-f1524",
     POLICY_YEAR_THEN_STAMP, {}),

    # --- ASME designator groups (the four monitored areas) ------------------
    ("ASME", "A112.18.1/CSA B125.1", "Plumbing supply fittings",
     "https://www.asme.org/codes-standards/find-codes-standards",
     POLICY_YEAR_THEN_STAMP, {"designator": "A112.18.1/CSA B125.1"}),
    ("ASME", "B94", "Cutting tools (all B94)",
     "https://www.asme.org/codes-standards/find-codes-standards",
     POLICY_YEAR_THEN_STAMP, {"designator": "B94"}),
    ("ASME", "B107", "Hand tools (all B107)",
     "https://www.asme.org/codes-standards/find-codes-standards",
     POLICY_YEAR_THEN_STAMP, {"designator": "B107"}),
    ("ASME", "PASE", "Portable Automotive Service Equipment",
     "https://www.asme.org/codes-standards/find-codes-standards",
     POLICY_YEAR_THEN_STAMP, {"designator": "PASE"}),

    # --- single-site publishers --------------------------------------------
    ("BIFMA", "BIFMA", "BIFMA furniture standards",
     "https://www.madcad.com/store/?libraryFilterPublisherOnce=BIFMA",
     POLICY_YEAR_THEN_STAMP, {}),
    ("ALI", "ALI", "American Ladder Institute A14 series",
     "https://www.americanladderinstitute.org/page/A14Standards",
     POLICY_YEAR_THEN_STAMP, {}),
    ("WSTDA", "WSTDA", "Web Sling & Tie Down Association",
     "https://wstda.com/page/products",
     POLICY_YEAR_THEN_STAMP, {}),
]


def ensure_admin(db: Session) -> User:
    admin = db.scalar(select(User).where(User.email == settings.bootstrap_admin_email))
    if admin is None:
        admin = User(
            email=settings.bootstrap_admin_email,
            full_name="Bootstrap administrator",
            api_key=settings.bootstrap_admin_api_key,
            role=ROLE_ADMIN,
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)
    return admin


def ensure_sources(db: Session) -> int:
    created = 0
    for body, tc_code, title, url, policy, options in SOURCES:
        existing = db.scalar(
            select(Source).where(Source.body == body, Source.tc_code == tc_code)
        )
        if existing is not None:
            continue
        db.add(
            Source(
                body=body,
                tc_code=tc_code,
                title=title,
                url=url,
                publish_date_policy=policy,
                fetch_strategy=DEFAULT_FETCH_STRATEGY.get(body, FETCH_HTTP),
                options=options,
                id_prefix={"ASTM": "ASTM", "ASME": "ASME"}.get(body),
            )
        )
        created += 1
    if created:
        db.commit()
    return created


def bootstrap(db: Session) -> dict:
    admin = ensure_admin(db)
    created = ensure_sources(db)
    return {"admin": admin.email, "sources_created": created}
