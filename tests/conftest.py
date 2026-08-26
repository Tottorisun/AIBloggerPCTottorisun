import json
from datetime import datetime
from pathlib import Path

import pytest

from pc_price_tracker.models import RawOffer

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_regard_item(name: str) -> dict:
    with (FIXTURES_DIR / f"{name}_item0.json").open(encoding="utf-8") as f:
        return json.load(f)


def raw_offer_from_regard_item(item: dict, category: str, **overrides) -> RawOffer:
    defaults = dict(
        source="regard",
        category=category,
        external_id=str(item["id"]),
        title=item["title"],
        brand_hint=item.get("vendor"),
        price=item["price"],
        price_old=item.get("price_old"),
        in_stock=True,
        url=f"https://www.regard.ru/product/{item['id']}/{item.get('seo_url', '')}",
        brief=item.get("brief"),
        width_mm=item.get("width"),
        height_mm=item.get("height"),
        depth_mm=item.get("depth"),
        captured_at=datetime(2026, 8, 18, 12, 0, 0),
    )
    defaults.update(overrides)
    return RawOffer(**defaults)


@pytest.fixture
def regard_items() -> dict:
    return {
        name: load_regard_item(name)
        for name in ["gpu", "cpu", "mobo", "ram", "ssd", "psu", "case", "cooler"]
    }
