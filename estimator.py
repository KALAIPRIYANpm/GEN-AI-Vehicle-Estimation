"""Turn raw detections into line items and a costed estimate."""
from database import part_price

SEVERITY_ORDER = {"minor": 0, "moderate": 1, "severe": 2}
# Glass and lamps are replaced rather than repaired, whatever the size of the damage.
ALWAYS_REPLACE = {"Light", "Windshield"}


def suggest_severity(part, area, confidence):
    """Rough heuristic from how much of the frame the damaged part fills and model confidence.
    It is only a suggestion; users and admins can change it per item."""
    score = area * (0.6 + 0.4 * confidence)
    if score > 0.18:
        return "severe"
    if score > 0.06:
        return "moderate"
    return "minor"


def suggest_action(part, severity):
    if part in ALWAYS_REPLACE or severity == "severe":
        return "replace"
    return "repair"


def aggregate(per_image_detections):
    """Combine detections from several photos of the same vehicle.

    The same damaged panel usually appears in more than one angle, so a part's instance count is
    the highest count seen in any single photo, not the sum across photos."""
    parts = {}
    for dets in per_image_detections:
        counts = {}
        for d in dets:
            counts[d["part"]] = counts.get(d["part"], 0) + 1
            p = parts.setdefault(d["part"], {"part": d["part"], "instances": 0, "confidence": 0.0,
                                             "severity": "minor"})
            p["confidence"] = max(p["confidence"], d["confidence"])
            sev = suggest_severity(d["part"], d.get("area", 0), d["confidence"])
            if SEVERITY_ORDER[sev] > SEVERITY_ORDER[p["severity"]]:
                p["severity"] = sev
        for part, c in counts.items():
            parts[part]["instances"] = max(parts[part]["instances"], c)
    items = []
    for p in parts.values():
        p["action"] = suggest_action(p["part"], p["severity"])
        items.append(p)
    return sorted(items, key=lambda i: -i["confidence"])


def price_items(conn, items, brand, model, settings):
    """Fill unit_price / labour / total on each item (in place) and return the totals dict."""
    any_fallback = False
    for it in items:
        base, found = part_price(conn, brand, model, it["part"])
        any_fallback |= not found
        it["base_price"] = base
        reprice_item(it, settings)
    totals = compute_totals(items, settings)
    totals["catalogue_match"] = not any_fallback
    return totals


def reprice_item(it, settings):
    base = int(it["base_price"] or 0)
    unit = base if it["action"] == "replace" else round(base * settings["repair_pct"] / 100)
    labour = round(base * settings["labour_pct"] / 100)
    n = max(1, int(it.get("instances") or 1))
    it["unit_price"] = unit
    it["labour"] = labour
    it["total"] = (unit + labour) * n
    return it


def compute_totals(items, settings, coverage_pct=None, deductible=None):
    cov = settings["coverage_pct"] if coverage_pct is None else coverage_pct
    ded = settings["deductible"] if deductible is None else deductible
    live = [i for i in items if i.get("included", 1)]
    parts_cost = sum(int(i["unit_price"]) * max(1, int(i["instances"] or 1)) for i in live)
    labour = sum(int(i["labour"]) * max(1, int(i["instances"] or 1)) for i in live)
    gst = round((parts_cost + labour) * settings["gst_pct"] / 100)
    total = parts_cost + labour + gst
    insurance = max(0, round(total * cov / 100) - ded) if total else 0
    return {
        "parts_cost": parts_cost,
        "labour_cost": labour,
        "gst": gst,
        "total_estimate": total,
        "insurance_coverage": insurance,
        "customer_pays": total - insurance,
        "coverage_pct": cov,
        "deductible": ded,
    }
