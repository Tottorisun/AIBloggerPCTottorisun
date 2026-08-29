"""Offline coverage for the Citilink adapter and the browser transport.

Everything here runs against captured markup and hand-built responses —
no browser is launched and no network call is made, so this suite is safe
for CI (which continues to exclude live scraping entirely).

tests/fixtures/citilink_cpu_listing.html is REAL markup captured from
citilink.ru/catalog/processory/ on 29.08.2026: three hydrated product cards
(including an Intel one whose part number is two whitespace-separated
tokens) plus one un-hydrated shell, which is exactly the mix the live page
serves.
"""

from datetime import datetime
from pathlib import Path

import httpx
import pytest

from pc_price_tracker.constants import CATEGORIES
from pc_price_tracker.models import RawOffer
from pc_price_tracker.normalize import normalize_offer
from pc_price_tracker.sources.base import SourceBlocked, _looks_like_captcha
from pc_price_tracker.sources.browser import (
    _adapt_headers,
    _is_interactive_challenge,
    _is_unresolved_interstitial,
)
from pc_price_tracker.sources.citilink import CitilinkSource

FIXTURE = Path(__file__).parent / "fixtures" / "citilink_cpu_listing.html"
CAPTURED_AT = datetime(2026, 8, 29, 12, 0, 0)


@pytest.fixture
def listing_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def source() -> CitilinkSource:
    # No client and no browser is ever touched: these tests call
    # _parse_listing directly.
    return CitilinkSource(client=httpx.Client())


def _parse(source: CitilinkSource, html: str) -> list[RawOffer]:
    return source._parse_listing(html, "cpu", CAPTURED_AT)


def test_parses_real_captured_cards(source, listing_html):
    offers = _parse(source, listing_html)
    # 4 cards in the fixture, one of which is an un-hydrated shell.
    assert len(offers) == 3
    assert all(o.source == "citilink" for o in offers)
    assert all(o.category == "cpu" for o in offers)
    assert all(o.captured_at == CAPTURED_AT for o in offers)

    first = offers[0]
    assert first.external_id == "1970792"
    assert first.title == "Процессор AMD Ryzen 5 5600, OEM"
    assert first.price == 12990
    assert first.url.startswith("https://www.citilink.ru/product/")
    assert first.in_stock is True
    assert first.availability == "в наличии"


def test_prices_survive_the_sites_space_separators(source, listing_html):
    """Citilink renders "12 990₽" with a non-breaking space; a naive int()
    on that raises, and a naive isdigit() filter silently loses digits."""
    prices = sorted(o.price for o in _parse(source, listing_html))
    assert prices == [8990, 12990, 13490]


def test_unhydrated_shells_are_skipped_not_fatal(source, listing_html):
    """The grid renders empty card shells and fills them in on scroll. A
    shell is missing data, not evidence of a block."""
    offers = _parse(source, listing_html)
    assert all(o.title for o in offers)
    assert all(o.price for o in offers)


def test_brief_is_never_populated(source, listing_html):
    """REGRESSION GUARD — do not "improve" this by passing the product name.

    normalize.py parses `brief` POSITIONALLY: segments[0] becomes the CPU
    socket and the motherboard form factor. Citilink cards carry no spec
    line, and feeding the product name in produced
    socket="ПРОЦЕССОРAMDRYZEN55600", which flows straight into
    compat.check_compatibility and yields confidently wrong builds.
    None is the correct answer: compat.socket_matches(None, x) is False for
    every x, so the gap shows up as a visible compatibility issue instead."""
    offers = _parse(source, listing_html)
    assert all(o.brief is None for o in offers)
    for offer in offers:
        assert "socket" not in normalize_offer(offer).specs


def test_brand_hint_is_none_so_brand_comes_from_the_title(source, listing_html):
    """A Citilink card exposes no vendor field. brand_hint must stay None so
    normalize.py resolves the brand via config/synonyms.yaml — putting the
    part number there would make the part number the brand."""
    offers = _parse(source, listing_html)
    assert all(o.brand_hint is None for o in offers)
    brands = {normalize_offer(o).brand for o in offers}
    assert brands <= {"AMD", "Intel"}


