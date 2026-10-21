# Automated Standard Update Monitoring

A FastAPI application that periodically captures standard publication/update
information from selected standards bodies, compares it with the internal
monitoring database, records the changes, and alerts subscribed authorised
users — as specified in *Automated Standard Update Monitoring.docx*, using the
schema of *Database Format.xlsx*, and capturing training data per
*Unified_Annotation_Training_Data_Framework.docx*.

## Quick start

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
```

ISO, BIFMA, ALI and WSTDA are fetched through a real browser, so install one:

```bash
./.venv/bin/python -m playwright install chromium
```

```bash
./.venv/bin/uvicorn app.main:app --reload --port 8000
```

Then open <http://localhost:8000/> for the dashboard (API key `admin-demo-key`)
or <http://localhost:8000/docs> for the OpenAPI documentation. Press `?` in the
dashboard for the keyboard shortcuts, ⌘K for the command palette.

The first startup creates the SQLite database, the bootstrap administrator and
the 40 monitored sources taken from `Database Format.xlsx`, the
specification and the ASTM F15 subcommittee list. Click **Run monitoring** to perform a first run — it fetches
every source live, so allow a few minutes for it to finish.

```bash
./.venv/bin/python -m pytest
```

## Azure App Service deployment

Deploy the repository with a Python App Service and set its **Startup Command**
to:

```bash
./startup.sh
```

The script starts `app.main:app` on the `PORT` supplied by App Service and
binds it to `0.0.0.0`. Ensure the script is executable before deploying:

```bash
chmod +x startup.sh
```

On App Service the database and every file the application writes default to
the persistent `/home` share (`/home/data/standards.db`, `/home/data/exports`,
`/home/data/outbox`, `/home/AI_Training_Data`). The code itself may run from a
temporary copy under `/tmp` (Oryx build), so never point these settings at the
app folder; the startup log line `storage: ...` shows the paths in use and
warns if any of them would be lost on restart. To override them:

```text
DATABASE_URL=sqlite:////home/data/standards.db
EXPORT_DIR=/home/data/exports
OUTBOX_DIR=/home/data/outbox
TRAINING_DATA_DIR=/home/AI_Training_Data
```

Turn on **Always On** (Configuration > General settings): without it App
Service unloads an idle app, and the scheduled runs inside it never fire.

Set production values for `BOOTSTRAP_ADMIN_EMAIL` and
`BOOTSTRAP_ADMIN_API_KEY` before the first startup. Configure
`EMAIL_BACKEND=smtp` and the relevant `SMTP_*` settings when alerts should be
delivered by email. The monitoring schedule is set in the portal (Runs >
Schedule); run it on a single App Service instance, or each instance fires its
own run.

`startup.sh` installs Chromium for the browser-backed sources into
`/home/ms-playwright` in the background (log: `/home/LogFiles/playwright-install.log`);
set `INSTALL_BROWSER=false` to skip. Until it is ready, those sources fall back
to plain HTTPS and any failure is shown, with its cause, on the Issues page.

## What it does

### 1. Extraction

**Every run fetches every source live from its own site.** There is no offline
or replay mode; the bundled sample payloads exist only so the test suite can
exercise the parsers without touching the network.

Each source uses one of two transports, chosen by what its site requires:

| Transport | Used by | Why |
|---|---|---|
| `http` | CEN, ASTM, ASME | The catalogue is served directly — a plain HTTPS request is enough, and cheaper. |
| `browser` | ISO, BIFMA, ALI, WSTDA | The listing is built client-side and/or the site serves an interstitial check that a bare HTTP client never clears. The page is opened in a real headless Chromium, given time to run its own scripts, and read once the listing is in the DOM. |

| Body | Access method | Implementation |
|---|---|---|
| **CEN** | downloadable export | Parses the CEN/TC "XLS export", which is SpreadsheetML XML (with fallbacks for real `.xls`/`.xlsx` and delimited text). Columns `Reference / Date / Title`. |
| **ASME** | filterable website | The `find-codes-standards` page is a Vue front end over the public `/api/products?type=Codes-Standards` catalogue; the extractor pages through it and filters by designator (`PASE`, `B94`, `B107`, `A112.18.1/CSA B125.1`), keeping the latest edition of each. |
| **ASTM** | website | Parses subcommittee jurisdiction pages, e.g. `F2813-18(2023) Standard Specification for …`; skips `See also WK…`, work items and `(Withdrawn ….)` entries. |
| **ISO** | filterable website, browser | The "Published only" filter is the `/p/1/u/0/w/0/d/0` URL suffix and is enforced on every fetch. The committee page nonetheless carries every stage, so the **stage code decides**: 60.60/90.xx are captured, 95.xx (withdrawn), 60.00 (approved but still under publication, so the reference has no year yet) and 00–50.xx (still under development) are excluded. Reference and title come from ISO's own `.entry-name` / `.entry-description` markup. |
| **BIFMA** | website, browser | MadCAD storefront listing → `BIFMA G.81-2026`. Companion documents (PCRs, guidance manuals) are not standards and are skipped. |
| **ALI** | website, browser | A14 listing → `ANSI/ALI A14.1-2018`. |
| **WSTDA** | website, browser | Product listing with "Current Edition" year → `WSTDA-WS-1-2015`. |

Every extractor emits the seven-column target format. Unavailable fields
(Chinese title, implement date) are left blank, and status is set to `Current`.

**Publish date** follows the rules in the specification:

* CEN carries a real date (`2012-07-18`) and it is stored verbatim.
* Other bodies expose only a year. The first run stores that year (`2024`);
  a standard first captured during a later monitoring run is stamped with the
  extraction year and month (`2026/08`), which is also the fallback whenever a
  source exposes no date at all. The stamp is written once and never rewritten.

### 2. The update rule

Standards are compared on the full **Standard ID + Version**, scoped to their
source:

* in the database but no longer in the extraction → status becomes `Withdrawn`;
* in the extraction but not in the database → added as `Current`;
* a withdrawn standard that reappears is reinstated rather than duplicated;
* title/date corrections are recorded as `modified` without touching status.

The specification's worked example is a test: with `EN 581-1:1998` stored and
`EN 581-1:2026` extracted, the first becomes `Withdrawn` and the second is
added as `Current` — two rows.

**Change classification and traceability.** Each reference is split into
family, edition and suffixes (`EN 16121:2023/AC:2025` → family `EN 16121`,
corrigendum to `EN 16121:2023`), so every change says what it is:

| Change | Meaning |
|---|---|
| `added` | a standard the source never published before |
| `updated` | a new edition, amendment (A1) or corrigendum (AC) of a standard already in the database |
| `withdrawn` | removed from the source with no replacement in the same run |
| `superseded` | removed and replaced in the same run (`superseded_by` names the replacement) |
| `reinstated` / `modified` | back in the source / metadata changed |

CEN lists corrigenda as their own rows, so a new AC is captured as its own
record (`updated`, relation `corrigendum`) and never withdraws its base
standard. Every record carries `first_seen_run_id`, `last_seen_run_id`,
`withdrawn_run_id`, `supersedes`/`superseded_by` and its last change;
`GET /api/standards/{id}/history` (click a row on the Standards page) shows
the record's changes across runs and its editions, amendments and corrigenda.

**Withdrawal validation.** A run that would withdraw more than
`MAX_WITHDRAWAL_RATIO` (default 50 %) of a source's current standards is held
back as a suspect extraction (`suspicious_withdrawal` on the Issues page) unless
the run is started with *Allow mass withdrawals*. CEN rows whose Reference is
not a CEN deliverable, and HTML error pages served in place of the export
(e.g. an expired `cs=` checksum), are reported instead of being parsed.

**Safety rule:** the withdrawal rule is applied only when a source reports a
successful extraction. A fetch error, a bot-protection interstitial, or an
empty result where the database holds records marks that source `failed` and
leaves its standards untouched, so a broken scraper can never mass-withdraw a
committee.

### 3. Subscriptions and alerts

Authorised employees subscribe **per standard**, **per committee (TC/SC)**,
**per standards body** or **per keyword**. After each run, every subscriber with
matching changes receives one digest email. A subscription to `EN 581-1` also
matches `EN 581-1:2026`, so subscribers follow a standard across versions;
`F1169` matches `ASTM F1169-25`, and committee `F15` matches `F15.42`. The first
capture of a newly added source is its whole catalogue, so it is not alerted.
Set `PUBLIC_BASE_URL` to link the digest to the portal (on App Service the
site's own host name is used automatically).

`EMAIL_BACKEND=console` (the default) writes each alert to `data/outbox/`
instead of sending it, so the flow is fully demonstrable offline. Set
`EMAIL_BACKEND=smtp` plus the `SMTP_*` variables for real delivery.

### 4. Access rights and export

Every request carries an individual API key in `X-API-Key`. Regular users read
the database and manage their own subscriptions; administrators additionally
manage users and sources, trigger runs, curate records and export.

`GET /api/export/standards.xlsx` (administrators only) produces a workbook whose
first seven columns are exactly the bilingual headers of `Database Format.xlsx`,
followed by the operational and traceability columns, plus `Sources`,
`Change Log` and `Export Check` sheets. It takes the same filters as the
Standards list (`q`, `status`, `body`, `tc_code`, `publication_type`,
`change_type`) and uses the same query, so **Export Excel** on the Standards page
exports exactly the rows on screen. After writing, the file is re-read and
compared row by row with the database; the result is in the `Export Check`
sheet, the `X-Export-Verified` header and `GET /api/export/verify`. Scraped text
is always written as text (no formula injection) and characters Excel rejects
are stripped instead of aborting the export.

### 5. Scheduling and coverage

The frequency is set in the portal (Runs > Schedule, `PUT /api/runs/schedule`):
daily, weekly, every two weeks, monthly or a crontab, optionally bounded by a
monitoring window (start/end date) so a trial period stops by itself. The
`SCHEDULE_*` variables are only the defaults until a schedule is saved.
`GET /api/runs/coverage` counts the new, updated and withdrawn/superseded
examples collected in the window against `COVERAGE_TARGET_*` (default 5 each);
first captures of a source are excluded, since they are a baseline, not news.

Runs started from the portal execute in the background (App Service drops
requests after 230 s); progress is committed source by source, only one run
executes at a time, and a run interrupted by a restart is closed as `failed`.

Monitoring covers the original 26 sources plus 14 further ASTM F15
subcommittees (juvenile and consumer products, which are revised often).
Administrators add CEN/TCs, ISO TCs/SCs or ASTM subcommittees on the Sources
page or in bulk with `POST /api/sources/import`; URLs are restricted to the
standards bodies' own sites.

### 6. AI training-data capture

Every mutation is written as W3C-compliant training data, per the mandatory
contract in §9.1 of the framework document:

* append-only JSONL at `AI_Training_Data/{session_id}/{project}.jsonl`;
* **events** with before/after state for each mutation, and **snapshots** as
  full `AnnotationCollection` documents after each run;
* `_serverTimestamp` and `_schemaVersion` stamped server-side;
* stable `ann-{uuid5}` identifiers so a standard keeps one annotation id for its
  whole lifecycle.

Because this application annotates catalogue records rather than images, the
image `PointSelector` is replaced by a **`RecordSelector`** — a §9.2 extension
point — and the domain action enums (`standard_create`, `standard_withdraw`, …)
are added. Both are backwards-compatible additions, so the schema version is
`1.1.0` under the SemVer policy of §9.3.

`GET /ai_training_schema` returns the machine-readable schema (public, as in the
framework document); `GET /ai_training_quality` runs the §10.1 data-quality
checks over the collected records.

## Browser-assisted fetching

ISO, BIFMA (MadCAD), ALI and WSTDA cannot be read with a plain HTTP client:
their listings are rendered client-side and their sites serve an interstitial
check first. Those four are fetched through a real headless Chromium
(Playwright), which runs the page's own scripts and waits for its interstitial
to finish the same way a person's browser does. One browser is launched per
run, lazily, and shared by every browser-backed source.

```bash
pip install playwright && python -m playwright install chromium
```

Each page is waited on by a **content selector** (for ISO, `table .entry-name`
— an actual catalogue entry), not by network-idle. Network-idle never settles
on pages that poll in the background, which stalls the fetch until the
navigation timeout; a selector says exactly what we came for and returns as
soon as it is there. A browser-backed source takes roughly 4–25 s, most of it
the site's own interstitial.

It does **not** attempt to solve interactive challenges. If one is still on
screen when `BROWSER_CHALLENGE_WAIT_SECONDS` runs out, that source fails loudly,
is recorded as a `source`-scope extraction issue, and its existing records are
left untouched — a site we could not read must never be mistaken for a
catalogue that emptied itself. A source needing more than this needs an
allow-listed egress IP or a licensed feed, and the Issues screen names it.

The transport is per source, not per body: `PATCH /api/sources/{id}` with
`{"fetch_strategy": "browser"}` moves any source across if a site changes.

### When a source is unavailable

Every request is logged with a cause — `timeout`, `dns`, `connection`, `tls`,
`rate_limited`, `forbidden`, `not_found`, `http_5xx`, `challenge`,
`error_page`, `empty_response`, `browser_unavailable`, `browser_crash`.
Transient causes are retried with exponential backoff (`FETCH_MAX_ATTEMPTS`,
`FETCH_RETRY_BACKOFF_SECONDS`); permanent ones fail at once. Error pages served
with HTTP 200 are recognised, a navigation timeout that still rendered the
listing is used, a crashed browser is relaunched, and if Chromium cannot start
at all, browser-backed sources are tried over plain HTTPS.

The portal shows it all: **Issues > Source fetch failures** lists each failed
source with cause, HTTP status, every attempt and its last success; the
**Sources** page shows each source's last result and failure streak
(`GET /api/runs/source-failures`, `GET /api/sources`).

### Sites that refuse automated access

WSTDA, MadCAD (BIFMA) and ALI answer automated requests with HTTP 403 or an
anti-bot check, from Azure and from the corporate network alike. The app does
not try to get past such checks. Instead, an administrator opens the source
link in a normal browser, saves the page (Ctrl+S, *Webpage, Complete*) and
clicks **Upload page** on the Sources page (`POST /api/sources/{id}/upload`).
The file goes through the same parser, validation and update rule as a fetch;
an uploaded verification page is rejected. Setting the source's transport to
`manual` makes scheduled runs skip it instead of reporting it as failed.

## Knowing what was missed

Silently dropping a catalogue entry is the one failure this app cannot afford:
a dropped entry is indistinguishable from a standard that left the catalogue,
and the update rule would eventually withdraw it. So every parser reports what
it could not capture, and each report is stored against the run with the
**verbatim source text**, so a user can see what the site actually said and
judge whether the parser or the site is at fault.

| Scope | Meaning | Effect on the database |
|---|---|---|
| `record` | An entry was seen but could not be turned into a standard — most often no edition year, so no "Standard ID + Version" could be formed. | **Not in the database.** Counted in `failed_record_count`. |
| `field` | The standard was captured, but a field could not be read (no title, no publish date). | In the database, with a gap. |
| `source` | A whole source could not be fetched or parsed. | Its existing records are left untouched; nothing is withdrawn. |

Surfaced in the **Issues** screen (KPI strip, scope chips, and a table showing
the raw text and its line/row number), on the Runs screen as a per-run *Failed*
count, as a warning toast after any run that lost something, and over the API:

```
GET /api/runs/issues?latest=true&scope=record
GET /api/runs/issues/summary?latest=true
GET /api/runs/{id}/issues
```

This reporting paid for itself immediately. Against the live sources it exposed
four parser defects that had been failing silently:

| Reported as | Actual defect | Effect |
|---|---|---|
| 105 × `unrecognised_reference` on ISO/TC 38/SC 1 | The reference pattern allowed only digits after a part dash, so letter-numbered parts (`ISO 105-F01:2001`) never matched | That committee captured **2 standards instead of 107** |
| `unrecognised_reference` on `IEC 80601-2-52:2026` | ISO committees also publish IEC-numbered standards; only `ISO` was accepted | Joint-committee standards were dropped |
| 4 × `missing_stage` on ISO/TC 173 | A TC page lists its own subcommittees, which look reference-shaped | Committee rows reported as lost standards |
| 4 × `missing_edition_year` on WSTDA | The lookahead window was shorter than a wrapped description, so `Current Edition: 2022` fell outside it | Four standards silently lost |

plus a BIFMA designator pattern that read "BIFMA PCR for Seating" as a standard
called "BIFMA PC". All are fixed, and every entry they were losing is now
captured. Without per-record reporting each of these would have looked like a
standard that had left the catalogue.

## User interface

The UI is built on the **CPS CoE Design System v1.1**, vendored under `app/ds/`
and mounted at `/ds` (app assets at `/static`), exactly as its README
prescribes — so the design system can be replaced wholesale on an update
without touching app code. Nothing in `app/static/app.css` overrides
`shell.css`.

**Corporate identity** — the gradient topbar with the TÜV SÜD mark and the
"by CPS Center of Excellence" sub-line, the top nav with the Setup pill, and
the page/rail layout system (`is-2col` for Runs and Subscriptions, `is-narrow`
for Sign in and Setup, full width for the dense tables: Standards, Change log,
Issues and Sources).

**Data surfaces follow `dashboards.md`**, which is where a monitoring database
belongs:

* page anatomy in order — head, scope chips, active-filter chips, KPI strip,
  then the rows;
* one glass card treatment on an atmospheric wash;
* four-tile KPI strip with the headline tile gradient-filled, `tabular-nums`
  everywhere, entrances staggered 60 ms and played **on mount only**;
* Pattern-A scan tables — fixed layout with an explicit `colgroup`, sticky
  header, one row = one line, text left, numbers right;
* Pattern-B master–detail for reading an alert in full while the list stays
  in view.

**Loading behaves as the guide requires.** The page never blanks: first open
paints a skeleton in the real layout's shape (tile and row heights measured
against the live ones, not guessed), and later loads keep the dashboard on
screen at 50% opacity — opacity only, never a transform. A filter change
**patches** values in place; the table, KPI tiles and chip row are the same DOM
nodes afterwards, which is asserted by node identity rather than by eye. Search
commits on a trailing 140 ms debounce, every repeated request carries a
sequence guard so a slow early response cannot overwrite a newer one, and
focus and caret position survive a filter change. Changing figures tick from
the value on screen — never from zero.

**Productivity nudges** — toasts instead of `alert()`, a ⌘K command palette and
a `?` shortcuts overlay (`/` to search, `R` to run monitoring, `E` to export,
`1`–`6` to navigate).

**The Savings chip** counts the cumulative catalogue updates the monitor has
caught. When a run finds changes, green octagons fly to the chip and the number
ticks up — the one exuberant moment, earned by the app's actual value event. A
run that finds nothing stays quiet.

**No AI identity, deliberately.** `AI-IDENTITY.md` reserves the command bar and
the octagon-swarm overlay for Layer-3 AI tools. This app's extraction is
deterministic scraping and its search box is a plain table filter, so it uses
none of it — using that language here would destroy the signal.

Every keyframe has a `prefers-reduced-motion` opt-out that keeps the colour and
state cues and drops the movement.

## Layout

```
app/
  main.py            FastAPI assembly, lifespan bootstrap, /ds + /static mounts
  config.py          settings (env / .env)
  models.py          SQLAlchemy models; Standard mirrors Database Format.xlsx
  schemas.py         Pydantic request/response models
  security.py        X-API-Key authentication, admin role
  seed.py            bootstrap administrator + the 25 monitored sources
  extractors/        one module per standards body, the http/browser transports,
                     and sample payloads used only by the tests
  services/
    diff.py          the update rule (pure)
    dates.py         publish-date rules
    ingest.py        run orchestration: extract -> reconcile -> record -> alert
    notify.py        subscription matching and email delivery
    exporter.py      Excel export in the Database Format layout
    scheduler.py     APScheduler periodic monitoring
    training.py      W3C/COCO training-data capture
    quality.py       §10.1 data-quality checks
  routers/           standards, sources, runs, subscriptions, users, export, training
  ds/                CPS CoE Design System v1.1, vendored verbatim (served at /ds)
  static/            index.html (app shell), app.css, app.js, logo.png
