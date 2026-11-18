"""Parsing helpers for duty-rate text (US HTS columns and UK Trade Tariff duty expressions)."""
import re

_NUM = r"(\d+(?:\.\d+)?)"
_SPECIFIC_CENTS = re.compile(rf"^{_NUM}\s*¢\s*/\s*(.+)$")
_SPECIFIC_DOLLARS = re.compile(rf"^\$\s*{_NUM}\s*/\s*(.+)$")
_ADV = re.compile(rf"^{_NUM}\s*%$")

# Units that a caller can supply a quantity for. Anything else stays unparsed (returned as raw text only).
UNIT_ALIASES = {"kg": "kg", "kg.": "kg", "no.": "item", "no": "item", "each": "item", "pr.": "pair", "doz.": "dozen",
                "liter": "liter", "l": "liter", "t": "tonne"}


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("¢", "¢")).strip()


def parse_us_rate(text: str | None) -> dict:
    """Parse one US HTS rate cell.

    Returns {"text": raw, "adValoremPct": float|None, "components": [...]|None, "parsed": bool}.
    adValoremPct is only set when the WHOLE rate is a single ad-valorem percentage (or "Free" -> 0.0).
    components is set when every '+'-separated part is either "N%" or "N¢/unit" / "$N/unit" with a known unit.
    """
    raw = _clean(text)
    out = {"text": raw or None, "adValoremPct": None, "components": None, "parsed": False}
    if not raw:
        return out
    if raw.lower() == "free":
        out.update(adValoremPct=0.0, components=[{"type": "ad_valorem", "pct": 0.0}], parsed=True)
        return out
    comps = []
    for part in [p.strip() for p in raw.split("+")]:
        m = _ADV.match(part)
        if m:
            comps.append({"type": "ad_valorem", "pct": float(m.group(1))})
            continue
        m = _SPECIFIC_CENTS.match(part) or _SPECIFIC_DOLLARS.match(part)
        unit = UNIT_ALIASES.get(m.group(2).strip().lower()) if m else None
        if not m or not unit:
            return out  # ambiguous: keep raw text only
        amount = float(m.group(1)) / (100 if "¢" in part else 1)
        comps.append({"type": "specific", "usdPerUnit": round(amount, 6), "unit": unit})
    out["components"] = comps
    out["parsed"] = True
    if len(comps) == 1 and comps[0]["type"] == "ad_valorem":
        out["adValoremPct"] = comps[0]["pct"]
    return out


def parse_us_special(text: str | None) -> list[dict]:
    """'Free (A+,AU,BH) 5.5% (JP)' -> [{"rate": "Free", "programs": ["A+","AU","BH"], ...parsed}, ...]."""
    raw = _clean(text)
    res = []
    for m in re.finditer(r"([^()]+?)\s*\(([^)]*)\)", raw):
        rate = m.group(1).strip()
        progs = [p.strip() for p in m.group(2).split(",") if p.strip()]
        res.append({"rate": rate, "programs": progs, "adValoremPct": parse_us_rate(rate)["adValoremPct"]})
    return res


def parse_uk_duty(verbose: str | None) -> float | None:
    """UK verbose duty '12.00%' -> 12.0. Compound/specific expressions -> None (raw text is returned separately)."""
    m = _ADV.match(_clean(verbose))
    return float(m.group(1)) if m else None


def duty_from_components(components: list[dict] | None, value: float, weight_kg: float | None,
                         quantity: float | None) -> tuple[float | None, str | None]:
    """Compute duty from parsed components. Returns (amount, problem)."""
    if components is None:
        return None, "rate text could not be parsed unambiguously"
    total = 0.0
    for c in components:
        if c["type"] == "ad_valorem":
            total += value * c["pct"] / 100
        elif c["unit"] == "kg":
            if weight_kg is None:
                return None, "specific rate per kg: pass weight_kg"
            total += c["usdPerUnit"] * weight_kg
        elif c["unit"] == "item":
            if quantity is None:
                return None, "specific rate per item: pass quantity"
            total += c["usdPerUnit"] * quantity
        else:
            return None, f"specific rate per {c['unit']}: not computed"
    return round(total, 2), None
