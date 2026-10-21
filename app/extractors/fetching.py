"""Transports used by live extraction.

Two transports, chosen per source:

* **http** — a plain HTTPS request. Fast, cheap, and enough for endpoints that
  serve their catalogue directly (CEN's export, ASTM's server-rendered pages,
  ASME's product API).
* **browser** — the page is opened in a real headless Chromium, given time to
  run its own scripts, and read back once the listing is in the DOM. Needed by
  ISO, BIFMA, ALI and WSTDA: their listings are either built client-side or
  served behind an interstitial check that a bare HTTP client never clears.

The browser transport waits for a site's own interstitial to finish, the same
way a person's browser does. It does not attempt to solve interactive
challenges: if one is still on screen when the wait runs out, the source fails
loudly with an explanation, and the update rule leaves that committee's
existing records untouched.

Reliability
-----------
Every request is logged as a `FetchAttemptRecord` (kept by the context and
persisted by the run) with a machine-readable *cause* when it fails. Transient
causes — timeouts, connection resets, rate limiting, 5xx, a crashed browser —
are retried with exponential backoff; permanent ones fail immediately so a run
never spends minutes retrying a 404.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import httpx

from app.config import settings
from app.extractors.base import ExtractorError, decode

logger = logging.getLogger(__name__)

_CHALLENGE_MARKERS = (
    "just a moment...",
    "cf-browser-verification",
    "enable javascript and cookies to continue",
    "checking your browser before accessing",
    "attention required! | cloudflare",
    "verifying you are human",
)

# Pages a site serves with HTTP 200 in place of the content we asked for.
_ERROR_PAGE_MARKERS = (
    "session state protection violation",  # CEN (Oracle APEX): stale cs= checksum
    "sorry, this page isn't available",
    "502 bad gateway",
    "504 gateway time-out",
)

_UPLOAD_HINT = (
    "The site refuses automated access: open the source URL in your browser, wait for "
    "the list to appear, save it (Ctrl+S, 'Webpage, Complete') and upload the .html file "
    "on the Sources page"
)
_INSTALL_HINT = (
    "browser-assisted fetching needs Playwright: "
    "`pip install playwright && python -m playwright install --with-deps chromium`"
)

# --- failure causes ----------------------------------------------------------
CAUSE_TIMEOUT = "timeout"
CAUSE_DNS = "dns"
CAUSE_CONNECT = "connection"
CAUSE_TLS = "tls"
CAUSE_NETWORK = "network"
CAUSE_RATE_LIMITED = "rate_limited"
CAUSE_FORBIDDEN = "forbidden"
CAUSE_NOT_FOUND = "not_found"
CAUSE_HTTP_4XX = "http_4xx"
CAUSE_HTTP_5XX = "http_5xx"
CAUSE_CHALLENGE = "challenge"
CAUSE_ERROR_PAGE = "error_page"
CAUSE_EMPTY = "empty_response"
CAUSE_BROWSER_UNAVAILABLE = "browser_unavailable"
CAUSE_BROWSER_CRASH = "browser_crash"

#: Human-readable explanation for every cause, shown in the portal.
CAUSE_LABELS: dict[str, str] = {
    CAUSE_TIMEOUT: "The site did not respond in time",
    CAUSE_DNS: "The host name could not be resolved",
    CAUSE_CONNECT: "The connection was refused or reset",
    CAUSE_TLS: "The TLS/SSL handshake failed",
    CAUSE_NETWORK: "The connection dropped mid-transfer",
    CAUSE_RATE_LIMITED: "The site rate-limited this host (HTTP 429)",
    CAUSE_FORBIDDEN: "The site refused access (HTTP 401/403)",
    CAUSE_NOT_FOUND: "The page no longer exists (HTTP 404/410)",
    CAUSE_HTTP_4XX: "The site rejected the request (HTTP 4xx)",
    CAUSE_HTTP_5XX: "The site returned a server error (HTTP 5xx)",
    CAUSE_CHALLENGE: "An anti-bot interstitial never cleared",
    CAUSE_ERROR_PAGE: "The site served an error page instead of the catalogue",
    CAUSE_EMPTY: "The site returned an empty response",
    CAUSE_BROWSER_UNAVAILABLE: "The headless browser could not start on this host",
    CAUSE_BROWSER_CRASH: "The headless browser crashed during the fetch",
    "parse_error": "The page was fetched but could not be parsed",
    "empty_result": "The page parsed to 0 standards while the database holds some",
    "suspicious_withdrawal": "Too many standards would be withdrawn at once",
    "unexpected_error": "An unexpected error occurred",
}

_TRANSIENT_CAUSES = {
    CAUSE_TIMEOUT,
    CAUSE_CONNECT,
    CAUSE_NETWORK,
    CAUSE_RATE_LIMITED,
    CAUSE_HTTP_5XX,
    CAUSE_EMPTY,
    CAUSE_BROWSER_CRASH,
    CAUSE_DNS,  # resolver hiccups on App Service are usually momentary
}


class FetchError(ExtractorError):
    """A source could not be downloaded. `cause` says why, in one word."""

    def __init__(
        self,
        message: str,
        *,
        cause: str,
        url: str = "",
        http_status: int | None = None,
        transient: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.cause = cause
        self.url = url
        self.http_status = http_status
        self.transient = cause in _TRANSIENT_CAUSES if transient is None else transient


@dataclass(slots=True)
class FetchAttemptRecord:
    url: str
    transport: str
    attempt: int
    outcome: str  # ok / failed
    duration_ms: int
    cause: str | None = None
    http_status: int | None = None
    bytes: int = 0
    message: str | None = None


def looks_like_challenge(html: str) -> bool:
    head = html[:6000].lower()
    return any(marker in head for marker in _CHALLENGE_MARKERS)


def looks_like_error_page(html: str) -> bool:
    head = html[:6000].lower()
    return any(marker in head for marker in _ERROR_PAGE_MARKERS)


def status_cause(status: int) -> str:
    if status == 429:
        return CAUSE_RATE_LIMITED
    if status in (401, 403):
        return CAUSE_FORBIDDEN
    if status in (404, 410):
        return CAUSE_NOT_FOUND
    if status >= 500:
        return CAUSE_HTTP_5XX
    return CAUSE_HTTP_4XX


def classify_http_exception(exc: Exception, url: str) -> FetchError:
    """Map an httpx exception onto a FetchError with a cause."""
    if isinstance(exc, FetchError):
        return exc
    text = str(exc) or exc.__class__.__name__
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        hint = f". {_UPLOAD_HINT}." if status in (401, 403) else ""
        return FetchError(
            f"HTTP {status} from {url}{hint}", cause=status_cause(status), url=url,
            http_status=status,
        )
    if isinstance(exc, httpx.TimeoutException):
        return FetchError(f"timed out fetching {url}: {text}", cause=CAUSE_TIMEOUT, url=url)
    if isinstance(exc, httpx.ConnectError):
        lowered = text.lower()
        if any(m in lowered for m in ("name or service", "getaddrinfo", "nodename", "resolve")):
            cause = CAUSE_DNS
        elif any(m in lowered for m in ("ssl", "certificate", "tls")):
            cause = CAUSE_TLS
        else:
            cause = CAUSE_CONNECT
        return FetchError(f"could not connect to {url}: {text}", cause=cause, url=url)
    if isinstance(exc, httpx.HTTPError):
        return FetchError(f"connection dropped fetching {url}: {text}", cause=CAUSE_NETWORK, url=url)
    return FetchError(f"unexpected error fetching {url}: {text}", cause=CAUSE_NETWORK, url=url)


def check_content(payload: bytes, *, url: str, body: str, transport: str) -> None:
    """Reject payloads that are not the catalogue: empty, challenge, error page."""
    if not payload or not payload.strip():
        raise FetchError(
            f"{body or 'source'} returned an empty response for {url}", cause=CAUSE_EMPTY, url=url
        )
    head = decode(payload[:6000])
    if looks_like_challenge(head):
        if transport == "upload":
            hint = "The uploaded file is the site's verification page, not the catalogue"
        elif transport == "http":
            hint = "Switch this source to the browser transport, or: " + _UPLOAD_HINT
        else:
            hint = _UPLOAD_HINT
        raise FetchError(
            f"{body or 'source'} returned an interstitial check for {url}. {hint}; no data "
            f"was captured and existing records were left untouched.",
            cause=CAUSE_CHALLENGE,
            url=url,
            transient=False,
        )
    if looks_like_error_page(head):
        detail = ""
        if "session state protection violation" in head.lower():
            detail = (
                " The export link's checksum (cs=...) is no longer accepted: open the "
                "committee page on the CEN site, copy a fresh 'XLS export' link and update "
                "this source's URL."
            )
        raise FetchError(
            f"{body or 'source'} served an error page instead of the catalogue at {url}.{detail}",
            cause=CAUSE_ERROR_PAGE,
            url=url,
            transient=False,
        )


def _browser_cause(exc: Exception) -> str:
    text = str(exc).lower()
    if "timeout" in text or exc.__class__.__name__ == "TimeoutError":
        return CAUSE_TIMEOUT
    if any(m in text for m in ("has been closed", "crashed", "target closed", "browser closed")):
        return CAUSE_BROWSER_CRASH
    if "err_name_not_resolved" in text:
        return CAUSE_DNS
    if any(m in text for m in ("err_connection", "err_address", "err_network", "err_internet")):
        return CAUSE_CONNECT
    if any(m in text for m in ("err_cert", "err_ssl")):
        return CAUSE_TLS
    return CAUSE_NETWORK


class BrowserSession:
    """A headless Chromium reused across every browser-backed source in a run.

    Launching Chromium costs a second or two, so one session serves the whole
    run and each fetch gets a fresh page. Start-up is lazy: a run with no
    browser-backed sources never pays for it. A failed launch is remembered so
    the remaining browser sources fail (or fall back) at once instead of each
    paying for another doomed launch.
    """

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._context = None
        self._launch_error: str | None = None
        self._lock = threading.Lock()

    def _ensure_started(self) -> None:
        if self._context is not None:
            return
        if self._launch_error:
            raise FetchError(self._launch_error, cause=CAUSE_BROWSER_UNAVAILABLE, transient=False)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - environment dependent
            self._launch_error = f"Playwright is not installed. {_INSTALL_HINT}"
            raise FetchError(
                self._launch_error, cause=CAUSE_BROWSER_UNAVAILABLE, transient=False
            ) from exc

        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=settings.browser_headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    # App Service containers have a small /dev/shm.
                    "--disable-dev-shm-usage",
                ],
            )
            self._context = self._browser.new_context(
                user_agent=settings.browser_user_agent,
                viewport={"width": 1440, "height": 900},
                locale="en-US",
            )
            self._context.set_default_timeout(settings.browser_timeout_seconds * 1000)
            logger.info("fetch browser started")
        except Exception as exc:  # noqa: BLE001
            self.close()
            self._launch_error = f"could not start the fetch browser: {exc}. {_INSTALL_HINT}"
            logger.error("%s", self._launch_error)
            raise FetchError(
                self._launch_error, cause=CAUSE_BROWSER_UNAVAILABLE, transient=False
            ) from exc

    def get(
        self,
        url: str,
        *,
        body: str = "",
        wait_for: str | None = None,
        wait_until: str = "domcontentloaded",
    ) -> bytes:
        with self._lock:
            self._ensure_started()
            try:
                page = self._context.new_page()
            except Exception as exc:  # noqa: BLE001 - the browser died between sources
                self.close()
                raise FetchError(
                    f"browser unusable before fetching {url}: {exc}",
                    cause=CAUSE_BROWSER_CRASH,
                    url=url,
                ) from exc
            try:
                return self._load(page, url, body=body, wait_for=wait_for, wait_until=wait_until)
            except FetchError:
                raise
            except Exception as exc:  # noqa: BLE001 - classify every Playwright failure
                cause = _browser_cause(exc)
                if cause == CAUSE_BROWSER_CRASH:
                    self.close()  # relaunch on the next attempt
                first_line = (str(exc).splitlines() or [exc.__class__.__name__])[0][:300]
                raise FetchError(
                    f"browser fetch of {url} failed: {first_line}", cause=cause, url=url
                ) from exc
            finally:
                try:
                    page.close()
                except Exception:  # noqa: BLE001 - page may already be gone
                    pass

    def _load(self, page, url: str, *, body: str, wait_for: str | None, wait_until: str) -> bytes:
        status: int | None = None
        try:
            response = page.goto(
                url, wait_until=wait_until, timeout=settings.browser_timeout_seconds * 1000
            )
            status = response.status if response is not None else None
        except Exception as exc:  # noqa: BLE001
            # A navigation timeout often still leaves a usable DOM: a slow
            # tracker, not the listing, is what never finished.
            if _browser_cause(exc) != CAUSE_TIMEOUT or len(page.content()) < 2000:
                raise
            logger.warning(
                "%s: navigation to %s timed out; continuing with what rendered",
                body or "browser", url,
            )

        was_challenge = looks_like_challenge(page.content())
        html = self._settle(page, url, body)

        # A challenge page is served with 403/503 and then replaced in place,
        # so the status only means something when there was no challenge.
        if status is not None and status >= 400 and not was_challenge:
            raise FetchError(
                f"HTTP {status} from {url} (browser)",
                cause=status_cause(status),
                url=url,
                http_status=status,
            )

        if wait_for:
            try:
                page.wait_for_selector(
                    wait_for, timeout=settings.browser_content_wait_seconds * 1000
                )
            except Exception:  # noqa: BLE001 - the selector is a hint, not a contract
                logger.info(
                    "%s: content selector %r never appeared for %s; parsing whatever rendered",
                    body or "browser", wait_for, url,
                )
            html = page.content()

        return html.encode("utf-8")

    def _settle(self, page, url: str, body: str) -> str:
        """Let the site's own interstitial finish, then hand back the HTML."""
        deadline = time.monotonic() + settings.browser_challenge_wait_seconds
        html = page.content()
        while looks_like_challenge(html) and time.monotonic() < deadline:
            page.wait_for_timeout(1000)
            html = page.content()

        if looks_like_challenge(html):
            raise FetchError(
                f"{body or 'source'} was still showing an interstitial check after "
                f"{settings.browser_challenge_wait_seconds:g}s at {url}. {_UPLOAD_HINT}; no "
                f"data was captured and existing records were left untouched.",
                cause=CAUSE_CHALLENGE,
                url=url,
                transient=False,
            )
        return html

    def close(self) -> None:
        for closer in (self._context, self._browser, self._playwright):
            if closer is None:
                continue
            try:
                closer.close() if closer is not self._playwright else closer.stop()
            except Exception:  # noqa: BLE001 - shutdown is best-effort
                pass
        self._playwright = self._browser = self._context = None


