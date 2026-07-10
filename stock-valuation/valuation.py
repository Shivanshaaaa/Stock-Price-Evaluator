"""
All valuation calculations. Pure Python, no external deps.
All monetary inputs are in Crores (INR) to match Screener.in units.
"""
import statistics as _stats


def dcf_valuation(
    revenue,
    net_profit,
    market_cap,
    share_price,
    shares_outstanding,
    debt,
    cash,
    growth_rate_1_5,
    growth_rate_6_10,
    target_margin,
    wacc,
    terminal_rate,
    esop_dilution_pct=None,
):
    """
    Returns a dict with all intermediate values and final verdict.
    growth_rate_1_5, growth_rate_6_10, target_margin, wacc, terminal_rate are in %.
    """
    g1 = growth_rate_1_5 / 100
    g2 = growth_rate_6_10 / 100
    w = wacc / 100
    tg = terminal_rate / 100
    current_margin = (net_profit / revenue) if revenue and revenue > 0 else 0
    target_m = target_margin / 100

    # Project revenues
    revenues = []
    rev = revenue
    for yr in range(1, 11):
        g = g1 if yr <= 5 else g2
        rev = rev * (1 + g)
        revenues.append(rev)

    # Ramp margin linearly from current to target over 10 years
    margins = [
        current_margin + (target_m - current_margin) * (yr / 10)
        for yr in range(1, 11)
    ]

    # Net profits and FCF proxy
    net_profits = [revenues[i] * margins[i] for i in range(10)]
    fcfs = [np * 0.85 for np in net_profits]

    # Discount FCFs
    pv_fcfs = [fcfs[i] / ((1 + w) ** (i + 1)) for i in range(10)]
    sum_pv_fcf = sum(pv_fcfs)

    # Terminal value
    fcf_yr10 = fcfs[9]
    if w <= tg:
        terminal_value = 0
    else:
        terminal_value = fcf_yr10 * (1 + tg) / (w - tg)
    pv_terminal = terminal_value / ((1 + w) ** 10)

    # Net debt
    net_debt = (debt or 0) - (cash or 0)

    # Equity value in Crores
    equity_value = sum_pv_fcf + pv_terminal - net_debt

    # ESOP dilution
    esop_pct = (esop_dilution_pct or 0) / 100
    diluted_shares = shares_outstanding * (1 + esop_pct) if shares_outstanding and shares_outstanding > 0 else shares_outstanding

    # Intrinsic per share (diluted)
    if diluted_shares and diluted_shares > 0:
        intrinsic_per_share = (equity_value * 1e7) / diluted_shares
    else:
        intrinsic_per_share = None

    # Impact of dilution vs undiluted
    dilution_impact_per_share = None
    if esop_pct > 0 and shares_outstanding and shares_outstanding > 0 and intrinsic_per_share is not None:
        intrinsic_undiluted = (equity_value * 1e7) / shares_outstanding
        dilution_impact_per_share = intrinsic_per_share - intrinsic_undiluted  # negative

    # DCF upside
    if intrinsic_per_share and share_price and share_price > 0:
        upside_pct = (intrinsic_per_share - share_price) / share_price * 100
    else:
        upside_pct = None

    if upside_pct is None:
        verdict = "Insufficient data"
    elif upside_pct > 20:
        verdict = "Undervalued"
    elif upside_pct < -20:
        verdict = "Overvalued"
    else:
        verdict = "Fairly Valued"

    return {
        "revenues": revenues,
        "margins": margins,
        "net_profits": net_profits,
        "fcfs": fcfs,
        "pv_fcfs": pv_fcfs,
        "sum_pv_fcf": sum_pv_fcf,
        "terminal_value": terminal_value,
        "pv_terminal": pv_terminal,
        "net_debt": net_debt,
        "equity_value": equity_value,
        "intrinsic_per_share": intrinsic_per_share,
        "upside_pct": upside_pct,
        "verdict": verdict,
        "esop_dilution_pct": esop_dilution_pct if esop_pct > 0 else None,
        "dilution_impact_per_share": dilution_impact_per_share,
    }


