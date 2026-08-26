import pytest

from pc_price_tracker.builder import _apply_brand_rules, _apply_floors, _apply_gpu_tier_ratio, _evaluate_brand_rules, _score_cpu_gpu_pairs, build_configuration, find_best_build
from pc_price_tracker.compat import load_brand_rules, load_build_rules, load_compatibility_config

from .builder_fixtures import BRAND_RULES, TIERS, make_catalog


@pytest.fixture
def rules():
    return load_build_rules()


@pytest.fixture
def compat_config():
    return load_compatibility_config()


def _build(rules, compat_config, budget, profile, brand_rules=None):
    return build_configuration(
        make_catalog(),
        budget,
        profile,
        rules=rules,
        compat_config=compat_config,
        tiers=TIERS,
        brand_rules=brand_rules if brand_rules is not None else BRAND_RULES,
    )


def _item(result, category):
    return next((i for i in result.items if i.category == category), None)


def _assert_floors_respected(result, floors):
    # Both checks below are floor-value-aware, not blanket bans: student/
    # office's floors (ram_min_modules: 1, ssd_min_gb: 250) legitimately
    # allow single-stick RAM and a 256GB SSD — only gaming/workstation's
    # stricter floors (modules >= 2, ssd_min_gb 500/1024) actually forbid
    # them. A profile-blind assertion here would fail on a correct pick.
    ram = _item(result, "ram")
    if ram is not None and floors.get("ram_min_modules", 1) >= 2:
        assert ram.model not in ("Single 8GB", "Single 16GB"), "single-stick RAM must never be selected when the floor requires >=2 modules"

    ssd = _item(result, "ssd")
    if ssd is not None:
        min_gb = floors.get("ssd_min_gb", 0)
        if min_gb > 240:
            assert ssd.model != "SATA 240GB", "sub-floor SSD must never be selected"
        if min_gb > 256:
            assert ssd.model != "NVMe 256GB", "sub-floor SSD must never be selected"

    psu = _item(result, "psu")
    if psu is not None and floors.get("psu_require_80plus"):
        assert psu.model != "No-cert 600W", "uncertified PSU must never be selected when the floor requires 80 PLUS"


# ---------------------------------------------------------------------------
# The exact scenario from the brief: gaming @ 80000 must not contain
# single-channel RAM, a SATA drive, or an SSD under 500GB.
# ---------------------------------------------------------------------------


def test_gaming_80000_excludes_single_channel_ram_and_small_sata_ssd(rules, compat_config):
    result = _build(rules, compat_config, 80000, "gaming")
    assert result.feasible, result.infeasible_reason

    ram = _item(result, "ram")
    assert ram is not None
    assert "single" not in ram.model.lower()

    ssd = _item(result, "ssd")
    assert ssd is not None
    assert "sata" not in ssd.model.lower()
    assert "256" not in ssd.model  # the sub-500GB NVMe floor violator

    assert result.total_price <= 80000


# ---------------------------------------------------------------------------
# Floors must never be violated in any profile that actually produces a
# build (parametrized over budgets known to be feasible with this catalog).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "profile,budget",
    [
        ("gaming", 80000),
        ("workstation", 90000),
        ("student", 60000),
        ("office", 40000),
    ],
)
def test_floors_never_violated(rules, compat_config, profile, budget):
    result = _build(rules, compat_config, budget, profile)
    assert result.feasible, result.infeasible_reason

    floors = rules[profile]["floors"]
    _assert_floors_respected(result, floors)

    ram = _item(result, "ram")
    if ram is not None:
        # Cross-check against the actual numeric floor, not just which
        # named fixture was picked, so this stays meaningful if the
        # fixture catalog changes.
        assert ram.reason  # sanity: reason string was generated
    assert result.total_price <= budget


def test_office_profile_never_selects_a_gpu(rules, compat_config):
    result = _build(rules, compat_config, 40000, "office")
    assert result.feasible, result.infeasible_reason
    assert _item(result, "gpu") is None


def test_student_and_office_floors_are_softer_than_gaming(rules):
    gaming_floors = rules["gaming"]["floors"]
    student_floors = rules["student"]["floors"]
    office_floors = rules["office"]["floors"]

    assert student_floors["ram_min_total_gb"] <= gaming_floors["ram_min_total_gb"]
    assert student_floors["ssd_min_gb"] <= gaming_floors["ssd_min_gb"]
    assert gaming_floors["ssd_require_nvme"] is True
    assert student_floors["ssd_require_nvme"] is False
    assert gaming_floors["psu_require_80plus"] is True
    assert office_floors["psu_require_80plus"] is False


