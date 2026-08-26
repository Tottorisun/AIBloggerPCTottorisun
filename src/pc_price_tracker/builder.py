"""Config builder: pick a good, compatible, budget-fitting set of parts.

Three phases, in order, per profile rules in data/build_rules.yaml:

  1. Floor filter — hard requirements (RAM total/modules, SSD size/NVMe,
     PSU certification, CPU/GPU must have a known performance tier). A
     candidate that fails its category's floor never enters the pool,
     regardless of price.
  2. CPU+GPU selection by objective — every (CPU, GPU) pair whose price
     falls in its category's budget corridor is scored by
     cpu_weight*tier(cpu) + gpu_weight*tier(gpu); pairs are tried best
     score first. For each candidate pair, the remaining categories
     (motherboard/ram/ssd/cooler/case/psu) are filled greedily within
     their own corridors and whatever budget is left, filtering at every
     step by pc_price_tracker.compat.check_compatibility.
  3. The first pair for which a complete, compatible, budget-respecting
     build exists wins — since pairs are tried in descending score order,
     this is the highest-scoring feasible build, not just *a* feasible one.

If no pair produces a complete build, nothing is returned silently: the
result reports what's missing and estimates the cheapest budget for which
a complete (if unremarkable) build is possible at all, computed the same
way but ignoring the budget corridors — cheapest floor-passing,
compatible option per category.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from pc_price_tracker.compat import (
    check_compatibility,
    load_brand_rules,
    load_build_rules,
    load_compatibility_config,
    load_performance_tiers,
)
from pc_price_tracker.constants import Category

_DISPLAY_ORDER: list[Category] = ["cpu", "motherboard", "ram", "gpu", "ssd", "cooler", "case", "psu"]
_FILL_AFTER_CPU_GPU: list[Category] = ["motherboard", "ram", "ssd", "cooler", "case", "psu"]

# Safety valve: with corridor pre-filtering this is normally tens of pairs,
# not hundreds — this just bounds worst-case runtime on a very wide budget.
MAX_PAIR_ATTEMPTS = 500

_CERT_TIER_ORDER = ["80 PLUS", "80 PLUS BRONZE", "80 PLUS SILVER", "80 PLUS GOLD", "80 PLUS PLATINUM", "80 PLUS TITANIUM"]


def _cert_tier_rank(cert: str | None) -> int:
    if not cert:
        return -1
    normalized = " ".join(cert.strip().upper().split())
    return _CERT_TIER_ORDER.index(normalized) if normalized in _CERT_TIER_ORDER else -1


def _tier_value(tiers: dict[str, dict[str, Any]], key: str) -> int | None:
    entry = tiers.get(key)
    return entry["tier"] if entry else None


def _tier_source(tiers: dict[str, dict[str, Any]], key: str) -> str | None:
    entry = tiers.get(key)
    return entry.get("source") if entry else None


@dataclass
class BuildItem:
    category: Category
    brand: str
    model: str
    price: int
    source: str
    url: str
    reason: str


@dataclass
class BuildResult:
    profile: str
    budget: int
    feasible: bool
    items: list[BuildItem] = field(default_factory=list)
    total_price: int = 0
    score: float | None = None
    compatibility_issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    untiered_skipped: dict[str, int] = field(default_factory=dict)
    floor_unrecognized: dict[str, int] = field(default_factory=dict)
    brand_rejected: dict[str, int] = field(default_factory=dict)
    brand_rule_unrecognized: dict[str, int] = field(default_factory=dict)
    availability_rejected: dict[str, int] = field(default_factory=dict)
    availability_unrecognized: dict[str, int] = field(default_factory=dict)
    infeasible_reason: str | None = None
    minimum_budget_estimate: int | None = None
    # Set only by find_best_build (None when build_configuration is called
    # directly) — the notional budget whose wider corridors actually
    # produced this result, which can be more than `budget` itself.
    notional_budget: int | None = None


def build_configuration(
    offers_by_category: dict[Category, list[dict[str, Any]]],
    budget: int,
    profile: str,
    *,
    rules: dict[str, Any] | None = None,
    compat_config: dict[str, Any] | None = None,
    tiers: dict[str, dict[str, Any]] | None = None,
    brand_rules: dict[str, Any] | None = None,
    include_preorder: bool = False,
) -> BuildResult:
    """rules/compat_config/tiers/brand_rules default to loading from
    config/data YAML — the CLI never passes them. Tests inject synthetic
    ones so build results don't depend on the live scraped DB, the
    hand-curated tier table, or the hand-curated brand whitelist.

    include_preorder disables the profile's require_availability floor
    entirely (not just adds "предзаказ" to the allowed set) — used to
    compare "what's buildable right now" against "what's buildable if
    preorder/out-of-stock listings counted too"."""
    rules = rules if rules is not None else load_build_rules()
    compat_config = compat_config if compat_config is not None else load_compatibility_config()
    tiers = tiers if tiers is not None else load_performance_tiers()
    brand_rules = brand_rules if brand_rules is not None else load_brand_rules()
    if profile not in rules:
        raise ValueError(f"unknown profile {profile!r}; choose from {sorted(rules)}")

    profile_rules = rules[profile]
    floors: dict[str, Any] = profile_rules.get("floors", {})
    corridors: dict[str, list[float]] = profile_rules.get("budget_corridors", {})
    weights: dict[str, float] = profile_rules.get("objective_weights", {})
    min_gpu_tier_ratio: float | None = profile_rules.get("min_gpu_tier_ratio")

    filtered, untiered_skipped, floor_unrecognized, availability_rejected, availability_unrecognized = _apply_floors(
        offers_by_category, corridors, floors, tiers, include_preorder=include_preorder
    )
    filtered, brand_rejected, brand_rule_unrecognized = _apply_brand_rules(filtered, brand_rules, profile)

    # A corridor's upper bound is a spend *preference* (favor better parts as
    # budget grows), not a hard cap — if the cheapest floor/whitelist-passing
    # option in a category costs more than that, the category just costs
    # that much, at any budget (a real DDR5 kit is ~28000 RUB whether the
    # total is 60000 or 300000; the ceiling doesn't scale down with it).
    # Treating that as "infeasible" manufactures a failure the market
    # didn't actually produce. Only ever widens the ceiling, never narrows
    # it and never touches the floor, which stays a real preference signal.
    corridors = _widen_corridors_to_market_floor(filtered, corridors, budget)

    warnings: list[str] = []
    for category in corridors:
        if not offers_by_category.get(category):
            warnings.append(f"нет данных по категории «{category}» — сборка неполная, доскрейпите её")

    # min_gpu_tier_ratio only makes sense relative to a budget corridor
    # ("best available for this money"), so it's applied as its own
    # narrowed copy rather than mutating `filtered` itself — the min-budget
    # fallback estimate below deliberately ignores budget corridors
    # entirely (see _cheapest_full_build) and needs the unrestricted pool
    # to stay an honest "cheapest technically valid" answer.
    search_pool = _apply_gpu_tier_ratio(filtered, budget, corridors, tiers, min_gpu_tier_ratio)

    pairs = _score_cpu_gpu_pairs(search_pool, budget, corridors, weights, tiers)

    attempts = 0
    for _score, cpu, gpu in pairs:
        if attempts >= MAX_PAIR_ATTEMPTS:
            warnings.append(f"достигнут лимит перебора пар CPU+GPU ({MAX_PAIR_ATTEMPTS}) — возможны более удачные варианты")
            break
        attempts += 1

        selected: dict[str, dict[str, Any]] = {}
        if cpu is not None:
            selected["cpu"] = cpu
        if gpu is not None:
            selected["gpu"] = gpu

        completed = _fill_remaining(selected, search_pool, budget, corridors, compat_config)
        if completed is None:
            continue

        total_price = sum(c["price"] for c in completed.values())
        if total_price > budget:
            # The corridor-fill is a greedy heuristic (each category's
            # "reserve for later" is an approximation, see _fill_remaining) —
            # a small overshoot doesn't mean this pair is truly infeasible,
            # just that the greedy pass overspent slightly. Try trimming
            # before giving up on an otherwise-good CPU+GPU pair.
            trimmed = _trim_to_budget(completed, search_pool, budget, corridors, compat_config)
            if trimmed is None:
                continue
            completed = trimmed
            total_price = sum(c["price"] for c in completed.values())
            if total_price > budget:
                continue
        issues = check_compatibility(completed, compat_config)
        if issues:
            continue

        # Recomputed from `completed`, not the `score` the pair was ranked
        # by going in: _trim_to_budget can swap the cpu or gpu itself for a
        # cheaper one to fit the budget (see its docstring), which makes
        # the pre-trim score stale — it would describe a pair that isn't
        # what's actually in this result anymore.
        actual_score = _pair_score(completed, tiers, weights)
        items = [_to_build_item(cat, completed[cat], tiers) for cat in _DISPLAY_ORDER if cat in completed]
        return BuildResult(
            profile=profile,
            budget=budget,
            feasible=True,
            items=items,
            total_price=total_price,
            score=actual_score,
            compatibility_issues=[],
            warnings=warnings,
            untiered_skipped=untiered_skipped,
            floor_unrecognized=floor_unrecognized,
            brand_rejected=brand_rejected,
            brand_rule_unrecognized=brand_rule_unrecognized,
            availability_rejected=availability_rejected,
            availability_unrecognized=availability_unrecognized,
        )

    # Nothing worked — figure out why, and what it would take.
    reason = _diagnose_infeasibility(search_pool, budget, corridors, pairs)
    min_build, blocking_category = _cheapest_full_build(filtered, corridors, compat_config)
    minimum_budget = sum(c["price"] for c in min_build.values()) if min_build else None
    if min_build is None and blocking_category:
        reason += f" Даже без учёта бюджета не нашлось ни одного варианта для категории «{blocking_category}», проходящего floor-требования и совместимость."

    return BuildResult(
        profile=profile,
        budget=budget,
        feasible=False,
        warnings=warnings,
        untiered_skipped=untiered_skipped,
        floor_unrecognized=floor_unrecognized,
        brand_rejected=brand_rejected,
        brand_rule_unrecognized=brand_rule_unrecognized,
        availability_rejected=availability_rejected,
        availability_unrecognized=availability_unrecognized,
        infeasible_reason=reason,
        minimum_budget_estimate=minimum_budget,
    )


