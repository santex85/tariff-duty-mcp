"""Tariff Duty MCP server: HS/HTS code search, import duty rates and landed-cost estimates for the US and the UK.

Data: USITC Harmonized Tariff Schedule REST API (hts.usitc.gov/reststop) and UK Trade Tariff API v2
(trade-tariff.service.gov.uk/api/v2). Both are official, public and keyless. Reference data only, not customs advice.
"""
import asyncio
import logging
import os
import sys

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(__file__))
from rates import duty_from_components  # noqa: E402
from sources import Http, NotFound, SourceError, uk_rate, uk_search, us_rate, us_search  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = FastMCP("tariff-duty", host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")))

CALL_TIMEOUT = 90   # seconds per tool call
MAX_RESULTS = 25

DISCLAIMER = ("Estimate from public tariff data for information only. Not customs, tax or legal advice. Classification, "
              "origin, valuation and additional duties are decided by the customs authority; verify with a licensed "
              "customs broker or the official tariff before importing.")

# US CBP user fees, fiscal year 2026 (1 Oct 2025 - 30 Sep 2026), 19 CFR 24.23 as adjusted annually by CBP.
US_MPF_RATE = 0.3464
US_MPF_MIN, US_MPF_MAX = 33.58, 651.50
US_HMF_RATE = 0.125
US_FORMAL_ENTRY_THRESHOLD = 2500
UK_LOW_VALUE_GBP = 135


def _market(m: str) -> str:
    m = (m or "").strip().lower()
    if m in ("us", "usa", "united states"):
        return "us"
    if m in ("uk", "gb", "united kingdom", "great britain"):
        return "uk"
    raise SourceError("market must be 'us' or 'uk'")


async def _run(fn):
    http = Http()
    try:
        return await asyncio.wait_for(fn(http), timeout=CALL_TIMEOUT)
    except asyncio.TimeoutError:
        return {"error": f"timed out after {CALL_TIMEOUT}s"}
    except NotFound as e:
        return {"error": f"not found: {e}"}
    except SourceError as e:
        return {"error": str(e)}
    except Exception as e:  # network errors etc.
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        await http.close()


@mcp.tool()
async def search_hs_code(description: str, market: str = "us", limit: int = 10) -> dict:
    """Find candidate tariff classification codes for a product description.

    description: plain-English product text, e.g. "cotton t-shirts", "lithium-ion batteries", "roasted coffee".
      A code (e.g. "6109100010") is also accepted for market "uk" and returns an exact match.
    market: "us" (US HTS, 8/10-digit codes like 6109.10.00.12) or "uk" (UK Trade Tariff, 10-digit commodity codes).
    limit: max candidates (1-25, default 10).
    Returns {"results": [{code, description, path (parent headings), generalRate (us) | declarable (uk), ...}]}.
    Candidates come from keyword search and are NOT a classification ruling; check the full path text and the
    General Rules of Interpretation, then confirm with get_duty_rate.
    """
    limit = max(1, min(int(limit), MAX_RESULTS))

    async def run(http):
        mk = _market(market)
        r = await (us_search(http, description, limit) if mk == "us" else uk_search(http, description, limit))
        return {"market": mk, **r, "disclaimer": DISCLAIMER}
    return await _run(run)


@mcp.tool()
async def get_duty_rate(code: str, market: str = "us", origin_country: str | None = None) -> dict:
    """Import duty rates for one tariff code, optionally for a specific country of origin.

    code: US HTS code (4-10 digits, dots optional, e.g. "6109.10.00" or "8712.00.15.10") or UK commodity code
      (e.g. "6109100010"). A heading or non-declarable code returns the list of rate lines under it.
    market: "us" or "uk".
    origin_country: ISO-3166 alpha-2 country of origin, e.g. "CN", "VN", "MX", "DE".
      US: picks the FTA special rate when the origin is a mapped FTA partner (AU, BH, CA, CL, CO, IL, JO, JP, KR,
      MA, MX, OM, PA, PE, SG, CAFTA-DR), column 2 for CU/KP/RU/BY, otherwise column 1 General.
      UK: filters measures to that origin (third-country duty, preferences, quotas, trade remedies, VAT).
    Returns raw rate text plus "adValoremPct" only when the rate is an unambiguous percentage ("Free" = 0).
    US results exclude Chapter 99 additional duties (Section 301/232/IEEPA etc.); footnote references are returned.
    """
    async def run(http):
        mk = _market(market)
        r = await (us_rate(http, code, origin_country) if mk == "us" else uk_rate(http, code, origin_country))
        return {**r, "disclaimer": DISCLAIMER}
    return await _run(run)


@mcp.tool()
async def estimate_landed_cost(code: str, market: str, customs_value: float, origin_country: str,
                               shipping: float = 0.0, insurance: float = 0.0, transport_mode: str = "ocean",
                               weight_kg: float | None = None, quantity: float | None = None,
                               use_preference: bool = True) -> dict:
    """Estimate import duty, taxes/fees and landed cost for one shipment line.

    code / market / origin_country: as in get_duty_rate (code must be a rate line / declarable code).
    customs_value: price of the goods (transaction value). Currency: USD for "us", GBP for "uk".
    shipping, insurance: international freight and insurance to the destination, same currency.
    transport_mode: "ocean", "air", "truck" or "rail"; US Harbor Maintenance Fee is only applied for "ocean".
    weight_kg / quantity: needed only when the duty includes a specific per-kg / per-item component.
    use_preference: apply the FTA/preferential rate for the origin when the data shows one (default true).
    Method (all figures are estimates):
      US: duty on customs_value (FOB basis); MPF 0.3464% (FY2026 min $33.58, max $651.50) for formal entries
          (value >= $2,500); HMF 0.125% for ocean; no sales tax at import. Chapter 99 duties NOT included.
      UK: duty on CIF (customs_value + shipping + insurance); import VAT at the standard rate from the tariff on
          (CIF + duty). Consignments <= GBP 135 are flagged (VAT normally charged at sale, no duty).
    Returns a line-by-line breakdown, assumptions, warnings and a disclaimer.
    """
    async def run(http):
        mk = _market(market)
        if customs_value is None or customs_value < 0:
            raise SourceError("customs_value must be >= 0")
        rate = await (us_rate(http, code, origin_country) if mk == "us" else uk_rate(http, code, origin_country))
        if rate.get("isRateLine") is False or rate.get("isDeclarable") is False:
            return {"error": "code is not a rate line / declarable code", **rate}
        warnings = list(rate.get("warnings") or [])
        assumptions = []
        lines = {}
        if mk == "us":
            app = rate["applicable"]
            comps, basis, text = app["components"], app["basis"], app["rateText"]
            if not use_preference and basis.startswith("special"):
                comps, basis, text = rate["general"]["components"], "general (preference not used)", rate["general"]["text"]
            duty, problem = duty_from_components(comps, customs_value, weight_kg, quantity)
            if problem:
                warnings.append(f"Duty not computed: {problem}. Raw rate: {text!r}.")
            lines["dutiableValue"] = round(customs_value, 2)
            lines["duty"] = duty
            if customs_value >= US_FORMAL_ENTRY_THRESHOLD:
                mpf = min(max(customs_value * US_MPF_RATE / 100, US_MPF_MIN), US_MPF_MAX)
                lines["merchandiseProcessingFee"] = round(mpf, 2)
            else:
                lines["merchandiseProcessingFee"] = None
                warnings.append("Value under $2,500: informal entry; a small flat MPF applies instead of the ad-valorem "
                                "MPF and is not computed. The $800 de minimis exemption is suspended for most "
                                "shipments since 29 Aug 2025; check current CBP rules.")
            lines["harborMaintenanceFee"] = round(customs_value * US_HMF_RATE / 100, 2) \
                if transport_mode.lower() == "ocean" else 0.0
            fees = sum(v for k, v in lines.items() if k in ("duty", "merchandiseProcessingFee", "harborMaintenanceFee")
                       and v)
            assumptions += [f"Duty basis: {basis}, rate {text!r}.",
                            "US customs value is the transaction value; international freight/insurance are excluded "
                            "from the dutiable value but included in landed cost.",
                            "No federal sales tax/VAT on imports; state taxes not included. Broker fees not included."]
            currency = "USD"
        else:
            app = rate.get("applicable") or {}
            third = app.get("thirdCountry") or {}
            pref = app.get("bestPreference")
            use = pref if (use_preference and pref and pref.get("adValoremPct") is not None
                           and (third.get("adValoremPct") is None or pref["adValoremPct"] <= third["adValoremPct"])) else third
            pct = use.get("adValoremPct") if use else None
            cif = customs_value + shipping + insurance
            lines["dutiableValueCIF"] = round(cif, 2)
            if customs_value <= UK_LOW_VALUE_GBP:
                lines["duty"] = 0.0
                warnings.append("Consignment value <= GBP 135: no customs duty; for B2C sales the seller normally "
                                "charges UK VAT at checkout instead of import VAT (excise goods/gifts differ).")
            elif pct is None:
                lines["duty"] = None
                warnings.append(f"Duty not computed: duty expression {use.get('duty') if use else None!r} is not a plain "
                                "percentage.")
            else:
                lines["duty"] = round(cif * pct / 100, 2)
            vat_pct = rate.get("standardVatPct")
            if vat_pct is None or lines["duty"] is None:
                lines["importVat"] = None
                warnings.append("Import VAT not computed (no standard VAT measure or duty unknown).")
            else:
                lines["importVat"] = round((cif + lines["duty"]) * vat_pct / 100, 2)
            fees = sum(v for k, v in lines.items() if k in ("duty", "importVat") and v)
            assumptions += [f"Duty basis: {'preference ' + use.get('scheme', '') if use is pref and pref else 'third-country (MFN)'} "
                            f"rate {use.get('duty') if use else None!r}.",
                            f"Import VAT at {vat_pct}% on CIF + duty (VAT-registered importers can usually reclaim it).",
                            "Excise, anti-dumping/countervailing duties and broker/handling fees not included."]
            if app.get("additionalDutiesMayApply"):
                warnings.append("Additional duties or trade remedies exist for this origin; see get_duty_rate.")
            currency = "GBP"
        landed = None if lines.get("duty") is None else round(customs_value + shipping + insurance + fees, 2)
        return {
            "market": mk, "code": rate.get("code"), "description": rate.get("description"),
            "origin": (origin_country or "").upper(), "currency": currency,
            "inputs": {"customsValue": customs_value, "shipping": shipping, "insurance": insurance,
                       "transportMode": transport_mode, "weightKg": weight_kg, "quantity": quantity},
            "breakdown": lines, "totalDutiesTaxesFees": round(fees, 2), "estimatedLandedCost": landed,
            "assumptions": assumptions, "warnings": warnings, "source": rate.get("source"),
            "isEstimate": True, "disclaimer": DISCLAIMER,
        }
    return await _run(run)


from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "healthy"})


if __name__ == "__main__":
    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "streamable-http"))
