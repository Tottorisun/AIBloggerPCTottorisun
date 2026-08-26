import pytest

from pc_price_tracker.compat import (
    check_compatibility,
    estimate_gpu_tdp_w,
    estimate_system_power_w,
    load_compatibility_config,
    socket_matches,
)


@pytest.fixture
def config():
    return load_compatibility_config()


def _part(category: str, **specs) -> dict:
    return {"category": category, "brand": "Test", "model": "Test", "price": 1000, "specs": specs}


# ---------------------------------------------------------------------------
# socket_matches: exact + the LGA115X wildcard family coolers advertise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("AM5", "AM5", True),
        ("AM4", "AM5", False),
        ("LGA1700", "LGA1700", True),
        ("LGA1700", "LGA1851", False),
        ("LGA115X", "LGA1151", True),  # cooler's generic family rating
        ("LGA115X", "LGA1155", True),
        ("LGA1151", "LGA115X", True),  # order shouldn't matter
        ("AM5", "LGA1700", False),  # different length, never matches
        (None, "AM5", False),
    ],
)
def test_socket_matches(a, b, expected):
    assert socket_matches(a, b) is expected


# ---------------------------------------------------------------------------
# CPU <-> motherboard socket check
# ---------------------------------------------------------------------------


def test_matching_cpu_and_motherboard_socket_is_compatible(config):
    parts = {
        "cpu": _part("cpu", socket="AM5", tdp_w=120),
        "motherboard": _part("motherboard", socket="AM5", form_factor="ATX", ram_type="DDR5"),
    }
    assert check_compatibility(parts, config) == []


def test_mismatched_cpu_and_motherboard_socket_is_flagged(config):
    parts = {
        "cpu": _part("cpu", socket="AM5", tdp_w=120),
        "motherboard": _part("motherboard", socket="LGA1700", form_factor="ATX", ram_type="DDR5"),
    }
    issues = check_compatibility(parts, config)
    assert any("сокет" in issue for issue in issues)


# ---------------------------------------------------------------------------
# RAM type <-> motherboard
# ---------------------------------------------------------------------------


def test_ram_type_mismatch_is_flagged(config):
    parts = {
        "motherboard": _part("motherboard", socket="AM5", form_factor="ATX", ram_type="DDR5"),
        "ram": _part("ram", ram_type="DDR4", capacity_gb=16),
    }
    issues = check_compatibility(parts, config)
    assert any("DDR4" in issue and "DDR5" in issue for issue in issues)


def test_ram_type_match_is_compatible(config):
    parts = {
        "motherboard": _part("motherboard", socket="AM5", form_factor="ATX", ram_type="DDR5"),
        "ram": _part("ram", ram_type="DDR5", capacity_gb=16),
    }
    assert check_compatibility(parts, config) == []


# ---------------------------------------------------------------------------
# Cooler <-> CPU socket + TDP headroom
# ---------------------------------------------------------------------------


def test_cooler_unsupported_socket_is_flagged(config):
    parts = {
        "cpu": _part("cpu", socket="AM5", tdp_w=120),
        "cooler": _part("cooler", sockets=["LGA1700", "LGA1851"], tdp_w=200, height_mm=150),
    }
    issues = check_compatibility(parts, config)
    assert any("кулер не поддерживает сокет" in issue for issue in issues)


def test_cooler_insufficient_tdp_is_flagged(config):
    parts = {
        "cpu": _part("cpu", socket="AM5", tdp_w=220),
        "cooler": _part("cooler", sockets=["AM5"], tdp_w=150, height_mm=150),
    }
    issues = check_compatibility(parts, config)
    assert any("TDP" in issue for issue in issues)


# ---------------------------------------------------------------------------
# Case <-> motherboard form factor, GPU length, cooler height
# ---------------------------------------------------------------------------


