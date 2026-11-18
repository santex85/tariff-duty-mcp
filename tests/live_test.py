"""Live smoke test: calls every tool against the real APIs. Run: .venv/bin/python tests/live_test.py"""
import asyncio, json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import server  # noqa: E402
from rates import parse_us_rate, parse_us_special  # noqa: E402


def show(name, r, ok):
    s = json.dumps(r, ensure_ascii=False)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {len(s)} bytes | {s[:700]}\n")
    return bool(ok)


async def timed(name, coro):
    t = time.time()
    r = await coro
    return f"{name} ({time.time() - t:.1f}s)", r


async def main():
    res = []
    tools = await server.mcp.list_tools()
    res.append(show("list_tools", [t.name for t in tools], len(tools) == 3))

    # offline parser checks
    p = [parse_us_rate(x) for x in ("6.5%", "Free", "2.4¢/kg", "5% + 2¢/kg", "The rate applicable to the article")]
    ok = (p[0]["adValoremPct"] == 6.5 and p[1]["adValoremPct"] == 0.0 and p[2]["adValoremPct"] is None
          and p[2]["components"][0]["usdPerUnit"] == 0.024 and p[3]["adValoremPct"] is None
          and len(p[3]["components"]) == 2 and not p[4]["parsed"])
    sp = parse_us_special("Free (A+,AU,BH,CL) 5.5% (JP)")
    res.append(show("rate parser", [p, sp], ok and sp[1]["programs"] == ["JP"] and sp[1]["adValoremPct"] == 5.5))

    n, r = await timed("search_hs_code us 'cotton t-shirts'", server.search_hs_code("cotton t-shirts", "us", 5))
    res.append(show(n, r, any(x["code"].startswith("6109") for x in r.get("results", []))))
    n, r = await timed("search_hs_code us 'bicycles'", server.search_hs_code("bicycles", "us", 5))
    res.append(show(n, r, any(x["code"].startswith("8712") for x in r.get("results", []))))
    n, r = await timed("search_hs_code uk 'cotton t-shirts'", server.search_hs_code("cotton t-shirts", "uk", 5))
    res.append(show(n, r, any((x["code"] or "").startswith("6109") for x in r.get("results", []))))
    n, r = await timed("search_hs_code bad market", server.search_hs_code("tea", "fr"))
    res.append(show(n, r, "error" in r))

    n, r = await timed("get_duty_rate us 6109.10.00.12 CN", server.get_duty_rate("6109.10.00.12", "us", "CN"))
    res.append(show(n, r, r.get("general", {}).get("adValoremPct") == 16.5 and r["applicable"]["adValoremPct"] == 16.5))
    n, r = await timed("get_duty_rate us 8712.00.15 JP", server.get_duty_rate("8712001510", "us", "JP"))
    res.append(show(n, r, r.get("applicable", {}).get("adValoremPct") == 5.5 and "JP" in r["applicable"]["basis"]))
    n, r = await timed("get_duty_rate us 0901.21 (heading list)", server.get_duty_rate("0901.21", "us"))
    res.append(show(n, r, r.get("isRateLine") is False and r.get("lines")))
    n, r = await timed("get_duty_rate us 7208.10.30 RU col2", server.get_duty_rate("7208.10.30.00", "us", "RU"))
    res.append(show(n, r, "column 2" in r.get("applicable", {}).get("basis", "") and r.get("chapter99References")))
    n, r = await timed("get_duty_rate uk 6109100010 VN", server.get_duty_rate("6109100010", "uk", "VN"))
    res.append(show(n, r, r.get("standardVatPct") == 20.0 and r["applicable"]["bestPreference"]["adValoremPct"] == 0.0
                    and r["thirdCountryDuty"][0]["adValoremPct"] == 12.0))
    n, r = await timed("get_duty_rate uk 6109 (candidates)", server.get_duty_rate("6109", "uk"))
    res.append(show(n, r, r.get("isDeclarable") is False and r.get("lines")))
    n, r = await timed("get_duty_rate us missing", server.get_duty_rate("0000.00.00", "us"))
    res.append(show(n, r, "error" in r))

    n, r = await timed("estimate_landed_cost us", server.estimate_landed_cost("6109.10.00.12", "us", 10000, "CN",
                                                                               shipping=800, insurance=50))
    b = r.get("breakdown", {})
    res.append(show(n, r, b.get("duty") == 1650.0 and b.get("merchandiseProcessingFee") == 34.64
                    and b.get("harborMaintenanceFee") == 12.5 and r.get("estimatedLandedCost") == 12547.14))
    n, r = await timed("estimate_landed_cost us specific", server.estimate_landed_cost("0901.90.20.00", "us", 3000, "BR",
                                                                                       transport_mode="air", weight_kg=500))
    res.append(show(n, r, r.get("breakdown", {}).get("duty") is not None))
    n, r = await timed("estimate_landed_cost uk VN", server.estimate_landed_cost("6109100010", "uk", 5000, "VN",
                                                                                 shipping=400, insurance=20))
    b = r.get("breakdown", {})
    res.append(show(n, r, b.get("duty") == 0.0 and b.get("importVat") == 1084.0))
    n, r = await timed("estimate_landed_cost uk TH mfn", server.estimate_landed_cost("6109100010", "uk", 5000, "TH",
                                                                                     shipping=400, insurance=20))
    b = r.get("breakdown", {})
    res.append(show(n, r, b.get("duty") == 650.4 and b.get("importVat") == 1214.08))
    print(f"{sum(res)}/{len(res)} passed")


asyncio.run(main())
