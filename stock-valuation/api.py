from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timedelta
import asyncio
import os
import scraper, valuation, exporter
from services.governance import fetch_governance, LightweightGovernance, _GOVERNANCE_CACHE, _GOVERNANCE_TTL

# ── Sector peer D/E cache ────────────────────────────────────────────────────
# key = market_url or sector.lower()  →  {"ts": datetime, "peers": [...]}
_SECTOR_CACHE: dict = {}
_CACHE_TTL = timedelta(hours=24)
_FINANCIAL_KEYWORDS = ("bank", "nbfc", "finance", "financial services", "insurance")

# ── Per-ticker sector resolution cache ───────────────────────────────────────
# key = ticker.upper()  →  {"ts": datetime, sector, sector_source, industry,
#                            sub_industry, formerly_known_as}
_SECTOR_TICKER_CACHE: dict = {}
_TICKER_CACHE_TTL = timedelta(days=7)


def _prefetch_sector_peers(ticker: str, scrape_data: dict) -> None:
    """
    Called during scrape (in executor). Uses sector info already extracted from
    soup — no extra HTTP request — to pre-populate both caches so that the
    subsequent resolve-sector and de-benchmark calls return instantly.
    """
    ticker = ticker.upper()
    sector = scrape_data.get("sector_broad")
    market_url = scrape_data.get("sector_sub_url")
    formerly = scrape_data.get("formerly_known_as", False)

    if not sector or not market_url:
        return

    # Populate ticker cache (overwrite regardless of age — scrape data is fresh)
    _SECTOR_TICKER_CACHE[ticker] = {
        "ts": datetime.now(),
        "ticker": ticker,
        "sector": sector,
        "market_url": market_url,
        "sector_source": "screener",
        "industry": scrape_data.get("sector_sub_name", ""),
        "sub_industry": scrape_data.get("sector_sub_name", ""),
        "formerly_known_as": formerly,
    }

    # Populate peer cache if stale or missing
    cache_key = market_url
    existing = _SECTOR_CACHE.get(cache_key)
    if existing and datetime.now() - existing["ts"] < _CACHE_TTL:
        return  # still fresh

    try:
        peers = scraper.fetch_sector_peers(sector, max_peers=30, market_url=market_url)
        _SECTOR_CACHE[cache_key] = {"ts": datetime.now(), "peers": peers}
        print(f"[sector] pre-fetched {len(peers)} peers for {ticker} ({sector})")
    except Exception as exc:
        print(f"[sector] peer pre-fetch failed for {ticker}: {exc}")