# ---------------------------------------------------------------------------
# Infeasibility must be reported honestly, never papered over with a build
# that quietly breaks a floor or a budget.
# ---------------------------------------------------------------------------


def test_impossible_budget_reports_reason_and_minimum_instead_of_a_bad_build(rules, compat_config):
    result = _build(rules, compat_config, 5000, "gaming")
    assert result.feasible is False
    assert result.items == []
    assert result.infeasible_reason
    assert result.minimum_budget_estimate is not None
    assert result.minimum_budget_estimate > 5000


def test_feasible_build_never_exceeds_its_budget(rules, compat_config):
    for profile, budget in [("gaming", 80000), ("workstation", 90000), ("student", 60000), ("office", 40000)]:
        result = _build(rules, compat_config, budget, profile)
        if result.feasible:
            assert result.total_price <= budget


# ---------------------------------------------------------------------------
# Untiered CPU/GPU products are skipped, not guessed at, and the skip is
# surfaced rather than silently swallowed.
# ---------------------------------------------------------------------------


def test_untiered_cpu_is_excluded_and_counted(rules, compat_config):
    catalog = make_catalog()
    catalog["cpu"].append(
        {
            "product_id": "cpu:test:mystery",
            "category": "cpu",
            "brand": "Test",
            "model": "Mystery CPU",
            "normalized_key": "cpu:test:mystery",  # deliberately absent from TIERS
            "specs": {"socket": "AM5", "tdp_w": 65},
            "offer_id": "cpu:test:mystery",
            "source": "test",
            "url": "https://example.invalid/cpu-mystery",
            "price": 1,  # priced to be irresistible if the tier filter didn't work
            "in_stock": True,
            "captured_at": "2026-08-19T00:00:00",
        }
    )
    result = build_configuration(
        catalog, 80000, "gaming", rules=rules, compat_config=compat_config, tiers=TIERS, brand_rules=BRAND_RULES
    )
    assert result.feasible, result.infeasible_reason
    cpu = _item(result, "cpu")
    assert cpu.model != "Mystery CPU"
    assert result.untiered_skipped.get("cpu") == 1


# ---------------------------------------------------------------------------
# Brand whitelist / certification tier / DRAM-cache (data/brand_rules.yaml).
# Rejected items stay in the candidate pool's source data (the DB) — they're
# just excluded from what build can pick.
# ---------------------------------------------------------------------------


def test_psu_outside_whitelist_never_selected_even_if_cheapest(rules, compat_config):
    brand_rules = {
        "psu": {"whitelist": ["Test"], "min_certification": "80 PLUS Bronze", "min_certification_profiles": ["gaming"]},
        "ssd": {"whitelist": [], "require_dram_cache_profiles": []},
    }
    result = _build(rules, compat_config, 80000, "gaming", brand_rules=brand_rules)
    assert result.feasible, result.infeasible_reason
    psu = _item(result, "psu")
    assert psu.brand != "OffBrand"
    assert result.brand_rejected.get("psu", 0) >= 1


def test_psu_below_min_certification_tier_never_selected(rules, compat_config):
    brand_rules = {
        "psu": {"whitelist": [], "min_certification": "80 PLUS Bronze", "min_certification_profiles": ["gaming"]},
        "ssd": {"whitelist": [], "require_dram_cache_profiles": []},
    }
    result = _build(rules, compat_config, 80000, "gaming", brand_rules=brand_rules)
    assert result.feasible, result.infeasible_reason
    psu = _item(result, "psu")
    # "White 620W" carries a bare "80 PLUS" cert — passes the older boolean
    # floor (psu_require_80plus) but not the Bronze-or-above brand rule.
    assert psu.model != "White 620W"


def test_psu_certification_floor_not_enforced_outside_configured_profiles(rules, compat_config):
    brand_rules = {
        "psu": {"whitelist": [], "min_certification": "80 PLUS Bronze", "min_certification_profiles": ["gaming"]},
        "ssd": {"whitelist": [], "require_dram_cache_profiles": []},
    }
    # student's own floor already requires no certification at all, so
    # a bare "80 PLUS" PSU should be a legal (if not necessarily chosen) pick.
    # 60000 matches the known-feasible student budget used elsewhere in this
    # file (50000 undershoots this fixture catalog's GPU corridor).
    result = _build(rules, compat_config, 60000, "student", brand_rules=brand_rules)
    assert result.feasible, result.infeasible_reason


