"""Affiliate/partner product feed adapter (Admitad, ePN, ...).

Unlike the scraping adapters (RegardSource, CitilinkSource), this doesn't
crawl a website — it downloads a structured product feed (YML or CSV) that
the partner network explicitly publishes for exactly this purpose. So
fetching the feed does NOT go through BaseSource.get()'s robots.txt/WAF
machinery: that exists to detect and respect a *site's* wishes about being
crawled, and a feed URL supplied by the network's own affiliate program
isn't a site to crawl — it's an API meant to be fetched programmatically.
It still reuses BaseSource's httpx client setup and a similar
retry-with-backoff shape for basic resilience against transient network
failures, since it's still a network call that can legitimately fail.

YML ("Yandex Market Language") is the long-standing, publicly documented
XML feed format used broadly across Russian e-commerce/price-comparison
integrations — both Admitad and ePN commonly carry merchant feeds in this
format, so the parser here is against a real documented spec, not
reverse-engineered guesswork the way a scraped site's markup would be.
Nothing about the actual parsing logic could be verified against a real
feed during development, though — no URL was supplied (network config has
feed_url: null until one is connected). See tests/test_affiliate_feed.py,
which exercises this against a hand-built spec-compliant sample, and be
clear that's what "tested" means here, not a live run.

Each subclass sets network_name (matches its section in config/feeds.yaml)
and category_map (feed category id/name -> our Category). fetch_category
raises FeedNotConfigured (a SourceBlocked subtype) when the feed isn't
enabled or has no URL yet — the caller's existing SourceBlocked handling
(log it, move to the next source) covers this with no special-casing.
"""

from __future__ import annotations

import csv
import io
import random
import time
from datetime import datetime
from typing import Any
from xml.etree import ElementTree as ET

import httpx
import structlog

from pc_price_tracker.compat import load_feeds_config
from pc_price_tracker.constants import Category
from pc_price_tracker.models import RawOffer
from pc_price_tracker.sources.base import BaseSource, SourceBlocked


class FeedNotConfigured(SourceBlocked):
    """This network's feed isn't enabled, or has no feed_url yet."""