# Fractions of `budget` from `budget` itself up to +50%, in 1% steps — how
# far find_best_build is willing to compute corridors from a larger notional
# number than what's actually being spent. A coarser step can straddle a
# genuinely feasible-and-affordable result instead of landing on it — e.g.
# a 5% grid missed an 84440 RUB build that only existed for notional in
# [90500, 92000], between its 89250 and 93500 grid points (confirmed by
# probing every 500 RUB). 1% is fine enough that this hasn't recurred on
# real data; the extra build_configuration calls (up to 51 instead of 11
# per query) are still fast enough not to matter for a CLI tool.
_NOTIONAL_BUDGET_STEPS: list[float] = [1.0 + 0.01 * i for i in range(51)]


def find_best_build(
    offers_by_category: dict[Category, list[dict[str, Any]]],
    budget: int,
    profile: str,
    *,
    rules: dict[str, Any] | None = None,
    compat_config: dict[str, Any] | None = None,
    tiers: dict[str, dict[str, Any]] | None = None,
    brand_rules: dict[str, Any] | None = None,
    include_preorder: bool = False,
) -> BuildResult:
    """budget_corridors are fractions of `budget`, which only reflects real
    market prices when the build actually spends close to all of it. A
    build that naturally lands well under budget — component prices rarely
    tile exactly, see _fill_remaining's greedy-but-approximate fill — has
    its corridors computed from money it never spends, wrongly excluding
    real, affordable parts. That's how a build costing 84550 got reported
    "impossible" at a real budget of 85000, when calling build_configuration
    directly with a larger number found that exact build fine. Budget
    should bound what gets spent, not dictate what corridors are computed
    from.

    This calls build_configuration completely unchanged — no corridor or
    algorithm logic here — at a series of progressively larger *notional*
    budgets (corridors computed as if more money were available) and keeps
    whichever accepted result actually costs no more than `budget` scores
    best, cheapest as tiebreak on a tie. Trying more headroom can only
    surface a result that a single call at `budget` would've missed too —
    this never produces a worse answer than build_configuration(budget)
    alone, only ever an equal or better one.
    """
    rules = rules if rules is not None else load_build_rules()
    compat_config = compat_config if compat_config is not None else load_compatibility_config()
    tiers = tiers if tiers is not None else load_performance_tiers()
    brand_rules = brand_rules if brand_rules is not None else load_brand_rules()

    best_key: tuple[float, int] | None = None
    best: BuildResult | None = None
    best_notional: int | None = None
    widest_attempt: BuildResult | None = None
    widest_notional: int | None = None

    for step in _NOTIONAL_BUDGET_STEPS:
        notional_budget = round(budget * step)
        result = build_configuration(
            offers_by_category,
            notional_budget,
            profile,
            rules=rules,
            compat_config=compat_config,
            tiers=tiers,
            brand_rules=brand_rules,
            include_preorder=include_preorder,
        )
        widest_attempt, widest_notional = result, notional_budget

        if not result.feasible or result.total_price > budget:
            continue

        key = (-(result.score or 0.0), result.total_price)
        if best_key is None or key < best_key:
            best_key, best, best_notional = key, result, notional_budget

    if best is not None:
        return replace(best, budget=budget, notional_budget=best_notional)

    # Nothing fit the real budget at any notional tried — report the
    # widest attempt's diagnosis: it's the most informative "why", since a
    # narrower notional can only be infeasible for a superset of the same
    # reasons a wider one already ran into.
    assert widest_attempt is not None and widest_notional is not None
    if widest_attempt.feasible:
        overshoot_pct = round((widest_notional / budget - 1) * 100)
        reason = (
            f"Даже расширив коридоры до notional-бюджета {widest_notional} ₽ (+{overshoot_pct}% от {budget} ₽), "
            f"лучшая найденная сборка стоит {widest_attempt.total_price} ₽ — дороже реального бюджета."
        )
        return replace(
            widest_attempt,
            feasible=False,
            items=[],
            total_price=0,
            score=None,
            budget=budget,
            notional_budget=widest_notional,
            infeasible_reason=reason,
            minimum_budget_estimate=widest_attempt.total_price,
        )

    return replace(widest_attempt, budget=budget, notional_budget=widest_notional)


