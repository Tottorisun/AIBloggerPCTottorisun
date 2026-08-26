"""Affiliate feed adapter tests.

No real Admitad/ePN feed was available during development (feed_url is
null in config/feeds.yaml until one is connected — see that file and
sources/affiliate_feed.py's docstring). What's tested here instead: the
parser against a hand-built sample that follows the public YML
("Yandex Market Language") spec and a generic CSV layout, which is a real,
verifiable target even without live credentials — unlike a scraped site's
undocumented markup, YML's structure is publicly documented and stable.
"""

from datetime import datetime

import httpx
import pytest

from pc_price_tracker.normalize import normalize_offer
from pc_price_tracker.sources.affiliate_feed import AffiliateFeedSource, FeedNotConfigured

from .conftest import load_regard_item, raw_offer_from_regard_item

SAMPLE_YML = """<?xml version="1.0" encoding="UTF-8"?>
<yml_catalog date="2026-08-20 12:00">
<shop>
  <name>Test Partner Shop</name>
  <company>Test Partner LLC</company>
  <url>https://partner-shop.example/</url>
  <currencies><currency id="RUB" rate="1"/></currencies>
  <categories>
    <category id="1">Видеокарты</category>
    <category id="2">Процессоры</category>
    <category id="99">Наушники</category>
  </categories>
  <offers>
    <offer id="1001" available="true">
      <url>https://partner-shop.example/product/1001</url>
      <price>47540</price>
      <currencyId>RUB</currencyId>
      <categoryId>1</categoryId>
      <vendor>Palit</vendor>
      <vendorCode>NE75060019P1-GB2063D</vendorCode>
      <name>Видеокарта NVIDIA GeForce RTX 5060 Palit Dual 8GB</name>
    </offer>
    <offer id="1002" available="false">
      <url>https://partner-shop.example/product/1002</url>
      <price>28990</price>
      <currencyId>RUB</currencyId>
      <categoryId>2</categoryId>
      <vendor>AMD</vendor>
      <name>Процессор AMD Ryzen 7 7800X3D OEM</name>
    </offer>
    <!-- Category not in our map -> must be skipped, not crash -->
    <offer id="1003" available="true">
      <url>https://partner-shop.example/product/1003</url>
      <price>5000</price>
      <currencyId>RUB</currencyId>
      <categoryId>99</categoryId>
      <name>Наушники Sony WH-1000XM5</name>
    </offer>
    <!-- Missing price -> must be skipped, not crash -->
    <offer id="1004" available="true">
      <url>https://partner-shop.example/product/1004</url>
      <currencyId>RUB</currencyId>
      <categoryId>1</categoryId>
      <name>Видеокарта без цены</name>
    </offer>
  </offers>
</shop>
</yml_catalog>
"""

SAMPLE_CSV = """id,title,price,brand,url,category
2001,"AMD Ryzen 5 5600X BOX",15760,AMD,https://partner-shop.example/product/2001,cpu
2002,"No price item",,AMD,https://partner-shop.example/product/2002,cpu
2003,"Unmapped category item",1000,Foo,https://partner-shop.example/product/2003,headphones
"""

CATEGORY_MAP = {"1": "gpu", "2": "cpu"}


class _FeedSource(AffiliateFeedSource):
    network_name = "test_network"


def _make_source(monkeypatch, *, feed_text: str, feed_format: str, category_map=None, csv_column_map=None, enabled=True, feed_url="https://feed.example/products.xml"):
    def fake_load_feeds_config():
        return {
            "test_network": {
                "enabled": enabled,
                "feed_url": feed_url,
                "format": feed_format,
                "category_map": category_map if category_map is not None else CATEGORY_MAP,
                "csv_column_map": csv_column_map or {},
            }
        }

    monkeypatch.setattr("pc_price_tracker.sources.affiliate_feed.load_feeds_config", fake_load_feeds_config)

    class _FakeClient:
        def get(self, url, **kwargs):
            return httpx.Response(200, text=feed_text, request=httpx.Request("GET", url))

    return _FeedSource(client=_FakeClient())


def test_not_configured_raises_cleanly(monkeypatch):
    source = _make_source(monkeypatch, feed_text="", feed_format="yml", enabled=False, feed_url=None)
    with pytest.raises(FeedNotConfigured):
        source.fetch_category("gpu", datetime(2026, 8, 20))


