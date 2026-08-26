"""Synthetic candidate catalog for builder tests.

Deliberately includes both floor-passing and floor-violating options per
category (single-channel RAM, SATA/small SSDs, uncertified PSUs) so tests
can assert the builder actually excludes the bad ones rather than just
happening not to pick them.
"""

from __future__ import annotations

from typing import Any

TIERS: dict[str, dict[str, Any]] = {
    "cpu:test:budget": {"tier": 30, "source": "estimated"},
    "cpu:test:mid": {"tier": 60, "source": "estimated"},
    "cpu:test:high": {"tier": 85, "source": "verified"},
    "gpu:test:budget": {"tier": 25, "source": "estimated"},
    "gpu:test:budget_plus": {"tier": 40, "source": "estimated"},
    "gpu:test:mid": {"tier": 55, "source": "estimated"},
    "gpu:test:upper_mid": {"tier": 68, "source": "verified"},
    "gpu:test:high": {"tier": 80, "source": "estimated"},
}

# Permissive by default (empty whitelist = no brand filtering) so existing
# tests aren't coupled to brand_rules specifics — the whitelist itself is
# exercised by dedicated tests that override this. The certification floor
# matches the real data/brand_rules.yaml so it composes correctly with the
# existing PSU fixtures (all Bronze/Gold, none below Bronze).
BRAND_RULES: dict[str, Any] = {
    "psu": {
        "whitelist": [],
        "min_certification": "80 PLUS Bronze",
        "min_certification_profiles": ["gaming", "workstation"],
    },
    "ssd": {
        "whitelist": [],
        "require_dram_cache_profiles": ["gaming"],
    },
}


def _offer(category: str, key: str, brand: str, model: str, price: int, specs: dict[str, Any]) -> dict[str, Any]:
    return {
        "product_id": key,
        "category": category,
        "brand": brand,
        "model": model,
        "normalized_key": key,
        "specs": specs,
        "offer_id": key,
        "source": "test",
        "url": f"https://example.invalid/{key}",
        "price": price,
        "in_stock": True,
        "captured_at": "2026-08-19T00:00:00",
    }