app = FastAPI(title="Stock Valuation API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ScrapeRequest(BaseModel):
    ticker: str
    consolidated: bool = True


class FinancialsInput(BaseModel):
    revenue: float = 0
    net_profit: float = 0
    eps: float = 0
    ebitda: float = 0
    market_cap: float = 0
    share_price: float = 0
    shares_outstanding: float = 0
    debt: float = 0
    cash: float = 0
    minority_interest: float = 0
    five_yr_avg_ev_rev: Optional[float] = None
    revenue_history: Optional[List[float]] = None
    median_pe: Optional[float] = None
    years_listed: int = 0
    esop_dilution_pct: Optional[float] = None
    # Quality & shareholding — used only in export, ignored by valuate/scenarios
    promoter_holding: Optional[float] = None
    promoter_pledged: Optional[float] = None
    fii_holding: Optional[float] = None
    dii_holding: Optional[float] = None
    roce_latest: Optional[float] = None
    roce_5yr_avg: Optional[float] = None
    roe_latest: Optional[float] = None
    roe_5yr_avg: Optional[float] = None
    net_margin: Optional[float] = None


class AssumptionsInput(BaseModel):
    growth_rate_1_5: float = 15
    growth_rate_6_10: float = 10
    target_margin: float = 15
    wacc: float = 14
    terminal_rate: float = 5
    sector_ev_rev: float = 5
    sector_pe: float = 25
    sector_ev_ebitda: Optional[float] = None


class ValuateRequest(BaseModel):
    financials: FinancialsInput
    assumptions: AssumptionsInput
    governance: Optional[dict] = None


class DeBenchmarkRequest(BaseModel):
    sector: str
    sector_source: str = "screener"   # "screener" | "user_dropdown"
    company_ticker: str = ""
    company_de: Optional[float] = None


class ResolveSectorRequest(BaseModel):
    ticker: str
    bse_code: Optional[str] = None
    screener_sector: Optional[str] = None       # from Screener breadcrumb
    formerly_known_as: Optional[bool] = False


class ExportRequest(BaseModel):
    company_name: str = ""
    ticker: str
    financials: FinancialsInput
    assumptions: AssumptionsInput
    dcf: dict
    rev_growth: dict
    ps_result: dict
    pe_result: dict = {}
    final_label: str
    scorecard: Optional[dict] = None
    scenarios_matrix: Optional[list] = None
    g1_rates: Optional[list] = None
    tg_rates: Optional[list] = None
    governance: Optional[dict] = None


@app.post("/api/scrape")
async def scrape_endpoint(req: ScrapeRequest):
    try:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(
            None,
            lambda: scraper.scrape(req.ticker, consolidated=req.consolidated),
        )
        bse_code = data.get("bse_code") or ""
        # Governance fetch + sector peer pre-fetch run concurrently
        gov_task   = fetch_governance(req.ticker, bse_code, data)
        peers_task = loop.run_in_executor(
            None, lambda: _prefetch_sector_peers(req.ticker, data)
        )
        gov, _ = await asyncio.gather(gov_task, peers_task, return_exceptions=True)
        if isinstance(gov, Exception):
            gov = LightweightGovernance()
        return {"success": True, "data": data, "governance": gov.model_dump()}
    except Exception as e:
        return {"success": False, "error": str(e), "data": {}, "governance": None}


@app.get("/api/governance")
async def governance_get_endpoint(ticker: str = Query(...)):
    ticker = ticker.strip().upper()
    cache_key = f"governance_{ticker}"
    cached = _GOVERNANCE_CACHE.get(cache_key)
    if cached and datetime.now() - cached["ts"] < _GOVERNANCE_TTL:
        return cached["data"].model_dump()
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, lambda: scraper.scrape(ticker))
    bse_code = data.get("bse_code") or ""
    gov = await fetch_governance(ticker, bse_code, data)
    return gov.model_dump()


def _percentile(sorted_vals: list, p: float) -> float:
    """Linear interpolation percentile on a pre-sorted list."""
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    idx = (n - 1) * p / 100
    lo, hi = int(idx), min(int(idx) + 1, n - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo)


