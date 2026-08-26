from datetime import datetime

import pytest

from pc_price_tracker.models import RawOffer
from pc_price_tracker.normalize import NormalizationError, normalize_offer

from .conftest import raw_offer_from_regard_item


# ---------------------------------------------------------------------------
# Real captured Regard data: one item per category, brand/model/specs must
# come out sane and every category must produce a usable key (no exceptions).
# ---------------------------------------------------------------------------

CATEGORY_FIXTURES = [
    ("gpu", "gpu"),
    ("cpu", "cpu"),
    ("motherboard", "mobo"),
    ("ram", "ram"),
    ("ssd", "ssd"),
    ("psu", "psu"),
    ("case", "case"),
    ("cooler", "cooler"),
]


@pytest.mark.parametrize("category,fixture_name", CATEGORY_FIXTURES)
def test_real_items_normalize_without_error(regard_items, category, fixture_name):
    raw = raw_offer_from_regard_item(regard_items[fixture_name], category)
    normalized = normalize_offer(raw)

    assert normalized.brand
    assert normalized.model
    assert normalized.normalized_key.startswith(f"{category}:")
    assert normalized.specs  # every sample fixture has at least one extractable spec


def test_gpu_specs_pull_length_and_memory_from_regard_item(regard_items):
    raw = raw_offer_from_regard_item(regard_items["gpu"], "gpu")
    normalized = normalize_offer(raw)

    assert normalized.brand == "Palit"
    assert normalized.specs["memory_gb"] == 8
    assert normalized.specs["length_mm"] == 262.0
    assert normalized.specs["vendor_code"] == "NE75060019P1-GB2063D"


def test_cpu_socket_and_tdp_extracted(regard_items):
    raw = raw_offer_from_regard_item(regard_items["cpu"], "cpu")
    normalized = normalize_offer(raw)

    assert normalized.brand == "AMD"
    assert normalized.specs["socket"] == "AM5"
    assert normalized.specs["tdp_w"] == 120
    assert normalized.specs["packaging"] == "OEM"


def test_case_specs_survive_html_tags_in_brief():
    """Regard wraps some units in <noindex> tags right where our regexes
    expect a bare number+unit (e.g. "высота кулера до 160 <noindex>мм</noindex>").
    This must not silently drop the field."""
    raw = RawOffer(
        source="regard",
        category="case",
        external_id="1",
        title="Test Case ATX",
        brand_hint="TestBrand",
        price=5000,
        in_stock=True,
        url="https://example.invalid/1",
        brief="Midi-Tower, ATX, mATX, без БП, длина видеокарты до 400 мм, "
        "высота кулера до 160 <noindex>мм</noindex>",
        captured_at=datetime(2026, 8, 18),
    )
    normalized = normalize_offer(raw)
    assert normalized.specs["max_gpu_length_mm"] == 400
    assert normalized.specs["max_cooler_height_mm"] == 160


# ---------------------------------------------------------------------------
# The exact scenario called out in the brief: differently formatted titles
# for the same chip/AIB/vendor-code SKU must collapse to one normalized_key,
# regardless of "GeForce" prefix, glued vs spaced chip number, or Ti casing.
# ---------------------------------------------------------------------------


def _gpu_offer(title: str) -> RawOffer:
    return RawOffer(
        source="regard",
        category="gpu",
        external_id="1",
        title=title,
        brand_hint="Palit",
        price=90000,
        in_stock=True,
        url="https://example.invalid/1",
        brief="PCI-E 5.0, длина 332 мм",
        width_mm=332,
        captured_at=datetime(2026, 8, 18),
    )


@pytest.mark.parametrize(
    "title",
    [
        "NVIDIA GeForce RTX 5070 Ti 16GB (ABC123-XYZ)",
        "GeForce RTX5070Ti 16GB (ABC123-XYZ)",
        "RTX 5070TI 16GB (ABC123-XYZ)",
    ],
)
def test_gpu_title_formatting_noise_collapses_to_same_key(title):
    normalized = normalize_offer(_gpu_offer(title))
    assert normalized.normalized_key == "gpu:palit:rtx-5070-ti-16gb"


def test_gpu_oc_variant_is_a_distinct_key_not_merged():
    """OC vs non-OC are different real-world SKUs with different prices —
    they must NOT collapse into the same product."""
    base = normalize_offer(_gpu_offer("RTX 5070 Ti 16GB (ABC123-XYZ)"))
    oc = normalize_offer(_gpu_offer("RTX 5070 Ti OC 16GB (ABC456-OC)"))
    assert base.normalized_key != oc.normalized_key


# ---------------------------------------------------------------------------
# Failure path: offers we genuinely can't identify go to `unmatched`, not a
# silent guess.
# ---------------------------------------------------------------------------


def test_empty_title_raises_normalization_error():
    raw = RawOffer(
        source="regard",
        category="cpu",
        external_id="1",
        title="   ",
        brand_hint="AMD",
        price=1000,
        in_stock=True,
        url="https://example.invalid/1",
        captured_at=datetime(2026, 8, 18),
    )
    with pytest.raises(NormalizationError):
        normalize_offer(raw)


def test_unresolvable_brand_raises_normalization_error():
    raw = RawOffer(
        source="regard",
        category="cpu",
        external_id="1",
        title="Some Completely Unknown Widget 9000",
        brand_hint=None,
        price=1000,
        in_stock=True,
        url="https://example.invalid/1",
        captured_at=datetime(2026, 8, 18),
    )
    with pytest.raises(NormalizationError):
        normalize_offer(raw)


def test_different_products_get_different_keys(regard_items):
    gpu = normalize_offer(raw_offer_from_regard_item(regard_items["gpu"], "gpu"))
    cpu = normalize_offer(raw_offer_from_regard_item(regard_items["cpu"], "cpu"))
    assert gpu.normalized_key != cpu.normalized_key
