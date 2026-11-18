"""Clients for the USITC HTS REST API and the UK Trade Tariff API v2 (both keyless, read-only)."""
import asyncio
import json
import re

import httpx

from rates import parse_uk_duty, parse_us_rate, parse_us_special

UA = "tariff-duty-mcp/0.1 (+MCP server; reads public USITC HTS and UK Trade Tariff APIs)"
HTS = "https://hts.usitc.gov/reststop"
UK = "https://www.trade-tariff.service.gov.uk/api/v2"
MAX_BYTES = 6_000_000
HTTP_TIMEOUT = 45


class SourceError(Exception):
    pass


class NotFound(SourceError):
    pass


class Http:
    def __init__(self):
        self.c = httpx.AsyncClient(headers={"User-Agent": UA, "Accept": "application/json"}, timeout=HTTP_TIMEOUT,
                                   follow_redirects=True)

    async def close(self):
        await self.c.aclose()

    async def get_json(self, url: str, params: dict | None = None):
        async with self.c.stream("GET", url, params=params) as r:
            if r.status_code == 404:
                raise NotFound(f"404 from {r.url}")
            if r.status_code >= 400:
                raise SourceError(f"HTTP {r.status_code} from {r.url}")
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > MAX_BYTES:
                    raise SourceError(f"response from {r.url} exceeded {MAX_BYTES} bytes")
        try:
            return json.loads(bytes(buf))
        except ValueError as e:
            raise SourceError(f"non-JSON response from {url}: {e}") from e


def digits(code: str) -> str:
    return re.sub(r"\D", "", code or "")


def fmt_us(d: str) -> str:
    parts = [d[:4], d[4:6], d[6:8], d[8:10]]
    return ".".join(p for p in parts if p)


def words(text: str) -> list[str]:
    out = []
    text = re.sub(r"\b([a-z0-9])-([a-z])", r"\1\2", (text or "").lower())  # "t-shirts" -> "tshirts"
    for w in re.findall(r"[a-z0-9]+", text):
        if len(w) < 2 or w in {"of", "and", "or", "for", "the", "with", "in", "to", "other"}:
            continue
        for suf in ("ies", "es", "s"):
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                w = w[: -len(suf)] + ("y" if suf == "ies" else "")
                break
        out.append(w)
    return out


# ---------------- US (USITC HTS) ----------------

async def us_heading(http: Http, heading4: str) -> list[dict]:
    """All rows of one 4-digit heading, each with 'path' (ancestor descriptions) and inherited rate columns."""
    rows = await http.get_json(f"{HTS}/exportList", {"from": heading4, "to": f"{heading4}.99.99.99", "format": "JSON",
                                                     "styles": "false"})
    stack: list[dict] = []
    out = []
    for r in rows if isinstance(rows, list) else []:
        try:
            ind = int(r.get("indent") or 0)
        except ValueError:
            ind = 0
        while stack and stack[-1]["indent"] >= ind:
            stack.pop()
        node = {
            "htsno": (r.get("htsno") or "").strip(), "indent": ind, "description": _strip(r.get("description")),
            "general": (r.get("general") or "").strip(), "special": (r.get("special") or "").strip(),
            "other": (r.get("other") or "").strip(), "units": r.get("units") or [],
            "footnotes": [_strip(f.get("value")) for f in (r.get("footnotes") or []) if f.get("value")],
            "additionalDuties": r.get("additionalDuties") or None,
            "path": [s["description"] for s in stack],
        }
        # rate columns are printed on the 8-digit line; statistical (10-digit) lines inherit them
        rate_src = next((s for s in [node] + stack[::-1] if s["general"] or s["special"] or s["other"]), None)
        node["rateFrom"] = rate_src["htsno"] if rate_src else None
        for k in ("general", "special", "other"):
            node["eff_" + k] = rate_src[k] if rate_src else ""
        node["inheritedFootnotes"] = [f for s in stack for f in s["footnotes"]]
        stack.append(node)
        out.append(node)
    return out