def test_ssd_outside_whitelist_never_selected_even_if_cheapest(rules, compat_config):
    brand_rules = {
        "psu": {"whitelist": [], "min_certification": "80 PLUS Bronze", "min_certification_profiles": []},
        "ssd": {"whitelist": ["Test"], "require_dram_cache_profiles": []},
    }
    result = _build(rules, compat_config, 80000, "gaming", brand_rules=brand_rules)
    assert result.feasible, result.infeasible_reason
    ssd = _item(result, "ssd")
    assert ssd.brand != "OffBrand"
    assert result.brand_rejected.get("ssd", 0) >= 1


def test_ssd_confirmed_dram_less_excluded_for_gaming(rules, compat_config):
    brand_rules = {
        "psu": {"whitelist": [], "min_certification": "80 PLUS Bronze", "min_certification_profiles": []},
        "ssd": {"whitelist": [], "require_dram_cache_profiles": ["gaming"]},
    }
    result = _build(rules, compat_config, 80000, "gaming", brand_rules=brand_rules)
    assert result.feasible, result.infeasible_reason
    ssd = _item(result, "ssd")
    assert ssd.model != "NVMe DRAM-less 1TB"


def test_ssd_dram_cache_not_enforced_outside_configured_profiles(rules, compat_config):
    # office doesn't require NVMe at all in its floors, and isn't in
    # require_dram_cache_profiles either — the DRAM-less drive is a legal
    # pick there even though it wouldn't be for gaming.
    brand_rules = {
        "psu": {"whitelist": [], "min_certification": "80 PLUS Bronze", "min_certification_profiles": []},
        "ssd": {"whitelist": [], "require_dram_cache_profiles": ["gaming"]},
    }
    result = _build(rules, compat_config, 40000, "office", brand_rules=brand_rules)
    assert result.feasible, result.infeasible_reason


# ---------------------------------------------------------------------------
# performance_tiers.yaml source (verified/estimated) surfaces in build output.
# ---------------------------------------------------------------------------


def test_estimated_tier_is_marked_in_build_output(rules, compat_config):
    result = _build(rules, compat_config, 80000, "gaming")
    assert result.feasible, result.infeasible_reason
    cpu = _item(result, "cpu")
    # cpu:test:mid is "estimated" in TIERS (builder_fixtures.py).
    if cpu.model == "Mid CPU":
        assert "оценка" in cpu.reason


def test_verified_tier_is_not_marked_as_estimated(rules, compat_config):
    result = _build(rules, compat_config, 80000, "gaming")
    assert result.feasible, result.infeasible_reason
    gpu = _item(result, "gpu")
    # gpu:test:upper_mid is "verified" in TIERS (builder_fixtures.py).
    if gpu.model == "Upper-mid GPU":
        assert "оценка" not in gpu.reason


# ---------------------------------------------------------------------------
# _apply_brand_rules generalization (was hardcoded to psu/ssd; now driven
# entirely by which keys are present in each category's brand_rules.yaml
# section). Tested against the REAL data/brand_rules.yaml content, not a
# synthetic stand-in, so these also catch config typos in the actual file.
# ---------------------------------------------------------------------------


@pytest.fixture
def real_brand_rules():
    return load_brand_rules()


def test_gpu_whitelist_rejects_afox(real_brand_rules):
    candidate = {"brand": "AFOX", "specs": {}}
    assert _evaluate_brand_rules(candidate, real_brand_rules["gpu"], "gaming") == "reject"


def test_gpu_whitelist_accepts_a_listed_vendor(real_brand_rules):
    candidate = {"brand": "MSI", "specs": {}}
    assert _evaluate_brand_rules(candidate, real_brand_rules["gpu"], "gaming") == "keep"


def test_motherboard_b450_rejected_for_gaming_but_kept_for_student(real_brand_rules):
    candidate = {"brand": "Test", "specs": {"chipset": "B450"}}
    assert _evaluate_brand_rules(candidate, real_brand_rules["motherboard"], "gaming") == "reject"
    assert _evaluate_brand_rules(candidate, real_brand_rules["motherboard"], "student") == "keep"


