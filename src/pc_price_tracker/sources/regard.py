"""Regard.ru adapter.

Regard's category pages are server-rendered Next.js pages that embed the
full product listing as JSON in a <script id="__NEXT_DATA__"> tag
(props.props.initialState.listing.data[<category_id>]) — no separate API
endpoint needed, no headless browser needed. Pagination is a plain
`?page=N` query param, which its own robots.txt explicitly allows
(`Allow: /*?page=`) ahead of a blanket `Disallow: *?*`.

Category IDs/slugs below were discovered by fetching regard.ru's homepage
nav and confirmed by inspecting a sample category page's embedded JSON.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from selectolax.parser import HTMLParser

from pc_price_tracker.constants import Category
from pc_price_tracker.models import RawOffer
from pc_price_tracker.sources.base import BaseSource, SourceBlocked


def _resolve_availability(item: dict[str, Any], price: int) -> str:
    """Maps the listing JSON's show_flag/preorder/price onto the exact text
    Regard's frontend renders in the product page's stock badge
    (`PriceBlock_inStock` span) — verified live against all three real
    cases: "В наличии", "Предзаказ" (item["preorder"] is a lead-time-in-days
    int, not a bool — any nonzero value means preorder), "Нет в наличии"
    (show_flag=0 or no price). Regard's data model and frontend i18n
    strings have no fourth "delivery from abroad" status — that's a
    plausible-sounding guess, not something this source actually reports.
    """
    if not price or not item.get("show_flag", 1):
        return "нет в наличии"
    if item.get("preorder"):
        return "предзаказ"
    return "в наличии"


class RegardSource(BaseSource):
    name = "regard"
    base_url = "https://www.regard.ru"

    CATEGORY_MAP: dict[Category, tuple[str, str]] = {
        "cpu": ("1001", "processory"),
        "gpu": ("1013", "videokarty"),
        "motherboard": ("1000", "materinskie-platy"),
        "ram": ("1010", "operativnaya-pamyat"),
        "ssd": ("1015", "nakopiteli-ssd"),
        "psu": ("1225", "bloki-pitaniya"),
        "case": ("1032", "korpusa"),
        "cooler": ("5162", "kulery-dlya-processorov"),
    }

    # Safety valve against an unbounded loop if the site's pagination shape
    # ever changes; real categories top out around a dozen pages.
    MAX_PAGES = 200

    def fetch_category(self, category: Category, captured_at: datetime) -> list[RawOffer]:
        if category not in self.CATEGORY_MAP:
            raise ValueError(f"regard: unsupported category {category!r}")
        cat_id, slug = self.CATEGORY_MAP[category]

        offers: list[RawOffer] = []
        seen_ids: set[str] = set()
        page = 1
        while True:
            url = f"{self.base_url}/catalog/{cat_id}/{slug}"
            if page > 1:
                url = f"{url}?page={page}"

            resp = self.get(url)
            items, total = self._parse_listing(resp.text, cat_id)

            page_offers: list[RawOffer] = []
            for item in items:
                ext_id = str(item["id"])
                if ext_id in seen_ids:
                    continue
                seen_ids.add(ext_id)
                page_offers.append(self._to_raw_offer(item, category, captured_at))
            new_count = len(page_offers)

            # Commit this page before fetching the next one. A category can
            # run to a dozen pages; if the process dies partway, everything
            # already fetched must survive rather than evaporate with the
            # in-memory list.
            self._checkpoint(page_offers)
            offers.extend(page_offers)

            self.log.info(
                "page_fetched",
                category=category,
                page=page,
                items_on_page=len(items),
                new_items=new_count,
                total_reported=total,
                collected_so_far=len(offers),
            )

            if not items or new_count == 0:
                break
            if total and len(seen_ids) >= total:
                break
            if page >= self.max_pages:
                self.log.warning("max_pages_reached", category=category, page=page)
                break

            page += 1
            self._sleep()

        return offers

    def _parse_listing(self, html: str, cat_id: str) -> tuple[list[dict[str, Any]], int]:
        tree = HTMLParser(html)
        node = tree.css_first("script#__NEXT_DATA__")
        if node is None:
            raise SourceBlocked("no __NEXT_DATA__ payload found (page shape changed, or we're blocked)")
        try:
            data = json.loads(node.text())
            listing = data["props"]["initialState"]["listing"]["data"][cat_id]
        except (json.JSONDecodeError, KeyError) as exc:
            raise SourceBlocked(f"unexpected listing payload shape: {exc}") from exc

        total = int(listing.get("recordsFiltered") or 0)
        items: list[dict[str, Any]] = []
        for page_data in listing.get("pages", {}).values():
            items.extend(page_data.get("data", []))
        return items, total

    def _to_raw_offer(self, item: dict[str, Any], category: Category, captured_at: datetime) -> RawOffer:
        price = int(item.get("price") or 0)
        availability = _resolve_availability(item, price)
        return RawOffer(
            source=self.name,
            category=category,
            external_id=str(item["id"]),
            title=item["title"],
            brand_hint=item.get("vendor"),
            price=price,
            price_old=item.get("price_old"),
            in_stock=availability == "в наличии",
            availability=availability,
            url=f"{self.base_url}/product/{item['id']}/{item.get('seo_url', '')}",
            brief=item.get("brief"),
            width_mm=item.get("width"),
            height_mm=item.get("height"),
            depth_mm=item.get("depth"),
            captured_at=captured_at,
        )