class AffiliateFeedSource(BaseSource):
    network_name: str  # set by subclass; must match a key in config/feeds.yaml
    base_url = ""  # unused (no robots.txt / site-relative URLs involved)

    def __init__(self, client: httpx.Client | None = None, **kwargs: Any) -> None:
        # BaseSource.__init__ itself reads self.name (to bind the logger),
        # so it has to be set before calling super(), not after.
        self.name = self.network_name
        super().__init__(client=client, **kwargs)

        config = load_feeds_config().get(self.network_name, {})
        self.enabled: bool = bool(config.get("enabled", False))
        self.feed_url: str | None = config.get("feed_url")
        self.feed_format: str = config.get("format", "yml")
        self.category_map: dict[str, Category] = config.get("category_map", {}) or {}
        self.csv_column_map: dict[str, str] = config.get("csv_column_map", {}) or {}

        self._all_offers_cache: list[RawOffer] | None = None

    def fetch_category(self, category: Category, captured_at: datetime) -> list[RawOffer]:
        if not self.enabled or not self.feed_url:
            raise FeedNotConfigured(
                f"{self.network_name}: фид не подключён (enabled={self.enabled}, "
                f"feed_url={'задан' if self.feed_url else 'не задан'}) — заполните config/feeds.yaml"
            )

        if self._all_offers_cache is None:
            self._all_offers_cache = self._fetch_all_offers(captured_at)
        return [o for o in self._all_offers_cache if o.category == category]

    # -- fetch ----------------------------------------------------------

    def _fetch_all_offers(self, captured_at: datetime) -> list[RawOffer]:
        last_exc: Exception | None = None
        resp: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                backoff = (2**attempt) + random.uniform(0, 1)
                self.log.warning("feed_retry", attempt=attempt, backoff=round(backoff, 1))
                time.sleep(backoff)
            try:
                resp = self.client.get(self.feed_url, timeout=60.0)
                resp.raise_for_status()
                break
            except httpx.HTTPError as exc:
                last_exc = exc
                resp = None
        if resp is None:
            raise SourceBlocked(f"{self.network_name}: не удалось загрузить фид после {self.max_retries} попыток: {last_exc}")

        if self.feed_format == "yml":
            return self._parse_yml(resp.text, captured_at)
        if self.feed_format == "csv":
            return self._parse_csv(resp.text, captured_at)
        raise ValueError(f"{self.network_name}: неизвестный feed_format {self.feed_format!r} (ожидается yml или csv)")

    # -- YML --------------------------------------------------------------

    def _parse_yml(self, xml_text: str, captured_at: datetime) -> list[RawOffer]:
        root = ET.fromstring(xml_text)
        offers_el = root.find(".//offers")
        if offers_el is None:
            return []

        results: list[RawOffer] = []
        skipped_unmapped_category = 0
        skipped_incomplete = 0
        for offer_el in offers_el.findall("offer"):
            raw = self._yml_offer_to_raw(offer_el, captured_at)
            if raw is None:
                category_id = offer_el.findtext("categoryId")
                if category_id is not None and category_id not in self.category_map:
                    skipped_unmapped_category += 1
                else:
                    skipped_incomplete += 1
                continue
            results.append(raw)

        if skipped_unmapped_category or skipped_incomplete:
            self.log.info(
                "feed_offers_skipped",
                unmapped_category=skipped_unmapped_category,
                incomplete=skipped_incomplete,
            )
        return results

    def _yml_offer_to_raw(self, offer_el: ET.Element, captured_at: datetime) -> RawOffer | None:
        category_id = offer_el.findtext("categoryId")
        category = self.category_map.get(category_id) if category_id is not None else None
        if category is None:
            return None

        name = offer_el.findtext("name") or offer_el.findtext("model") or ""
        price_text = offer_el.findtext("price")
        url = offer_el.findtext("url") or ""
        if not name.strip() or not price_text or not url:
            return None
        try:
            price = int(round(float(price_text)))
        except ValueError:
            return None

        external_id = offer_el.get("id") or ""
        if not external_id:
            return None
        available = offer_el.get("available", "true").strip().lower() == "true"
        vendor = offer_el.findtext("vendor")

        return RawOffer(
            source=self.name,
            category=category,
            external_id=external_id,
            title=name.strip(),
            brand_hint=vendor.strip() if vendor else None,
            price=price,
            in_stock=available,
            # YML's "available" attribute is a plain bool, no preorder concept —
            # only two of the three real statuses are reachable from this source.
            availability="в наличии" if available else "нет в наличии",
            url=url,
            brief=None,  # YML <param> elements could feed this later; not assumed here without a real feed to check against
            captured_at=captured_at,
        )

    # -- CSV ----------------------------------------------------------------

    def _parse_csv(self, csv_text: str, captured_at: datetime) -> list[RawOffer]:
        colmap = self.csv_column_map
        reader = csv.DictReader(io.StringIO(csv_text))
        results: list[RawOffer] = []

        for row in reader:
            category_raw = row.get(colmap.get("category", "category"))
            category = self.category_map.get(category_raw) if category_raw is not None else None
            if category is None:
                continue

            title = row.get(colmap.get("title", "title"), "")
            price_raw = row.get(colmap.get("price", "price"), "")
            url = row.get(colmap.get("url", "url"), "")
            external_id = row.get(colmap.get("external_id", "id"), "")
            if not title.strip() or not price_raw or not url or not external_id:
                continue
            try:
                price = int(round(float(price_raw)))
            except ValueError:
                continue

            brand_hint = row.get(colmap.get("brand", "brand"))
            results.append(
                RawOffer(
                    source=self.name,
                    category=category,
                    external_id=external_id,
                    title=title.strip(),
                    brand_hint=brand_hint.strip() if brand_hint else None,
                    price=price,
                    in_stock=True,
                    availability="в наличии",
                    url=url,
                    brief=None,
                    captured_at=captured_at,
                )
            )
        return results


class AdmitadSource(AffiliateFeedSource):
    network_name = "admitad"


class EpnSource(AffiliateFeedSource):
    network_name = "epn"
