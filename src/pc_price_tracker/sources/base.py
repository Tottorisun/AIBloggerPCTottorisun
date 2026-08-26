"""Adapter contract shared by every price source.

Every source implements fetch_category(category) -> list[RawOffer]. Nothing
else in the codebase is allowed to know how a particular store's HTML/JSON
is shaped — that knowledge stays inside the adapter.
"""

from __future__ import annotations

import random
import time
from abc import ABC, abstractmethod
from datetime import datetime
from urllib.parse import urljoin

import httpx
import structlog

from pc_price_tracker.constants import Category
from pc_price_tracker.models import RawOffer
from pc_price_tracker.sources.robots import RobotsPolicy, parse_robots

USER_AGENT = "pc-price-tracker-bot/0.1 (+contact: arthursunarrow@gmail.com)"


class SourceBlocked(Exception):
    """Raised when a source won't serve us: robots.txt disallow, captcha,
    persistent 403s, or repeated failures. Callers must stop that source's
    run and log it, not crash the whole scrape."""


class BaseSource(ABC):
    name: str
    base_url: str

    def __init__(
        self,
        client: httpx.Client | None = None,
        min_delay: float = 3.0,
        max_delay: float = 5.0,
        max_retries: int = 3,
    ) -> None:
        self.client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=20.0,
            follow_redirects=True,
        )
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_retries = max_retries
        self._robots: RobotsPolicy | None = None
        self.log = structlog.get_logger().bind(source=self.name)

    def _load_robots(self) -> RobotsPolicy:
        if self._robots is None:
            robots_url = urljoin(self.base_url, "/robots.txt")
            waf = None
            try:
                resp = self.client.get(robots_url, timeout=10.0)
                waf = _waf_vendor(resp) if resp.status_code in (403, 429) else None
                resp.raise_for_status()
                self._robots = parse_robots(resp.text, USER_AGENT)
            except httpx.HTTPError as exc:
                reason = f"blocked by {waf} before robots.txt could even be fetched" if waf else str(exc)
                self.log.warning("robots_fetch_failed", url=robots_url, error=reason)
                # Fail closed: if we can't verify robots.txt, we don't scrape.
                self._robots = RobotsPolicy.deny_everything()
                self._robots_fetch_error = reason
        return self._robots

    def _check_allowed(self, url: str) -> None:
        if not self._load_robots().can_fetch(url):
            reason = getattr(self, "_robots_fetch_error", None)
            if reason:
                raise SourceBlocked(f"couldn't verify robots.txt for {url}: {reason}")
            raise SourceBlocked(f"robots.txt disallows {url}")

    def _sleep(self) -> None:
        time.sleep(random.uniform(self.min_delay, self.max_delay))

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        """Polite, retrying, robots-respecting GET. Raises SourceBlocked on
        anything that looks like an anti-bot response or persistent failure."""
        self._check_allowed(url)

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                backoff = (2**attempt) + random.uniform(0, 1)
                self.log.warning("request_retry", url=url, attempt=attempt, backoff=round(backoff, 1))
                time.sleep(backoff)
            try:
                resp = self.client.get(url, **kwargs)
            except httpx.HTTPError as exc:
                last_exc = exc
                continue

            if resp.status_code == 403 or resp.status_code == 429:
                waf = _waf_vendor(resp)
                if waf:
                    # A dedicated anti-bot/WAF product (Qrator, Cloudflare
                    # challenge, etc.) rejecting us isn't a transient
                    # overload — retrying won't get past it, and hammering
                    # it anyway would be disrespectful. Stop immediately.
                    raise SourceBlocked(f"WAF block at {url} (status {resp.status_code}, server={waf})")
                last_exc = SourceBlocked(f"status {resp.status_code} from {url}")
                continue
            if resp.status_code >= 500:
                last_exc = SourceBlocked(f"status {resp.status_code} from {url}")
                continue
            if _looks_like_captcha(resp):
                raise SourceBlocked(f"anti-bot / captcha response at {url}")

            resp.raise_for_status()
            return resp

        raise SourceBlocked(f"giving up on {url} after {self.max_retries} retries: {last_exc}")

    @abstractmethod
    def fetch_category(self, category: Category, captured_at: datetime) -> list[RawOffer]:
        """Fetch every current listing in `category`. `captured_at` is set by
        the caller once per scrape run so all offers from that run share a
        timestamp, keeping price-history comparisons aligned."""
        ...


def _looks_like_captcha(resp: httpx.Response) -> bool:
    if resp.status_code in (401, 403):
        return True
    content_type = resp.headers.get("content-type", "")
    if "text/html" not in content_type:
        return False
    snippet = resp.text[:4000].lower()
    return any(marker in snippet for marker in ("captcha", "are you a robot", "access denied", "attention required"))


_WAF_SERVER_MARKERS = ("qrator", "cloudflare", "akamaighost", "distil", "perimeterx", "datadome", "incapsula", "imperva")


def _waf_vendor(resp: httpx.Response) -> str | None:
    """Name of the anti-bot product rejecting us, read off the Server
    header, if this 403/429 looks like a WAF challenge rather than plain
    rate-limiting. Only meaningful on an already-non-2xx response — a 200
    from a Cloudflare-fronted site is just a normal page."""
    server = resp.headers.get("server", "").lower()
    for marker in _WAF_SERVER_MARKERS:
        if marker in server:
            return resp.headers.get("server", marker)
    return None