def make_catalog() -> dict[str, list[dict[str, Any]]]:
    return {
        "cpu": [
            _offer("cpu", "cpu:test:budget", "Test", "Budget CPU", 8000, {"socket": "AM5", "tdp_w": 65}),
            _offer("cpu", "cpu:test:mid", "Test", "Mid CPU", 15000, {"socket": "AM5", "tdp_w": 105}),
            _offer("cpu", "cpu:test:high", "Test", "High CPU", 25000, {"socket": "AM5", "tdp_w": 120}),
        ],
        "gpu": [
            _offer("gpu", "gpu:test:budget", "Test", "Budget GPU", 15000, {"memory_gb": 6, "length_mm": 200.0}),
            # Fills the gap between "budget" and "mid" — needed so student's
            # gpu corridor (35-48% of budget, widened 2026-08-21) has a
            # fixture option to land on at the 60000 budget several tests
            # anchor to; real Regard data doesn't have this gap (the catalog
            # is dense), only this synthetic fixture did.
            _offer("gpu", "gpu:test:budget_plus", "Test", "Budget+ GPU", 24000, {"memory_gb": 8, "length_mm": 220.0}),
            _offer("gpu", "gpu:test:mid", "Test", "Mid GPU", 30000, {"memory_gb": 12, "length_mm": 280.0}),
            _offer("gpu", "gpu:test:upper_mid", "Test", "Upper-mid GPU", 35000, {"memory_gb": 16, "length_mm": 300.0}),
            _offer("gpu", "gpu:test:high", "Test", "High GPU", 45000, {"memory_gb": 16, "length_mm": 320.0}),
        ],
        "motherboard": [
            _offer("motherboard", "mobo:test:cheap", "Test", "Cheap Mobo", 5000, {"socket": "AM5", "form_factor": "ATX", "ram_type": "DDR5"}),
            _offer("motherboard", "mobo:test:mid", "Test", "Mid Mobo", 7000, {"socket": "AM5", "form_factor": "ATX", "ram_type": "DDR5"}),
            _offer("motherboard", "mobo:test:premium", "Test", "Premium Mobo", 9000, {"socket": "AM5", "form_factor": "ATX", "ram_type": "DDR5"}),
        ],
        "ram": [
            # Floor violators (excluded regardless of price/profile-fit):
            _offer("ram", "ram:test:single_8gb", "Test", "Single 8GB", 2000, {"capacity_gb": 8, "modules": 1, "ram_type": "DDR5"}),
            _offer("ram", "ram:test:single_16gb", "Test", "Single 16GB", 4500, {"capacity_gb": 16, "modules": 1, "ram_type": "DDR5"}),
            # Floor-passing:
            _offer("ram", "ram:test:dual_16gb", "Test", "Dual 16GB", 5500, {"capacity_gb": 16, "modules": 2, "ram_type": "DDR5"}),
            _offer("ram", "ram:test:dual_32gb", "Test", "Dual 32GB", 8500, {"capacity_gb": 32, "modules": 2, "ram_type": "DDR5"}),
            _offer("ram", "ram:test:dual_64gb", "Test", "Dual 64GB", 16000, {"capacity_gb": 64, "modules": 2, "ram_type": "DDR5"}),
        ],
        "ssd": [
            # Floor violators:
            _offer("ssd", "ssd:test:sata_240", "Test", "SATA 240GB", 1800, {"capacity_gb": 240, "interface": "SATA", "form_factor": "2.5"}),
            _offer("ssd", "ssd:test:nvme_256", "Test", "NVMe 256GB", 2200, {"capacity_gb": 256, "interface": "NVMe", "form_factor": "M.2"}),
            # Floor-passing:
            _offer("ssd", "ssd:test:nvme_500", "Test", "NVMe 500GB", 5000, {"capacity_gb": 500, "interface": "NVMe", "form_factor": "M.2"}),
            _offer("ssd", "ssd:test:nvme_1000", "Test", "NVMe 1TB", 7500, {"capacity_gb": 1000, "interface": "NVMe", "form_factor": "M.2"}),
            _offer("ssd", "ssd:test:nvme_2000", "Test", "NVMe 2TB", 11000, {"capacity_gb": 2000, "interface": "NVMe", "form_factor": "M.2"}),
            # Confirmed DRAM-less (explicit 0, unlike a real listing where
            # the key is simply absent when unknown — see normalize.py).
            _offer("ssd", "ssd:test:nvme_dramless", "Test", "NVMe DRAM-less 1TB", 6500, {"capacity_gb": 1000, "interface": "NVMe", "form_factor": "M.2", "dram_cache_mb": 0}),
            # Cheap, floor-passing, but from a brand a whitelist test can exclude.
            _offer("ssd", "ssd:test:offbrand_cheap", "OffBrand", "Cheap NVMe 1TB", 6000, {"capacity_gb": 1000, "interface": "NVMe", "form_factor": "M.2"}),
        ],
        "psu": [
            # Floor violator (no 80 PLUS certification):
            _offer("psu", "psu:test:nocert_600", "Test", "No-cert 600W", 2500, {"wattage_w": 600, "certification": None}),
            # Passes the boolean "has some 80 PLUS cert" floor but not a
            # "Bronze or above" brand-rules requirement — base/white tier.
            _offer("psu", "psu:test:white_620", "Test", "White 620W", 3500, {"wattage_w": 620, "certification": "80 PLUS"}),
            # Floor-passing, generously above any headroom requirement in
            # this test's tdp range so PSU headroom isn't the bottleneck
            # being tested here (that's covered in test_compatibility.py):
            _offer("psu", "psu:test:bronze_650", "Test", "Bronze 650W", 4000, {"wattage_w": 650, "certification": "80 PLUS Bronze"}),
            _offer("psu", "psu:test:gold_750", "Test", "Gold 750W", 5500, {"wattage_w": 750, "certification": "80 PLUS Gold"}),
            _offer("psu", "psu:test:gold_850", "Test", "Gold 850W", 7000, {"wattage_w": 850, "certification": "80 PLUS Gold"}),
            # Cheap, well-certified, but from a brand a whitelist test can
            # exclude to confirm the whitelist actually bites.
            _offer("psu", "psu:test:offbrand_cheap", "OffBrand", "Cheap Bronze 700W", 3000, {"wattage_w": 700, "certification": "80 PLUS Bronze"}),
        ],
        "case": [
            _offer("case", "case:test:basic", "Test", "Basic Case", 3000, {"supported_form_factors": ["ATX", "MATX"], "max_gpu_length_mm": 400, "max_cooler_height_mm": 170}),
            _offer("case", "case:test:mid", "Test", "Mid Case", 4500, {"supported_form_factors": ["ATX", "MATX"], "max_gpu_length_mm": 400, "max_cooler_height_mm": 170}),
            _offer("case", "case:test:premium", "Test", "Premium Case", 6000, {"supported_form_factors": ["ATX", "MATX"], "max_gpu_length_mm": 400, "max_cooler_height_mm": 170}),
        ],
        "cooler": [
            _offer("cooler", "cooler:test:basic", "Test", "Basic Cooler", 2000, {"sockets": ["AM5"], "tdp_w": 150, "height_mm": 150}),
            _offer("cooler", "cooler:test:mid", "Test", "Mid Cooler", 3000, {"sockets": ["AM5"], "tdp_w": 200, "height_mm": 155}),
            _offer("cooler", "cooler:test:premium", "Test", "Premium Cooler", 4500, {"sockets": ["AM5"], "tdp_w": 250, "height_mm": 160}),
        ],
    }
