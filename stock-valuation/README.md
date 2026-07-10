# Stock Valuation App

A Streamlit web app that scrapes Screener.in for company financials and runs a 3-method valuation (DCF, Reverse Growth Check, Historical P/S).

## Setup

**Requirements:** Python 3.9+

```bash
cd stock-valuation
pip install -r requirements.txt
```

## Run

```bash
streamlit run app.py
```

The app opens at `http://localhost:8501` in your browser.

## How to Use

1. Enter a **NSE ticker** (e.g. `RELIANCE`, `TCS`, `INFY`) in the search box
2. Click **Fetch & Analyse** — financials are pulled from Screener.in
3. If any fields fail to scrape, fill them in manually
4. Adjust the assumption sliders (growth rates, WACC, margins)
5. Click **Run Valuation** to see results
6. Download the full analysis as an Excel file

## Valuation Methods

| Method | What it tests |
|--------|--------------|
| DCF | Are future cash flows worth more than today's price? |
| Reverse Growth | Is the market pricing in growth you don't expect? |
| Historical P/S | Is the stock cheap vs its own history? |

## Notes

- All monetary values are in **Indian Rupees (Crores)** to match Screener.in
- Scraping depends on Screener.in's page structure; if the site changes, some fields may fail
- This is a tool for analysis, not financial advice
