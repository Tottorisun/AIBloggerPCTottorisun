from typing import Literal

Category = Literal["cpu", "gpu", "motherboard", "ram", "ssd", "psu", "case", "cooler"]

CATEGORIES: tuple[Category, ...] = (
    "cpu",
    "gpu",
    "motherboard",
    "ram",
    "ssd",
    "psu",
    "case",
    "cooler",
)

# Packaging / noise words stripped from titles before tokenizing the model.
# NB: "KIT" is intentionally excluded — a 2x16GB kit is a different SKU from
# a single 32GB module, so it stays part of the model identity.
PACKAGING_WORDS = {"OEM", "BOX", "TRAY", "RETAIL"}