# ---------------------------------------------------------------------------
# Phase 1: floors
# ---------------------------------------------------------------------------


def _apply_floors(
    offers_by_category: dict[Category, list[dict[str, Any]]],
    corridors: dict[str, list[float]],
    floors: dict[str, Any],
    tiers: dict[str, dict[str, Any]],
    include_preorder: bool = False,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int], dict[str, int], dict[str, int], dict[str, int]]:
    filtered: dict[str, list[dict[str, Any]]] = {}
    untiered_skipped: dict[str, int] = {}
    floor_unrecognized: dict[str, int] = {}
    availability_rejected: dict[str, int] = {}
    availability_unrecognized: dict[str, int] = {}

    # "в наличии" only, by default — a profile can widen this explicitly in
    # its floors (data/build_rules.yaml documents the exact statuses Regard
    # reports). include_preorder (CLI --include-preorder) drops the check
    # entirely rather than widening the allowed set, so it also lets through
    # "нет в наличии" — that's deliberate: the flag's purpose is comparing
    # "buildable right now" against "buildable ignoring stock status at
    # all", not just admitting preorders specifically.
    require_availability = None if include_preorder else floors.get("require_availability", ["в наличии"])

    for category in corridors:
        candidates = offers_by_category.get(category, [])
        kept = []
        skipped = 0
        unrecognized_count = 0
        avail_reject_count = 0
        avail_unrecognized_count = 0
        for c in candidates:
            specs = c.get("specs", {})

            if category in ("cpu", "gpu"):
                if c["normalized_key"] not in tiers:
                    skipped += 1
                    continue

            if require_availability:
                availability = c.get("availability")
                if availability is None:
                    avail_unrecognized_count += 1
                elif availability not in require_availability:
                    avail_reject_count += 1
                    continue

            if category == "cpu":
                # Unlike the other floors below, min_cores/min_threads can
                # be unenforceable per-candidate rather than just off for
                # the whole profile — same "don't reject blind" shape as
                # brand_rules.yaml's chipset/fan-count checks, just sourced
                # from build_rules.yaml instead. See normalize.py: cores is
                # reliably extracted, threads currently never is.
                candidate_unrecognized = False
                min_cores = floors.get("min_cores")
                if min_cores:
                    cores = specs.get("cores")
                    if cores is None:
                        candidate_unrecognized = True
                    elif cores < min_cores:
                        continue
                min_threads = floors.get("min_threads")
                if min_threads:
                    threads = specs.get("threads")
                    if threads is None:
                        candidate_unrecognized = True
                    elif threads < min_threads:
                        continue
                if candidate_unrecognized:
                    unrecognized_count += 1

            if category == "ram":
                if specs.get("capacity_gb", 0) < floors.get("ram_min_total_gb", 0):
                    continue
                if specs.get("modules", 1) < floors.get("ram_min_modules", 1):
                    continue

            if category == "ssd":
                if specs.get("capacity_gb", 0) < floors.get("ssd_min_gb", 0):
                    continue
                if floors.get("ssd_require_nvme") and specs.get("interface") != "NVMe":
                    continue

            if category == "psu":
                if floors.get("psu_require_80plus"):
                    cert = (specs.get("certification") or "").upper()
                    if not cert.startswith("80 PLUS"):
                        continue

            kept.append(c)

        filtered[category] = kept
        if skipped:
            untiered_skipped[category] = skipped
        if unrecognized_count:
            floor_unrecognized[category] = unrecognized_count
        if avail_reject_count:
            availability_rejected[category] = avail_reject_count
        if avail_unrecognized_count:
            availability_unrecognized[category] = avail_unrecognized_count

    return filtered, untiered_skipped, floor_unrecognized, availability_rejected, availability_unrecognized


