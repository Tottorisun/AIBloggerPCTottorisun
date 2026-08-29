"""Playwright-backed browser transport, for sources whose catalog content
only exists after the page's JavaScript has run.

POLICY — READ BEFORE CHANGING ANYTHING HERE
-------------------------------------------
Until 29.08.2026 this project had a blanket rule: "no headless browser, no
JS execution, no CAPTCHA/challenge solving". The owner lifted the *browser*
half of that rule on 29.08.2026. The CAPTCHA half was NOT lifted and is not
negotiable. Concretely, this module:

  - drives an ordinary, unmodified Chromium so that we look like a normal
    visitor — that is the entire point of the browser;
  - never enables, imports, wraps, or implements any challenge/CAPTCHA
    solver (no `solve_*` options, no third-party solving service, no
    token replay);
  - treats a challenge that is actually presented to us as a hard stop:
    the fetcher reports it and the source raises SourceBlocked, exactly
    like any other "this site won't serve us" case.

The browser exists so we can read a JS-rendered page, not so we can defeat
a gate. If a site starts genuinely gating us, the correct outcome is a
logged SourceBlocked and a human decision — not a workaround.

Playwright is an OPTIONAL dependency (`pip install -e ".[browser]"`). It is
imported lazily inside BrowserFetcher.start() so that importing this module
— and therefore the whole package, the CLI, and the test suite — works fine
without it. CI never installs it and never runs a browser.

Why this returns httpx.Response
-------------------------------
BaseSource's WAF detection, retry/backoff and captcha heuristics all
operate on httpx.Response. Rather than fork that logic for a second
transport, the fetcher adapts the browser result into a real
httpx.Response so a browser-backed source flows through exactly the same
safety checks as an HTTP-backed one.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
import structlog

if TYPE_CHECKING:  # pragma: no cover - typing only
    from playwright.sync_api import Page

log = structlog.get_logger().bind(component="browser")

# Conservative pacing, reused unchanged from the RUSIMEX scrapers that this
# portfolio already runs against live sites. Faster hardware is not a reason
# to lower these.
NAV_MIN_DELAY = 2.0
NAV_MAX_DELAY = 4.0
NAV_TIMEOUT_MS = 45_000

# Scroll-to-stability. Citilink renders a fixed number of card shells and
# hydrates them lazily; measured live on 29.08.2026, hydration finished two
# polls AFTER the scroll position stopped moving, so the "unchanged" streak
# has to outlast that. 5 polls at 1.2s is deliberately patient.
SCROLL_STEP_PX = 1200
SCROLL_POLL_MS = 1200
SCROLL_STABLE_ROUNDS = 5
SCROLL_MAX_STEPS = 60


def _adapt_headers(headers: dict[str, str]) -> dict[str, str]:
    """Drop entity headers that no longer describe the body we're carrying.

    Playwright hands back text that is ALREADY decompressed, but its header
    dict still advertises `content-encoding: gzip` (and the compressed
    `content-length`). Passing those straight into httpx.Response makes httpx
    try to gunzip plain text — "Error -3 while decompressing data: incorrect
    header check" — which then surfaces as a bogus "couldn't verify
    robots.txt" and fails the whole source closed. Caught on the first live
    run, 29.08.2026."""
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in ("content-encoding", "content-length")
    }


class BrowserUnavailable(RuntimeError):
    """Playwright (or its browser binary) isn't installed."""


class ChallengePresented(Exception):
    """A real anti-bot challenge was actually shown to us.

    This is a full stop. We do not solve challenges — see the module
    policy. The owning source is expected to turn this into SourceBlocked.
    """


@dataclass
class StabilizeSpec:
    """How to wait for a lazily-hydrated list to finish rendering.

    item_selector  — every row/card slot on the page (shells included)
    ready_selector — the marker that a slot has actually been filled in
    """

    item_selector: str
    ready_selector: str
    max_steps: int = SCROLL_MAX_STEPS
    poll_ms: int = SCROLL_POLL_MS
    stable_rounds: int = SCROLL_STABLE_ROUNDS