@app.post("/api/de-benchmark")
def de_benchmark_endpoint(req: DeBenchmarkRequest):
    sector = req.sector.strip()

    # Financial sector suppression
    sector_lower = sector.lower()
    if any(kw in sector_lower for kw in _FINANCIAL_KEYWORDS):
        return {
            "flag_financial_sector": True,
            "sector": sector,
            "sector_source": req.sector_source,
            "company_de": req.company_de,
            "error": None,
        }

    # Look up market_url from ticker cache (populated by resolve-sector)
    ticker_data = _SECTOR_TICKER_CACHE.get((req.company_ticker or "").upper(), {})
    market_url = ticker_data.get("market_url")

    # Cache lookup — key on market_url so different sub-industries don't share a bucket
    cache_key = market_url or sector_lower
    cached = _SECTOR_CACHE.get(cache_key)
    if cached and datetime.now() - cached["ts"] < _CACHE_TTL:
        peers = cached["peers"]
    else:
        try:
            peers = scraper.fetch_sector_peers(sector, max_peers=30, market_url=market_url)
            _SECTOR_CACHE[cache_key] = {"ts": datetime.now(), "peers": peers}
        except Exception as exc:
            print(f"[de-benchmark] peer fetch failed for '{sector}': {exc}")
            return {
                "error": "Peer benchmark unavailable — try again later",
                "sector": sector,
                "sector_source": req.sector_source,
                "flag_financial_sector": False,
                "peers": [],
                "peer_count_total": 0,
                "peer_count_valid_de": 0,
            }

    valid = [p for p in peers if p.get("de") is not None]
    if len(valid) < 5:
        return {
            "error": "Insufficient peer data for sector D/E benchmark",
            "sector": sector,
            "sector_source": req.sector_source,
            "flag_financial_sector": False,
            "peers": peers,
            "peer_count_total": len(peers),
            "peer_count_valid_de": len(valid),
        }

    de_vals = sorted(p["de"] for p in valid)
    p25    = round(_percentile(de_vals, 25), 2)
    median = round(_percentile(de_vals, 50), 2)
    p75    = round(_percentile(de_vals, 75), 2)

    # Sector P/E median (exclude negatives and outliers > 200)
    pe_vals = sorted(p["pe"] for p in peers if p.get("pe") is not None and 0 < p["pe"] < 200)
    sector_median_pe = round(_percentile(pe_vals, 50), 1) if len(pe_vals) >= 3 else None

    # Sector EV/Revenue median (exclude negatives and outliers > 50)
    evr_vals = sorted(p["ev_rev"] for p in peers if p.get("ev_rev") is not None and 0 < p["ev_rev"] < 50)
    sector_median_ev_rev = round(_percentile(evr_vals, 50), 2) if len(evr_vals) >= 3 else None

    company_de = req.company_de
    classification = None
    if company_de is not None:
        if company_de < p25:
            classification = "Conservative vs peers"
        elif company_de < median:
            classification = "Below median — healthy"
        elif company_de < p75:
            classification = "Above median — monitor"
        else:
            classification = "High vs peers — investigate"

    flag_elevated = (
        company_de is not None and company_de > p75 and company_de > 2.0
    )

    return {
        "error": None,
        "company_de": company_de,
        "sector": sector,
        "sector_source": req.sector_source,
        "sector_median_de": median,
        "sector_p25_de": p25,
        "sector_p75_de": p75,
        "sector_median_pe": sector_median_pe,
        "sector_median_ev_rev": sector_median_ev_rev,
        "classification": classification,
        "peer_count_total": len(peers),
        "peer_count_valid_de": len(valid),
        "flag_elevated": flag_elevated,
        "flag_financial_sector": False,
        "peers": peers,
    }


@app.post("/api/resolve-sector")
def resolve_sector_endpoint(req: ResolveSectorRequest):
    ticker = req.ticker.strip().upper()
    cached = _SECTOR_TICKER_CACHE.get(ticker)
    if cached and datetime.now() - cached["ts"] < _TICKER_CACHE_TTL:
        return {**cached, "from_cache": True, "ts": None}

    result = {
        "ticker": ticker,
        "sector": None,
        "market_url": None,
        "sector_source": None,
        "industry": "",
        "sub_industry": "",
        "formerly_known_as": req.formerly_known_as or False,
        "from_cache": False,
        "error": None,
    }

    # Source 1: NSE API
    try:
        nse = scraper.fetch_sector_nse(ticker)
        result["sector"] = nse["sector"]
        result["sector_source"] = "nse_api"
        result["industry"] = nse.get("industry", "")
        result["sub_industry"] = nse.get("sub_industry", "")
        # NSE sector name — still need market_url for peer fetch; try Screener for that
        try:
            sc = scraper.fetch_sector_screener(ticker)
            result["market_url"] = sc.get("market_url")
        except Exception:
            pass
    except Exception as e1:
        # Source 2: Screener (returns sector + market_url)
        try:
            sc = scraper.fetch_sector_screener(ticker)
            result["sector"] = sc["sector"]
            result["market_url"] = sc.get("market_url")
            result["sector_source"] = "screener"
        except Exception as e2:
            # Source 3: BSE API
            if req.bse_code:
                try:
                    bse_sector = scraper.fetch_sector_bse(req.bse_code)
                    result["sector"] = bse_sector
                    result["sector_source"] = "bse_api"
                except Exception as e3:
                    result["error"] = (
                        f"All sources failed. NSE: {e1}. Screener: {e2}. BSE: {e3}."
                    )
            else:
                result["error"] = f"NSE: {e1}. Screener: {e2}. No BSE code available."

    if result["sector"]:
        entry = {
            "ts": datetime.now(),
            "ticker": ticker,
            "sector": result["sector"],
            "market_url": result["market_url"],
            "sector_source": result["sector_source"],
            "industry": result["industry"],
            "sub_industry": result["sub_industry"],
            "formerly_known_as": result["formerly_known_as"],
        }
        _SECTOR_TICKER_CACHE[ticker] = entry

    return result


