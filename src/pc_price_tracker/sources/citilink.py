"""Citilink (citilink.ru) adapter.

STATUS: WORKING via a real browser, as of 29.08.2026.

POLICY CHANGE — 29.08.2026 (do not silently revert this)
--------------------------------------------------------
This module used to state the project's own hard rule:

    "no headless browser, no JS execution, no CAPTCHA/challenge solving"

and marked Citilink permanently unsupported on the strength of it. That
rule is recorded here verbatim because it was deliberate and it stood for
months — a future reader should see that it CHANGED and why, not find it
quietly deleted.

**The owner lifted the browser half of that rule on 29.08.2026**
("можешь запускать полноценный браузер когда нужно"). So JS execution and
a real browser are now permitted.

**The CAPTCHA half was NOT lifted and will not be.** We do not solve
challenges — not with a solver library, not with a paid service, not by
replaying tokens, not with any `solve_*` option of any library. If a
challenge is actually presented, this adapter stops and reports the source
as blocked (SourceBlocked), exactly as it did when it was blocked at the
HTTP level. The browser is here so that we look like an ordinary visitor
reading a JS-rendered page — not to defeat a gate.

What actually changed on the wire
---------------------------------
Nothing about Citilink's protection was bypassed; a normal browser simply
satisfies it. Qrator still fronts the site and still answers the FIRST
navigation of a cold session with a JS interstitial (HTTP 429) that sets a
session cookie and reloads into the real page. A plain HTTP client can't
run that JavaScript, which is exactly why every earlier attempt saw 429 and
nothing else. Verified live 29.08.2026 with plain Chromium via Playwright:

  - `https://www.citilink.ru/catalog/processory/` -> settles at HTTP 200,
    ~1.36 MB, real shop title, 36 product cards with prices;
  - `robots.txt` -> 429 cold, **200** once the session is warm (see
    `browser.BrowserFetcher._ensure_warm`);
  - no interactive CAPTCHA was presented at any point, and none was solved.

robots.txt
----------
Re-verified 29.08.2026 with this project's own RFC-9309 parser
(sources/robots.py): citilink.ru/robots.txt declares groups for `Yandex`,
`Googlebot` and `Applebot` **only** — there is no `User-agent: *` group at
all, so zero rules bind our UA and `/catalog/` is formally unrestricted for
us. This is re-checked on EVERY run (BaseSource._check_allowed), not
trusted from this comment: if Citilink ever adds a wildcard group that
disallows us, the next run stops on its own.

Politeness still applies regardless of what robots.txt permits: one page at
a time, 2-4s between navigations, a handful of pages per category, and an
honest User-Agent carrying a contact address.

Listing markup
--------------
Unlike Regard, the SSR payload is NOT usable here: `__NEXT_DATA__` ships
`subcategory.productList` as `{isPending: true, payload: {products: [],
total: 0}}` — the grid is hydrated client-side afterwards. So we parse the
rendered DOM instead, keyed on Citilink's stable `data-meta-*` attributes
(`data-meta-product-id`, `data-meta-name="Snippet__title"` / `"Snippet__price"`)
rather than on its emotion-generated class hashes, which are pure churn.

The page renders a fixed number of card shells and fills them in lazily on
scroll — on load only 8 of 36 carry a price. Hence the scroll-to-stability
pass in browser.py; see the measured timing note there.
"""

from __future__ import annotations

import random
import re
from datetime import datetime

import httpx
from selectolax.parser import HTMLParser

from pc_price_tracker.constants import Category
from pc_price_tracker.models import RawOffer
from pc_price_tracker.sources.base import USER_AGENT, BaseSource, SourceBlocked
from pc_price_tracker.sources.browser import (
    BrowserFetcher,
    BrowserUnavailable,
    ChallengePresented,
    StabilizeSpec,
)

CARD_SELECTOR = "[data-meta-product-id]"
PRICE_SELECTOR = '[data-meta-name="Snippet__price"]'
TITLE_SELECTOR = '[data-meta-name="Snippet__title"]'
CART_BUTTON_SELECTOR = '[data-meta-name="Snippet__cart-button"]'

# "12 990₽" / "8990 ₽" / non-breaking and narrow-no-break spaces.
_NON_DIGITS = re.compile(r"[^\d]")


def _parse_price(text: str | None) -> int:
    if not text:
        return 0
    digits = _NON_DIGITS.sub("", text)
    return int(digits) if digits else 0


def _resolve_availability(card: object) -> tuple[bool, str | None]:
    """(in_stock, availability) for one catalog card.

    HONEST LIMIT: on the pages captured live 29.08.2026 every single card
    carried an enabled add-to-cart control, so the in-stock branch is the
    only one that has ever been observed. Rather than hardcode
    `in_stock=True` — which would assert something this source has not
    actually told us — we read the control and fall back to *unknown*
    (availability=None) when it's missing. Downstream already treats an
    unknown availability as a first-class state and declines to filter on
    it (see cli.build -> availability_unrecognized), so an unseen
    out-of-stock rendering degrades into "we don't know" rather than into a
    confident wrong answer.

    Deliberately NOT invented: Regard reports "предзаказ" and "нет в
    наличии"; nothing on a Citilink catalog card corresponds to either, and
    guessing an equivalent would be exactly the plausible-sounding fiction
    regard.py's own availability note warns against.
    """
    button = card.css_first(CART_BUTTON_SELECTOR)  # type: ignore[attr-defined]
    if button is None:
        return False, None
    return True, "в наличии"


