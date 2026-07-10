import streamlit as st
import scraper
import valuation
import exporter

st.set_page_config(page_title="Stock Valuation", page_icon="📊", layout="wide")

# ── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("About the Methods")
    st.markdown("""
**Method 1 — DCF**
Projects revenue for 10 years using two growth rates, ramps margins to your target,
proxies FCF at 85% of net profit, discounts at WACC, and adds a terminal value.
Compares intrinsic value per share to current price.

---

**Method 2 — Reverse Growth Check**
Asks: what revenue CAGR does the *market already price in*?
If the implied CAGR is much higher than your assumed CAGR, the stock is priced for
growth you don't expect — likely overvalued.

---

**Method 3 — Historical P/S**
Compares the current Price-to-Sales ratio against the stock's own 5-year average.
Skipped if fewer than 3 years of data are available.

---

**Final Verdict**
Counts undervalued vs overvalued signals across all three methods:
- 3 undervalued → **Strong Buy Signal**
- 2 undervalued → **Lean Undervalued**
- 3 overvalued → **Strong Avoid Signal**
- 2 overvalued → **Lean Overvalued**
- Otherwise → **Mixed Signals**
""")

# ── Main UI ──────────────────────────────────────────────────────────────────
st.title("📊 Stock Valuation — NSE")
st.caption("Enter a company name or NSE ticker to fetch financials from Screener.in and run a 3-method valuation.")

col_search, col_btn = st.columns([4, 1])
with col_search:
    ticker_input = st.text_input(
        "Company Name or NSE Ticker",
        placeholder="e.g. RELIANCE or TCS",
        label_visibility="collapsed",
    )
with col_btn:
    fetch_clicked = st.button("Fetch & Analyse", type="primary", use_container_width=True)

st.divider()

# ── Session state init ────────────────────────────────────────────────────────
if "financials" not in st.session_state:
    st.session_state.financials = {}
if "ticker" not in st.session_state:
    st.session_state.ticker = ""
if "fetch_error" not in st.session_state:
    st.session_state.fetch_error = None

# ── Fetch financials ──────────────────────────────────────────────────────────
if fetch_clicked and ticker_input.strip():
    st.session_state.ticker = ticker_input.strip()
    with st.spinner(f"Fetching data for **{ticker_input.strip()}** from Screener.in…"):
        try:
            data = scraper.scrape(ticker_input.strip())
            st.session_state.financials = data
            st.session_state.fetch_error = None
        except Exception as e:
            st.session_state.fetch_error = str(e)
            st.session_state.financials = {}

if st.session_state.fetch_error:
    st.error(f"Scrape failed: {st.session_state.fetch_error}")
    st.info("You can still enter all values manually below.")

# ── Financials display + manual override ─────────────────────────────────────
fin = st.session_state.financials

def manual_field(label, key, fin_key, unit="Cr", help_text=None):
    """Show a number input, pre-filled if scraped, with a warning badge if missing."""
    scraped = fin.get(fin_key)
    if scraped is not None:
        default = float(scraped)
        placeholder = f"Scraped: {round(scraped, 2)}"
    else:
        default = 0.0
        placeholder = "Could not fetch — enter manually"

    return st.number_input(
        f"{label} ({unit})" if unit else label,
        value=default,
        help=help_text or placeholder,
        key=key,
        format="%.2f",
    )