def _strip(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()


async def us_search(http: Http, description: str, limit: int) -> dict:
    q = words(description)
    if not q:
        raise SourceError("description has no searchable words")
    hits = await http.get_json(f"{HTS}/search", {"keyword": description})
    hits = hits if isinstance(hits, list) else []
    # rank headings by how many query words the API hits contain, then pull full heading context
    cover: dict[str, set] = {}
    count: dict[str, int] = {}
    head_hit: set = set()
    for h in hits:
        no = digits(h.get("htsno"))
        if len(no) < 4 or no.startswith("99"):
            continue
        txt = set(words(h.get("description")))
        m = {w for w in q if w in txt or any(t.startswith(w) for t in txt)}
        if m:
            cover.setdefault(no[:4], set()).update(m)
            count[no[:4]] = count.get(no[:4], 0) + 1
            if len(no) == 4:
                head_hit.add(no[:4])
    score = {k: len(v) + 0.5 * (k in head_hit) + min(count[k], 20) / 100 for k, v in cover.items()}
    heads = sorted(score, key=lambda k: -score[k])[:8]
    tables = await asyncio.gather(*[us_heading(http, h) for h in heads], return_exceptions=True)
    cands = []
    for t in tables:
        if isinstance(t, Exception):
            continue
        for n in t:
            if len(digits(n["htsno"])) < 8:
                continue
            text = " ".join(n["path"] + [n["description"]])
            tw = set(words(text))
            matched = [w for w in q if w in tw or any(x.startswith(w) for x in tw)]
            if not matched:
                continue
            leaf = set(words(n["description"]))
            head = set(words(n["path"][0] if n["path"] else n["description"]))
            s = (len(matched) / len(q) + 0.15 * sum(1 for w in q if w in head) + 0.1 * sum(1 for w in q if w in leaf)
                 + 0.05 * (len(digits(n["htsno"])) == 10))
            cands.append((s, n))
    cands.sort(key=lambda x: -x[0])
    return {
        "query": description, "apiHits": len(hits), "headingsExamined": heads,
        "results": [{
            "code": n["htsno"], "description": n["description"], "path": n["path"],
            "generalRate": n["eff_general"] or None, "units": n["units"], "relevance": round(s, 2),
        } for s, n in cands[:limit]],
    }


US_PROGRAMS = {  # origin ISO-2 -> special-column program indicators of free-trade agreements
    "AU": ["AU"], "BH": ["BH"], "CL": ["CL"], "CO": ["CO"], "IL": ["IL"], "JO": ["JO"], "KR": ["KR"], "MA": ["MA"],
    "OM": ["OM"], "PA": ["PA"], "PE": ["PE"], "SG": ["SG"], "JP": ["JP"], "CA": ["S", "S+", "CA"],
    "MX": ["S", "S+", "MX"], **{c: ["P", "P+"] for c in ("CR", "DO", "SV", "GT", "HN", "NI")},
}
US_COLUMN2 = {"CU", "KP", "RU", "BY"}
US_UNMAPPED = {"A": "GSP", "A+": "GSP (LDC)", "A*": "GSP (some countries excluded)", "D": "AGOA",
               "E": "CBERA", "E*": "CBERA", "R": "CBTPA", "N": "Nepal preference", "C": "civil aircraft",
               "B": "auto parts", "J": "ATPA", "J*": "ATPA", "J+": "ATPDEA", "K": "pharmaceuticals",
               "L": "uruguay round concessions on intermediate chemicals for dyes"}


async def us_rate(http: Http, code: str, origin: str | None) -> dict:
    d = digits(code)
    if len(d) < 4:
        raise SourceError("US HTS code needs at least 4 digits")
    rows = await us_heading(http, d[:4])
    if not rows:
        raise NotFound(f"HTS heading {d[:4]} not found")
    exact = [n for n in rows if digits(n["htsno"]) == d]
    if not exact or len(d) < 8:
        lines = [n for n in rows if digits(n["htsno"]).startswith(d) and len(digits(n["htsno"])) >= 8]
        if not lines:
            raise NotFound(f"HTS code {fmt_us(d)} not found")
        return {"market": "us", "code": fmt_us(d), "isRateLine": False,
                "message": "Not a rate line. Pick one of the 8/10-digit lines below and call again.",
                "lines": [{"code": n["htsno"], "description": n["description"], "path": n["path"][-2:],
                           "general": n["eff_general"] or None} for n in lines[:40]],
                "linesTotal": len(lines)}
    n = exact[0]
    general = parse_us_rate(n["eff_general"])
    special = parse_us_special(n["eff_special"])
    col2 = parse_us_rate(n["eff_other"])
    foot = list(dict.fromkeys(n["inheritedFootnotes"] + n["footnotes"]))
    ch99 = sorted(set(re.findall(r"99\d\d\.\d\d\.\d\d", " ".join(foot))))
    res = {
        "market": "us", "code": n["htsno"], "isRateLine": True, "description": n["description"], "path": n["path"],
        "units": n["units"], "ratesFromLine": n["rateFrom"],
        "general": general, "special": special, "column2": col2,
        "footnotes": foot[:20], "chapter99References": ch99,
        "source": f"https://hts.usitc.gov/search?query={n['htsno']}",
    }
    warnings = [
        "Rates are the HTS column 1 General (MFN/NTR), Special and column 2 rates only. Chapter 99 additional duties "
        "(e.g. Section 301, Section 232, IEEPA or other emergency tariffs, safeguards) and AD/CVD are NOT added; "
        "they can exceed the base rate. Check the footnotes/chapter99References and current CBP guidance.",
    ]
    if origin:
        o = origin.strip().upper()
        app = {"origin": o, "basis": "general (column 1 General, MFN/NTR)", "rateText": general["text"],
               "adValoremPct": general["adValoremPct"], "components": general["components"]}
        if o in US_COLUMN2:
            app.update(basis="column 2 (statutory rate for countries without NTR status)", rateText=col2["text"],
                       adValoremPct=col2["adValoremPct"], components=col2["components"])
        else:
            progs = US_PROGRAMS.get(o, [])
            for sp in special:
                hit = [p for p in sp["programs"] if p in progs]
                if hit:
                    pr = parse_us_rate(sp["rate"])
                    app.update(basis=f"special program {'/'.join(hit)} (requires qualifying origin and a claim)",
                               rateText=sp["rate"], adValoremPct=pr["adValoremPct"], components=pr["components"])
                    break
        listed = {p for sp in special for p in sp["programs"]}
        unmapped = sorted(listed & set(US_UNMAPPED))
        if unmapped:
            warnings.append("Preference programs " + ", ".join(f"{p} ({US_UNMAPPED[p]})" for p in unmapped)
                            + " are listed but not evaluated: country designation/program status (e.g. GSP lapsed "
                            "at end-2020) must be checked separately.")
        if o == "CN":
            warnings.append("Origin CN: Section 301 and other Chapter 99 duties commonly apply on top of the base rate.")
        res["applicable"] = app
    res["warnings"] = warnings
    return res


# ---------------- UK Trade Tariff ----------------

async def uk_search(http: Http, description: str, limit: int) -> dict:
    d = await http.get_json(f"{UK}/search", {"q": description})
    attrs = (d.get("data") or {}).get("attributes") or {}
    if attrs.get("type") == "exact_match":
        e = attrs.get("entry") or {}
        return {"query": description, "exactMatch": {"endpoint": e.get("endpoint"), "code": e.get("id")}, "results": []}
    gm = attrs.get("goods_nomenclature_match") or {}
    res = []
    for kind in ("commodities", "headings", "chapters"):
        for h in gm.get(kind) or []:
            s = h.get("_source") or {}
            res.append({
                "code": s.get("goods_nomenclature_item_id"), "level": kind[:-1] if kind != "commodities" else "commodity",
                "description": s.get("description"), "path": (s.get("ancestor_descriptions") or [])[:-1],
                "declarable": s.get("declarable"), "score": round(h.get("_score") or 0, 2),
            })
    res.sort(key=lambda r: (not r["declarable"], -r["score"]))
    refs = [{"title": (x.get("_source") or {}).get("title"),
             "code": ((x.get("_source") or {}).get("reference") or {}).get("goods_nomenclature_item_id")}
            for x in (attrs.get("reference_match") or {}).get("commodities", [])][:5]
    return {"query": description, "results": res[:limit], **({"referenceMatches": refs} if refs else {})}


_DUTY_TYPES_THIRD = {"103", "105"}
_DUTY_TYPES_PREF = {"142", "145", "106"}
_DUTY_TYPES_QUOTA = {"122", "123", "143", "146", "147", "653", "654"}


async def uk_rate(http: Http, code: str, origin: str | None) -> dict:
    d = digits(code)
    if len(d) < 4:
        raise SourceError("UK commodity code needs at least 4 digits")
    full = (d + "0" * 10)[:10]
    params = {"filter[geographical_area_id]": origin.strip().upper()} if origin else None
    kind = "commodities"
    try:
        doc = await http.get_json(f"{UK}/commodities/{full}", params)
    except NotFound:
        doc = None
        if len(d.rstrip("0")) <= 4 or d.endswith("000000"):
            try:  # a heading with no sub-divisions is itself declarable
                doc = await http.get_json(f"{UK}/headings/{d[:4]}", params)
                kind = "headings"
            except NotFound:
                doc = None
    if doc is None or not (doc.get("data") or {}).get("attributes", {}).get("declarable"):
        return await _uk_candidates(http, d)
    a = doc["data"]["attributes"]
    inc = {(x["type"], x["id"]): x for x in doc.get("included") or []}
    rel = doc["data"].get("relationships") or {}
    measures = []
    for ref in (rel.get("import_measures") or {}).get("data") or []:
        m = inc.get(("measure", ref["id"]))
        if not m:
            continue
        r = m.get("relationships") or {}
        mt = inc.get(("measure_type", (r.get("measure_type") or {}).get("data", {}).get("id")), {})
        de = inc.get(("duty_expression", ((r.get("duty_expression") or {}).get("data") or {}).get("id")), {})
        ga = inc.get(("geographical_area", ((r.get("geographical_area") or {}).get("data") or {}).get("id")), {})
        ac = inc.get(("additional_code", ((r.get("additional_code") or {}).get("data") or {}).get("id")), {})
        duty = re.sub(r"<[^>]+>", "", (de.get("attributes") or {}).get("verbose_duty") or "").strip() or None
        measures.append({
            "typeId": mt.get("id"), "type": (mt.get("attributes") or {}).get("description"),
            "series": (mt.get("attributes") or {}).get("measure_type_series_id"),
            "area": ga.get("id"), "areaName": (ga.get("attributes") or {}).get("description"),
            "duty": duty, "adValoremPct": parse_uk_duty(duty),
            "additionalCode": ((ac.get("attributes") or {}).get("code")),
            "additionalCodeName": ((ac.get("attributes") or {}).get("description")),
            "quotaOrderNumber": ((r.get("order_number") or {}).get("data") or {}).get("id"),
            "conditions": len(((r.get("measure_conditions") or {}).get("data")) or []),
            "excludedCountries": [x["id"] for x in ((r.get("excluded_countries") or {}).get("data") or [])][:20],
            "vat": (m.get("attributes") or {}).get("vat"),
        })
    third = [x for x in measures if x["typeId"] in _DUTY_TYPES_THIRD and not x["additionalCode"]]
    prefs = [x for x in measures if x["typeId"] in _DUTY_TYPES_PREF]
    quotas = [x for x in measures if x["typeId"] in _DUTY_TYPES_QUOTA]
    other_duty = [x for x in measures if x["series"] == "C" and x not in third + prefs + quotas]
    remedies = [x for x in measures if x["series"] in ("D", "J")]
    vat = [x for x in measures if x["series"] == "P"]
    excise = [x for x in measures if x["series"] == "Q"]
    controls = sorted({x["type"] for x in measures if x["series"] in ("A", "B") and x["type"]})
    std_vat = next((x for x in vat if not x["additionalCode"]), None)

    def slim(x):
        return {k: v for k, v in x.items() if v not in (None, [], 0, False) and k not in ("series", "vat")}

    res = {
        "market": "uk", "code": a.get("goods_nomenclature_item_id"), "level": kind[:-1].replace("commoditie", "commodity"),
        "description": a.get("description_plain") or a.get("description"), "declarable": a.get("declarable"),
        "origin": origin.strip().upper() if origin else None,
        "thirdCountryDuty": [slim(x) for x in third],
        "preferences": [slim(x) for x in prefs][:60],
        "preferentialQuotas": [slim(x) for x in quotas][:30],
        "otherDutyMeasures": [slim(x) for x in other_duty][:20],
        "additionalDutiesAndRemedies": [slim(x) for x in remedies][:20],
        "vat": [slim(x) for x in vat], "standardVatPct": std_vat["adValoremPct"] if std_vat else None,
        "excise": [slim(x) for x in excise][:10], "importControls": controls[:20],
        "source": f"https://www.trade-tariff.service.gov.uk/commodities/{a.get('goods_nomenclature_item_id')}",
    }
    if origin:
        best_pref = min((x for x in prefs if x["adValoremPct"] is not None), key=lambda x: x["adValoremPct"], default=None)
        base = third[0] if third else None
        res["applicable"] = {
            "thirdCountry": {"duty": base and base["duty"], "adValoremPct": base and base["adValoremPct"]},
            "bestPreference": best_pref and {"duty": best_pref["duty"], "adValoremPct": best_pref["adValoremPct"],
                                             "scheme": best_pref["areaName"],
                                             "note": "requires goods to meet the scheme's rules of origin and a valid "
                                                     "origin statement/proof"},
            "additionalDutiesMayApply": bool(remedies),
        }
    res["warnings"] = [
        "Quotas, end-use reliefs, suspensions with conditions, and anti-dumping duties keyed to additional codes are "
        "listed but not applied automatically. Duty expressions other than a plain percentage are returned as text."]
    return res


async def _uk_candidates(http: Http, d: str) -> dict:
    try:
        h = await http.get_json(f"{UK}/headings/{d[:4]}")
    except NotFound:
        raise NotFound(f"UK commodity code {d} not found")
    prefix = d.rstrip("0") if len(d) > 4 else d[:4]
    rows = [x["attributes"] for x in h.get("included") or [] if x.get("type") == "commodity"]
    lines = [r for r in rows if r.get("declarable") and r.get("goods_nomenclature_item_id", "").startswith(prefix)]
    if not lines:
        lines = [r for r in rows if r.get("declarable")]
    return {"market": "uk", "code": d, "isDeclarable": False,
            "message": "Not a declarable 10-digit commodity code. Pick one of the codes below and call again.",
            "lines": [{"code": r["goods_nomenclature_item_id"], "description": r.get("description_plain") or r.get("description")}
                      for r in lines[:40]], "linesTotal": len(lines)}
