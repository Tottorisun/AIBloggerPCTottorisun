"""Cross-source product matching.

Citilink turned out to be blocked at the HTTP level by its Qrator WAF (see
sources/citilink.py) — every request, including robots.txt, comes back
429 before we ever see a real category page. There's no way to get actual
Citilink listing data without executing JS or solving their challenge,
which this project doesn't do. So there's no live second-source data to
demonstrate a real compare run against.

What *can* be verified without live data: that normalize_offer() would
actually collapse a second source's listing for the same physical product
into the same normalized_key as Regard's, given how such a listing is
likely to be formatted. The main risk is that Regard's `title` field
conveniently omits the Russian category word ("Процессор", "Видеокарта",
...) while a generic listing is very likely to include it right in the
name — if normalize.py didn't strip that, two stores' listings for the
same product would tokenize to different keys and silently never match.
"""

from datetime import datetime

from pc_price_tracker.models import RawOffer
from pc_price_tracker.normalize import normalize_offer

from .conftest import raw_offer_from_regard_item


def _other_store_offer(**overrides) -> RawOffer:
    defaults = dict(
        source="citilink",
        category="cpu",
        external_id="citilink-1",
        title="Процессор AMD Ryzen 7 7800X3D (OEM)",
        brand_hint="AMD",
        price=27990,
        in_stock=True,
        url="https://example.invalid/citilink/1",
        brief="AM5, 8-ядерный, TDP 120 Вт",
        captured_at=datetime(2026, 8, 19),
    )
    defaults.update(overrides)
    return RawOffer(**defaults)


def test_same_cpu_from_a_different_store_collapses_to_the_regard_key(regard_items):
    regard = normalize_offer(raw_offer_from_regard_item(regard_items["cpu"], "cpu"))
    assert regard.model  # sanity: fixture is "AMD Ryzen 7 7800X3D OEM"

    other_store = normalize_offer(_other_store_offer())

    assert other_store.normalized_key == regard.normalized_key
    assert other_store.brand == regard.brand


def test_category_prefix_alone_does_not_change_the_key():
    with_prefix = normalize_offer(_other_store_offer(title="Процессор AMD Ryzen 7 7800X3D (OEM)"))
    without_prefix = normalize_offer(_other_store_offer(title="AMD Ryzen 7 7800X3D (OEM)"))
    assert with_prefix.normalized_key == without_prefix.normalized_key


def test_gpu_category_prefix_also_stripped():
    a = normalize_offer(
        _other_store_offer(
            category="gpu",
            title="Видеокарта NVIDIA GeForce RTX 5060 Palit Dual 8GB",
            brand_hint="Palit",
            brief="PCI-E 5.0, длина 262 мм",
            width_mm=262,
        )
    )
    b = normalize_offer(
        _other_store_offer(
            category="gpu",
            title="NVIDIA GeForce RTX 5060 Palit Dual 8GB",
            brand_hint="Palit",
            brief="PCI-E 5.0, длина 262 мм",
            width_mm=262,
        )
    )
    assert a.normalized_key == b.normalized_key


def test_different_stores_different_products_still_get_different_keys():
    cpu = normalize_offer(_other_store_offer(title="Процессор AMD Ryzen 7 7800X3D (OEM)"))
    other_cpu = normalize_offer(_other_store_offer(title="Процессор AMD Ryzen 5 5600X (BOX)", price=12000))
    assert cpu.normalized_key != other_cpu.normalized_key