tests/               97 tests, including every worked example from the specification.
                     Extraction is stubbed with bundled sample payloads so the
                     suite never touches the network — the real parsers still run.
```

## API summary

| Method | Path | Access |
|---|---|---|
| GET | `/health` | public |
| GET | `/api/standards`, `/api/standards/{id}`, `/api/standards/{id}/history`, `/api/standards/statistics` | user |
| PATCH | `/api/standards/{id}` | admin |
| GET | `/api/sources`, `/api/sources/bodies` | user |
| POST/PATCH/DELETE | `/api/sources`, `/api/sources/import`, `/api/sources/{id}` | admin |
| POST | `/api/runs` (`background: true` returns 202) | admin |
| GET | `/api/runs`, `/api/runs/{id}`, `/api/runs/active`, `/api/runs/{id}/summary`, `/api/runs/changes`, `/api/runs/schedule`, `/api/runs/coverage` | user |
| PUT | `/api/runs/schedule` | admin |
| GET | `/api/runs/issues`, `/api/runs/issues/summary`, `/api/runs/{id}/issues`, `/api/runs/source-failures` | user |
| GET/POST/DELETE | `/api/subscriptions` | user (admins may act for others) |
| GET | `/api/subscriptions/alerts`, `/api/subscriptions/alerts/{id}` | user |
| GET | `/api/users/me` | user |
| GET/POST/PATCH/DELETE | `/api/users` | admin |
| GET | `/api/export/standards.xlsx`, `/api/export/verify` | admin |
| POST | `/ai_training_event`, `/ai_training_snapshot` | user |
| GET | `/ai_training_schema` | public |
| GET | `/ai_training_quality`, `/ai_training_sessions` | admin |

## Open points for the customer

* **Monitoring frequency** is "TBD" in the specification — it is now set in the
  portal, with an optional monitoring window and coverage targets.
* **Chinese titles** are never available from these sources; the column is left
  blank and can be curated via `PATCH /api/standards/{id}`.
* **Keyword subscriptions** are implemented as case-insensitive substring
  matching over Standard ID, English title and committee — the specification
  marks the keyword rules as "to be discussed".
* The **full CEN/TC and ISO/TC lists** are still to be supplied; the 25 seeded
  sources come from the specification and the Excel. New ones are added through
  `POST /api/sources` with no code change.