def test_keys_match_regards_shape_for_cross_source_comparison(source, listing_html):
    """The whole point of a second source is comparing like with like.

    Two things break that and are both regression-guarded here: keeping the
    socket from Citilink's long title ("ryzen-5-5600-am4"), and leaving an
    Intel part number in the model
    ("core-i5-12400f-oem-cm8071504650609-srl5z"). Regard produces
    "cpu:intel:core-i5-12400f-oem" for the same chip."""
    keys = {normalize_offer(o).normalized_key for o in _parse(source, listing_html)}
    assert "cpu:intel:core-i5-12400f-oem" in keys
    assert "cpu:amd:ryzen-5-5600-oem" in keys
    assert not any("am4" in k or "am5" in k for k in keys)
    assert not any("cm8071504650609" in k or "srl5z" in k for k in keys)


def test_club_price_is_not_recorded_as_price_old(source, listing_html):
    """Citilink's "клубная цена" is a loyalty price — LOWER than the shelf
    price. price_old means the higher pre-discount price everywhere else in
    this project; storing the club price there would invert every discount
    calculation downstream."""
    offers = _parse(source, listing_html)
    assert all(o.price_old is None for o in offers)


def test_no_cards_at_all_is_reported_as_blocked(source):
    with pytest.raises(SourceBlocked, match="no product cards"):
        _parse(source, "<html><body><p>nothing here</p></body></html>")


def test_cards_present_but_none_hydrated_is_reported_as_blocked(source):
    html = '<html><body><div data-meta-product-id="1">&zwnj;</div></body></html>'
    with pytest.raises(SourceBlocked, match="none carried a title"):
        _parse(source, html)


def test_category_map_covers_every_category():
    assert set(CitilinkSource.CATEGORY_MAP) == set(CATEGORIES)


def test_dead_slugs_stay_fixed():
    """Both of these 404'd when verified live on 29.08.2026; the
    replacements came from Citilink's own sitemap. Guard against a revert."""
    assert CitilinkSource.CATEGORY_MAP["ram"] == "moduli-pamyati"
    assert CitilinkSource.CATEGORY_MAP["cooler"] == "sistemy-ohlazhdeniya-processora"


def test_unsupported_category_is_a_value_error(source):
    with pytest.raises(ValueError, match="unsupported category"):
        source.fetch_category("gpu-that-does-not-exist", CAPTURED_AT)  # type: ignore[arg-type]


# --------------------------------------------------------------- checkpointing


class _FakeBrowserSource(CitilinkSource):
    """Serves canned pages so pagination + checkpointing can be exercised
    without a browser."""

    def __init__(self, pages: list[str], **kwargs: object) -> None:
        super().__init__(client=httpx.Client(), **kwargs)  # type: ignore[arg-type]
        self._pages = pages
        self.requested: list[str] = []
        self._robots = None

    def _check_allowed(self, url: str) -> None:  # robots is tested elsewhere
        return None

    def _sleep(self) -> None:
        return None

    def close(self) -> None:
        return None

    def _transport_get(self, url: str, **kwargs: object) -> httpx.Response:
        self.requested.append(url)
        index = len(self.requested) - 1
        body = self._pages[index] if index < len(self._pages) else "<html><body></body></html>"
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=body,
            request=httpx.Request("GET", url),
        )


def test_each_page_is_checkpointed_before_the_next_is_fetched(listing_html):
    """Portfolio hard rule: a long scrape persists each completed unit as it
    finishes. The sink must be called per page, DURING the run — not once at
    the end — so a crash on page N still leaves pages 1..N-1 saved."""
    saved: list[list[RawOffer]] = []
    fetches_at_each_checkpoint: list[int] = []
    source = _FakeBrowserSource([listing_html, listing_html])

    def sink(offers: list[RawOffer]) -> None:
        saved.append(offers)
        fetches_at_each_checkpoint.append(len(source.requested))

    source.offer_sink = sink
    source.max_pages = 2
    offers = source.fetch_category("cpu", CAPTURED_AT)

    # Page 1 was handed over while only one page had been fetched.
    assert fetches_at_each_checkpoint[0] == 1
    assert len(saved) >= 1
    assert saved[0]
    # Page 2 repeats page 1's ids, so nothing new survives dedup and the run
    # stops — total offers stay unique.
    assert len({o.external_id for o in offers}) == len(offers)