def _evaluate_brand_rules(candidate: dict[str, Any], rules: dict[str, Any], profile: str) -> str:
    """Returns "keep", "reject", or "unrecognized" (passes through like
    "keep", but counted separately — a rule that requires data the source
    didn't provide, e.g. no chipset parsed, must not silently reject; the
    caller surfaces how often that happened so it stays visible).

    Which checks apply is entirely driven by which keys are present in
    `rules` (this category's section of data/brand_rules.yaml) — adding a
    rule to a new category is a YAML change, not a code change, as long as
    it's built from these primitives:
      whitelist                                  -> brand membership
      min_certification / min_certification_profiles   -> PSU-style cert rank floor
      require_dram_cache_profiles                 -> SSD-style boolean spec gate
      chipsets_by_profile                         -> per-profile allowed-value set
      min_fans / min_fans_profiles                -> numeric spec floor
    """
    brand = candidate.get("brand", "")
    specs: dict[str, Any] = candidate.get("specs", {})

    whitelist = {b.lower() for b in rules.get("whitelist", [])}
    if whitelist and brand.lower() not in whitelist:
        return "reject"

    if "min_certification" in rules and profile in rules.get("min_certification_profiles", []):
        min_rank = _cert_tier_rank(rules["min_certification"])
        if _cert_tier_rank(specs.get("certification")) < min_rank:
            return "reject"

    if profile in rules.get("require_dram_cache_profiles", []):
        # dram_cache_mb is only ever present (and always > 0) when the
        # source explicitly stated a cache size — its absence means "not
        # stated", not "confirmed DRAM-less", so it does not reject here.
        if "dram_cache_mb" in specs and specs["dram_cache_mb"] <= 0:
            return "reject"

    chipsets_by_profile = rules.get("chipsets_by_profile", {})
    if profile in chipsets_by_profile:
        chipset = specs.get("chipset")
        if not chipset:
            return "unrecognized"
        allowed = {c.upper() for c in chipsets_by_profile[profile]}
        if chipset.upper() not in allowed:
            return "reject"

    if "min_fans" in rules and profile in rules.get("min_fans_profiles", []):
        fans = specs.get("fan_count")
        if fans is None:
            return "unrecognized"
        if fans < rules["min_fans"]:
            return "reject"

    return "keep"


