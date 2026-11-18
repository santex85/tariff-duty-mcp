# Tariff Duty MCP server (US HTS + UK Trade Tariff)

An MCP server that lets AI agents look up import tariff codes, duty rates by country of origin, and rough
landed-cost estimates for the **United States** and the **United Kingdom**. It reads two official, public, keyless
APIs live on every call: the USITC Harmonized Tariff Schedule REST API and the UK Trade Tariff API v2.

> **Not customs advice.** Every response carries a disclaimer. Results are reference data and estimates. Tariff
> classification, origin, valuation and additional duties are decided by the customs authority. Confirm with a
> licensed customs broker or the official tariff before you import.

## Tools

| Tool | What it does |
|---|---|
| `search_hs_code` | Candidate tariff codes for a product description, with the parent-heading path (US: plus the General rate) |
| `get_duty_rate` | Rates for one code. US: General (MFN), Special (FTA programs) and column 2, with the rate applicable to an origin. UK: third-country duty, preferences, quotas, trade remedies, VAT and import controls for an origin |
| `estimate_landed_cost` | Duty, US MPF/HMF or UK import VAT, and total landed cost for one shipment line, with assumptions and warnings |

### Example calls

```jsonc
// search_hs_code
{"description": "cotton t-shirts", "market": "us", "limit": 5}
//   -> 6109.10.00.xx "T-shirts ... of cotton", generalRate "16.5%"
{"description": "lithium-ion batteries", "market": "us"}   // -> 8507.60.00 (3.4%)
{"description": "cotton t-shirts", "market": "uk"}        // -> 6109100010 "T-shirts" (declarable)

// get_duty_rate
{"code": "6109.10.00.12", "market": "us", "origin_country": "CN"}
//   general {"text": "16.5%", "adValoremPct": 16.5}, special "Free (AU,BH,CL,...)", column2 "90%",
//   applicable: general (MFN) 16.5% + a warning that Section 301/Chapter 99 duties are NOT included
{"code": "8712001510", "market": "us", "origin_country": "JP"}   // applicable: special program JP, 5.5%
{"code": "0901.21", "market": "us"}                               // not a rate line -> list of 8/10-digit lines
{"code": "6109100010", "market": "uk", "origin_country": "VN"}
//   third-country 12.00%, preferences: UK-Vietnam 0.00%, CPTPP 0.00%; standard VAT 20%

// estimate_landed_cost
{"code": "6109.10.00.12", "market": "us", "customs_value": 10000, "origin_country": "CN",
 "shipping": 800, "insurance": 50, "transport_mode": "ocean"}
//   duty 1650.00, MPF 34.64, HMF 12.50, landed 12547.14 USD (Chapter 99 duties excluded, see warnings)
{"code": "6109100010", "market": "uk", "customs_value": 5000, "origin_country": "TH", "shipping": 400, "insurance": 20}
//   CIF 5420, duty 650.40 (12%), import VAT 1214.08, landed 7284.48 GBP
```

### How rates are returned

- The raw rate text is always returned (`"6.5%"`, `"Free"`, `"2.4¢/kg"`, `"5% + 2¢/kg"`, `"8.00% + 21.40 GBP / 100 kg"`).
- `adValoremPct` is set **only** when the whole rate is one unambiguous percentage (`"Free"` = 0). Specific and
  compound rates leave it `null`.
- US `components` break simple specific rates into `usdPerUnit` + unit (kg, item, pair, dozen, liter, tonne).
  `estimate_landed_cost` uses them only when you pass `weight_kg` / `quantity`. Anything it cannot parse is left
  uncomputed with a warning.
- US 10-digit statistical lines inherit the rate printed on their 8-digit parent (`ratesFromLine`).

### Estimation method

- **US**: duty on `customs_value` (transaction value; freight and insurance excluded from the dutiable value but
  added to landed cost). Merchandise Processing Fee 0.3464% with the FY2027 floor $34.58 and cap $670.86 for formal
  entries (value ≥ $2,500). Harbor Maintenance Fee 0.125% for ocean freight only. Origin selects the FTA special rate for
  mapped partners (AU, BH, CA, CL, CO, IL, JO, JP, KR, MA, MX, OM, PA, PE, SG, CAFTA-DR), column 2 for CU/KP/RU/BY,
  otherwise General.
