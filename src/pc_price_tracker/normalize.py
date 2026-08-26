"""Turn a RawOffer into a canonical NormalizedProduct.

Approach (rule-based, no LLM, per project brief):
  1. Resolve brand — prefer the source's structured vendor field
     (RawOffer.brand_hint), fall back to a regex/synonym lookup over the title.
  2. Strip the vendor part-number out of the title (it's kept separately in
     specs.vendor_code and folded into the key for uniqueness).
  3. Strip the resolved brand name and generic chip-brand noise words
     (NVIDIA/GeForce/AMD/Radeon/...) that add no identity once the brand is
     already known.
  4. Apply a small set of targeted synonym fixes for glued/miscased chip
     names (e.g. "RTX5070Ti" -> "RTX 5070 TI") — deliberately narrow rather
     than a blanket letter/digit splitter, which would mangle legitimate
     alphanumeric model numbers like "7800X3D".
  5. Tokenize what's left into the display model name and a matching key.
  6. Pull category-specific compatibility-relevant specs out of the
     source's free-text spec line (`brief`) with per-category regexes.

Anything that can't be resolved (no brand, empty title) raises
NormalizationError; the caller is expected to route that offer into the
`unmatched` table instead of guessing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from pc_price_tracker.constants import PACKAGING_WORDS
from pc_price_tracker.models import NormalizedProduct, RawOffer

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"


class NormalizationError(ValueError):
    pass


def _load_synonyms() -> dict[str, Any]:
    path = _CONFIG_DIR / "synonyms.yaml"
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


_SYNONYMS = _load_synonyms()
BRAND_ALIASES: dict[str, str] = _SYNONYMS["brand_aliases"]
NOISE_WORDS: list[str] = _SYNONYMS["noise_words"]

_VENDOR_CODE_RE = re.compile(r"\(([^()]+)\)")
# No trailing \b: \d and a following letter are both "word" characters, so a
# boundary can never occur between them — which is exactly the glued case
# this is meant to catch ("RTX5070Ti"). \d{3,4} already bounds the digit run.
_CHIP_PREFIX_RE = re.compile(r"\b(RTX|GTX|RX|GT|Arc)\s*-?\s*(\d{3,4})", re.IGNORECASE)
_SUFFIX_GLUE_RE = re.compile(r"(?<=\d)(Ti|Super|XTX|XT|OC)\b", re.IGNORECASE)
_SUFFIX_CANON = {"ti": "TI", "super": "SUPER", "oc": "OC", "xtx": "XTX", "xt": "XT"}
_SUFFIX_WORD_RE = re.compile(r"\b(Ti|Super|XTX|XT|OC)\b", re.IGNORECASE)


def _resolve_brand(raw: RawOffer) -> str:
    if raw.brand_hint and raw.brand_hint.strip():
        return raw.brand_hint.strip()
    lowered = raw.title.lower()
    for token, canonical in BRAND_ALIASES.items():
        if re.search(rf"\b{re.escape(token)}\b", lowered):
            return canonical
    raise NormalizationError(f"could not resolve brand for title: {raw.title!r}")


def _extract_vendor_code(text: str) -> tuple[str, str | None]:
    for match in _VENDOR_CODE_RE.finditer(text):
        candidate = match.group(1).strip()
        if " " not in candidate and len(candidate) >= 4 and any(ch.isdigit() for ch in candidate):
            return text[: match.start()] + " " + text[match.end() :], candidate
    return text, None


def _strip_word(text: str, word: str) -> str:
    if not word:
        return text
    return re.sub(rf"\b{re.escape(word)}\b", " ", text, flags=re.IGNORECASE)


def _strip_noise_words(text: str) -> str:
    for word in NOISE_WORDS:
        text = _strip_word(text, word)
    return text


def _canonicalize_chip_names(text: str) -> str:
    text = _CHIP_PREFIX_RE.sub(lambda m: f"{m.group(1).upper()} {m.group(2)}", text)
    text = _SUFFIX_GLUE_RE.sub(r" \1", text)
    text = _SUFFIX_WORD_RE.sub(lambda m: _SUFFIX_CANON[m.group(1).lower()], text)
    return text


def _slug(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9а-я]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")


def _tokenize_model(raw: RawOffer, brand: str) -> tuple[str, str | None, str, str | None]:
    """Returns (display_model, vendor_code, key_string, packaging).

    The key deliberately does NOT include the vendor part-number, even
    though it's a perfect SKU disambiguator when present: it's also the
    single most format-fragile field across stores (different casing,
    punctuation, or a source that just doesn't publish it at all), and
    cross-source matching — the same physical product listed by two stores
    collapsing into one product — depends on the key surviving that. The
    marketing-name tokens (AIB cooler name, OC/variant words) already
    disambiguate real SKU differences in the overwhelming majority of
    cases; see normalize_offer's RAM-specific CL-rating addition for the
    one category where that wasn't quite enough on real data.
    """
    text, vendor_code = _extract_vendor_code(raw.title)
    text = _strip_word(text, brand)
    text = _strip_noise_words(text)
    text = _canonicalize_chip_names(text)
    text = text.replace("(", " ").replace(")", " ")
    tokens = [t for t in re.split(r"[\s,]+", text) if t]

    packaging = next((t for t in tokens if t.upper() in PACKAGING_WORDS), None)

    display_model = " ".join(tokens)
    key_string = _slug("-".join(t.upper() for t in tokens))
    return display_model, vendor_code, key_string, packaging


# ---------------------------------------------------------------------------
# Category-specific spec extraction (compatibility-relevant fields only).
# All of these read from the source's free-text spec line, so they degrade
# gracefully (missing key) rather than raising when a field isn't present.
# ---------------------------------------------------------------------------

_SOCKET_TOKEN_RE = re.compile(r"^(AM\d\+?|FM\d\+?|TR\d\+?|STRX\d|STR\d|\d{3,4}X?|LGA\s?\d+)$", re.IGNORECASE)


def _canonical_socket(token: str) -> str:
    t = token.strip().upper().replace(" ", "")
    if t.isdigit():
        return f"LGA{t}"
    if re.match(r"^\d{3,4}X$", t):
        return f"LGA{t}"
    if re.match(r"^LGA\d+X?$", t):
        return t
    return t


def _clean_brief_text(brief: str | None) -> str:
    """Strip embedded HTML tags (Regard wraps some units in <noindex>...</noindex>,
    which otherwise breaks "<number> <unit>" regexes when a tag lands between them)."""
    if not brief:
        return ""
    return re.sub(r"<[^>]+>", "", brief)


def _brief_segments(brief: str | None) -> list[str]:
    clean = _clean_brief_text(brief)
    if not clean:
        return []
    return [s.strip() for s in clean.split(",")]


def _extract_cpu_specs(raw: RawOffer) -> dict[str, Any]:
    segments = _brief_segments(raw.brief)
    specs: dict[str, Any] = {}
    if segments:
        specs["socket"] = _canonical_socket(segments[0])
    clean = _clean_brief_text(raw.brief)
    m = re.search(r"TDP\s*(\d+)\s*Вт", clean, re.IGNORECASE)
    if m:
        specs["tdp_w"] = int(m.group(1))
    # Core count ("8-ядерный") is stated for every listing checked. Thread
    # count is NOT — Regard's CPU brief never mentions it at all (checked
    # across a live sample spanning AMD and Intel, including hybrid
    # P/E-core Intel chips where threads != 2*cores, so it also isn't
    # something safe to infer/guess from core count). specs["threads"] is
    # deliberately never set by this extractor; a floor that requires it
    # will always see it as unrecognized until a source that states it
    # directly is added — that's an honest reflection of the data, not a
    # bug to "fix" with a guess.
    m = re.search(r"(\d+)-ядерный", clean, re.IGNORECASE)
    if m:
        specs["cores"] = int(m.group(1))
    return specs


def _extract_gpu_specs(raw: RawOffer) -> dict[str, Any]:
    specs: dict[str, Any] = {}
    if raw.width_mm:
        specs["length_mm"] = raw.width_mm
    m = re.search(r"(\d+)\s*GB", raw.title, re.IGNORECASE)
    if m:
        specs["memory_gb"] = int(m.group(1))
    clean = _clean_brief_text(raw.brief)
    m = re.search(r"PCI-E\s*([\d.]+)", clean, re.IGNORECASE)
    if m:
        specs["pcie"] = m.group(1)
    return specs


def _extract_motherboard_specs(raw: RawOffer) -> dict[str, Any]:
    segments = _brief_segments(raw.brief)
    specs: dict[str, Any] = {}
    if segments:
        specs["form_factor"] = segments[0].upper()
    for seg in segments:
        m = re.match(r"сокет\s+(.+)", seg, re.IGNORECASE)
        if m:
            specs["socket"] = _canonical_socket(m.group(1))
        m = re.match(r"чипсет\s+\S+\s+(\S+)", seg, re.IGNORECASE)
        if m:
            specs["chipset"] = m.group(1)
        # re.search, not re.match: the segment is like "4xDDR5", so "DDR5"
        # isn't at position 0.
        m = re.search(r"(DDR\d)", seg, re.IGNORECASE)
        if m:
            specs["ram_type"] = m.group(1).upper()
    return specs


def _extract_ram_specs(raw: RawOffer) -> dict[str, Any]:
    specs: dict[str, Any] = {}
    m = re.match(r"(\d+)\s*GB", raw.title, re.IGNORECASE)
    if m:
        specs["capacity_gb"] = int(m.group(1))
    m = re.search(r"(DDR\d)", raw.title, re.IGNORECASE)
    if m:
        specs["ram_type"] = m.group(1).upper()
    else:
        # Regard writes older DDR2/DDR3 kits with a Roman-numeral
        # generation ("DDR-III 1600MHz ...") instead of "DDR3" — without
        # this, ~5% of the RAM catalog (all pre-DDR4 stock) silently had
        # no ram_type, which then fails compatibility checks closed rather
        # than actually comparing against the motherboard's type.
        m = re.search(r"\bDDR-?\s*(I{1,3}|IV)\b", raw.title, re.IGNORECASE)
        if m:
            specs["ram_type"] = {"I": "DDR1", "II": "DDR2", "III": "DDR3", "IV": "DDR4"}[m.group(1).upper()]
    m = re.search(r"(\d+)\s*MHz", raw.title, re.IGNORECASE)
    if m:
        specs["speed_mhz"] = int(m.group(1))
    # Regard only writes the "N модуля" segment for multi-stick kits; a
    # single-stick listing's brief omits it entirely rather than saying
    # "1 модуль" — so a miss here means exactly one module, not unknown data.
    clean = _clean_brief_text(raw.brief)
    m = re.search(r"(\d+)\s*модул", clean, re.IGNORECASE)
    specs["modules"] = int(m.group(1)) if m else 1
    # CAS latency: the one real collision class found when the normalized
    # key stopped including the vendor code (see _tokenize_model) — two
    # kits can be identical in brand/capacity/speed/marketing name and
    # differ only by timing (CL30 vs CL36), which Regard puts in `brief`
    # but not in the title.
    m = re.search(r"\bCL\s?(\d{1,3})\b", clean, re.IGNORECASE)
    if m:
        specs["cl"] = int(m.group(1))
    return specs


def _extract_ssd_specs(raw: RawOffer) -> dict[str, Any]:
    specs: dict[str, Any] = {}
    m = re.match(r"(\d+)\s*(TB|GB)", raw.title, re.IGNORECASE)
    if m:
        value, unit = int(m.group(1)), m.group(2).upper()
        specs["capacity_gb"] = value * 1024 if unit == "TB" else value
    clean = _clean_brief_text(raw.brief)
    if re.search(r"\bM\.2\b", clean, re.IGNORECASE):
        specs["form_factor"] = "M.2"
    elif re.search(r'2\.5', clean):
        specs["form_factor"] = "2.5"
    if re.search(r"\bNVMe\b", clean, re.IGNORECASE):
        specs["interface"] = "NVMe"
    elif re.search(r"\bSATA\b", clean, re.IGNORECASE):
        specs["interface"] = "SATA"
    # Only present when the source explicitly states a DRAM cache size
    # ("кэш - 1024 МБ") — verified against real listings that this is
    # reliably present on known DRAM-cached drives (e.g. Samsung 990 PRO)
    # and absent on known DRAM-less ones (WD Black SN7100, Kingston NV3).
    # Its absence means "not stated", not "confirmed no cache" — callers
    # that require a DRAM cache should only reject on an explicit 0/miss,
    # not treat a missing key as a failure.
    m = re.search(r"кэш\s*-?\s*(\d+)\s*МБ", clean, re.IGNORECASE)
    if m:
        specs["dram_cache_mb"] = int(m.group(1))
    return specs


def _extract_psu_specs(raw: RawOffer) -> dict[str, Any]:
    specs: dict[str, Any] = {}
    clean = _clean_brief_text(raw.brief)
    m = re.match(r"(\d+)\s*W", raw.title, re.IGNORECASE)
    if m:
        specs["wattage_w"] = int(m.group(1))
    else:
        m = re.search(r"мощность\s*(\d+)\s*Вт", clean, re.IGNORECASE)
        if m:
            specs["wattage_w"] = int(m.group(1))
    m = re.search(r"80\s*PLUS\s*(\w+)?", clean, re.IGNORECASE)
    if m:
        specs["certification"] = ("80 PLUS " + m.group(1)) if m.group(1) else "80 PLUS"
    return specs


_CASE_FORM_FACTORS = {"ATX", "MATX", "MICRO-ATX", "M-ATX", "MINI-ITX", "E-ATX", "EATX"}


def _extract_case_specs(raw: RawOffer) -> dict[str, Any]:
    segments = _brief_segments(raw.brief)
    specs: dict[str, Any] = {}
    supported = [s.upper() for s in segments if s.upper() in _CASE_FORM_FACTORS]
    if supported:
        specs["supported_form_factors"] = supported
    clean = _clean_brief_text(raw.brief)
    m = re.search(r"длина видеокарты до\s*(\d+)\s*мм", clean, re.IGNORECASE)
    if m:
        specs["max_gpu_length_mm"] = int(m.group(1))
    m = re.search(r"высота кулера до\s*(\d+)\s*мм", clean, re.IGNORECASE)
    if m:
        specs["max_cooler_height_mm"] = int(m.group(1))
    # Fan count: Regard's case `brief` never states this (checked against
    # a live sample of 120 listings, zero mentions) — the only extractable
    # signal is DeepCool's own "<N>F" model-name suffix ("CC550 4F" = 4
    # fans included), which covers a small minority of listings. Its
    # absence means "not stated", not "confirmed 0 fans" — callers that
    # require a fan count should treat a miss as unknown, not a failure.
    m = re.search(r"\b(\d+)F\b", raw.title)
    if m:
        specs["fan_count"] = int(m.group(1))
    return specs


def _extract_cooler_specs(raw: RawOffer) -> dict[str, Any]:
    segments = _brief_segments(raw.brief)
    specs: dict[str, Any] = {}
    sockets: list[str] = []
    collecting = False
    for seg in segments:
        if not collecting:
            m = re.match(r"socket\s+(.+)", seg, re.IGNORECASE)
            if m:
                collecting = True
                first = m.group(1).strip()
                if _SOCKET_TOKEN_RE.match(first):
                    sockets.append(_canonical_socket(first))
            continue
        if _SOCKET_TOKEN_RE.match(seg):
            sockets.append(_canonical_socket(seg))
        else:
            break
    if sockets:
        specs["sockets"] = sockets
    clean = _clean_brief_text(raw.brief)
    m = re.search(r"высота\s*(\d+)\s*мм", clean, re.IGNORECASE)
    if m:
        specs["height_mm"] = int(m.group(1))
    m = re.search(r"TDP\s*(\d+)\s*Вт", clean, re.IGNORECASE)
    if m:
        specs["tdp_w"] = int(m.group(1))
    return specs


_SPEC_EXTRACTORS = {
    "cpu": _extract_cpu_specs,
    "gpu": _extract_gpu_specs,
    "motherboard": _extract_motherboard_specs,
    "ram": _extract_ram_specs,
    "ssd": _extract_ssd_specs,
    "psu": _extract_psu_specs,
    "case": _extract_case_specs,
    "cooler": _extract_cooler_specs,
}


def extract_specs(raw: RawOffer) -> dict[str, Any]:
    extractor = _SPEC_EXTRACTORS.get(raw.category)
    if extractor is None:
        return {}
    return extractor(raw)


def normalize_offer(raw: RawOffer) -> NormalizedProduct:
    if not raw.title or not raw.title.strip():
        raise NormalizationError("empty title")

    brand = _resolve_brand(raw)
    display_model, vendor_code, key_string, packaging = _tokenize_model(raw, brand)
    if not key_string:
        raise NormalizationError(f"empty model after tokenizing title: {raw.title!r}")

    specs = extract_specs(raw)
    if vendor_code:
        specs["vendor_code"] = vendor_code
    if packaging:
        specs["packaging"] = packaging

    if raw.category == "ram" and specs.get("cl"):
        key_string = f"{key_string}-cl{specs['cl']}"

    normalized_key = f"{raw.category}:{_slug(brand)}:{key_string}"
    return NormalizedProduct(
        category=raw.category,
        brand=brand,
        model=display_model,
        normalized_key=normalized_key,
        specs=specs,
    )