def _apply_brand_rules(
    filtered: dict[str, list[dict[str, Any]]],
    brand_rules: dict[str, Any],
    profile: str,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int], dict[str, int]]:
    """Per data/brand_rules.yaml, generically over whichever categories it
    defines (not hardcoded to any specific set). Runs after floors, on the
    already-floor-filtered pool — same "reduce the candidate pool, never
    touch the DB" shape. Rejections and "rule skipped, data unavailable"
    counts are both returned (not logged per-item here) so the caller can
    report them; the products themselves are untouched in the DB either way.
    """
    result = dict(filtered)
    rejected: dict[str, int] = {}
    unrecognized: dict[str, int] = {}

    for category, rules in brand_rules.items():
        if category not in result:
            continue

        kept = []
        reject_count = 0
        unrecognized_count = 0
        for candidate in result[category]:
            verdict = _evaluate_brand_rules(candidate, rules, profile)
            if verdict == "reject":
                reject_count += 1
                continue
            if verdict == "unrecognized":
                unrecognized_count += 1
            kept.append(candidate)

        result[category] = kept
        if reject_count:
            rejected[category] = reject_count
        if unrecognized_count:
            unrecognized[category] = unrecognized_count

    return result, rejected, unrecognized


# ---------------------------------------------------------------------------
# Phase 2: CPU+GPU objective search
# ---------------------------------------------------------------------------


def _widen_corridors_to_market_floor(
    filtered: dict[str, list[dict[str, Any]]],
    corridors: dict[str, list[float]],
    budget: int,
) -> dict[str, list[float]]:
    """Raises a category's corridor ceiling to the cheapest floor/whitelist-
    passing price actually available, when that's above what the
    percentage ceiling would allow — data-driven, not a hardcoded RUB
    constant, so it stays correct as the catalog's prices move instead of
    needing recalibration every time they do (see build_rules.yaml's
    require_availability comment for how fast that already happens).
    Applies to every category with a corridor, not just RAM/motherboard —
    any category can have a real price floor above what a small budget's
    percentage window allows. Only ever raises the ceiling, never the
    floor (that stays a real spend-preference signal) and never lowers it
    (a category that's cheaper than the percentage ceiling is untouched).
    """
    if budget <= 0:
        return corridors
    widened: dict[str, list[float]] = {}
    for category, corridor in corridors.items():
        if not corridor:
            widened[category] = corridor
            continue
        lo, hi = corridor
        candidates = filtered.get(category, [])
        if candidates:
            market_floor_fraction = min(c["price"] for c in candidates) / budget
            hi = max(hi, market_floor_fraction)
        widened[category] = [lo, hi]
    return widened


def _within_corridor(price: int, budget: int, corridor: list[float] | None) -> bool:
    if not corridor:
        return True
    lo, hi = corridor
    return budget * lo <= price <= budget * hi


def _apply_gpu_tier_ratio(
    filtered: dict[str, list[dict[str, Any]]],
    budget: int,
    corridors: dict[str, list[float]],
    tiers: dict[str, dict[str, Any]],
    min_gpu_tier_ratio: float | None,
) -> dict[str, list[dict[str, Any]]]:
    """Drops GPUs priced within the gpu budget corridor whose tier is below
    min_gpu_tier_ratio of the best-tiered GPU also priced in that corridor —
    not an absolute cutoff, so it doesn't need retuning as the catalog's
    price/tier landscape shifts. Applied as its own phase (rather than
    inline in _score_cpu_gpu_pairs) so filtered["gpu"] is the single source
    of truth for every later step that reads it too — _fill_remaining and
    _trim_to_budget both do, when swapping the GPU itself — instead of
    each needing to know about and reapply the ratio separately.
    """
    if not min_gpu_tier_ratio or "gpu" not in filtered:
        return filtered
    gpu_corridor = corridors.get("gpu")
    in_corridor = [g for g in filtered["gpu"] if _within_corridor(g["price"], budget, gpu_corridor)]
    if not in_corridor:
        return filtered
    tiers_in_corridor = [_tier_value(tiers, g["normalized_key"]) for g in in_corridor]
    best_tier = max((t for t in tiers_in_corridor if t is not None), default=None)
    if best_tier is None:
        return filtered
    min_tier = best_tier * min_gpu_tier_ratio

    result = dict(filtered)
    result["gpu"] = [g for g, t in zip(in_corridor, tiers_in_corridor) if t is not None and t >= min_tier]
    return result