class CitilinkSource(BaseSource):
    name = "citilink"
    base_url = "https://www.citilink.ru"

    # VERIFIED LIVE 29.08.2026. Each slug was loaded in a real browser and
    # checked against Next.js's own `props.pageProps.initialProps.notFound`
    # flag, not eyeballed. Two of the previous best-effort guesses were
    # wrong and 404'd; their replacements came from Citilink's own published
    # sitemap (sitemap/main/product_groups.xml), not from guessing again:
    #   ram    : "operativnaya-pamyat"       -> 404, now "moduli-pamyati"
    #   cooler : "kulery-dlya-protsessorov"  -> 404, now
    #            "sistemy-ohlazhdeniya-processora"
    #            ("kulery-dlya-processorov" and "sistemy-ohlazhdeniya" were
    #             also tried and also 404.)
    CATEGORY_MAP: dict[Category, str] = {
        "cpu": "processory",
        "gpu": "videokarty",
        "motherboard": "materinskie-platy",
        "ram": "moduli-pamyati",
        "ssd": "ssd-nakopiteli",
        "psu": "bloki-pitaniya",
        "case": "korpusa",
        "cooler": "sistemy-ohlazhdeniya-processora",
    }

    # Citilink paginates with ?p=N. Kept low on purpose: this is a price
    # tracker, not a catalog mirror, and every page is a real browser
    # navigation against a live shop.
    MAX_PAGES = 5

    def __init__(self, *args: object, headless: bool = True, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._headless = headless
        self._browser: BrowserFetcher | None = None

    # ------------------------------------------------------------ transport

    def _backoff_seconds(self, attempt: int) -> float:
        """30s, 60s, 90s — the conservative retry ladder this portfolio's
        RUSIMEX scrapers already use against protected sites, rather than
        BaseSource's 2/4/8s default.

        Retries here are for genuinely transient trouble (a navigation
        timeout, a 5xx). They are NOT how we respond to being blocked: a WAF
        response or a presented challenge raises SourceBlocked immediately,
        with no retry at all, so this ladder can never turn into hammering a
        site that has told us to go away. Faster hardware is not a reason to
        shorten it."""
        return 30.0 * attempt + random.uniform(0, 1)

    def _get_browser(self) -> BrowserFetcher:
        if self._browser is None:
            self._browser = BrowserFetcher(
                user_agent=USER_AGENT,
                base_url=self.base_url,
                headless=self._headless,
            )
            self._browser.start()
        return self._browser

    def _transport_get(self, url: str, **kwargs: object) -> httpx.Response:
        """Everything this source fetches goes through the browser, including
        robots.txt — a cold plain-HTTP robots.txt fetch is 429 here, and
        failing closed on that would stop the source before it began."""
        try:
            browser = self._get_browser()
            if url.endswith("/robots.txt"):
                return browser.fetch_raw(url)
            return browser.fetch_document(
                url,
                wait_for_selector=CARD_SELECTOR,
                stabilize=StabilizeSpec(item_selector=CARD_SELECTOR, ready_selector=PRICE_SELECTOR),
            )
        except ChallengePresented as exc:
            # Absolute stop. Never solve, never retry into it.
            raise SourceBlocked(f"citilink presented a challenge — not solving it, stopping: {exc}") from exc
        except BrowserUnavailable as exc:
            raise SourceBlocked(f"citilink needs a browser and none is available: {exc}") from exc

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None

    # -------------------------------------------------------------- fetching

    def fetch_category(self, category: Category, captured_at: datetime) -> list[RawOffer]:
        if category not in self.CATEGORY_MAP:
            raise ValueError(f"citilink: unsupported category {category!r}")
        slug = self.CATEGORY_MAP[category]

        offers: list[RawOffer] = []
        seen_ids: set[str] = set()
        try:
            for page in range(1, self.max_pages + 1):
                url = f"{self.base_url}/catalog/{slug}/"
                if page > 1:
                    url = f"{url}?p={page}"

                resp = self.get(url)
                page_offers = self._parse_listing(resp.text, category, captured_at)

                fresh = [o for o in page_offers if o.external_id not in seen_ids]
                for offer in fresh:
                    seen_ids.add(offer.external_id)

                # Persist this page BEFORE fetching the next one. If the run
                # dies on page 4, pages 1-3 are already in the database.
                self._checkpoint(fresh)
                offers.extend(fresh)

                self.log.info(
                    "page_fetched",
                    category=category,
                    page=page,
                    items_on_page=len(page_offers),
                    new_items=len(fresh),
                    collected_so_far=len(offers),
                )

                if not page_offers or not fresh:
                    break
                if page < self.max_pages:
                    self._sleep()
        finally:
            self.close()

        return offers

    def _parse_listing(self, html: str, category: Category, captured_at: datetime) -> list[RawOffer]:
        tree = HTMLParser(html)
        cards = tree.css(CARD_SELECTOR)
        if not cards:
            raise SourceBlocked(
                "no product cards found on the Citilink listing "
                "(page shape changed, or we were served something other than the catalog)"
            )

        offers: list[RawOffer] = []
        skipped_unhydrated = 0
        skipped_no_price = 0

        for card in cards:
            external_id = card.attributes.get("data-meta-product-id")
            if not external_id:
                continue

            title_node = card.css_first(TITLE_SELECTOR)
            if title_node is None:
                # An un-hydrated shell (the grid lazy-renders). Not an error
                # and not a block — just nothing to read yet.
                skipped_unhydrated += 1
                continue

            title = " ".join(title_node.text().split())
            price = _parse_price(card.css_first(PRICE_SELECTOR).text() if card.css_first(PRICE_SELECTOR) else None)
            if not title or not price:
                skipped_no_price += 1
                continue

            anchor = card.css_first("a[href]")
            href = anchor.attributes.get("href", "") if anchor else ""
            url = href if href.startswith("http") else f"{self.base_url}{href}"

            # Use the SHORT card title, not the long one, so the normalized
            # key lines up with Regard's and cross-source comparison works:
            #   Regard   -> "AMD Ryzen 7 7800X3D OEM" -> cpu:amd:ryzen-7-7800x3d-oem
            #   Citilink -> "Процессор AMD Ryzen 7 7800X3D, OEM"
            #                                          -> cpu:amd:ryzen-7-7800x3d-oem
            # (verified identical against tests/fixtures/cpu_item0.json).
            # The long title adds the socket ("AM4"), which Regard's title
            # omits — keeping it would produce "ryzen-5-5600-am4" and silently
            # match nothing. The Russian category nouns need no stripping here;
            # config/synonyms.yaml already lists them as noise words.
            #
            # The manufacturer part number is deliberately NOT appended to the
            # title. normalize._tokenize_model explicitly keeps vendor codes
            # out of the key, so it buys no identity — while Citilink's Intel
            # codes are two whitespace-separated tokens ("cm8071504650609
            # srl5z") that normalize._extract_vendor_code refuses (it requires
            # a space-free candidate), leaving them stranded in the model and
            # turning "core-i5-12400f-oem" into
            # "core-i5-12400f-oem-cm8071504650609-srl5z" — which would never
            # match Regard. It stays available in `url` instead.

            in_stock, availability = _resolve_availability(card)

            offers.append(
                RawOffer(
                    source=self.name,
                    category=category,
                    external_id=str(external_id),
                    title=title,
                    # No structured vendor field exists on a Citilink card.
                    # Leave this None so normalize.py resolves the brand from
                    # the title via config/synonyms.yaml, exactly as it does
                    # for any source without a vendor field. Putting the part
                    # number here would make the part number the *brand*.
                    brand_hint=None,
                    price=price,
                    # Citilink's "клубная цена" is a LOYALTY price — lower
                    # than, not older than, the shelf price. Deliberately not
                    # mapped onto price_old, whose meaning across this project
                    # is "the higher pre-discount price". Recording it there
                    # would invert every discount calculation downstream.
                    price_old=None,
                    in_stock=in_stock,
                    availability=availability,
                    url=url,
                    # DELIBERATELY None. A Citilink catalog card publishes no
                    # spec line at all, and `brief` is not free text to the
                    # rest of this project — normalize.py parses it
                    # POSITIONALLY, taking segments[0] as the CPU socket and
                    # the motherboard form factor. Feeding it the product name
                    # yields socket="ПРОЦЕССОРAMDRYZEN55600" (measured), which
                    # then flows into compat.check_compatibility and produces
                    # confidently wrong PC builds.
                    #
                    # Nothing is the safe answer here, not the lazy one:
                    # compat.socket_matches(None, x) is False for every x, so a
                    # missing socket surfaces as a visible compatibility issue,
                    # whereas a fabricated one silently passes or silently
                    # rejects. Fail loud, never plausibly wrong.
                    brief=None,
                    captured_at=captured_at,
                )
            )

        if skipped_unhydrated or skipped_no_price:
            # A page that only half-rendered still returns offers, so it can
            # masquerade as a clean run. Say so loudly when most of the grid
            # never filled in — that's a scroll/stability problem to fix, not
            # a smaller catalog.
            emit = self.log.warning if skipped_unhydrated > len(offers) else self.log.info
            emit(
                "cards_skipped",
                unhydrated=skipped_unhydrated,
                no_price=skipped_no_price,
                parsed=len(offers),
                total_cards=len(cards),
            )
        if not offers:
            raise SourceBlocked(
                f"{len(cards)} Citilink cards on the page but none carried a title+price "
                "(hydration never completed, or the card markup changed)"
            )
        return offers
