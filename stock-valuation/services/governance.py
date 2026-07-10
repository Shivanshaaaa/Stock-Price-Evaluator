"""
Governance orchestrator — runs all 6 governance sub-fetches in parallel
and returns a LightweightGovernance Pydantic model.

Network-bound calls (auditor, CARO, shareholding, concall) run concurrently
via asyncio.gather + run_in_executor. Compute-only calls (WC, dividend) are
synchronous and run after the network fetches complete.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import List, Optional

from pydantic import BaseModel

import scraper


_GOVERNANCE_CACHE: dict = {}
_GOVERNANCE_TTL = timedelta(hours=24)

# Total non-meta data fields across all 6 sub-fetches
_FIELDS_TOTAL = 34

# Flags whose True value signals a governance risk
_RISK_FLAGS = (
    "flag_auditor_changed",
    "flag_caro_qualified",
    "flag_promoter_pledging_high",
    "flag_promoter_stake_declining",
    "flag_working_capital_deteriorating",
)

_AUDITOR_NONE: dict = {
    "current_auditor_name": None,
    "previous_auditor": None,
    "flag_auditor_changed": None,
}
_CARO_NONE: dict = {
    "flag_caro_qualified": None,
    "caro_source_note": None,
}
_SHAREHOLDING_NONE: dict = {
    "promoter_holding_pct": None,
    "promoter_pledged_pct": None,
    "fii_holding_pct": None,
    "dii_holding_pct": None,
    "promoter_holding_trend": None,
    "promoter_pledged_trend": None,
    "fii_trend": None,
    "dii_trend": None,
    "promoter_holding_change_qoq": None,
    "fii_change_qoq": None,
    "flag_promoter_pledging_high": None,
    "flag_promoter_stake_declining": None,
    "flag_fii_accumulating": None,
}
_WC_NONE: dict = {
    "debtor_days_trend": None,
    "creditor_days_trend": None,
    "cash_conversion_cycle_latest": None,
    "ccc_trend": None,
    "flag_working_capital_deteriorating": None,
    "deterioration_reason": None,
}
_CONCALL_NONE: dict = {
    "mgmt_commentary_snippet": None,
    "mgmt_guided_revenue_growth": None,
    "mgmt_guided_margin": None,
    "guidance_text_snippet": None,
    "concall_source": None,
}
_DIVIDEND_NONE: dict = {
    "dividend_per_share_5yr": None,
    "dividend_yield_latest": None,
    "dividend_consistency": None,
    "dividend_growing": None,
    "buyback_announced": False,
}


class LightweightGovernance(BaseModel):
    # Auditor (fetch_auditor_data)
    current_auditor_name: Optional[str] = None
    previous_auditor: Optional[str] = None
    flag_auditor_changed: Optional[bool] = None

    # CARO (fetch_caro_status)
    flag_caro_qualified: Optional[bool] = None
    caro_source_note: Optional[str] = None

    # Shareholding trend (fetch_shareholding_trend)
    promoter_holding_pct: Optional[float] = None
    promoter_pledged_pct: Optional[float] = None
    fii_holding_pct: Optional[float] = None
    dii_holding_pct: Optional[float] = None
    promoter_holding_trend: Optional[List[Optional[float]]] = None
    promoter_pledged_trend: Optional[List[Optional[float]]] = None
    fii_trend: Optional[List[Optional[float]]] = None
    dii_trend: Optional[List[Optional[float]]] = None
    promoter_holding_change_qoq: Optional[float] = None
    fii_change_qoq: Optional[float] = None
    flag_promoter_pledging_high: Optional[bool] = None
    flag_promoter_stake_declining: Optional[bool] = None
    flag_fii_accumulating: Optional[bool] = None

    # Working capital (compute_working_capital_health)
    debtor_days_trend: Optional[List[Optional[float]]] = None
    creditor_days_trend: Optional[List[Optional[float]]] = None
    cash_conversion_cycle_latest: Optional[float] = None
    ccc_trend: Optional[List[Optional[float]]] = None
    flag_working_capital_deteriorating: Optional[bool] = None
    deterioration_reason: Optional[str] = None

    # Concall (fetch_concall_snippet)
    mgmt_commentary_snippet: Optional[str] = None
    mgmt_guided_revenue_growth: Optional[float] = None
    mgmt_guided_margin: Optional[float] = None
    guidance_text_snippet: Optional[str] = None
    concall_source: Optional[str] = None

    # Dividend history (extract_dividend_history)
    dividend_per_share_5yr: Optional[List[Optional[float]]] = None
    dividend_yield_latest: Optional[float] = None
    dividend_consistency: Optional[bool] = None
    dividend_growing: Optional[bool] = None
    buyback_announced: Optional[bool] = None

    # Meta
    governance_source: str = "lightweight"
    data_fetched_at: Optional[datetime] = None
    fetch_duration_seconds: Optional[float] = None
    fields_populated: int = 0
    fields_total: int = _FIELDS_TOTAL
    fetch_warnings: List[str] = []

    def active_risk_flag_count(self) -> int:
        return sum(1 for f in _RISK_FLAGS if getattr(self, f, None) is True)


async def fetch_governance(
    ticker: str,
    bse_code: str,
    financials: dict,
) -> LightweightGovernance:
    """
    Run all 6 governance sub-fetches concurrently and return a
    LightweightGovernance instance. Results are cached per ticker for 24h.
    """
    cache_key = f"governance_{ticker.upper()}"
    cached = _GOVERNANCE_CACHE.get(cache_key)
    if cached and datetime.now() - cached["ts"] < _GOVERNANCE_TTL:
        return cached["data"]

    t0 = time.monotonic()
    loop = asyncio.get_running_loop()
    warnings: List[str] = []

    # 4 network-bound functions run concurrently; return_exceptions=True so
    # a single timeout doesn't cancel the rest
    raw = await asyncio.gather(
        loop.run_in_executor(None, scraper.fetch_auditor_data, ticker, bse_code or ""),
        loop.run_in_executor(None, scraper.fetch_caro_status, bse_code or ""),
        loop.run_in_executor(None, scraper.fetch_shareholding_trend, ticker),
        loop.run_in_executor(None, scraper.fetch_concall_snippet, ticker),
        return_exceptions=True,
    )

    def _safe(result, default, label):
        if isinstance(result, Exception):
            warnings.append(f"{label} fetch error: {result}")
            return default
        return result

    auditor_data    = _safe(raw[0], _AUDITOR_NONE,     "auditor")
    caro_data       = _safe(raw[1], _CARO_NONE,        "caro")
    shareholding_data = _safe(raw[2], _SHAREHOLDING_NONE, "shareholding")
    concall_data    = _safe(raw[3], _CONCALL_NONE,     "concall")

    # 2 compute-only functions — synchronous, no I/O
    try:
        wc_data = scraper.compute_working_capital_health(financials)
    except Exception as e:
        warnings.append(f"WC compute error: {e}")
        wc_data = _WC_NONE

    try:
        div_data = scraper.extract_dividend_history(financials)
    except Exception as e:
        warnings.append(f"Dividend extract error: {e}")
        div_data = _DIVIDEND_NONE

    duration = round(time.monotonic() - t0, 2)

    merged = {
        **auditor_data,
        **caro_data,
        **shareholding_data,
        **wc_data,
        **concall_data,
        **div_data,
    }

    populated = sum(1 for v in merged.values() if v is not None)

    gov = LightweightGovernance(
        **merged,
        governance_source="lightweight",
        data_fetched_at=datetime.now(),
        fetch_duration_seconds=duration,
        fields_populated=populated,
        fields_total=_FIELDS_TOTAL,
        fetch_warnings=warnings,
    )

    _GOVERNANCE_CACHE[cache_key] = {"ts": datetime.now(), "data": gov}
    return gov
