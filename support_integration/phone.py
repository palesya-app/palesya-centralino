"""Normalizzazione conservativa dei numeri per il riconoscimento chiamante."""
import re


def _digits(value):
    return re.sub(r"\D+", "", str(value or ""))


def normalize_phone(value, default_country_code="39"):
    digits = _digits(value)
    if not digits:
        return ""
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith(default_country_code) and len(digits) >= len(default_country_code) + 7:
        return "+" + digits
    # I numeri italiani locali sono il caso richiesto dal brief. Per altri
    # paesi non si inventa il prefisso: si conserva il valore internazionale.
    if default_country_code == "39" and len(digits) in {9, 10} and not digits.startswith("0"):
        return "+39" + digits
    return "+" + digits


def phone_variants(value, default_country_code="39"):
    canonical = normalize_phone(value, default_country_code)
    if not canonical:
        return set()
    digits = canonical[1:]
    variants = {canonical, digits}
    if digits.startswith(default_country_code):
        national = digits[len(default_country_code):]
        variants.update({national, "0" + national})
    if len(digits) >= 9:
        variants.add(digits[-9:])
    return {item for item in variants if item}


def phones_match(left, right, default_country_code="39"):
    left_variants = phone_variants(left, default_country_code)
    right_variants = phone_variants(right, default_country_code)
    return bool(left_variants & right_variants)