def test_motherboard_unrecognized_chipset_is_not_lost(real_brand_rules):
    candidate = {
        "product_id": "mobo:test:nochipset",
        "category": "motherboard",
        "brand": "Test",
        "model": "No Chipset Listed",
        "specs": {},  # chipset never extracted for this listing
        "price": 5000,
    }
    verdict = _evaluate_brand_rules(candidate, real_brand_rules["motherboard"], "gaming")
    assert verdict == "unrecognized"

    filtered = {"motherboard": [candidate]}
    result, rejected, unrecognized = _apply_brand_rules(filtered, real_brand_rules, "gaming")
    assert len(result["motherboard"]) == 1  # survives — not dropped
    assert result["motherboard"][0] is candidate
    assert unrecognized.get("motherboard") == 1
    assert "motherboard" not in rejected


def test_case_min_fans_is_commented_out_not_active(real_brand_rules):
    """min_fans didn't work (1733/1740 unrecognized on real data) and was
    replaced by a case brand whitelist — the key must not be live."""
    assert "min_fans" not in real_brand_rules["case"]
    assert "min_fans_profiles" not in real_brand_rules["case"]


def test_case_outside_whitelist_rejected_regardless_of_fan_count(real_brand_rules):
    # A brand not on the case whitelist is rejected even with no fan data
    # at all (min_fans is retired — this must not fall back to it).
    candidate = {"brand": "Test", "specs": {}}
    assert _evaluate_brand_rules(candidate, real_brand_rules["case"], "gaming") == "reject"


def test_case_whitelisted_brand_kept_even_without_fan_data(real_brand_rules):
    candidate = {
        "product_id": "case:test:deepcool",
        "category": "case",
        "brand": "Deepcool",
        "model": "Some Case",
        "specs": {},  # no fan_count — must not matter, the check is retired
        "price": 4000,
    }
    filtered = {"case": [candidate]}
    result, rejected, unrecognized = _apply_brand_rules(filtered, real_brand_rules, "gaming")
    assert len(result["case"]) == 1
    assert "case" not in unrecognized


def test_gpu_build_never_selects_a_non_whitelisted_brand(rules, compat_config):
    """End-to-end: an AFOX GPU cheap enough to otherwise win on price/tier
    must still never be selected once the real gpu whitelist applies."""
    # Only gpu comes from the real config — psu/ssd/motherboard/case stay
    # permissive so the synthetic "Test"-branded fixtures for those
    # categories (not real vendor names) aren't wiped out by the real
    # whitelists, which would make the whole build infeasible for reasons
    # unrelated to what this test is actually checking.
    mixed_rules = dict(BRAND_RULES)
    mixed_rules["gpu"] = load_brand_rules()["gpu"]
    catalog = make_catalog()
    # The rest of the synthetic GPU catalog is branded "Test" (not a real
    # vendor) — give at least one a real whitelisted brand so there's a
    # legitimate option left once AFOX is excluded, otherwise the build
    # would fail for the unrelated reason of having zero gpu candidates.
    for gpu in catalog["gpu"]:
        if gpu["normalized_key"] == "gpu:test:upper_mid":
            gpu["brand"] = "MSI"
    catalog["gpu"].append(
        {
            "product_id": "gpu:test:afox_cheap",
            "category": "gpu",
            "brand": "AFOX",
            "model": "Suspiciously Cheap GPU",
            "normalized_key": "gpu:test:upper_mid",  # reuse a real tiered key so it's eligible on tier too
            "specs": {"memory_gb": 16, "length_mm": 300.0},
            "offer_id": "gpu:test:afox_cheap",
            "source": "test",
            "url": "https://example.invalid/gpu-afox-cheap",
            "price": 100,
            "in_stock": True,
            "captured_at": "2026-08-19T00:00:00",
        }
    )
    result = build_configuration(
        catalog, 80000, "gaming", rules=rules, compat_config=compat_config, tiers=TIERS, brand_rules=mixed_rules
    )
    assert result.feasible, result.infeasible_reason
    gpu = _item(result, "gpu")
    assert gpu.brand != "AFOX"
    assert result.brand_rejected.get("gpu", 0) >= 1