@app.post("/api/sector-cache/clear")
def clear_sector_cache(body: dict):
    ticker = (body.get("ticker") or "").strip().upper()
    if ticker and ticker in _SECTOR_TICKER_CACHE:
        del _SECTOR_TICKER_CACHE[ticker]
    return {"cleared": True, "ticker": ticker}


def _compute_ev(f: FinancialsInput) -> float:
    return (f.market_cap or 0) + (f.debt or 0) - (f.cash or 0) + (f.minority_interest or 0)


@app.post("/api/valuate")
def valuate_endpoint(req: ValuateRequest):
    f = req.financials
    a = req.assumptions

    shares = f.shares_outstanding
    if shares == 0 and f.market_cap > 0 and f.share_price > 0:
        shares = (f.market_cap * 1e7) / f.share_price

    ev = _compute_ev(f)

    dcf = valuation.dcf_valuation(
        revenue=f.revenue,
        net_profit=f.net_profit,
        market_cap=f.market_cap,
        share_price=f.share_price,
        shares_outstanding=shares,
        debt=f.debt,
        cash=f.cash,
        growth_rate_1_5=a.growth_rate_1_5,
        growth_rate_6_10=a.growth_rate_6_10,
        target_margin=a.target_margin,
        wacc=a.wacc,
        terminal_rate=a.terminal_rate,
        esop_dilution_pct=f.esop_dilution_pct,
    )

    rev_growth = valuation.reverse_growth_check(
        revenue=f.revenue,
        ev=ev,
        wacc=a.wacc,
        target_margin=a.target_margin,
        sector_ev_rev=a.sector_ev_rev,
        growth_rate_1_5=a.growth_rate_1_5,
        growth_rate_6_10=a.growth_rate_6_10,
    )

    ps_result = valuation.historical_ev_rev_check(
        ev=ev,
        revenue=f.revenue,
        five_yr_avg_ev_rev=f.five_yr_avg_ev_rev,
        years_listed=f.years_listed,
    )

    pe_result = valuation.pe_valuation(
        eps=f.eps,
        share_price=f.share_price,
        sector_pe=a.sector_pe,
        ebitda=f.ebitda or None,
        ev=ev or None,
        sector_ev_ebitda=a.sector_ev_ebitda,
    )

    reliability_score, reliability_label = valuation.dcf_reliability_score(
        revenue_history=f.revenue_history,
        net_profit=f.net_profit,
        revenue=f.revenue,
        debt=f.debt,
        market_cap=f.market_cap,
    )

    scorecard = valuation.weighted_scorecard(
        dcf_verdict=dcf["verdict"],
        rev_growth_verdict=rev_growth["verdict"],
        ev_rev_verdict=ps_result["verdict"],
        pe_verdict=pe_result["verdict"],
        reliability_label=reliability_label,
        governance=req.governance,
    )
    scorecard["reliability_score"] = reliability_score

    return {
        "dcf": dcf,
        "rev_growth": rev_growth,
        "ps_result": ps_result,
        "pe_result": pe_result,
        "final_label": scorecard["final_label"],
        "final_type":  scorecard["final_type"],
        "ev":          ev,
        "scorecard":   scorecard,
    }