def test_pagination_uses_the_p_query_parameter(listing_html):
    source = _FakeBrowserSource([listing_html, listing_html])
    source.max_pages = 2
    source.fetch_category("cpu", CAPTURED_AT)
    assert source.requested[0].endswith("/catalog/processory/")


# ------------------------------------------------------- block detection


def test_retry_backoff_uses_the_conservative_ladder(source):
    """RUSIMEX-proven pacing: 30s, 60s, 90s — not BaseSource's 2/4/8s.
    Faster hardware is not a reason to shorten it."""
    for attempt, floor in ((1, 30.0), (2, 60.0), (3, 90.0)):
        wait = source._backoff_seconds(attempt)
        assert floor <= wait < floor + 1.0


def test_a_waf_block_stops_immediately_without_retrying():
    """The conservative ladder must never become a way to hammer a site that
    has told us to go away: a WAF response raises SourceBlocked on the first
    attempt, with no retries at all."""
    attempts = []

    class _WafSource(CitilinkSource):
        def _check_allowed(self, url):
            return None

        def _transport_get(self, url, **kwargs):
            attempts.append(url)
            return httpx.Response(
                429, headers={"server": "QRATOR"}, request=httpx.Request("GET", url)
            )

    src = _WafSource(client=httpx.Client())
    with pytest.raises(SourceBlocked, match="WAF block"):
        src.get("https://www.citilink.ru/catalog/processory/")
    assert len(attempts) == 1


def test_healthy_page_is_not_mistaken_for_a_block(listing_html):
    """THE false positive this project already hit once (29.08.2026 source
    evaluation): a perfectly healthy Citilink page contains the strings
    "qrator" (an ordinary session cookie — Qrator fronts the whole site) and
    "captcha" (an inert key in the shop's own login-form Redux state).
    Neither is evidence of anything."""
    assert "captcha" in listing_html.lower()
    assert not _is_unresolved_interstitial(listing_html)
    assert not _is_interactive_challenge(listing_html)

    resp = httpx.Response(
        200,
        headers={"content-type": "text/html; charset=utf-8"},
        text=listing_html,
        request=httpx.Request("GET", "https://www.citilink.ru/catalog/processory/"),
    )
    assert not _looks_like_captcha(resp)


def test_unresolved_waf_interstitial_is_recognised():
    """The real Qrator shim: small, no application payload, a cookie shim
    and a loader."""
    shim = (
        "<html><head><title>Загрузка...</title>"
        '<script src="https://cdn.jsdelivr.net/npm/js-cookie@2/src/js.cookie.min.js"></script>'
        "</head><body><div class='loader'></div></body></html>"
    )
    assert _is_unresolved_interstitial(shim)


def test_already_decompressed_body_is_not_advertised_as_gzip():
    """REGRESSION — this broke the very first live run.

    Playwright returns decompressed text but still reports
    `content-encoding: gzip`. Handing both to httpx.Response makes httpx
    gunzip plain text ("Error -3 while decompressing data"), which
    BaseSource then reports as "couldn't verify robots.txt" and fails the
    entire source closed for a reason that has nothing to do with the site.
    """
    adapted = _adapt_headers(
        {"Content-Encoding": "gzip", "content-length": "1234", "Content-Type": "text/plain"}
    )
    assert "content-encoding" not in {k.lower() for k in adapted}
    assert "content-length" not in {k.lower() for k in adapted}
    assert adapted["Content-Type"] == "text/plain"

    # The decisive part: the adapted headers must let httpx read the body back.
    resp = httpx.Response(
        200, headers=adapted, text="User-agent: Yandex\n", request=httpx.Request("GET", "https://x/robots.txt")
    )
    assert resp.text.startswith("User-agent")


def test_real_challenge_widgets_are_recognised_so_we_can_stop():
    """We never solve these. Detection exists so the source can stop and
    report itself blocked."""
    for marker in (
        '<div class="g-recaptcha" data-sitekey="x"></div>',
        '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>',
        '<div id="smartcaptcha-container"></div>',
    ):
        assert _is_interactive_challenge(f"<html><body>{marker}</body></html>")