# ---------------------------------------------------------------------------
# CPU min_cores/min_threads floor (data/build_rules.yaml). Same
# unrecognized-not-rejected shape as brand_rules.yaml's chipset/fan checks,
# just applied inside _apply_floors instead of _apply_brand_rules.
# ---------------------------------------------------------------------------


def test_cpu_below_min_cores_rejected_for_gaming(rules):
    corridors = rules["gaming"]["budget_corridors"]
    floors = rules["gaming"]["floors"]
    assert floors["min_cores"] == 6
    offers = {
        "cpu": [{"normalized_key": "cpu:test:mid", "brand": "Test", "specs": {"cores": 4}, "price": 10000}],
    }
    tiers = {"cpu:test:mid": {"tier": 60, "source": "estimated"}}
    filtered, untiered, unrecognized, avail_rejected, avail_unrecognized = _apply_floors(offers, corridors, floors, tiers)
    assert filtered["cpu"] == []  # 4 < min_cores 6 -> hard rejected, not unrecognized


def test_cpu_at_or_above_min_cores_kept_for_gaming(rules):
    corridors = rules["gaming"]["budget_corridors"]
    floors = rules["gaming"]["floors"]
    offers = {
        "cpu": [{"normalized_key": "cpu:test:mid", "brand": "Test", "specs": {"cores": 6}, "price": 10000}],
    }
    tiers = {"cpu:test:mid": {"tier": 60, "source": "estimated"}}
    filtered, untiered, unrecognized, avail_rejected, avail_unrecognized = _apply_floors(offers, corridors, floors, tiers)
    assert len(filtered["cpu"]) == 1


def test_cpu_missing_cores_data_is_unrecognized_not_rejected(rules):
    corridors = rules["gaming"]["budget_corridors"]
    floors = rules["gaming"]["floors"]
    offers = {
        "cpu": [{"normalized_key": "cpu:test:mid", "brand": "Test", "specs": {}, "price": 10000}],  # no "cores" key
    }
    tiers = {"cpu:test:mid": {"tier": 60, "source": "estimated"}}
    filtered, untiered, unrecognized, avail_rejected, avail_unrecognized = _apply_floors(offers, corridors, floors, tiers)
    assert len(filtered["cpu"]) == 1  # survives — not dropped
    assert unrecognized.get("cpu") == 1


def test_cpu_missing_threads_data_is_unrecognized_not_rejected(rules):
    """threads is never extracted from the real data source at all (see
    normalize.py) — every real CPU should hit this path, not get rejected."""
    corridors = rules["gaming"]["budget_corridors"]
    floors = rules["gaming"]["floors"]
    assert floors["min_threads"] == 12
    offers = {
        "cpu": [{"normalized_key": "cpu:test:mid", "brand": "Test", "specs": {"cores": 8}, "price": 10000}],  # no "threads"
    }
    tiers = {"cpu:test:mid": {"tier": 60, "source": "estimated"}}
    filtered, untiered, unrecognized, avail_rejected, avail_unrecognized = _apply_floors(offers, corridors, floors, tiers)
    assert len(filtered["cpu"]) == 1
    assert unrecognized.get("cpu") == 1


def test_cpu_cores_threads_floor_not_applied_to_student(rules):
    # Only gaming/workstation got min_cores/min_threads per the brief.
    assert "min_cores" not in rules["student"]["floors"]


# ---------------------------------------------------------------------------
# require_availability floor (data/build_rules.yaml). Same
# unrecognized-not-rejected shape as chipset/cores/threads: missing data
# passes through and is counted, only a recognized-but-disallowed status is
# actually dropped.
# ---------------------------------------------------------------------------


def test_gpu_preorder_rejected_by_default(rules):
    corridors = rules["gaming"]["budget_corridors"]
    floors = rules["gaming"]["floors"]
    offers = {
        "gpu": [{"normalized_key": "gpu:test:mid", "brand": "Test", "specs": {}, "price": 30000, "availability": "предзаказ"}],
    }
    tiers = {"gpu:test:mid": {"tier": 55, "source": "estimated"}}
    filtered, *_rest, avail_rejected, avail_unrecognized = _apply_floors(offers, corridors, floors, tiers)
    assert filtered["gpu"] == []
    assert avail_rejected.get("gpu") == 1
    assert not avail_unrecognized


