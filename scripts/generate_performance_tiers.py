"""One-time / quarterly authoring tool for data/performance_tiers.yaml.

The actual reference file is meant to be hand-edited going forward (see its
own header comment) — this script exists so that refreshing it against a
changed catalog is a re-run instead of 400 manual lookups. It:

  1. Reads every CPU/GPU product currently in the DB.
  2. Reduces each product's display model to a "chip key" (packaging like
     OEM/BOX stripped for CPUs; AIB marketing name stripped down to just
     the chip line for GPUs, e.g. "RTX 5070 Ti GamingPro OC 16GB" -> "RTX
     5070 TI" — many different AIB cards share one chip and should share
     one tier).
  3. Looks the chip key up in the curated CHIP_TIERS tables below (hand-set
     from known relative desktop CPU/GPU performance, gaming-leaning for
     GPUs, blended multi-core+gaming for CPUs, and workstation cards tiered
     by relative compute class rather than gaming since that's not their
     job).
  4. Writes data/performance_tiers.yaml, one entry per normalized_key.
  5. Prints any product whose chip key isn't in the table — those
     deliberately fall through with no tier, exactly like a future new SKU
     the reference file hasn't been updated for yet, so the builder's
     "no tier -> logged, not selected" path stays exercised for real.

Run: python scripts/generate_performance_tiers.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pc_price_tracker import db as db_module  # noqa: E402

DB_PATH = PROJECT_ROOT / "data" / "pc_price_tracker.db"
OUT_PATH = PROJECT_ROOT / "data" / "performance_tiers.yaml"

# ---------------------------------------------------------------------------
# Curated chip -> tier tables (1-100). Hand-set from well-known relative
# performance across generations; not derived from any single benchmark
# suite. See the generated file's header for the update policy.
# ---------------------------------------------------------------------------

CPU_TIERS: dict[str, int] = {
    # Ancient / office-only
    "A6-7480": 3,
    "ATHLON 3000G": 8,
    "ATHLON PRO 300GE": 8,
    "CELERON G5905": 10,
    "CELERON G6900": 13,
    "PENTIUM G4400": 7,
    "PENTIUM G4560": 10,
    "PENTIUM GOLD G6400": 14,
    "PENTIUM GOLD G6405": 14,
    "PENTIUM GOLD G7400": 17,
    # Entry quad-core
    "RYZEN 5 3400G": 21,
    "RYZEN 3 3200G": 18,
    "RYZEN 3 4300G": 22,
    "RYZEN 3 PRO 4350G": 23,
    "RYZEN 3 PRO 5350G": 25,
    "RYZEN 3 PRO 5350GE": 24,
    "CORE I3 - 10100": 24,
    "CORE I3 - 10100F": 24,
    "CORE I3 - 10105": 25,
    "CORE I3 - 10105F": 25,
    "CORE I3 - 12100": 33,
    "CORE I3 - 12100F": 33,
    "CORE I3 - 13100": 35,
    "CORE I3 - 13100F": 35,
    "CORE I3 - 14100": 36,
    "CORE I3 - 14100F": 36,
    # Older i5/i7/HEDT (pre-12th-gen)
    "CORE I7 - 6700": 28,
    "CORE I5 - 10400": 30,
    "CORE I5 - 10400F": 30,
    "CORE I5 - 10500": 32,
    "CORE I5 - 10600K": 35,
    "CORE I5 - 11400": 37,
    "CORE I5 - 11400F": 37,
    "CORE I7 - 8700": 40,
    "CORE I7 - 9700": 42,
    "CORE I9 - 10900X": 48,
    "CORE I9 - 10920X": 50,
    "CORE I9 - 10940X": 52,
    # Ryzen 5000 (Zen 3, AM4)
    "RYZEN 5 3500X": 28,
    "RYZEN 5 4500": 34,
    "RYZEN 5 5500": 38,
    "RYZEN 5 5500GT": 39,
    "RYZEN 5 5500X3D": 44,
    "RYZEN 5 5600": 47,
    "RYZEN 5 5600 XT": 48,
    "RYZEN 5 5600GT": 46,
    "RYZEN 5 5600X": 50,
    "RYZEN 5 PRO 5650G": 45,
    "RYZEN 5 PRO 5655G": 46,
    "RYZEN 7 5700": 53,
    "RYZEN 7 5700G": 48,
    "RYZEN 7 5700X": 55,
    "RYZEN 7 5800X": 58,
    "RYZEN 7 PRO 5755G": 50,
    "RYZEN 9 5900X": 68,
    "RYZEN 9 5950X": 74,
    # Intel 12th gen (Alder Lake)
    "CORE I5 - 12400": 45,
    "CORE I5 - 12400F": 45,
    "CORE I5 - 12500": 47,
    "CORE I5 - 12600K": 55,
    "CORE I5 - 12600KF": 55,
    "CORE I7 - 12700": 62,
    "CORE I7 - 12700F": 62,
    "CORE I7 - 12700K": 68,
    "CORE I7 - 12700KF": 68,
    "CORE I9 - 12900K": 78,
    "CORE I9 - 12900KF": 78,
    # Intel 13th gen (Raptor Lake)
    "CORE I5 - 13400": 48,
    "CORE I5 - 13400F": 48,
    "CORE I5 - 13500": 52,
    "CORE I5 - 13600KF": 62,
    "CORE I7 - 13700F": 72,
    "CORE I7 - 13700K": 76,
    "CORE I7 - 13700KF": 76,
    "CORE I9 - 13900": 84,
    "CORE I9 - 13900F": 84,
    "CORE I9 - 13900K": 90,
    "CORE I9 - 13900KF": 90,
    "CORE I9 - 13900KS": 92,
    # Intel 14th gen (Raptor Lake Refresh)
    "CORE I5 - 14400": 50,
    "CORE I5 - 14400F": 50,
    "CORE I5 - 14500": 54,
    "CORE I5 - 14600K": 64,
    "CORE I5 - 14600KF": 64,
    "CORE I7 - 14700": 74,
    "CORE I7 - 14700F": 74,
    "CORE I7 - 14700K": 79,
    "CORE I7 - 14700KF": 79,
    "CORE I9 - 14900": 85,
    "CORE I9 - 14900F": 85,
    "CORE I9 - 14900K": 91,
    "CORE I9 - 14900KF": 91,
    "CORE I9 - 14900KS": 93,
    # Intel Core Ultra 200 (Arrow Lake)
    "CORE ULTRA 5 225": 52,
    "CORE ULTRA 5 225F": 52,
    "CORE ULTRA 5 235": 56,
    "CORE ULTRA 5 245K": 66,
    "CORE ULTRA 5 245KF": 66,
    "CORE ULTRA 5 250K PLUS": 68,
    "CORE ULTRA 5 250KF PLUS": 68,
    "CORE ULTRA 7 265": 78,
    "CORE ULTRA 7 265K": 78,
    "CORE ULTRA 7 265KF": 78,
    "CORE ULTRA 7 270K PLUS": 82,
    "CORE ULTRA 9 285": 90,
    "CORE ULTRA 9 285K": 90,
    # Ryzen 7000/8000/9000 (Zen 4/5, AM5)
    "RYZEN 5 7400F": 52,
    "RYZEN 5 7500F": 55,
    "RYZEN 5 7500X3D": 60,
    "RYZEN 5 7600": 58,
    "RYZEN 5 7600X": 61,
    "RYZEN 5 8400F": 50,
    "RYZEN 5 8500G": 47,
    "RYZEN 5 8600G": 52,
    "RYZEN 5 PRO 8500G": 48,
    "RYZEN 5 PRO 8600G": 52,
    "RYZEN 5 9500F": 60,
    "RYZEN 5 9600X": 65,
    "RYZEN 7 7700": 68,
    "RYZEN 7 7700X": 71,
    "RYZEN 7 7800X3D": 82,
    "RYZEN 7 8700F": 66,
    "RYZEN 7 8700G": 65,
    "RYZEN 7 9700X": 76,
    "RYZEN 7 9800X3D": 91,
    "RYZEN 7 9850X3D": 92,
    "RYZEN 9 7900X": 80,
    "RYZEN 9 7950X3D": 90,
    "RYZEN 9 9900X": 84,
    "RYZEN 9 9900X3D": 89,
    "RYZEN 9 9950X": 94,
    "RYZEN 9 9950X3D": 97,
    "RYZEN 9 9950X3D2 DUAL EDITION": 98,
}

GPU_TIERS: dict[str, int] = {
    # Ancient / near-unusable for anything current
    "GT 210": 1,
    # The 2010 "GeForce 210" is officially named without a GT/GTX prefix at
    # all, unlike every later card — after "GeForce" is stripped as noise,
    # what's left in the title is a bare "210".
    "210": 1,
    "R5 220": 1,
    "R5 230": 2,
    "GT 610": 2,
    "GT 710": 2,
    "GT 240": 3,
    "GT 420": 3,
    "GT 220": 1,
    "FIREPRO S400": 3,
    "R7 350": 4,
    "R9 370": 5,
    "GT 730": 4,
    "GT 740": 5,
    "RX 550": 6,
    "GT 1030": 6,
    "GTX 750": 8,
    "GTX 750 TI": 10,
    "ARC A310": 8,
    "RX 560": 9,
    "ARC A380": 13,
    # Entry 1080p
    "GTX 1050": 12,
    "GTX 1050 TI": 15,
    "RX 580": 16,
    "GTX 1650": 18,
    "RX 5500 XT": 19,
    "RX 6500 XT": 20,
    "GTX 1650 SUPER": 21,
    "RTX 3050": 23,
    "GTX 1660 SUPER": 26,
    "GTX 1660 TI": 27,
    "RTX 2060": 28,
    "RTX 5050": 30,
    "QUADRO RTX A2000": 30,
    "RTX 2060 SUPER": 32,
    "ARC B570": 33,
    "QUADRO P6000": 35,
    "RX 5700 XT": 36,
    "RX 7600": 37,
    "RX 6650 XT": 38,
    "RTX 5060": 39,
    "RX 7650 GRE": 40,
    "QUADRO RTX 2000 ADA": 38,
    # Mid
    "RTX 3060": 34,
    "RTX 3060 TI": 40,
    "RTX 5060 TI": 47,
    "RX 9060 XT": 45,
    "RTX 3070": 46,
    "RTX 3070 TI": 49,
    "QUADRO RTX A4000": 47,
    "QUADRO RTX PRO 2000": 42,
    "RTX 5070": 56,
    "QUADRO RTX A4500": 55,
    "QUADRO RTX PRO 4000": 58,
    # Upper-mid / high
    "RX 9070 GRE": 58,
    "RX 9070": 65,
    "RTX 5070 TI": 68,
    "QUADRO RTX 5000 ADA": 62,
    "QUADRO RTX PRO 4500": 70,
    "RX 9070 XT": 72,
    "RTX 4080": 78,
    "QUADRO RTX PRO 5000": 80,
    "RTX 5080": 85,
    # Flagship
    "RTX 4090": 92,
    "QUADRO RTX PRO 6000": 95,
    "RTX 5090": 100,
    # Very old/entry pro cards not really usable as a "build" choice, but
    # present in the catalog — tiered low rather than left out, since they
    # ARE real, buyable, rankable cards (unlike a genuinely unknown future
    # SKU, which is what the "no tier" path is actually for).
    "QUADRO RTX A400": 10,
    "QUADRO RTX 5880 ADA": 65,
    "QUADRO RTX 5000": 62,
    "QUADRO RTX 2000": 38,
}

_PACKAGING_RE = re.compile(r"\s+(OEM|BOX|TRAY|RETAIL)\b.*$", re.IGNORECASE)
_GPU_CHIP_RE = re.compile(r"\b(RTX|GTX|RX|GT|ARC|R5|R7|R9)\s*([AB]?\d{3,4})\s*(TI|SUPER|XT|XTX|GRE)?\b", re.IGNORECASE)
_QUADRO_RE = re.compile(
    # "RTX PRO <n>" workstation cards drop the vendor/generation words that
    # can appear between the number and "Blackwell" ("... 5000 PNY 48GB",
    # "... 5000 Blackwell 48GB") — the number alone already identifies the
    # chip, so BLACKWELL isn't required to match.
    r"QUADRO\s+(RTX\s+PRO\s+\d+|RTX\s+A?\d+\s*(?:ADA)?|P\d+)",
    re.IGNORECASE,
)


def cpu_chip_key(model: str) -> str:
    return _PACKAGING_RE.sub("", model).strip().upper()


def gpu_chip_key(model: str) -> str | None:
    upper = model.upper()
    m = _QUADRO_RE.search(upper)
    if m:
        return re.sub(r"\s+", " ", m.group(0)).strip()
    m = re.search(r"FIREPRO\s+\S+", upper)
    if m:
        return re.sub(r"\s+", " ", m.group(0)).strip()
    m = _GPU_CHIP_RE.search(upper)
    if m:
        prefix, num, suffix = m.group(1), m.group(2), m.group(3) or ""
        return f"{prefix} {num}" + (f" {suffix}" if suffix else "")
    # A handful of pre-2012 NVIDIA cards (e.g. "GeForce 210") were officially
    # named with no GT/GTX prefix — after "GeForce" is stripped as noise,
    # what's left is a bare number.
    m = re.match(r"(\d{2,3})\b", upper)
    if m:
        return m.group(1)
    return None


def _load_existing() -> dict[str, dict]:
    if not OUT_PATH.exists():
        return {}
    with OUT_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    # One-time migration from the old flat "key: tier_int" format (no
    # source field yet) — everything written before this had no
    # verification tracking, so it's all "estimated" by definition.
    migrated = {}
    for key, value in data.items():
        if isinstance(value, dict):
            migrated[key] = value
        else:
            migrated[key] = {"tier": value, "source": "estimated"}
    return migrated


def main() -> None:
    conn = db_module.connect(DB_PATH)
    rows = conn.execute(
        "SELECT category, normalized_key, brand, model FROM products WHERE category IN ('cpu', 'gpu') ORDER BY category, model"
    ).fetchall()

    # Entries already in the file are left exactly as they are — a human
    # may have hand-verified a tier (source: verified) or hand-tweaked an
    # estimate, and a regen must not silently clobber that. Only genuinely
    # new normalized_keys get a fresh auto-derived (source: estimated) entry.
    existing = _load_existing()

    entries: dict[str, dict] = {}
    new_count = 0
    missed: list[tuple[str, str, str]] = []

    for row in rows:
        key = row["normalized_key"]
        if key in existing:
            entries[key] = existing[key]
            continue

        if row["category"] == "cpu":
            chip = cpu_chip_key(row["model"])
            tier = CPU_TIERS.get(chip)
        else:
            chip = gpu_chip_key(row["model"])
            tier = GPU_TIERS.get(chip) if chip else None

        if tier is None:
            missed.append((row["category"], row["model"], chip or "?"))
            continue
        entries[key] = {"tier": tier, "source": "estimated"}
        new_count += 1

    lines = [
        "# Справочник условной производительности CPU/GPU, шкала 1-100.",
        "#",
        "# ОБНОВЛЯЕТСЯ ВРУЧНУЮ, ориентировочно раз в квартал — не пересчитывается",
        "# автоматически при каждом scrape. Значения — экспертная оценка по",
        "# известному соотношению поколений и уровней в линейке (не результат",
        "# конкретного бенчмарка): 5060 выше 3050, 7800X3D выше 5600X и т.п.",
        "# Для GPU скорость привязана к чипу, а не к конкретной AIB-плате —",
        "# разные партнёрские версии одной RTX 5070 Ti получают один tier.",
        "#",
        "# source: estimated — проставлено по общей логике поколений/уровней,",
        "#         не сверялось с внешними данными.",
        "#         verified — сверено вручную с реальными бенчмарками/обзорами;",
        "#         проставляется человеком, скрипт этого не делает и не трогает",
        "#         уже выставленные verified-записи при перегенерации.",
        "#",
        "# Позиции без tier (новая модель, которую ещё не оценили руками) не",
        "# участвуют в сборке build — попадают в лог, а не выбираются вслепую.",
        "#",
        "# Невалидированные записи, отсортированные по частоте появления в",
        "# базе (что стоит сверить в первую очередь):",
        "#   python -m pc_price_tracker.cli tiers --unverified --top 40",
        "#",
        "# Пересобрать (добавляет только новые normalized_key, не трогает",
        "# существующие записи):",
        "#   python scripts/generate_performance_tiers.py",
        "",
    ]
    for row in rows:
        key = row["normalized_key"]
        if key in entries:
            entry = entries[key]
            lines.append(f"{key}: {{tier: {entry['tier']}, source: {entry['source']}}}  # {row['brand']} {row['model']}")

    OUT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"Written {len(entries)} tiered entries to {OUT_PATH} ({new_count} new)")
    print(f"Skipped (no chip tier found): {len(missed)}")
    seen_chips = set()
    for category, model, chip in missed:
        if chip in seen_chips:
            continue
        seen_chips.add(chip)
        print(f"  [{category}] chip={chip!r} e.g. model={model!r}")


if __name__ == "__main__":
    main()