class FetchContext:
    """Holds both transports, and the attempt log, for one monitoring run."""

    def __init__(self) -> None:
        self._client: httpx.Client | None = None
        self._browser: BrowserSession | None = None
        self._cache: dict[tuple, bytes] = {}
        self.attempts: list[FetchAttemptRecord] = []

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=settings.http_timeout_seconds,
                headers={"User-Agent": settings.http_user_agent},
                follow_redirects=True,
            )
        return self._client

    @property
    def browser(self) -> BrowserSession:
        if self._browser is None:
            self._browser = BrowserSession()
        return self._browser

    def drain_attempts(self) -> list[FetchAttemptRecord]:
        """Hand over (and forget) the attempts logged since the last drain."""
        attempts, self.attempts = self.attempts, []
        return attempts

    def _with_retries(self, fetch, *, url: str, transport: str, body: str) -> bytes:
        max_attempts = max(1, settings.fetch_max_attempts)
        for attempt in range(1, max_attempts + 1):
            started = time.perf_counter()
            try:
                payload = fetch()
            except FetchError as exc:
                self.attempts.append(
                    FetchAttemptRecord(
                        url=url,
                        transport=transport,
                        attempt=attempt,
                        outcome="failed",
                        duration_ms=int((time.perf_counter() - started) * 1000),
                        cause=exc.cause,
                        http_status=exc.http_status,
                        message=str(exc)[:1000],
                    )
                )
                retry = exc.transient and attempt < max_attempts
                logger.warning(
                    "%s fetch via %s failed (attempt %d/%d, cause=%s, http=%s)%s: %s",
                    body or "source", transport, attempt, max_attempts, exc.cause,
                    exc.http_status or "-", " - retrying" if retry else "", exc,
                )
                if not retry:
                    raise
                delay = settings.fetch_retry_backoff_seconds * (2 ** (attempt - 1))
                if exc.cause == CAUSE_RATE_LIMITED:
                    delay *= 3
                time.sleep(delay)
                continue

            self.attempts.append(
                FetchAttemptRecord(
                    url=url,
                    transport=transport,
                    attempt=attempt,
                    outcome="ok",
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    bytes=len(payload),
                )
            )
            if attempt > 1:
                logger.info("%s fetch %s succeeded on attempt %d", body or "source", url, attempt)
            return payload
        raise AssertionError("unreachable")  # pragma: no cover

    def http_get(
        self,
        url: str,
        *,
        body: str = "",
        headers: dict[str, str] | None = None,
        transport: str = "http",
    ) -> bytes:
        cache_key = (url, tuple(sorted((headers or {}).items())))
        if cache_key in self._cache:
            return self._cache[cache_key]

        def fetch() -> bytes:
            try:
                response = self.client.get(url, headers=headers or {}, follow_redirects=True)
                response.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                raise classify_http_exception(exc, url) from exc
            check_content(response.content, url=url, body=body, transport="http")
            return response.content

        payload = self._with_retries(fetch, url=url, transport=transport, body=body)
        # Several sources share one endpoint (ASME's catalogue): fetch it once per run.
        self._cache[cache_key] = payload
        return payload

    def browser_get(
        self,
        url: str,
        *,
        body: str = "",
        wait_for: str | None = None,
        wait_until: str = "domcontentloaded",
    ) -> bytes:
        def fetch() -> bytes:
            payload = self.browser.get(url, body=body, wait_for=wait_for, wait_until=wait_until)
            check_content(payload, url=url, body=body, transport="browser")
            return payload

        return self._with_retries(fetch, url=url, transport="browser", body=body)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None

    def __enter__(self) -> "FetchContext":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