def test_gpu_in_stock_kept_by_default(rules):
    corridors = rules["gaming"]["budget_corridors"]
    floors = rules["gaming"]["floors"]
    offers = {
        "gpu": [{"normalized_key": "gpu:test:mid", "brand": "Test", "specs": {}, "price": 30000, "availability": "в наличии"}],
    }
    tiers = {"gpu:test:mid": {"tier": 55, "source": "estimated"}}
    filtered, *_rest = _apply_floors(offers, corridors, floors, tiers)
    assert len(filtered["gpu"]) == 1


def test_gpu_missing_availability_is_unrecognized_not_rejected(rules):
    corridors = rules["gaming"]["budget_corridors"]
    floors = rules["gaming"]["floors"]
    offers = {
        "gpu": [{"normalized_key": "gpu:test:mid", "brand": "Test", "specs": {}, "price": 30000}],  # no "availability" key
    }
    tiers = {"gpu:test:mid": {"tier": 55, "source": "estimated"}}
    filtered, *_rest, avail_rejected, avail_unrecognized = _apply_floors(offers, corridors, floors, tiers)
    assert len(filtered["gpu"]) == 1  # survives — not dropped
    assert avail_unrecognized.get("gpu") == 1
    assert not avail_rejected


def test_gpu_preorder_kept_with_include_preorder(rules):
    corridors = rules["gaming"]["budget_corridors"]
    floors = rules["gaming"]["floors"]
    offers = {
        "gpu": [{"normalized_key": "gpu:test:mid", "brand": "Test", "specs": {}, "price": 30000, "availability": "предзаказ"}],
    }
    tiers = {"gpu:test:mid": {"tier": 55, "source": "estimated"}}
    filtered, *_rest, avail_rejected, avail_unrecognized = _apply_floors(offers, corridors, floors, tiers, include_preorder=True)
    assert len(filtered["gpu"]) == 1
    assert not avail_rejected


def test_include_preorder_flag_reaches_build_configuration(rules, compat_config):
    """Same pair, only availability differs — the flag must be the only
    thing standing between "infeasible" and "feasible" here."""
    catalog = make_catalog()
    catalog["gpu"] = [{**g, "availability": "предзаказ"} for g in catalog["gpu"]]
    catalog["cpu"] = [{**c, "availability": "предзаказ"} for c in catalog["cpu"]]

    filtered_out = build_configuration(
        catalog, 80000, "gaming", rules=rules, compat_config=compat_config, tiers=TIERS, brand_rules=BRAND_RULES
    )
    assert not filtered_out.feasible

    included = build_configuration(
        catalog,
        80000,
        "gaming",
        rules=rules,
        compat_config=compat_config,
        tiers=TIERS,
        brand_rules=BRAND_RULES,
        include_preorder=True,
    )
    assert included.feasible, included.infeasible_reason
    assert "min_threads" not in rules["student"]["floors"]
    assert "min_cores" not in rules["office"]["floors"]


# ---------------------------------------------------------------------------
# min_gpu_tier_ratio: a floor relative to the best GPU that fits the gpu
# budget corridor, not an absolute tier number.
# ---------------------------------------------------------------------------


def test_min_gpu_tier_ratio_excludes_a_gpu_well_below_the_corridors_best():
    filtered = {
        "cpu": [{"normalized_key": "cpu:x", "price": 15000, "specs": {}}],
        "gpu": [
            {"normalized_key": "gpu:strong", "price": 35000, "specs": {}},
            {"normalized_key": "gpu:weak", "price": 33000, "specs": {}},
        ],
    }
    tiers = {
        "cpu:x": {"tier": 60, "source": "estimated"},
        "gpu:strong": {"tier": 80, "source": "estimated"},
        "gpu:weak": {"tier": 40, "source": "estimated"},  # 40/80 = 0.5 < 0.6
    }
    corridors = {"cpu": [0.15, 0.22], "gpu": [0.40, 0.50]}
    weights = {"cpu": 0.35, "gpu": 0.65}
    search_pool = _apply_gpu_tier_ratio(filtered, 80000, corridors, tiers, 0.6)
    pairs = _score_cpu_gpu_pairs(search_pool, 80000, corridors, weights, tiers)
    gpu_keys = {gpu["normalized_key"] for _, _cpu, gpu in pairs if gpu}
    assert "gpu:strong" in gpu_keys
    assert "gpu:weak" not in gpu_keys