def test_yml_parses_offers_into_right_categories(monkeypatch):
    source = _make_source(monkeypatch, feed_text=SAMPLE_YML, feed_format="yml")
    gpus = source.fetch_category("gpu", datetime(2026, 8, 20))
    cpus = source.fetch_category("cpu", datetime(2026, 8, 20))

    assert len(gpus) == 1
    assert gpus[0].title == "Видеокарта NVIDIA GeForce RTX 5060 Palit Dual 8GB"
    assert gpus[0].brand_hint == "Palit"
    assert gpus[0].price == 47540
    assert gpus[0].external_id == "1001"
    assert gpus[0].source == "test_network"

    assert len(cpus) == 1
    assert cpus[0].title == "Процессор AMD Ryzen 7 7800X3D OEM"
    assert cpus[0].in_stock is False  # available="false"


def test_yml_skips_unmapped_category_and_incomplete_offers_without_crashing(monkeypatch):
    source = _make_source(monkeypatch, feed_text=SAMPLE_YML, feed_format="yml")
    all_offers = source.fetch_category("gpu", datetime(2026, 8, 20)) + source.fetch_category("cpu", datetime(2026, 8, 20))
    # Only offers 1001 (gpu) and 1002 (cpu) should survive; 1003 (unmapped
    # category) and 1004 (no price) are silently dropped, not crashed on.
    assert {o.external_id for o in all_offers} == {"1001", "1002"}


def test_yml_offer_caches_across_category_calls(monkeypatch):
    """fetch_category is called once per our 8 categories in scrape-all —
    the whole feed must be downloaded/parsed once, not 8 times."""
    calls = []

    def fake_load_feeds_config():
        return {
            "test_network": {
                "enabled": True,
                "feed_url": "https://feed.example/products.xml",
                "format": "yml",
                "category_map": CATEGORY_MAP,
                "csv_column_map": {},
            }
        }

    monkeypatch.setattr("pc_price_tracker.sources.affiliate_feed.load_feeds_config", fake_load_feeds_config)

    class _CountingClient:
        def get(self, url, **kwargs):
            calls.append(url)
            return httpx.Response(200, text=SAMPLE_YML, request=httpx.Request("GET", url))

    source = _FeedSource(client=_CountingClient())
    for category in ("cpu", "gpu", "motherboard", "ram", "ssd", "psu", "case", "cooler"):
        source.fetch_category(category, datetime(2026, 8, 20))
    assert len(calls) == 1


def test_csv_parses_offers_into_right_categories(monkeypatch):
    source = _make_source(monkeypatch, feed_text=SAMPLE_CSV, feed_format="csv", category_map={"cpu": "cpu"})
    cpus = source.fetch_category("cpu", datetime(2026, 8, 20))
    assert len(cpus) == 1
    assert cpus[0].title == "AMD Ryzen 5 5600X BOX"
    assert cpus[0].price == 15760
    assert cpus[0].brand_hint == "AMD"


def test_csv_skips_incomplete_and_unmapped_rows(monkeypatch):
    source = _make_source(monkeypatch, feed_text=SAMPLE_CSV, feed_format="csv", category_map={"cpu": "cpu"})
    cpus = source.fetch_category("cpu", datetime(2026, 8, 20))
    assert len(cpus) == 1  # row 2002 (no price) and 2003 (unmapped category) dropped


# ---------------------------------------------------------------------------
# The actual point of adding a second source: a feed offer for the same
# real-world product as an existing Regard listing should normalize to the
# same key and collapse into one product.
# ---------------------------------------------------------------------------


def test_feed_offer_collapses_with_regard_product(monkeypatch, regard_items):
    source = _make_source(monkeypatch, feed_text=SAMPLE_YML, feed_format="yml")
    gpus = source.fetch_category("gpu", datetime(2026, 8, 20))
    feed_normalized = normalize_offer(gpus[0])

    regard_raw = raw_offer_from_regard_item(regard_items["gpu"], "gpu")
    regard_normalized = normalize_offer(regard_raw)

    assert feed_normalized.normalized_key == regard_normalized.normalized_key