def _pair_score(
    parts: dict[str, dict[str, Any]],
    tiers: dict[str, dict[str, Any]],
    weights: dict[str, float],
) -> float:
    """The objective value for whichever cpu/gpu are actually in `parts` —
    used both to rank candidate pairs up front and, separately, to report
    the true score of a FINAL result after _trim_to_budget has had a
    chance to swap the cpu or gpu out for a cheaper one (see its call site
    in build_configuration): the pre-trim score doesn't necessarily
    describe the pair that's actually in the returned build anymore.
    """
    cpu = parts.get("cpu")
    gpu = parts.get("gpu")
    cpu_tier = _tier_value(tiers, cpu["normalized_key"]) if cpu else 0
    gpu_tier = _tier_value(tiers, gpu["normalized_key"]) if gpu else 0
    return weights.get("cpu", 0.0) * (cpu_tier or 0) + weights.get("gpu", 0.0) * (gpu_tier or 0)


def _score_cpu_gpu_pairs(
    filtered: dict[str, list[dict[str, Any]]],
    budget: int,
    corridors: dict[str, list[float]],
    weights: dict[str, float],
    tiers: dict[str, dict[str, Any]],
) -> list[tuple[float, dict[str, Any] | None, dict[str, Any] | None]]:
    cpu_corridor = corridors.get("cpu")
    gpu_corridor = corridors.get("gpu")

    cpus = [c for c in filtered.get("cpu", []) if _within_corridor(c["price"], budget, cpu_corridor)]
    uses_gpu = "gpu" in corridors
    gpus = [g for g in filtered.get("gpu", []) if _within_corridor(g["price"], budget, gpu_corridor)] if uses_gpu else [None]

    scored: list[tuple[float, dict[str, Any] | None, dict[str, Any] | None]] = []
    for cpu in cpus:
        for gpu in gpus:
            parts = {"cpu": cpu} if gpu is None else {"cpu": cpu, "gpu": gpu}
            scored.append((_pair_score(parts, tiers, weights), cpu, gpu))

    price_of = lambda pair: pair[1]["price"] + (pair[2]["price"] if pair[2] else 0)  # noqa: E731
    scored.sort(key=lambda pair: (-pair[0], price_of(pair)))
    return scored


# ---------------------------------------------------------------------------
# Phase 2b: fill the rest within corridors + remaining budget
# ---------------------------------------------------------------------------


def _fill_remaining(
    selected: dict[str, dict[str, Any]],
    filtered: dict[str, list[dict[str, Any]]],
    budget: int,
    corridors: dict[str, list[float]],
    compat_config: dict[str, Any],
) -> dict[str, dict[str, Any]] | None:
    selected = dict(selected)
    total_so_far = sum(c["price"] for c in selected.values())

    fill_categories = [c for c in _FILL_AFTER_CPU_GPU if c in corridors]

    for i, category in enumerate(fill_categories):
        corridor = corridors.get(category)
        candidates = filtered.get(category, [])
        in_corridor = [c for c in candidates if _within_corridor(c["price"], budget, corridor)]
        if not in_corridor:
            return None

        # Reserve the cheapest in-corridor price for every category still
        # to come, so an earlier pick maxing out its own corridor doesn't
        # starve a later one down to zero affordable options. Filtered by
        # compatibility with what's already selected (cpu/gpu, and by now
        # possibly motherboard) — a naive price-only reserve underestimates
        # whenever the cheapest in-corridor option turns out incompatible,
        # e.g. the cheapest RAM in corridor is DDR4 but the motherboard
        # already picked needs DDR5, so the real cheapest reachable price is
        # higher than the naive reserve assumed, and the total creeps over
        # budget by the gap. Doesn't account for compatibility between two
        # categories that are BOTH still upcoming (a deeper cross-category
        # constraint), but resolves this by the time each is actually filled.
        reserve = 0
        for later_category in fill_categories[i + 1 :]:
            later_corridor = corridors.get(later_category)
            later_candidates = [c for c in filtered.get(later_category, []) if _within_corridor(c["price"], budget, later_corridor)]
            if not later_candidates:
                return None
            later_compatible = [c for c in later_candidates if not check_compatibility({**selected, later_category: c}, compat_config)]
            pool = later_compatible if later_compatible else later_candidates
            reserve += min(c["price"] for c in pool)

        remaining_budget = budget - total_so_far - reserve
        affordable = [c for c in in_corridor if c["price"] <= remaining_budget]

        if affordable:
            # Normal case: spend up toward the top of what's both
            # in-corridor and affordable (spec-for-money proxy).
            compatible = [c for c in affordable if not check_compatibility({**selected, category: c}, compat_config)]
            if compatible:
                choice = max(compatible, key=lambda c: c["price"])
                selected[category] = choice
                total_so_far += choice["price"]
                continue

        # Nothing both in-corridor and affordable (or nothing compatible
        # among what was) — damage control: take the CHEAPEST compatible
        # in-corridor option instead of falling back to the priciest one.
        # Picking the priciest here would silently blow the budget, which
        # defeats the whole point of reserving budget for later categories
        # in the first place.
        compatible = [c for c in in_corridor if not check_compatibility({**selected, category: c}, compat_config)]
        if not compatible:
            return None
        choice = min(compatible, key=lambda c: c["price"])
        selected[category] = choice
        total_so_far += choice["price"]

    return selected


_MAX_TRIM_ROUNDS = 20