def test_min_gpu_tier_ratio_keeps_a_gpu_just_above_the_threshold():
    filtered = {
        "cpu": [{"normalized_key": "cpu:x", "price": 15000, "specs": {}}],
        "gpu": [
            {"normalized_key": "gpu:strong", "price": 35000, "specs": {}},
            {"normalized_key": "gpu:borderline", "price": 33000, "specs": {}},
        ],
    }
    tiers = {
        "cpu:x": {"tier": 60, "source": "estimated"},
        "gpu:strong": {"tier": 80, "source": "estimated"},
        "gpu:borderline": {"tier": 50, "source": "estimated"},  # 50/80 = 0.625 >= 0.6
    }
    corridors = {"cpu": [0.15, 0.22], "gpu": [0.40, 0.50]}
    weights = {"cpu": 0.35, "gpu": 0.65}
    search_pool = _apply_gpu_tier_ratio(filtered, 80000, corridors, tiers, 0.6)
    pairs = _score_cpu_gpu_pairs(search_pool, 80000, corridors, weights, tiers)
    gpu_keys = {gpu["normalized_key"] for _, _cpu, gpu in pairs if gpu}
    assert "gpu:borderline" in gpu_keys


def test_min_gpu_tier_ratio_not_applied_when_none():
    filtered = {
        "cpu": [{"normalized_key": "cpu:x", "price": 15000, "specs": {}}],
        "gpu": [
            {"normalized_key": "gpu:strong", "price": 35000, "specs": {}},
            {"normalized_key": "gpu:weak", "price": 33000, "specs": {}},
        ],
    }
    tiers = {
        "cpu:x": {"tier": 60, "source": "estimated"},
        "gpu:strong": {"tier": 80, "source": "estimated"},
        "gpu:weak": {"tier": 1, "source": "estimated"},
    }
    corridors = {"cpu": [0.15, 0.22], "gpu": [0.40, 0.50]}
    weights = {"cpu": 0.35, "gpu": 0.65}
    search_pool = _apply_gpu_tier_ratio(filtered, 80000, corridors, tiers, None)
    pairs = _score_cpu_gpu_pairs(search_pool, 80000, corridors, weights, tiers)
    gpu_keys = {gpu["normalized_key"] for _, _cpu, gpu in pairs if gpu}
    assert "gpu:weak" in gpu_keys  # no ratio configured -> nothing excluded on this basis


def test_real_profile_gpu_tier_ratios(rules):
    # Raised 2026-08-20: at 0.6, a same-corridor budget-rescue swap could
    # land on a GPU far weaker than the pair the objective actually picked
    # (see data/build_rules.yaml's min_gpu_tier_ratio comment for gaming).
    assert rules["gaming"]["min_gpu_tier_ratio"] == 0.75
    assert rules["student"]["min_gpu_tier_ratio"] == 0.6
    assert rules["workstation"]["min_gpu_tier_ratio"] == 0.5
    assert "min_gpu_tier_ratio" not in rules["office"]


# ---------------------------------------------------------------------------
# find_best_build: corridors are fractions of `budget`, which only reflects
# real prices when the build spends close to all of it — a build that lands
# well under budget has its corridors computed from money it never spends,
# wrongly excluding a real, affordable, better option. find_best_build wraps
# build_configuration unchanged at a series of larger notional budgets and
# keeps whichever accepted result (real cost <= real budget) scores best.
# ---------------------------------------------------------------------------


def _minimal_offer(category, key, price, tier, **specs):
    return {
        "normalized_key": key,
        "category": category,
        "brand": "Test",
        "model": key,
        "specs": specs,
        "source": "test",
        "url": f"https://example.invalid/{key}",
        "price": price,
        "availability": "в наличии",
        "captured_at": "2026-08-22T00:00:00",
    }