@dataclass
class BrowserFetcher:
    """A single long-lived browser session.

    Reusing one context across a category run matters: the first navigation
    to a Qrator-fronted site answers with the WAF's JS interstitial, which
    sets a session cookie and reloads into the real page. Every later
    request in that context already carries the cookie. A fresh context per
    request would re-trigger the interstitial every single time — more load
    on the site, and more chance of looking abnormal.
    """

    user_agent: str
    base_url: str
    headless: bool = True
    locale: str = "ru-RU"
    timezone_id: str = "Asia/Yekaterinburg"
    viewport: dict[str, int] = field(default_factory=lambda: {"width": 1440, "height": 900})

    _pw: Any = field(default=None, init=False, repr=False)
    _browser: Any = field(default=None, init=False, repr=False)
    _ctx: Any = field(default=None, init=False, repr=False)
    _page: Any = field(default=None, init=False, repr=False)
    _warm: bool = field(default=False, init=False, repr=False)

    # ---------------------------------------------------------------- setup

    def start(self) -> None:
        if self._ctx is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - env dependent
            raise BrowserUnavailable(
                "playwright is not installed — this source needs a real browser. "
                'Install with: pip install -e ".[browser]" && python -m playwright install chromium'
            ) from exc

        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(
                headless=self.headless,
                # Chromium advertises itself as automated by default. Turning
                # that banner off is about looking like an ordinary visitor;
                # it is not challenge solving and defeats nothing.
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as exc:  # pragma: no cover - env dependent
            self._pw.stop()
            self._pw = None
            raise BrowserUnavailable(
                f"couldn't launch chromium ({exc}). Run: python -m playwright install chromium"
            ) from exc

        self._ctx = self._browser.new_context(
            user_agent=self.user_agent,
            locale=self.locale,
            timezone_id=self.timezone_id,
            viewport=self.viewport,
        )
        self._ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        self._page = self._ctx.new_page()
        log.info("browser_started", headless=self.headless)

    def close(self) -> None:
        for closer in (self._ctx, self._browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # noqa: BLE001 - teardown must never mask the real error
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._pw = self._browser = self._ctx = self._page = None
        self._warm = False

    def __enter__(self) -> "BrowserFetcher":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------- fetching

    def _pace(self) -> None:
        time.sleep(random.uniform(NAV_MIN_DELAY, NAV_MAX_DELAY))

    def _ensure_warm(self) -> None:
        """Load one ordinary HTML page so a JS interstitial (if the site
        uses one) can run and set its session cookie.

        Without this, the very first request of a run — which for us is
        always robots.txt, a text/plain file that cannot execute the
        interstitial's JavaScript — answers 429 forever. Verified live on
        29.08.2026: robots.txt was 429 cold and 200 immediately after a
        single HTML navigation in the same context.
        """
        if self._warm:
            return
        self.start()
        self._pace()
        resp = self._page.goto(self.base_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        self._page.wait_for_timeout(3000)
        self._warm = True
        log.info("browser_warmup", url=self.base_url, status=resp.status if resp else None)

    def fetch_raw(self, url: str) -> httpx.Response:
        """Fetch a non-HTML resource (robots.txt, sitemap) through the
        browser context, so it inherits the session's cookies. Returns the
        true status and body — no JS involved, nothing to render."""
        self._ensure_warm()
        self._pace()
        r = self._ctx.request.get(url, timeout=NAV_TIMEOUT_MS)
        return httpx.Response(
            status_code=r.status,
            headers=_adapt_headers(dict(r.headers)),
            text=r.text(),
            request=httpx.Request("GET", url),
        )

    def fetch_document(
        self,
        url: str,
        wait_for_selector: str | None = None,
        stabilize: StabilizeSpec | None = None,
    ) -> httpx.Response:
        """Navigate to an HTML page, let it render, return the final DOM.

        The status reported is that of the *settled* page, not of the first
        network response. That distinction is load-bearing: a Qrator-fronted
        first navigation legitimately answers 429 with the interstitial, then
        reloads into the real 200 page in the same navigation. Reporting the
        raw 429 would make BaseSource declare a WAF block on a page we can
        actually read. Conversely, if the interstitial never resolves, we
        report the 429 and let BaseSource stop the source.
        """
        self._ensure_warm()
        self._pace()

        resp = self._page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        raw_status = resp.status if resp else 0
        raw_headers = _adapt_headers(dict(resp.headers)) if resp else {}

        if wait_for_selector:
            try:
                self._page.wait_for_selector(wait_for_selector, timeout=NAV_TIMEOUT_MS)
            except Exception:  # noqa: BLE001 - absence is diagnosed below, not here
                pass
        else:
            self._page.wait_for_timeout(2500)

        if stabilize is not None:
            self._scroll_until_stable(self._page, stabilize)

        html = self._page.content()

        if _is_interactive_challenge(html):
            # Hard stop. We never attempt to solve this. See module policy.
            raise ChallengePresented(f"an interactive challenge was presented at {url}")

        if _is_unresolved_interstitial(html):
            status = raw_status if raw_status >= 400 else 429
            log.warning("browser_interstitial_unresolved", url=url, raw_status=raw_status)
            return httpx.Response(
                status_code=status,
                headers=raw_headers or {"server": "unknown-waf"},
                text=html,
                request=httpx.Request("GET", url),
            )

        # Page settled into real content — report it as such.
        headers = dict(raw_headers)
        headers["content-type"] = "text/html; charset=utf-8"
        return httpx.Response(
            status_code=200,
            headers=headers,
            text=html,
            request=httpx.Request("GET", url),
        )

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _scroll_until_stable(page: "Page", spec: StabilizeSpec) -> None:
        """Scroll down until every card slot has been filled in, or until the
        filled-in count goes quiet *having already reached the bottom*.

        The bottom condition is not decoration. Measured on Citilink's CPU
        catalog (29.08.2026): 36 slots, 8 filled on load, all 36 filled two
        polls AFTER the scroll position stopped moving — so a quiet count on
        its own means nothing while there is still page left to travel. A
        first version without the bottom check stopped a taller category
        (coolers) at 8 of 36 hydrated and silently returned a quarter of the
        catalog, which looks exactly like a successful run.

        So: scroll to the end first, and only then let a quiet hydration
        count end the wait.
        """
        previous = -1
        stable = 0
        at_bottom = False
        for step in range(spec.max_steps):
            if not at_bottom:
                page.evaluate(f"window.scrollBy(0, {SCROLL_STEP_PX})")
            page.wait_for_timeout(spec.poll_ms)

            scroll_y, inner_h, doc_h = page.evaluate(
                "[Math.round(window.scrollY), window.innerHeight,"
                " Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)]"
            )
            # 64px of slack: lazy grids keep nudging the document height.
            at_bottom = (scroll_y + inner_h) >= (doc_h - 64)

            total = len(page.query_selector_all(spec.item_selector))
            ready = len(page.query_selector_all(spec.ready_selector))

            if total and ready >= total:
                log.info("scroll_complete", step=step, items=total, ready=ready)
                return

            if at_bottom and ready == previous:
                stable += 1
                if stable >= spec.stable_rounds:
                    log.info("scroll_settled", step=step, items=total, ready=ready, at_bottom=True)
                    return
            else:
                stable = 0
            previous = ready

        log.warning("scroll_max_steps", ready=previous, max_steps=spec.max_steps, at_bottom=at_bottom)


# --------------------------------------------------------------- detection

# NB: deliberately NOT keyed on the bare substrings "qrator" or "captcha".
# The 29.08.2026 evaluation raised a false alarm doing exactly that: on a
# perfectly healthy Citilink page, "qrator" appears as the name of an
# ordinary session cookie (Qrator fronts the entire site, including pages it
# serves normally) and "captcha" appears as an inert key in the shop's own
# login-form Redux state (`initialState.captcha = {isPending: false, ...}`).
# Substring presence is not evidence of a block. Structure is.

_INTERSTITIAL_MARKERS = ("js.cookie", "jquery.cookie")


def _is_unresolved_interstitial(html: str) -> bool:
    """True when we're still looking at a WAF's "please wait" shim rather
    than the site.

    The shim is recognised by what it LACKS (any application payload) plus
    what it carries (a cookie-setting shim and a loader, in a page far too
    small to be a real catalog), never by a vendor name appearing somewhere
    in the text.
    """
    if len(html) > 60_000:
        return False
    low = html.lower()
    if "__next_data__" in low:
        return False  # the real application shell rendered
    has_shim = any(marker in low for marker in _INTERSTITIAL_MARKERS)
    has_loader = "loader" in low or "загрузка" in low
    return has_shim and has_loader


_CHALLENGE_MARKERS = (
    "g-recaptcha",
    "h-captcha",
    "cf-turnstile",
    "smartcaptcha",
    "/recaptcha/api.js",
    "hcaptcha.com/1/api.js",
    "challenges.cloudflare.com/turnstile",
)


def _is_interactive_challenge(html: str) -> bool:
    """True when an actual solvable challenge widget is on the page.

    Reaching this means a human gate was put in front of us. The only
    correct response is to stop and report — never to solve it.
    """
    low = html.lower()
    return any(marker in low for marker in _CHALLENGE_MARKERS)