def reverse_growth_check(
    revenue,
    ev,
    wacc,
    target_margin,
    sector_ev_rev,
    growth_rate_1_5,
    growth_rate_6_10,
):
    """
    Implied CAGR from enterprise value vs user's blended CAGR.
    Uses EV so debt-heavy companies are not falsely flagged as cheap.
    Returns dict with key numbers and verdict.
    """
    w = wacc / 100
    tm = target_margin / 100

    # Implied Year 10 revenue from current EV (all in Crores)
    implied_rev_yr10 = (ev * (1 + w) ** 10) / (sector_ev_rev * tm) if tm > 0 else None

    if implied_rev_yr10 is None or revenue is None or revenue <= 0:
        return {
            "implied_rev_yr10": None,
            "implied_cagr": None,
            "your_cagr": None,
            "gap": None,
            "verdict": "Insufficient data",
        }

    implied_cagr = (implied_rev_yr10 / revenue) ** (1 / 10) - 1

    # Blended CAGR: simple average of two rates (5 years each)
    your_cagr = (growth_rate_1_5 / 100 * 5 + growth_rate_6_10 / 100 * 5) / 10

    gap = (implied_cagr - your_cagr) * 100  # in percentage points

    if gap > 10:
        verdict = "Overvalued"
    elif gap < -10:
        verdict = "Undervalued"
    else:
        verdict = "Fairly Valued"

    return {
        "implied_rev_yr10": implied_rev_yr10,
        "implied_cagr": implied_cagr * 100,
        "your_cagr": your_cagr * 100,
        "gap": gap,
        "verdict": verdict,
    }


def historical_ev_rev_check(ev, revenue, five_yr_avg_ev_rev, years_listed):
    """
    Compare current EV/Revenue to 5Y historical average EV/Revenue.
    Returns dict with verdict.
    """
    if years_listed < 3:
        return {
            "current_ev_rev": None,
            "five_yr_avg_ev_rev": None,
            "ratio_to_avg": None,
            "verdict": "Skipped",
            "reason": "Insufficient listing history (< 3 years of data)",
        }

    if five_yr_avg_ev_rev is None:
        return {
            "current_ev_rev": None,
            "five_yr_avg_ev_rev": None,
            "ratio_to_avg": None,
            "verdict": "Skipped",
            "reason": "5Y average EV/Revenue not available",
        }

    if revenue is None or revenue <= 0 or ev is None or ev <= 0:
        return {
            "current_ev_rev": None,
            "five_yr_avg_ev_rev": None,
            "ratio_to_avg": None,
            "verdict": "Insufficient data",
            "reason": "Missing revenue or EV",
        }

    current_ev_rev = ev / revenue
    ratio = current_ev_rev / five_yr_avg_ev_rev

    if ratio < 0.85:
        verdict = "Undervalued"
    elif ratio > 1.15:
        verdict = "Overvalued"
    else:
        verdict = "Fairly Valued"

    return {
        "current_ev_rev": current_ev_rev,
        "five_yr_avg_ev_rev": five_yr_avg_ev_rev,
        "ratio_to_avg": ratio,
        "verdict": verdict,
        "reason": None,
    }


def pe_valuation(eps, share_price, sector_pe, ebitda=None, ev=None, sector_ev_ebitda=None):
    """
    P/E-based fair value: Intrinsic = EPS × Sector P/E.
    Optionally computes EV/EBITDA vs sector median as a secondary display.
    eps and share_price in INR; ebitda and ev in Crores.
    """
    if not eps or eps <= 0 or not sector_pe or sector_pe <= 0:
        return {
            "intrinsic_per_share": None,
            "upside_pct": None,
            "verdict": "Insufficient data",
            "reason": "EPS or Sector P/E not available",
            "ev_ebitda": None,
            "sector_ev_ebitda": sector_ev_ebitda,
            "ev_ebitda_vs_sector": None,
        }
    intrinsic = eps * sector_pe
    if share_price and share_price > 0:
        upside_pct = (intrinsic - share_price) / share_price * 100
    else:
        upside_pct = None

    if upside_pct is None:
        verdict = "Insufficient data"
    elif upside_pct > 20:
        verdict = "Undervalued"
    elif upside_pct < -20:
        verdict = "Overvalued"
    else:
        verdict = "Fairly Valued"

    # EV/EBITDA secondary (display only, does not affect verdict)
    ev_ebitda = None
    ev_ebitda_vs_sector = None
    if ev and ev > 0 and ebitda and ebitda > 0:
        ev_ebitda = ev / ebitda
        if sector_ev_ebitda and sector_ev_ebitda > 0:
            ev_ebitda_vs_sector = ev_ebitda / sector_ev_ebitda

    return {
        "intrinsic_per_share": intrinsic,
        "upside_pct": upside_pct,
        "verdict": verdict,
        "reason": None,
        "ev_ebitda": ev_ebitda,
        "sector_ev_ebitda": sector_ev_ebitda,
        "ev_ebitda_vs_sector": ev_ebitda_vs_sector,
    }