def _trim_to_budget(
    selected: dict[str, dict[str, Any]],
    filtered: dict[str, list[dict[str, Any]]],
    budget: int,
    corridors: dict[str, list[float]],
    compat_config: dict[str, Any],
) -> dict[str, dict[str, Any]] | None:
    """A completed-but-over-budget build gets here when the greedy fill's
    per-step reserve estimate (see _fill_remaining) slightly underspent the
    true cost of a later category — usually a matter of tens to low
    thousands of RUB, not a sign the pair is genuinely unaffordable.
    Repeatedly swaps whichever category has the largest available saving
    for a cheaper in-corridor, still-compatible alternative, until the
    total fits or no swap can help any further.

    Includes cpu/gpu themselves, not just the peripheral categories: once
    brand whitelists / chipset allowlists / a relative GPU tier floor are
    all narrowing candidate pools at once, it's common for every
    peripheral to already sit at its cheapest compatible option while cpu
    or gpu still has a bit of same-corridor room — e.g. the tier-optimal
    GPU pick costs 500 RUB more than another one in the same corridor that
    would still clear the min_gpu_tier_ratio floor. cpu/gpu are tried only
    after peripherals in any given round (same "biggest saving wins"
    comparison, but ties keep whichever was found first) — cheapening the
    part the objective function was built around should be the last
    resort, not the first.
    """
    selected = dict(selected)
    total = sum(c["price"] for c in selected.values())
    fillable = [c for c in _FILL_AFTER_CPU_GPU if c in selected]
    fillable += [c for c in ("cpu", "gpu") if c in selected]

    for _ in range(_MAX_TRIM_ROUNDS):
        if total <= budget:
            return selected

        best = _best_trim_swap(selected, filtered, budget, corridors, compat_config, fillable, respect_corridor=True)
        if best is None:
            # No in-corridor swap helps any further. Corridors are a
            # spec-for-money *preference*, not a hard requirement the way
            # floors/whitelist/compatibility are — blocking an otherwise
            # fully valid combination over what's often a small residual
            # gap is the wrong trade-off, so last resort: allow going
            # below (never above — that direction is what got us over
            # budget in the first place) a category's corridor floor too.
            best = _best_trim_swap(selected, filtered, budget, corridors, compat_config, fillable, respect_corridor=False)
        if best is None:
            return None  # no swap can reduce the total any further

        savings, category, cheaper = best
        selected[category] = cheaper
        total -= savings

    return selected if total <= budget else None


def _best_trim_swap(
    selected: dict[str, dict[str, Any]],
    filtered: dict[str, list[dict[str, Any]]],
    budget: int,
    corridors: dict[str, list[float]],
    compat_config: dict[str, Any],
    fillable: list[str],
    respect_corridor: bool,
) -> tuple[int, str, dict[str, Any]] | None:
    best: tuple[int, str, dict[str, Any]] | None = None
    for category in fillable:
        current_price = selected[category]["price"]
        corridor = corridors.get(category)
        others = {k: v for k, v in selected.items() if k != category}
        cheaper_candidates = [
            c
            for c in filtered.get(category, [])
            if c["price"] < current_price and (not respect_corridor or _within_corridor(c["price"], budget, corridor))
        ]
        compatible = [c for c in cheaper_candidates if not check_compatibility({**others, category: c}, compat_config)]
        if not compatible:
            continue
        cheapest = min(compatible, key=lambda c: c["price"])
        savings = current_price - cheapest["price"]
        if best is None or savings > best[0]:
            best = (savings, category, cheapest)
    return best


# ---------------------------------------------------------------------------
# Infeasibility diagnostics
# ---------------------------------------------------------------------------


def _diagnose_infeasibility(
    filtered: dict[str, list[dict[str, Any]]],
    budget: int,
    corridors: dict[str, list[float]],
    pairs: list[tuple[float, dict[str, Any] | None, dict[str, Any] | None]],
) -> str:
    empty_categories = [cat for cat, corridor in corridors.items() if cat not in ("cpu", "gpu") and not filtered.get(cat)]
    if empty_categories:
        return f"После floor-требований не осталось ни одного варианта в категориях: {', '.join(empty_categories)}."

    for category in ("cpu", "gpu"):
        if category not in corridors:
            continue
        corridor = corridors[category]
        candidates = filtered.get(category, [])
        in_corridor = [c for c in candidates if _within_corridor(c["price"], budget, corridor)]
        if not in_corridor:
            lo, hi = corridor
            return (
                f"Ни один {category.upper()} с известным tier не попадает в коридор бюджета "
                f"{lo * 100:.0f}-{hi * 100:.0f}% от {budget} ₽ ({budget * lo:.0f}-{budget * hi:.0f} ₽)."
            )

    if not pairs:
        return "Не найдено ни одной пары CPU+GPU, удовлетворяющей коридорам бюджета."

    return (
        f"Ни одна из {len(pairs)} пар CPU+GPU, попадающих в коридоры бюджета, "
        "не позволила подобрать совместимый набор остальных категорий в рамках бюджета и их коридоров."
    )


_MIN_BUILD_SEARCH_WIDTH = 20