- **UK**: duty on CIF (`customs_value + shipping + insurance`) at the lowest plain-percentage preference available for
  the origin (or the third-country duty when `use_preference=false` or none applies). Import VAT at the tariff's
  standard VAT rate on CIF + duty. Consignments ≤ £135 are flagged: no duty, and VAT is normally charged at sale.

## Limits and known gaps

- **US Chapter 99 additional duties are not added** (Section 301 on China, Section 232 steel/aluminium/autos,
  IEEPA/emergency tariffs, safeguards). In 2025–26 these often exceed the base rate. The tool returns footnote
  references (e.g. `9903.90.09`) and a warning, but it does not compute them. AD/CVD orders are not covered either.
- US preference programs other than the mapped FTAs (GSP `A/A+/A*`, AGOA `D`, CBERA `E`, etc.) are listed but not
  applied. Program status and country designation must be checked separately.
- US informal-entry MPF (value < $2,500), state taxes, broker fees, excise and UK excise/anti-dumping duties keyed to
  additional codes are not computed. UK quotas, end-use reliefs and conditional suspensions are listed only.
- `search_hs_code` for the US uses the USITC keyword search plus local re-ranking over the full heading context.
  It is a keyword match, not a classifier. The UK search uses the Trade Tariff's own fuzzy search.
- The MPF floor and cap are FY2027 values (1 Oct 2026 – 30 Sep 2027, CBP Dec. 26-14) hard-coded in `src/server.py`.
  CBP adjusts them every 1 October; the 0.3464% rate and the 0.125% HMF are unchanged.
- Currency: USD for the US, GBP for the UK. No FX conversion.
- Caps: ≤ 25 search results, ≤ 40 sub-lines listed, ≤ 6 MB per upstream response, 45 s HTTP timeout, 90 s per tool call.

## Data sources and licences

- **USITC Harmonized Tariff Schedule** REST API, `https://hts.usitc.gov/reststop` (`/search`, `/exportList`). It is a
  US federal government work. No key is required.
- **UK Trade Tariff API v2**, `https://www.trade-tariff.service.gov.uk/api/v2`, operated by HMRC/DBT. No key is
  required. The data is Crown copyright. Like most GOV.UK data it is expected to be under the Open Government Licence
  v3.0, but I could not confirm that on the service's own pages (they returned a short interstitial page to scripted
  requests), so verify it before commercial use. Suggested attribution: "Contains public sector information licensed
  under the Open Government Licence v3.0."
- The User-Agent is `tariff-duty-mcp/0.1 (+MCP server; ...)`. Please keep request volume reasonable. Both services are free public
  infrastructure.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python src/server.py                         # streamable-http on 0.0.0.0:8080, endpoint /mcp
PORT=8096 .venv/bin/python src/server.py               # other port
MCP_TRANSPORT=stdio .venv/bin/python src/server.py     # stdio, for local MCP clients
.venv/bin/python tests/live_test.py                    # live smoke test of every tool
```

Environment variables: `HOST` (default `0.0.0.0`), `PORT` (default `8080`), `MCP_TRANSPORT` (`streamable-http`, `stdio` or `sse`; default `streamable-http`). No API key is needed.

`mcpize.yaml` describes the build and start commands for MCPize.

## Use with Claude Desktop or another MCP client

Local (stdio). Clone the repo, create the venv as above, then add this to `claude_desktop_config.json`
(or your client's MCP config), with the paths changed to where you cloned it:

```json
{
  "mcpServers": {
    "tariff-duty": {
      "command": "/path/to/tariff-duty-mcp/.venv/bin/python",
      "args": ["/path/to/tariff-duty-mcp/src/server.py"],
      "env": {"MCP_TRANSPORT": "stdio"}
    }
  }
}
```

Remote (streamable HTTP). Run `.venv/bin/python src/server.py` on a server and point any client that supports
remote MCP servers at `http://<host>:8080/mcp`. `GET /health` returns `{"status": "healthy"}`.

## License

MIT, see [LICENSE](LICENSE). The data returned by the tools stays under the terms of its sources (see above).