_VERDICT_WEIGHTS = {
    "High":   {"dcf": 0.35, "rev_growth": 0.25, "ev_rev": 0.20, "pe": 0.20},
    "Medium": {"dcf": 0.20, "rev_growth": 0.30, "ev_rev": 0.25, "pe": 0.25},
    "Low":    {"dcf": 0.10, "rev_growth": 0.30, "ev_rev": 0.30, "pe": 0.30},
}

_SIGNAL_SCORE = {"Undervalued": 1, "Fairly Valued": 0, "Overvalued": -1}


def dcf_reliability_score(revenue_history, net_profit, revenue, debt, market_cap):
    """
    Returns (score: float 0–1, label: 'Low'|'Medium'|'High').
    Rules:
      +0.3  ≥5 years of revenue history
      +0.3  revenue growth std dev < 15% (stable grower)
      +0.2  PAT margin > 8%
      +0.2  D/E (debt/market_cap) < 1
    """
    score = 0.0

    if revenue_history and len(revenue_history) >= 5:
        score += 0.3
        growths = [
            (revenue_history[i] - revenue_history[i - 1]) / revenue_history[i - 1]
            for i in range(1, len(revenue_history))
            if revenue_history[i - 1] > 0
        ]
        if len(growths) >= 4 and _stats.stdev(growths) * 100 < 15:
            score += 0.3

    if revenue and revenue > 0 and net_profit is not None:
        if net_profit / revenue > 0.08:
            score += 0.2

    if market_cap and market_cap > 0:
        if (debt or 0) / market_cap < 1:
            score += 0.2

    score = min(round(score, 2), 1.0)
    label = "Low" if score < 0.4 else ("Medium" if score <= 0.7 else "High")
    return score, label


_GOVERNANCE_RISK_FLAGS = (
    "flag_auditor_changed",
    "flag_caro_qualified",
    "flag_promoter_pledging_high",
    "flag_promoter_stake_declining",
    "flag_working_capital_deteriorating",
)


def weighted_scorecard(dcf_verdict, rev_growth_verdict, ev_rev_verdict, pe_verdict,
                       reliability_label, governance=None):
    """
    Returns a dict with final label/type, weighted score, per-method contributions,
    and the weights applied. Skipped/Insufficient methods are excluded and weights
    are renormalised so the score remains on the same –1 to +1 scale.

    governance: optional dict from LightweightGovernance.model_dump(). When supplied,
    each active risk flag subtracts 0.05 from the weighted score (floor –1.0). Three
    or more active flags also appends a warning to governance_warnings.
    """
    weights = _VERDICT_WEIGHTS.get(reliability_label, _VERDICT_WEIGHTS["Medium"])
    verdicts = {
        "dcf":        dcf_verdict,
        "rev_growth": rev_growth_verdict,
        "ev_rev":     ev_rev_verdict,
        "pe":         pe_verdict,
    }

    contributions = {}
    active_weight = 0.0
    weighted_sum  = 0.0

    for method, verdict in verdicts.items():
        sig = _SIGNAL_SCORE.get(verdict)
        w   = weights[method]
        if sig is not None:
            contrib       = sig * w
            weighted_sum  += contrib
            active_weight += w
        else:
            contrib = None
        contributions[method] = {
            "verdict":      verdict,
            "signal_score": sig,
            "weight":       w,
            "contribution": round(contrib, 4) if contrib is not None else None,
        }

    # Renormalise when some methods are skipped so the scale stays –1 to +1
    if 0 < active_weight < 1.0:
        weighted_sum = weighted_sum / active_weight

    ws = round(weighted_sum, 3)

    # Governance penalty: –0.05 per active risk flag; floor at –1.0
    governance_warnings: list = []
    if governance is not None:
        active_flags = [f for f in _GOVERNANCE_RISK_FLAGS if governance.get(f) is True]
        if active_flags:
            penalty = round(0.05 * len(active_flags), 3)
            ws = round(max(ws - penalty, -1.0), 3)
        if len(active_flags) >= 3:
            governance_warnings.append(
                f"{len(active_flags)} active governance flags — elevated risk: "
                + ", ".join(active_flags)
            )

    if ws > 0.5:
        label, type_ = "Strong Buy Signal", "undervalued"
    elif ws > 0.2:
        label, type_ = "Lean Undervalued", "undervalued"
    elif ws < -0.5:
        label, type_ = "Strong Avoid Signal", "overvalued"
    elif ws < -0.2:
        label, type_ = "Lean Overvalued", "overvalued"
    else:
        label, type_ = "Mixed Signals", "mixed"

    return {
        "final_label":         label,
        "final_type":          type_,
        "weighted_score":      ws,
        "reliability_label":   reliability_label,
        "weights_used":        weights,
        "contributions":       contributions,
        "governance_warnings": governance_warnings,
    }
