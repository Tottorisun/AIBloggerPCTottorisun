from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from pc_price_tracker.constants import Category


class RawOffer(BaseModel):
    """One listing as returned by a source adapter, before normalization."""

    source: str
    category: Category
    external_id: str
    title: str
    brand_hint: str | None = None
    price: int
    price_old: int | None = None
    in_stock: bool
    availability: str | None = None
    url: str
    brief: str | None = None
    width_mm: float | None = None
    height_mm: float | None = None
    depth_mm: float | None = None
    captured_at: datetime


class NormalizedProduct(BaseModel):
    """Result of normalizing a RawOffer into a canonical product identity."""

    category: Category
    brand: str
    model: str
    normalized_key: str
    specs: dict[str, Any] = Field(default_factory=dict)


class Product(BaseModel):
    id: int | None = None
    category: Category
    brand: str
    model: str
    normalized_key: str
    specs: dict[str, Any] = Field(default_factory=dict)


class Offer(BaseModel):
    id: int | None = None
    product_id: int
    source: str
    url: str
    price: int
    in_stock: bool
    availability: str | None = None
    captured_at: datetime


class UnmatchedOffer(BaseModel):
    id: int | None = None
    source: str
    category: Category
    external_id: str
    title: str
    price: int
    url: str
    reason: str
    captured_at: datetime