SCENARIO_G1 = [5, 10, 15, 20, 30]
SCENARIO_TG = [2, 3, 4, 5, 6]


@app.post("/api/scenarios")
def scenarios_endpoint(req: ValuateRequest):
    f = req.financials
    a = req.assumptions

    shares = f.shares_outstanding
    if shares == 0 and f.market_cap > 0 and f.share_price > 0:
        shares = (f.market_cap * 1e7) / f.share_price

    matrix = []
    for tg in SCENARIO_TG:
        row = []
        for g1 in SCENARIO_G1:
            dcf = valuation.dcf_valuation(
                revenue=f.revenue,
                net_profit=f.net_profit,
                market_cap=f.market_cap,
                share_price=f.share_price,
                shares_outstanding=shares,
                debt=f.debt,
                cash=f.cash,
                growth_rate_1_5=g1,
                growth_rate_6_10=a.growth_rate_6_10,
                target_margin=a.target_margin,
                wacc=a.wacc,
                terminal_rate=tg,
                esop_dilution_pct=f.esop_dilution_pct,
            )
            row.append({
                "g1": g1,
                "tg": tg,
                "intrinsic_per_share": dcf["intrinsic_per_share"],
                "upside_pct": dcf["upside_pct"],
            })
        matrix.append(row)

    return {"matrix": matrix, "g1_rates": SCENARIO_G1, "tg_rates": SCENARIO_TG}


class AuditorRequest(BaseModel):
    ticker: str
    bse_code: Optional[str] = None


@app.post("/api/auditor")
def auditor_endpoint(req: AuditorRequest):
    return scraper.fetch_auditor_data(req.ticker, req.bse_code or "")


class CaroRequest(BaseModel):
    bse_code: Optional[str] = None


@app.post("/api/caro")
def caro_endpoint(req: CaroRequest):
    return scraper.fetch_caro_status(req.bse_code or "")


class ShareholdingTrendRequest(BaseModel):
    ticker: str


@app.post("/api/shareholding-trend")
def shareholding_trend_endpoint(req: ShareholdingTrendRequest):
    return scraper.fetch_shareholding_trend(req.ticker)


class WorkingCapitalRequest(BaseModel):
    financials: dict


@app.post("/api/working-capital")
def working_capital_endpoint(req: WorkingCapitalRequest):
    return scraper.compute_working_capital_health(req.financials)


class ConcallRequest(BaseModel):
    ticker: str


@app.post("/api/concall")
def concall_endpoint(req: ConcallRequest):
    return scraper.fetch_concall_snippet(req.ticker)


class DividendHistoryRequest(BaseModel):
    financials: dict


@app.post("/api/dividend-history")
def dividend_history_endpoint(req: DividendHistoryRequest):
    return scraper.extract_dividend_history(req.financials)


@app.post("/api/export")
def export_endpoint(req: ExportRequest):
    f = req.financials.model_dump()
    f["company_name"] = req.company_name

    excel_bytes = exporter.build_excel(
        financials=f,
        assumptions=req.assumptions.model_dump(),
        dcf=req.dcf,
        rev_growth=req.rev_growth,
        ps=req.ps_result,
        final_label=req.final_label,
        ticker=req.ticker,
        scorecard=req.scorecard,
        scenarios_matrix=req.scenarios_matrix,
        g1_rates=req.g1_rates,
        tg_rates=req.tg_rates,
        pe_result=req.pe_result if req.pe_result else {},
        governance=req.governance,
    )

    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": (
                f"attachment; filename={req.ticker.upper()}_valuation.xlsx"
            )
        },
    )


# Static files must be mounted last so API routes take priority
static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
