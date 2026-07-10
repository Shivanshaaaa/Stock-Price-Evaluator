"""
Standardised handoff contract between Tool B (equity research agent) and
Tool A (stock valuation web app).

Tool B writes:  <base_dir>/<TICKER>/thesis-output.json
Tool A reads:   same path via ResearchHandoff.load()

No other integration code should hardcode field names — import from here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field


class ResearchHandoff(BaseModel):

    # ── Identity ──────────────────────────────────────────────────────────────
    ticker: str
    company_name: str
    generated_at: datetime
    data_as_of: str                     # e.g. "Q3 FY25"
    source_tier: str                    # "screener" | "bse_nse" | "third_party"
    is_consolidated: bool
    cache_valid_until: datetime

    # ── Valuation inputs (pre-populate sliders) ───────────────────────────────
    cmp: Optional[float] = None
    market_cap_cr: Optional[float] = None
    total_debt_cr: Optional[float] = None
    cash_cr: Optional[float] = None
    shares_outstanding_cr: Optional[float] = None
    revenue_ttm_cr: Optional[float] = None
    ebitda_ttm_cr: Optional[float] = None
    pat_ttm_cr: Optional[float] = None
    eps_ttm: Optional[float] = None
    revenue_cagr_3yr: Optional[float] = None
    revenue_cagr_5yr: Optional[float] = None
    pat_cagr_3yr: Optional[float] = None
    roce_5yr_avg: Optional[float] = None
    roe_5yr_avg: Optional[float] = None
    historical_ps_5yr_avg: Optional[float] = None
    sector_median_pe: Optional[float] = None
    sector_name: Optional[str] = None

    # ── Management-derived growth signal (from concall parsing) ───────────────
    mgmt_guided_revenue_growth: Optional[float] = None
    mgmt_guidance_confidence: Optional[str] = None  # "high"|"medium"|"low"|None
    guidance_vs_delivery_score: Optional[float] = None  # 0–1; 1 = always delivers
    guidance_source: Optional[str] = None              # e.g. "Q4FY25 concall"

    # ── Governance red flags (True = flag is active) ──────────────────────────
    flag_caro_qualified: Optional[bool] = None
    flag_auditor_changed: Optional[bool] = None
    flag_rpt_elevated: Optional[bool] = None
    flag_contingent_liab_rising: Optional[bool] = None
    flag_promoter_pledging_high: Optional[bool] = None
    flag_esop_dilution_material: Optional[bool] = None
    flag_working_capital_deteriorating: Optional[bool] = None
    flag_subsidiary_opacity: Optional[bool] = None

    # ── Shareholding ──────────────────────────────────────────────────────────
    promoter_holding_pct: Optional[float] = None
    promoter_pledged_pct: Optional[float] = None
    fii_holding_pct: Optional[float] = None
    dii_holding_pct: Optional[float] = None
    promoter_holding_trend: Optional[List[float]] = None  # last 8 quarters

    # ── Qualitative outputs ───────────────────────────────────────────────────
    conviction_label: Optional[str] = None   # "Interesting"|"Watchlist"|"Avoid"|"Pass"
    bull_thesis: Optional[str] = None
    bear_thesis: Optional[str] = None
    red_flags_summary: Optional[List[str]] = None
    peer_comparison: Optional[List[dict]] = None

    # ── Source provenance ─────────────────────────────────────────────────────
    sources_used: Optional[List[str]] = None
    warnings: Optional[List[str]] = None

    # ── Methods ───────────────────────────────────────────────────────────────

    def save(self, output_dir: Path) -> Path:
        """Write thesis-output.json into output_dir. Creates the directory if needed."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / "thesis-output.json"
        out_path.write_text(
            self.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return out_path

    @classmethod
    def load(cls, ticker: str, base_dir: Path) -> Optional["ResearchHandoff"]:
        """
        Read and validate thesis-output.json for ticker.
        Returns None if the file does not exist.
        """
        path = Path(base_dir) / ticker.upper() / "thesis-output.json"
        if not path.exists():
            return None
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    @classmethod
    def is_cache_valid(cls, ticker: str, base_dir: Path) -> bool:
        """Return True if thesis-output.json exists and cache_valid_until is in the future."""
        handoff = cls.load(ticker, base_dir)
        if handoff is None:
            return False
        now = datetime.now(tz=timezone.utc)
        expiry = handoff.cache_valid_until
        # normalise to UTC if the stored datetime is naive
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry > now
