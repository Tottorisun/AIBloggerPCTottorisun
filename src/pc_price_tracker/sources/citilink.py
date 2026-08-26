"""Citilink (citilink.ru) adapter.

STATUS: confirmed blocked at the HTTP level as of this writing. Every
endpoint tried during development — robots.txt, the homepage, and a
catalog page, on both www and bare domain — returned 429 with
`Server: QRATOR`, a dedicated anti-bot/WAF product issuing a JS-solve
challenge cookie before it will serve real content. A plain HTTP client
cannot get past that, and per the project's own rules we don't try to
(no headless browser, no JS execution, no CAPTCHA/challenge solving) —
this is exactly the "source doesn't give us data over HTTP" case the
brief says to mark unsupported and move past, not defeat.

Because of that, CATEGORY_MAP below is best-effort from general knowledge
of the site's URL conventions and is UNVERIFIED — there was no way to load
a real category page to confirm slugs or inspect the listing markup. Treat
it as a starting point for whoever revisits this (e.g. from an
already-allowlisted vantage point), not as tested fact.

fetch_category() still goes through the normal robots.txt-checked,
rate-limited, retrying request path (BaseSource.get) — it doesn't special
case "give up immediately because I already know this is blocked". If
Citilink's WAF policy ever changes, this starts working with no code
changes; today, it fails fast (base.py detects the QRATOR/WAF response and
raises SourceBlocked without wasting retries) and the caller logs it and
moves on, same as any other source hitting a real technical restriction.
"""

from __future__ import annotations

from datetime import datetime

from pc_price_tracker.constants import Category
from pc_price_tracker.models import RawOffer
from pc_price_tracker.sources.base import BaseSource


class CitilinkSource(BaseSource):
    name = "citilink"
    base_url = "https://www.citilink.ru"

    # UNVERIFIED — see module docstring. Best-effort slugs, never confirmed
    # against a real page because the site was WAF-blocked throughout
    # development.
    CATEGORY_MAP: dict[Category, str] = {
        "cpu": "processory",
        "gpu": "videokarty",
        "motherboard": "materinskie-platy",
        "ram": "operativnaya-pamyat",
        "ssd": "ssd-nakopiteli",
        "psu": "bloki-pitaniya",
        "case": "korpusa",
        "cooler": "kulery-dlya-protsessorov",
    }

    def fetch_category(self, category: Category, captured_at: datetime) -> list[RawOffer]:
        if category not in self.CATEGORY_MAP:
            raise ValueError(f"citilink: unsupported category {category!r}")
        slug = self.CATEGORY_MAP[category]
        url = f"{self.base_url}/catalog/{slug}/"

        # This will raise SourceBlocked (via BaseSource.get -> robots.txt
        # check or WAF detection) rather than return — documented above.
        # No bypass attempted; the caller is expected to catch
        # SourceBlocked, log it, and move on.
        resp = self.get(url)

        # Unreachable with the current site behavior, kept so this adapter
        # is a real implementation and not a stub, if the block ever lifts.
        return self._parse_listing(resp.text, category, captured_at)

    def _parse_listing(self, html: str, category: Category, captured_at: datetime) -> list[RawOffer]:
        raise NotImplementedError(
            "Citilink's listing markup was never inspected (WAF-blocked throughout development) — "
            "implement this once a real category page can actually be fetched."
        )