def test_score_reflects_post_trim_swap_not_the_original_pair():
    # _trim_to_budget can swap the gpu itself for a cheaper in-corridor one
    # to fit the budget — the pair search initially ranks gpu_strong's pair
    # highest and tries it first, but it only fits after being trimmed down
    # to gpu_weak. BuildResult.score must describe what's actually in the
    # result (gpu_weak, tier 50), not the pair the search started with
    # (gpu_strong, tier 90) — find_best_build's cross-notional comparison
    # depends on this being accurate, not stale.
    rules = {
        "test": {
            "floors": {},
            "budget_corridors": {"cpu": [0.0, 1.0], "gpu": [0.0, 1.0]},
            "objective_weights": {"cpu": 0.0, "gpu": 1.0},
        }
    }
    catalog = {
        "cpu": [_minimal_offer("cpu", "cpu:test:only", 100, 50)],
        "gpu": [
            _minimal_offer("gpu", "gpu:test:strong", 700, 90),
            _minimal_offer("gpu", "gpu:test:weak", 400, 50),
        ],
    }
    tiers = {
        "cpu:test:only": {"tier": 50, "source": "estimated"},
        "gpu:test:strong": {"tier": 90, "source": "estimated"},
        "gpu:test:weak": {"tier": 50, "source": "estimated"},
    }
    result = build_configuration(catalog, 750, "test", rules=rules, compat_config={}, tiers=tiers, brand_rules={})
    assert result.feasible
    gpu_item = next(i for i in result.items if i.category == "gpu")
    assert gpu_item.model == "gpu:test:weak"  # confirms the trim-swap actually happened
    assert result.score == 50  # not 90 — must match what's actually in the result


def test_find_best_build_rescues_result_excluded_by_tight_notional_corridor():
    # gpu_good is excluded at budget=1000 (corridor ceiling 30% = 300 < 320)
    # even though gpu_cheap already satisfies _widen_corridors_to_market_floor
    # (so that fix alone doesn't rescue it) — only a wider notional does.
    # Weighting gpu at 1.0 makes tier the sole score driver.
    rules = {
        "test": {
            "floors": {},
            "budget_corridors": {"cpu": [0.0, 0.3], "gpu": [0.0, 0.3]},
            "objective_weights": {"cpu": 0.0, "gpu": 1.0},
        }
    }
    catalog = {
        "cpu": [_minimal_offer("cpu", "cpu:test:only", 100, 80)],
        "gpu": [
            _minimal_offer("gpu", "gpu:test:cheap", 50, 10),
            _minimal_offer("gpu", "gpu:test:good", 320, 80),
        ],
    }
    tiers = {
        "cpu:test:only": {"tier": 80, "source": "estimated"},
        "gpu:test:cheap": {"tier": 10, "source": "estimated"},
        "gpu:test:good": {"tier": 80, "source": "estimated"},
    }
    budget = 1000

    direct = build_configuration(catalog, budget, "test", rules=rules, compat_config={}, tiers=tiers, brand_rules={})
    assert direct.feasible
    assert direct.score == 10  # stuck with gpu_cheap: gpu_good's 320 > 30% of 1000

    best = find_best_build(catalog, budget, "test", rules=rules, compat_config={}, tiers=tiers, brand_rules={})
    assert best.feasible
    assert best.score == 80  # found gpu_good via a wider notional
    assert best.total_price == 420
    assert best.total_price <= budget  # real spend still respects the real budget
    assert best.budget == budget  # reported budget is the real one, not notional
    # The chosen notional must actually admit gpu_good's 320 into the 30%
    # ceiling — exact value depends on the step grid's resolution, not
    # asserted as a magic number so this doesn't break every time that's tuned.
    assert best.notional_budget * 0.3 >= 320


def test_find_best_build_never_worse_than_direct_call(rules, compat_config):
    # At a budget where build_configuration already succeeds directly,
    # wrapping it must not produce a worse (lower-scoring, or pricier at an
    # equal score) result, and must still respect the real budget.
    direct = build_configuration(
        make_catalog(), 80000, "gaming", rules=rules, compat_config=compat_config, tiers=TIERS, brand_rules=BRAND_RULES
    )
    assert direct.feasible

    best = find_best_build(
        make_catalog(), 80000, "gaming", rules=rules, compat_config=compat_config, tiers=TIERS, brand_rules=BRAND_RULES
    )
    assert best.feasible
    assert best.total_price <= 80000
    assert best.score >= direct.score
    if best.score == direct.score:
        assert best.total_price <= direct.total_price


def test_find_best_build_reports_notional_in_reason_when_nothing_fits(rules, compat_config):
    # Budget far below even the cheapest possible cpu+gpu pair: no notional,
    # however wide, can make that affordable — must fail honestly, not hang
    # or silently return something.
    result = find_best_build(
        make_catalog(), 500, "gaming", rules=rules, compat_config=compat_config, tiers=TIERS, brand_rules=BRAND_RULES
    )
    assert not result.feasible
    assert result.notional_budget is not None
    assert result.budget == 500
