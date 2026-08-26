"""Compatibility checks used by the config builder.

The actual comparisons (socket equality, RAM type equality, PSU headroom,
...) live here in code; the thresholds and estimate tables they use live in
config/compatibility.yaml so they can be tuned without touching code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"
_DATA_DIR = _PROJECT_ROOT / "data"


def load_compatibility_config() -> dict[str, Any]:
    with (_CONFIG_DIR / "compatibility.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_build_rules() -> dict[str, Any]:
    with (_DATA_DIR / "build_rules.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_performance_tiers() -> dict[str, dict[str, Any]]:
    """Each entry is {"tier": int, "source": "verified"|"estimated"}."""
    with (_DATA_DIR / "performance_tiers.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_brand_rules() -> dict[str, Any]:
    with (_DATA_DIR / "brand_rules.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_feeds_config() -> dict[str, Any]:
    with (_CONFIG_DIR / "feeds.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def socket_matches(a: str | None, b: str | None) -> bool:
    """True if two canonical socket strings refer to compatible sockets.

    Coolers are sometimes rated for a whole LGA "11xx" family (encoded as
    e.g. "LGA115X") rather than one exact socket, so a trailing "X" acts as
    a wildcard digit on either side.
    """
    if not a or not b:
        return False
    a, b = a.upper(), b.upper()
    if len(a) != len(b):
        return False
    return all(x == y or x == "X" or y == "X" for x, y in zip(a, b))


def estimate_gpu_tdp_w(memory_gb: int | None, config: dict[str, Any]) -> int:
    if not memory_gb:
        return int(config.get("default_gpu_tdp_w", 0))
    for tier in config["gpu_tdp_estimate_by_memory_gb"]:
        if memory_gb <= tier["max_memory_gb"]:
            return int(tier["tdp_w"])
    return int(config["gpu_tdp_estimate_by_memory_gb"][-1]["tdp_w"])


def estimate_system_power_w(parts: dict[str, dict[str, Any]], config: dict[str, Any]) -> int:
    total = int(config.get("baseline_system_overhead_w", 60))
    cpu = parts.get("cpu")
    if cpu:
        total += int(cpu["specs"].get("tdp_w") or 65)
    gpu = parts.get("gpu")
    if gpu:
        total += estimate_gpu_tdp_w(gpu["specs"].get("memory_gb"), config)
    return total


def check_compatibility(parts: dict[str, dict[str, Any]], config: dict[str, Any]) -> list[str]:
    """Returns a list of human-readable compatibility problems. Empty means OK."""
    issues: list[str] = []

    cpu = parts.get("cpu")
    mobo = parts.get("motherboard")
    ram = parts.get("ram")
    gpu = parts.get("gpu")
    psu = parts.get("psu")
    case = parts.get("case")
    cooler = parts.get("cooler")

    if cpu and mobo:
        cpu_socket = cpu["specs"].get("socket")
        mobo_socket = mobo["specs"].get("socket")
        if not socket_matches(cpu_socket, mobo_socket):
            issues.append(f"сокет CPU ({cpu_socket}) не совпадает с сокетом платы ({mobo_socket})")

    if mobo and ram:
        mobo_ram = mobo["specs"].get("ram_type")
        ram_type = ram["specs"].get("ram_type")
        # Fail closed: if either side's RAM type couldn't be determined, an
        # automated search will happily exploit that gap and pair, say, a
        # DDR5-only board with leftover DDR3 the check silently let through.
        if not mobo_ram or not ram_type:
            issues.append(f"не удалось проверить тип памяти (плата: {mobo_ram or '?'}, модуль: {ram_type or '?'})")
        elif mobo_ram != ram_type:
            issues.append(f"память {ram_type} не подходит плате с поддержкой {mobo_ram}")

    if cpu and cooler:
        cpu_socket = cpu["specs"].get("socket")
        cooler_sockets = cooler["specs"].get("sockets") or []
        if cpu_socket and cooler_sockets and not any(socket_matches(cpu_socket, s) for s in cooler_sockets):
            issues.append(f"кулер не поддерживает сокет CPU {cpu_socket} (поддерживает: {', '.join(cooler_sockets)})")
        cooler_tdp = cooler["specs"].get("tdp_w")
        cpu_tdp = cpu["specs"].get("tdp_w")
        if cooler_tdp and cpu_tdp and cooler_tdp < cpu_tdp:
            issues.append(f"кулер рассчитан на TDP {cooler_tdp} Вт, а CPU выделяет до {cpu_tdp} Вт")

    if case and mobo:
        supported = case["specs"].get("supported_form_factors")
        mobo_ff = mobo["specs"].get("form_factor")
        if supported and mobo_ff and mobo_ff not in supported:
            issues.append(f"корпус не поддерживает форм-фактор платы {mobo_ff} (поддерживает: {', '.join(supported)})")

    if case and gpu:
        max_len = case["specs"].get("max_gpu_length_mm")
        gpu_len = gpu["specs"].get("length_mm")
        if max_len and gpu_len and gpu_len > max_len:
            issues.append(f"видеокарта длиннее допустимого в корпусе ({gpu_len} мм > {max_len} мм)")

    if case and cooler:
        max_height = case["specs"].get("max_cooler_height_mm")
        cooler_height = cooler["specs"].get("height_mm")
        if max_height and cooler_height and cooler_height > max_height:
            issues.append(f"кулер выше допустимого в корпусе ({cooler_height} мм > {max_height} мм)")

    if psu:
        required = estimate_system_power_w(parts, config) * float(config.get("psu_headroom_ratio", 1.3))
        wattage = psu["specs"].get("wattage_w")
        if wattage and wattage < required:
            issues.append(f"БП {wattage} Вт меньше расчётной потребности с запасом ({required:.0f} Вт)")

    return issues