def test_case_gpu_too_long_is_flagged(config):
    parts = {
        "case": _part("case", supported_form_factors=["ATX"], max_gpu_length_mm=300, max_cooler_height_mm=160),
        "gpu": _part("gpu", memory_gb=16, length_mm=332.0),
    }
    issues = check_compatibility(parts, config)
    assert any("видеокарта длиннее" in issue for issue in issues)


def test_case_gpu_fits_is_compatible(config):
    parts = {
        "case": _part("case", supported_form_factors=["ATX"], max_gpu_length_mm=400, max_cooler_height_mm=160),
        "gpu": _part("gpu", memory_gb=16, length_mm=332.0),
    }
    assert check_compatibility(parts, config) == []


def test_case_form_factor_mismatch_is_flagged(config):
    parts = {
        "case": _part("case", supported_form_factors=["MINI-ITX"], max_gpu_length_mm=300, max_cooler_height_mm=160),
        "motherboard": _part("motherboard", socket="AM5", form_factor="ATX", ram_type="DDR5"),
    }
    issues = check_compatibility(parts, config)
    assert any("форм-фактор" in issue for issue in issues)


def test_case_cooler_too_tall_is_flagged(config):
    parts = {
        "case": _part("case", supported_form_factors=["ATX"], max_gpu_length_mm=400, max_cooler_height_mm=140),
        "cooler": _part("cooler", sockets=["AM5"], tdp_w=200, height_mm=165),
    }
    issues = check_compatibility(parts, config)
    assert any("кулер выше" in issue for issue in issues)


# ---------------------------------------------------------------------------
# PSU headroom (30% over estimated draw)
# ---------------------------------------------------------------------------


def test_gpu_tdp_estimate_by_memory_tier(config):
    assert estimate_gpu_tdp_w(6, config) == 130
    assert estimate_gpu_tdp_w(8, config) == 200
    assert estimate_gpu_tdp_w(12, config) == 260
    assert estimate_gpu_tdp_w(16, config) == 320
    assert estimate_gpu_tdp_w(24, config) == 400
    assert estimate_gpu_tdp_w(None, config) == 0


def test_estimate_system_power_sums_cpu_gpu_and_overhead(config):
    parts = {
        "cpu": _part("cpu", socket="AM5", tdp_w=120),
        "gpu": _part("gpu", memory_gb=16, length_mm=300.0),
    }
    power = estimate_system_power_w(parts, config)
    # 60 (baseline) + 120 (cpu) + 320 (16GB tier)
    assert power == 60 + 120 + 320


def test_psu_below_headroom_requirement_is_flagged(config):
    parts = {
        "cpu": _part("cpu", socket="AM5", tdp_w=120),
        "gpu": _part("gpu", memory_gb=16, length_mm=300.0),
        "psu": _part("psu", wattage_w=500, certification="80 PLUS Bronze"),
    }
    # required = (60+120+320) * 1.3 = 650
    issues = check_compatibility(parts, config)
    assert any("БП" in issue for issue in issues)


def test_psu_meeting_headroom_requirement_is_compatible(config):
    parts = {
        "cpu": _part("cpu", socket="AM5", tdp_w=120),
        "gpu": _part("gpu", memory_gb=16, length_mm=300.0),
        "psu": _part("psu", wattage_w=750, certification="80 PLUS Gold"),
    }
    assert check_compatibility(parts, config) == []


def test_fully_compatible_build_has_no_issues(config):
    parts = {
        "cpu": _part("cpu", socket="AM5", tdp_w=120),
        "motherboard": _part("motherboard", socket="AM5", form_factor="ATX", ram_type="DDR5"),
        "ram": _part("ram", ram_type="DDR5", capacity_gb=32),
        "gpu": _part("gpu", memory_gb=12, length_mm=280.0),
        "cooler": _part("cooler", sockets=["AM4", "AM5"], tdp_w=180, height_mm=155),
        "case": _part("case", supported_form_factors=["ATX", "MATX"], max_gpu_length_mm=350, max_cooler_height_mm=165),
        "psu": _part("psu", wattage_w=650, certification="80 PLUS Gold"),
    }
    assert check_compatibility(parts, config) == []