if fin or st.session_state.fetch_error:
    company_name = fin.get("company_name") or st.session_state.ticker.upper()
    st.subheader(f"Financials — {company_name}")

    failed = fin.get("errors", [])
    if failed and "all_fields" not in failed:
        st.warning(f"Some fields could not be scraped: **{', '.join(failed)}**. Please fill them in manually.")

    # Financials table (scraped values)
    if fin and "all_fields" not in failed:
        table_data = {
            "Field": [
                "Market Cap (Cr)", "Share Price (INR)", "Revenue (Cr)",
                "Net Profit (Cr)", "EPS (INR)", "Total Debt (Cr)",
                "Cash (Cr)", "Years of Data", "5Y Avg P/S",
            ],
            "Value": [
                fin.get("market_cap", "N/A"),
                fin.get("share_price", "N/A"),
                fin.get("revenue", "N/A"),
                fin.get("net_profit", "N/A"),
                fin.get("eps", "N/A"),
                fin.get("debt", "N/A"),
                fin.get("cash", "N/A"),
                fin.get("years_listed", "N/A"),
                round(fin.get("five_yr_avg_ps"), 2) if fin.get("five_yr_avg_ps") else "N/A",
            ],
        }
        st.table(table_data)

    st.subheader("Override / Fill Missing Financials")
    st.caption("Pre-filled from Screener.in where available. Edit if needed.")

    c1, c2, c3 = st.columns(3)
    with c1:
        revenue = manual_field("Revenue / Sales", "inp_revenue", "revenue")
        net_profit = manual_field("Net Profit / PAT", "inp_net_profit", "net_profit")
        eps = manual_field("EPS (Basic)", "inp_eps", "eps", unit="INR")
    with c2:
        debt = manual_field("Total Debt / Borrowings", "inp_debt", "debt")
        cash = manual_field("Cash & Equivalents", "inp_cash", "cash")
        market_cap = manual_field("Market Cap", "inp_market_cap", "market_cap")
    with c3:
        share_price = manual_field("Current Share Price", "inp_share_price", "share_price", unit="INR")
        shares_out = manual_field(
            "Shares Outstanding", "inp_shares", "shares_outstanding", unit="count",
            help_text="Auto-calculated from Market Cap / Price if not entered",
        )
        five_yr_ps = manual_field("5Y Avg P/S", "inp_5yr_ps", "five_yr_avg_ps", unit="x")

    years_listed = fin.get("years_listed", 0)

    st.divider()

    # ── Assumptions ───────────────────────────────────────────────────────────
    st.subheader("Your Assumptions")
    a1, a2 = st.columns(2)
    with a1:
        g1 = st.slider("Revenue Growth Rate — Years 1–5 (%)", 0, 50, 15)
        g2 = st.slider("Revenue Growth Rate — Years 6–10 (%)", 0, 40, 10)
        tm = st.slider("Target Net Profit Margin — Year 10 (%)", 0, 50, 15)
    with a2:
        wacc = st.slider("Discount Rate / WACC (%)", 8, 25, 14)
        tg = st.slider("Terminal Growth Rate (%)", 2, 8, 5)
        sector_ps = st.number_input("Sector P/S Multiple", value=5.0, step=0.5)
        sector_pe = st.number_input("Sector P/E Multiple", value=25.0, step=1.0)

    st.divider()

    # ── Run Valuation ─────────────────────────────────────────────────────────
    if st.button("Run Valuation", type="primary"):
        # Derive shares if not entered
        derived_shares = shares_out
        if derived_shares == 0 and market_cap > 0 and share_price > 0:
            derived_shares = (market_cap * 1e7) / share_price

        # ── Method 1: DCF
        dcf = valuation.dcf_valuation(
            revenue=revenue,
            net_profit=net_profit,
            market_cap=market_cap,
            share_price=share_price,
            shares_outstanding=derived_shares,
            debt=debt,
            cash=cash,
            growth_rate_1_5=g1,
            growth_rate_6_10=g2,
            target_margin=tm,
            wacc=wacc,
            terminal_rate=tg,
        )

        # ── Method 2: Reverse growth
        rev_growth = valuation.reverse_growth_check(
            revenue=revenue,
            market_cap=market_cap,
            wacc=wacc,
            target_margin=tm,
            sector_ps=sector_ps,
            growth_rate_1_5=g1,
            growth_rate_6_10=g2,
        )

        # ── Method 3: Historical P/S
        ps_result = valuation.historical_ps_check(
            market_cap=market_cap,
            revenue=revenue,
            five_yr_avg_ps=five_yr_ps if five_yr_ps > 0 else None,
            years_listed=years_listed,
        )

        # ── Final scorecard
        final_label, final_type = valuation.final_scorecard(
            dcf["verdict"], rev_growth["verdict"], ps_result["verdict"]
        )

        # ── Display results ───────────────────────────────────────────────────
        def verdict_badge(v):
            color = {"Undervalued": "green", "Overvalued": "red", "Fairly Valued": "orange",
                     "Skipped": "gray", "Insufficient data": "gray"}.get(v, "gray")
            return f":{color}[**{v}**]"

        st.header("Valuation Results")

        # Method 1
        with st.expander("Method 1 — DCF on Earnings", expanded=True):
            m1c1, m1c2 = st.columns(2)
            with m1c1:
                st.metric("Intrinsic Value / Share",
                          f"₹{dcf['intrinsic_per_share']:.2f}" if dcf['intrinsic_per_share'] else "N/A")
                st.metric("DCF Upside",
                          f"{dcf['upside_pct']:.1f}%" if dcf['upside_pct'] is not None else "N/A",
                          delta=f"{dcf['upside_pct']:.1f}%" if dcf['upside_pct'] is not None else None)
            with m1c2:
                st.metric("PV of FCFs (Cr)", f"₹{dcf['sum_pv_fcf']:,.0f}")
                st.metric("PV Terminal Value (Cr)", f"₹{dcf['pv_terminal']:,.0f}")
            st.markdown(f"**Verdict:** {verdict_badge(dcf['verdict'])}")

            # Year-by-year table
            yr_data = {
                "Year": list(range(1, 11)),
                "Revenue (Cr)": [round(r, 0) for r in dcf["revenues"]],
                "Margin (%)": [round(m * 100, 1) for m in dcf["margins"]],
                "FCF (Cr)": [round(f, 0) for f in dcf["fcfs"]],
                "PV FCF (Cr)": [round(p, 0) for p in dcf["pv_fcfs"]],
            }
            st.dataframe(yr_data, use_container_width=True)

        # Method 2
        with st.expander("Method 2 — Reverse Growth Check", expanded=True):
            m2c1, m2c2, m2c3 = st.columns(3)
            with m2c1:
                st.metric("Your Assumed CAGR",
                          f"{rev_growth['your_cagr']:.1f}%" if rev_growth['your_cagr'] is not None else "N/A")
            with m2c2:
                st.metric("Market-Implied CAGR",
                          f"{rev_growth['implied_cagr']:.1f}%" if rev_growth['implied_cagr'] is not None else "N/A")
            with m2c3:
                st.metric("Gap (Implied − Yours)",
                          f"{rev_growth['gap']:.1f} pp" if rev_growth['gap'] is not None else "N/A",
                          delta=f"{rev_growth['gap']:.1f} pp" if rev_growth['gap'] is not None else None,
                          delta_color="inverse")
            if rev_growth.get("implied_rev_yr10"):
                st.caption(f"Implied Year 10 Revenue: ₹{rev_growth['implied_rev_yr10']:,.0f} Cr")
            st.markdown(f"**Verdict:** {verdict_badge(rev_growth['verdict'])}")

        # Method 3
        with st.expander("Method 3 — Historical P/S", expanded=True):
            if ps_result["verdict"] in ("Skipped", "Insufficient data"):
                st.info(ps_result.get("reason", "Skipped"))
            else:
                m3c1, m3c2, m3c3 = st.columns(3)
                with m3c1:
                    st.metric("Current P/S",
                              f"{ps_result['current_ps']:.2f}x" if ps_result['current_ps'] else "N/A")
                with m3c2:
                    st.metric("5Y Avg P/S",
                              f"{ps_result['five_yr_avg_ps']:.2f}x" if ps_result['five_yr_avg_ps'] else "N/A")
                with m3c3:
                    st.metric("Current / Avg",
                              f"{ps_result['ratio_to_avg']:.2f}x" if ps_result['ratio_to_avg'] else "N/A")
            st.markdown(f"**Verdict:** {verdict_badge(ps_result['verdict'])}")

        st.divider()

        # ── Final verdict card ────────────────────────────────────────────────
        card_color = {"undervalued": "#d4edda", "overvalued": "#f8d7da", "mixed": "#fff3cd"}.get(final_type, "#e2e3e5")
        border_color = {"undervalued": "#28a745", "overvalued": "#dc3545", "mixed": "#ffc107"}.get(final_type, "#6c757d")
        text_color = {"undervalued": "#155724", "overvalued": "#721c24", "mixed": "#856404"}.get(final_type, "#383d41")

        st.markdown(
            f"""
            <div style="
                background-color: {card_color};
                border: 2px solid {border_color};
                border-radius: 10px;
                padding: 24px;
                text-align: center;
            ">
                <h2 style="color: {text_color}; margin: 0;">{final_label}</h2>
                <p style="color: {text_color}; margin: 8px 0 0 0; font-size: 15px;">
                    Based on {sum([
                        dcf['verdict'] == 'Undervalued',
                        rev_growth['verdict'] == 'Undervalued',
                        ps_result['verdict'] == 'Undervalued'
                    ])} undervalued / {sum([
                        dcf['verdict'] == 'Overvalued',
                        rev_growth['verdict'] == 'Overvalued',
                        ps_result['verdict'] == 'Overvalued'
                    ])} overvalued signals across 3 methods
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.divider()

        # ── Excel download ────────────────────────────────────────────────────
        assumptions_dict = {
            "growth_rate_1_5": g1,
            "growth_rate_6_10": g2,
            "target_margin": tm,
            "wacc": wacc,
            "terminal_rate": tg,
            "sector_ps": sector_ps,
            "sector_pe": sector_pe,
        }

        financials_for_export = {
            "company_name": company_name,
            "market_cap": market_cap,
            "share_price": share_price,
            "revenue": revenue,
            "net_profit": net_profit,
            "eps": eps,
            "debt": debt,
            "cash": cash,
            "shares_outstanding": derived_shares,
            "five_yr_avg_ps": five_yr_ps if five_yr_ps > 0 else None,
            "years_listed": years_listed,
        }

        excel_bytes = exporter.build_excel(
            financials=financials_for_export,
            assumptions=assumptions_dict,
            dcf=dcf,
            rev_growth=rev_growth,
            ps=ps_result,
            final_label=final_label,
            ticker=st.session_state.ticker,
        )

        st.download_button(
            label="Download Results as Excel",
            data=excel_bytes,
            file_name=f"{st.session_state.ticker.upper()}_valuation.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

elif not fetch_clicked:
    st.info("Enter a ticker above and click **Fetch & Analyse** to begin.")