def _cheapest_full_build(
    filtered: dict[str, list[dict[str, Any]]],
    corridors: dict[str, list[float]],
    compat_config: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]] | None, str | None]:
    """Cheapest complete build ignoring budget corridors — floors and
    compatibility only. Used to answer "what budget would actually work".

    Tries CPUs (and GPUs, if the profile uses one) cheapest-first and
    completes the rest greedily-cheapest for each combination, rather than
    locking in the single globally-cheapest CPU: that naive approach can
    pick something like a CPU on a long-discontinued socket that has zero
    compatible motherboards in the current catalog, and report the whole
    profile as impossible when a CPU one step up the price list works fine.
    Bounded to the N cheapest of each to keep this a fast diagnostic, not
    an exhaustive search — the result is a reasonable lower-bound estimate,
    not a guaranteed global minimum.
    """
    uses_gpu = "gpu" in corridors
    cpus = sorted(filtered.get("cpu", []), key=lambda c: c["price"])[:_MIN_BUILD_SEARCH_WIDTH]
    gpus = sorted(filtered.get("gpu", []), key=lambda c: c["price"])[:_MIN_BUILD_SEARCH_WIDTH] if uses_gpu else [None]

    if not cpus:
        return None, "cpu"
    if uses_gpu and not gpus:
        return None, "gpu"

    blocking_category = None
    for cpu in cpus:
        for gpu in gpus:
            selected: dict[str, dict[str, Any]] = {"cpu": cpu}
            if gpu is not None:
                selected["gpu"] = gpu

            ok = True
            for category in _FILL_AFTER_CPU_GPU:
                if category not in corridors:
                    continue
                candidates = filtered.get(category, [])
                compatible = [c for c in candidates if not check_compatibility({**selected, category: c}, compat_config)]
                if not compatible:
                    ok = False
                    blocking_category = category
                    break
                selected[category] = min(compatible, key=lambda c: c["price"])

            if ok:
                return selected, None

    return None, blocking_category


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def _to_build_item(category: Category, choice: dict[str, Any], tiers: dict[str, dict[str, Any]]) -> BuildItem:
    return BuildItem(
        category=category,
        brand=choice["brand"],
        model=choice["model"],
        price=choice["price"],
        source=choice["source"],
        url=choice["url"],
        reason=_reason_for(category, choice, tiers),
    )


def _reason_for(category: Category, choice: dict[str, Any], tiers: dict[str, dict[str, Any]]) -> str:
    specs: dict[str, Any] = choice.get("specs", {})
    parts: list[str] = []
    key = choice.get("normalized_key", "")
    tier = _tier_value(tiers, key)
    tier_source = _tier_source(tiers, key)
    tier_label = f"tier {tier}/100" + (" (оценка)" if tier_source == "estimated" else "") if tier is not None else None

    if category == "cpu":
        if tier_label:
            parts.append(tier_label)
        if specs.get("socket"):
            parts.append(f"сокет {specs['socket']}")
        if specs.get("tdp_w"):
            parts.append(f"TDP {specs['tdp_w']} Вт")
    elif category == "gpu":
        if tier_label:
            parts.append(tier_label)
        if specs.get("memory_gb"):
            parts.append(f"{specs['memory_gb']} ГБ памяти")
        if specs.get("length_mm"):
            parts.append(f"длина {specs['length_mm']:.0f} мм")
    elif category == "motherboard":
        if specs.get("socket"):
            parts.append(f"сокет {specs['socket']}")
        if specs.get("form_factor"):
            parts.append(f"форм-фактор {specs['form_factor']}")
        if specs.get("ram_type"):
            parts.append(f"память {specs['ram_type']}")
    elif category == "ram":
        if specs.get("capacity_gb"):
            parts.append(f"{specs['capacity_gb']} ГБ")
        if specs.get("modules"):
            parts.append(f"{specs['modules']}x модулей")
        if specs.get("ram_type"):
            parts.append(specs["ram_type"])
        if specs.get("speed_mhz"):
            parts.append(f"{specs['speed_mhz']} МГц")
    elif category == "ssd":
        if specs.get("capacity_gb"):
            gb = specs["capacity_gb"]
            parts.append(f"{gb // 1024} ТБ" if gb >= 1024 and gb % 1024 == 0 else f"{gb} ГБ")
        if specs.get("interface"):
            parts.append(specs["interface"])
        if specs.get("form_factor"):
            parts.append(specs["form_factor"])
    elif category == "psu":
        if specs.get("wattage_w"):
            parts.append(f"{specs['wattage_w']} Вт")
        if specs.get("certification"):
            parts.append(specs["certification"])
    elif category == "case":
        if specs.get("supported_form_factors"):
            parts.append("плата: " + "/".join(specs["supported_form_factors"]))
        if specs.get("max_gpu_length_mm"):
            parts.append(f"видеокарта до {specs['max_gpu_length_mm']} мм")
    elif category == "cooler":
        if specs.get("sockets"):
            parts.append("сокеты: " + ", ".join(specs["sockets"]))
        if specs.get("tdp_w"):
            parts.append(f"TDP до {specs['tdp_w']} Вт")

    return ", ".join(parts) if parts else "лучший вариант в рамках коридора бюджета категории"
