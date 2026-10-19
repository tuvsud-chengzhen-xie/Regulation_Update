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
"""

from __future__ import annotations

import logging
import threading
import time

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

_INSTALL_HINT = (
    "browser-assisted fetching needs Playwright: "
    "`pip install playwright && python -m playwright install chromium`"
)


def looks_like_challenge(html: str) -> bool:
    head = html[:6000].lower()
    return any(marker in head for marker in _CHALLENGE_MARKERS)


class BrowserSession:
    """A headless Chromium reused across every browser-backed source in a run.

    Launching Chromium costs a second or two, so one session serves the whole
    run and each fetch gets a fresh page. Start-up is lazy: a run with no
    browser-backed sources never pays for it.
    """

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._context = None
        self._lock = threading.Lock()

    def _ensure_started(self) -> None:
        if self._context is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ExtractorError(_INSTALL_HINT) from exc

        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=settings.browser_headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            self._context = self._browser.new_context(
                user_agent=settings.browser_user_agent,
                viewport={"width": 1440, "height": 900},
                locale="en-US",
            )
            self._context.set_default_timeout(settings.browser_timeout_seconds * 1000)
        except Exception as exc:  # noqa: BLE001
            self.close()
            raise ExtractorError(f"could not start the fetch browser: {exc}. {_INSTALL_HINT}") from exc

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
            page = self._context.new_page()
            try:
                page.goto(url, wait_until=wait_until, timeout=settings.browser_timeout_seconds * 1000)

                html = self._settle(page, url, body)

                if wait_for:
                    try:
                        page.wait_for_selector(
                            wait_for, timeout=settings.browser_content_wait_seconds * 1000
                        )
                    except Exception:  # noqa: BLE001 - the selector is a hint, not a contract
                        logger.info(
                            "%s: content selector %r never appeared for %s; "
                            "parsing whatever rendered",
                            body or "browser", wait_for, url,
                        )
                    html = page.content()

                return html.encode("utf-8")
            finally:
                page.close()

    def _settle(self, page, url: str, body: str) -> str:
        """Let the site's own interstitial finish, then hand back the HTML."""
        deadline = time.monotonic() + settings.browser_challenge_wait_seconds
        html = page.content()
        while looks_like_challenge(html) and time.monotonic() < deadline:
            page.wait_for_timeout(1000)
            html = page.content()

        if looks_like_challenge(html):
            raise ExtractorError(
                f"{body or 'source'} was still showing an interstitial check after "
                f"{settings.browser_challenge_wait_seconds:g}s at {url}. It needs an "
                f"allow-listed egress IP or vendor API access from this host; no data was "
                f"captured and existing records were left untouched."
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
    """Holds both transports for the duration of one monitoring run."""

    def __init__(self) -> None:
        self._client: httpx.Client | None = None
        self._browser: BrowserSession | None = None

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

    def http_get(self, url: str, *, body: str = "", headers: dict[str, str] | None = None) -> bytes:
        response = self.client.get(url, headers=headers or {}, follow_redirects=True)
        response.raise_for_status()
        payload = response.content
        if looks_like_challenge(decode(payload[:6000])):
            raise ExtractorError(
                f"{body or 'source'} returned an interstitial check for {url}. Switch this "
                f"source to the browser transport (fetch_strategy='browser'); no data was "
                f"captured and existing records were left untouched."
            )
        return payload

    def browser_get(
        self,
        url: str,
        *,
        body: str = "",
        wait_for: str | None = None,
        wait_until: str = "domcontentloaded",
    ) -> bytes:
        return self.browser.get(url, body=body, wait_for=wait_for, wait_until=wait_until)

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
