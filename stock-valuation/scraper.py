import calendar
import requests
from bs4 import BeautifulSoup
from datetime import date
import re


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
    "X-Requested-With": "XMLHttpRequest",
}

_BSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.bseindia.com",
    "Referer": "https://www.bseindia.com/",
}

BASE_URL = "https://www.screener.in/company/{ticker}/"

_QTR_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_period_date(text):
    """Parse 'Sep 2024' → date(2024, 9, 30). Returns None on failure."""
    if not text:
        return None
    m = re.match(r"([A-Za-z]{3})\s+(\d{4})", text.strip())
    if not m:
        return None
    month = _QTR_MONTHS.get(m.group(1).lower())
    year = int(m.group(2))
    if not month:
        return None
    return date(year, month, calendar.monthrange(year, month)[1])


def _to_fy_quarter(dt):
    """date(2024, 12, 31) → 'Q3 FY25' using Indian fiscal year (Apr–Mar)."""
    if dt is None:
        return None
    m, yr = dt.month, dt.year
    if m >= 4:
        fy = yr + 1
        q = 1 if m <= 6 else (2 if m <= 9 else 3)
    else:
        fy, q = yr, 4
    return f"Q{q} FY{fy % 100:02d}"


def _extract_latest_quarter(soup):
    """Returns (period_text, date) from the quarterly results header row."""
    section = soup.find("section", {"id": "quarters"})
    if not section:
        return None, None
    header_row = section.find("tr")
    if not header_row:
        return None, None
    ths = header_row.find_all(["th", "td"])
    periods = [th.get_text(strip=True) for th in ths[1:] if th.get_text(strip=True)]
    if not periods:
        return None, None
    latest = periods[-1]
    return latest, _parse_period_date(latest)


def _extract_ratio_history(soup, *labels):
    """Search all table rows for a row whose first cell contains any label (substring).
    Returns numeric values oldest → newest, or [] if not found."""
    for tr in soup.find_all("tr"):
        tds = tr.find_all(["td", "th"])
        if len(tds) < 3:
            continue
        row_label = tds[0].get_text(strip=True).lower()
        if any(lbl.lower() in row_label for lbl in labels):
            vals = []
            for td in tds[1:]:
                v = _parse_number(td.get_text(strip=True))
                if v is not None:
                    vals.append(v)
            if vals:
                return vals
    return []


def _extract_shareholding(soup):
    """
    Extracts shareholding data from Screener's #shareholding section.
    Screener shows most-recent quarter first in columns.
    Returns a dict with promoter, pledging, FII, DII values.
    """
    out = {
        "promoter_holding": None,
        "promoter_holding_prev": None,
        "promoter_holding_history": [],   # oldest → newest, up to 8 quarters
        "promoter_pledged": None,
        "fii_holding": None,
        "fii_holding_prev": None,
        "dii_holding": None,
    }

    section = soup.find("section", {"id": "shareholding"})
    if not section:
        return out

    for tr in section.find_all("tr"):
        tds = tr.find_all(["td", "th"])
        if not tds:
            continue
        label = tds[0].get_text(strip=True).lower()

        vals = []
        for td in tds[1:]:
            v = _parse_number(td.get_text(strip=True))
            if v is not None:
                vals.append(v)

        if not vals:
            continue

        # Screener shows columns oldest→newest (left→right).
        # Use vals[-1] for the most recent value, vals[-2] for previous.
        # First-occurrence-wins: skip if field already populated from the
        # quarterly table (prevents the annual historical table from overwriting).
        latest = vals[-1]
        prev   = vals[-2] if len(vals) >= 2 else None
        history = list(vals[-8:])   # already oldest→newest

        if "promoter" in label and "pledg" not in label and "percent" not in label:
            if out["promoter_holding"] is None:
                out["promoter_holding"]         = latest
                out["promoter_holding_prev"]    = prev
                out["promoter_holding_history"] = history

        elif "pledg" in label or "pledge" in label:
            if out["promoter_pledged"] is None:
                out["promoter_pledged"] = latest

        elif any(k in label for k in ("fii", "foreign portfolio", "fpi",
                                       "foreign institutional")):
            if out["fii_holding"] is None:
                out["fii_holding"]      = latest
                out["fii_holding_prev"] = prev

        elif any(k in label for k in ("dii", "domestic institutional")):
            out["dii_holding"] = latest

    return out


def fetch_sector_screener(ticker):
    """
    Extract sector from screener.in/company/<TICKER>/ using the
    <a title="Broad Sector"> element, which is reliably present.
    Returns {"sector": str, "market_url": str | None}.
    Raises ValueError if sector cannot be found.
    """
    for url in (
        f"https://www.screener.in/company/{ticker.upper()}/consolidated/",
        f"https://www.screener.in/company/{ticker.upper()}/",
    ):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=12)
            if resp.status_code != 200:
                continue
        except Exception:
            continue

        soup = BeautifulSoup(resp.text, "html.parser")

        # Collect all /market/ hierarchy links — ordered broadest→most-specific
        # title="Broad Sector" → top-level, title="Industry" → leaf-level
        market_links = []
        for a in soup.find_all("a", title=True, href=re.compile(r"^/market/")):
            text = a.get_text(strip=True)
            if text and 2 < len(text) < 80:
                market_links.append((a["title"], text, a["href"]))

        if market_links:
            # Display: broad sector (first); peers: most-specific sub-industry (last)
            _, sector_text, _ = market_links[0]
            _, _, peer_url = market_links[-1]
            return {"sector": sector_text, "market_url": peer_url}

        # Fallback: any /market/ link without title
        for tag in soup.find_all("a", href=re.compile(r"^/market/[A-Z0-9]+/$")):
            text = tag.get_text(strip=True)
            if text and 2 < len(text) < 80:
                return {"sector": text, "market_url": tag["href"]}

        break

    raise ValueError(f"Screener: sector not found for {ticker}")


def _extract_bse_code(soup):
    """Extract 6-digit BSE scripcode from Screener company page."""
    # Look for bseindia.com links with a numeric code in href or text
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "bseindia.com" in href:
            m = re.search(r"(\d{6})", href)
            if m:
                return m.group(1)
            # Check link text for 6-digit code
            m = re.search(r"\b(\d{6})\b", a.get_text(strip=True))
            if m:
                return m.group(1)

    # Look for BSE label followed by a 6-digit number in nearby text
    for tag in soup.find_all(string=re.compile(r"\bBSE\b", re.I)):
        parent = tag.parent
        # Check text within parent and siblings
        block = parent.get_text(" ", strip=True) if parent else ""
        m = re.search(r"\b(\d{6})\b", block)
        if m:
            return m.group(1)

    return None


def _check_formerly_known_as(soup):
    """Return True if the company page mentions a prior name ("formerly known as")."""
    patterns = (r"formerly\s+known\s+as", r"erstwhile", r"formerly\s+called")
    # Check company name area and any visible note text
    for tag in soup.find_all(["h1", "h2", "p", "span", "div"],
                              class_=re.compile(r"name|note|info|formerly", re.I)):
        text = tag.get_text(" ", strip=True).lower()
        if any(re.search(p, text) for p in patterns):
            return True
    # Broader scan of page text (limited to first 5000 chars to keep it fast)
    body_text = soup.get_text(" ", strip=True)[:5000].lower()
    return any(re.search(p, body_text) for p in patterns)


def fetch_sector_nse(ticker):
    """
    Fetch sector info from NSE API.
    Returns dict with keys: sector, industry, sub_industry.
    Raises ValueError if not found or request fails.
    """
    session = requests.Session()
    session.headers.update(_NSE_HEADERS)
    # Establish session cookie
    try:
        session.get(
            f"https://www.nseindia.com/get-quotes/equity?symbol={ticker.upper()}",
            timeout=10,
        )
    except Exception:
        pass  # Cookie establishment may fail silently; proceed anyway

    resp = session.get(
        f"https://www.nseindia.com/api/quote-equity?symbol={ticker.upper()}",
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    info = data.get("industryInfo", {})
    sector = info.get("sector") or info.get("industry") or info.get("basicIndustry")
    if not sector:
        raise ValueError(f"NSE API: no sector info for {ticker}")
    return {
        "sector": sector,
        "industry": info.get("industry", ""),
        "sub_industry": info.get("basicIndustry", ""),
    }


def fetch_sector_bse(bse_code):
    """
    Fetch sector from BSE API using 6-digit scripcode.
    Returns sector string or raises ValueError.
    """
    resp = requests.get(
        f"https://api.bseindia.com/BseIndiaAPI/api/ComHeader/w"
        f"?quotetype=EQ&scripcode={bse_code}",
        headers=_BSE_HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    # BSE response may have sector in multiple fields
    sector = (
        data.get("Sector")
        or data.get("Industry")
        or data.get("Segment")
        or (data.get("Table") or [{}])[0].get("SECTOR")
    )
    if not sector:
        raise ValueError(f"BSE API: no sector for scripcode {bse_code}")
    return sector.strip()


def fetch_auditor_data(ticker: str, bse_code: str) -> dict:
    """
    Detect auditor changes from BSE Annual Report filings.
    Returns {"current_auditor_name", "previous_auditor", "flag_auditor_changed"}.
    All fields are None on failure or missing BSE code.
    """
    _NONE = {"current_auditor_name": None, "previous_auditor": None, "flag_auditor_changed": None}

    if not bse_code:
        return _NONE

    try:
        resp = requests.get(
            f"https://api.bseindia.com/BseIndiaAPI/api/AnnualReport/w"
            f"?scripcode={bse_code}&type=AR",
            headers=_BSE_HEADERS,
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()

        # Normalize: some endpoints wrap the list in {"Table": [...]}
        if isinstance(data, list):
            filings = data
        elif isinstance(data, dict):
            filings = (
                data.get("Table")
                or data.get("table")
                or data.get("data")
                or []
            )
        else:
            return _NONE

        if not filings:
            return _NONE

        # Sort by submission date descending, take up to 3
        def _sort_key(f):
            raw = (
                f.get("SUBMISSION_DATE")
                or f.get("SubmissionDate")
                or f.get("submission_date")
                or ""
            )
            # ISO "2025-09-12T00:00:00" → keep first 10 chars for sorting
            # DD/MM/YYYY "12/09/2025" → reformat to YYYY-MM-DD
            raw = str(raw).strip()
            if re.match(r"\d{4}-\d{2}-\d{2}", raw):
                return raw[:10]
            m = re.match(r"(\d{2})/(\d{2})/(\d{4})", raw)
            if m:
                return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
            return raw

        recent = sorted(filings, key=_sort_key, reverse=True)[:3]

        # Extract auditor name — try common field names used by BSE API
        _AUDITOR_KEYS = ("AUDITORNAME", "AUDITOR_NAME", "AuditorName", "Auditor", "AUDITOR")

        def _auditor(f):
            for key in _AUDITOR_KEYS:
                val = f.get(key)
                if val and isinstance(val, str) and val.strip():
                    return val.strip()
            return None

        # auditors[0] = most recent year, [1] = previous, [2] = two years ago
        auditors = [_auditor(f) for f in recent]

        current = auditors[0] if auditors else None

        # Need at least two non-null entries to detect a change
        non_null = [(i, a) for i, a in enumerate(auditors) if a is not None]
        if len(non_null) < 2:
            return {"current_auditor_name": current, "previous_auditor": None, "flag_auditor_changed": None}

        flag = False
        prev_on_change = None

        # Check consecutive pairs in reverse-chronological order
        for i in range(len(auditors) - 1):
            a_new, a_old = auditors[i], auditors[i + 1]
            if a_new is None or a_old is None:
                continue
            if a_new.lower() != a_old.lower():
                flag = True
                prev_on_change = a_old
                break   # report the most recent change

        return {
            "current_auditor_name": current,
            "previous_auditor": prev_on_change,
            "flag_auditor_changed": flag,
        }

    except Exception as e:
        print(f"[auditor] fetch_auditor_data({ticker!r}, {bse_code!r}) failed: {e}")
        return _NONE


_CARO_KEYWORDS = (
    "qualified opinion",
    "adverse opinion",
    "disclaimer of opinion",
    "emphasis of matter",
)

_CARO_SOURCE_NOTE = (
    "BSE filing metadata only — verify in full annual report for confirmation"
)


def fetch_caro_status(bse_code: str) -> dict:
    """
    Surface-check for audit qualification keywords in BSE Annual Report metadata.
    Returns {"flag_caro_qualified": True|None, "caro_source_note": str}.
    flag_caro_qualified is True when a keyword is found, None otherwise
    (absence of keyword in metadata does not confirm a clean audit).
    """
    _NONE = {"flag_caro_qualified": None, "caro_source_note": _CARO_SOURCE_NOTE}

    if not bse_code:
        return _NONE

    try:
        resp = requests.get(
            f"https://api.bseindia.com/BseIndiaAPI/api/AnnualReport/w"
            f"?scripcode={bse_code}&type=AR",
            headers=_BSE_HEADERS,
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, list):
            filings = data
        elif isinstance(data, dict):
            filings = (
                data.get("Table")
                or data.get("table")
                or data.get("data")
                or []
            )
        else:
            return _NONE

        if not filings:
            return _NONE

        # Sort descending by submission date, take only the most recent
        def _sort_key(f):
            raw = str(
                f.get("SUBMISSION_DATE")
                or f.get("SubmissionDate")
                or f.get("submission_date")
                or ""
            ).strip()
            if re.match(r"\d{4}-\d{2}-\d{2}", raw):
                return raw[:10]
            m = re.match(r"(\d{2})/(\d{2})/(\d{4})", raw)
            if m:
                return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
            return raw

        latest = sorted(filings, key=_sort_key, reverse=True)[0]

        # Collect all string values from the filing dict as the text corpus
        corpus = " ".join(
            v for v in latest.values()
            if isinstance(v, str) and v.strip()
        ).lower()

        for keyword in _CARO_KEYWORDS:
            if keyword in corpus:
                return {"flag_caro_qualified": True, "caro_source_note": _CARO_SOURCE_NOTE}

        return _NONE

    except Exception as e:
        print(f"[caro] fetch_caro_status({bse_code!r}) failed: {e}")
        return _NONE


# Quarter-end dates to fetch, newest → oldest.
# Trend lists are returned oldest → newest (reversed).
_TREND_DATES = [
    "2025-03-31", "2024-12-31", "2024-09-30", "2024-06-30",
    "2024-03-31", "2023-12-31", "2023-09-30", "2023-06-30",
]

_HOLDING_KEYS = (
    "percentageOfShareHolding",
    "percentOfShares",
    "holdingPerc",
    "holding_pct",
    "percentage",
)

_PLEDGED_KEYS = (
    "percentageOfSharesPledgedEncumbered",
    "pledgedPerc",
    "pledged_pct",
    "encumberedPerc",
    "percentageOfPledgedShares",
)

_CATEGORY_KEYS = ("category", "shareHolderType", "Category", "shareholderType")


def _parse_shareholding_quarter(data) -> dict | None:
    """
    Parse one NSE corporate-share-holdings response.
    Returns {"promoter", "pledged", "fii", "dii"} or None if unreadable.
    pledged is expressed as % of promoter holding (converted from % of total if needed).
    """
    if not data:
        return None

    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = (
            data.get("data")
            or data.get("Table")
            or data.get("shareholdingData")
            or []
        )
    else:
        return None

    if not rows:
        return None

    def _pct(row):
        for key in _HOLDING_KEYS:
            v = row.get(key)
            if v is not None:
                try:
                    return round(float(v), 2)
                except (ValueError, TypeError):
                    pass
        return None

    def _cat(row):
        for key in _CATEGORY_KEYS:
            v = row.get(key)
            if v and isinstance(v, str):
                return v.lower()
        return ""

    promoter = pledged = fii = dii = None

    for row in rows:
        cat = _cat(row)
        pct = _pct(row)

        if "promoter" in cat:
            if promoter is None:
                promoter = pct
            # NSE returns pledged as % of total paid-up capital; convert to % of promoter.
            for key in _PLEDGED_KEYS:
                v = row.get(key)
                if v is not None:
                    try:
                        raw = float(v)
                        if promoter and promoter > 0:
                            pledged = round(raw / promoter * 100, 2)
                        else:
                            pledged = round(raw, 2)
                    except (ValueError, TypeError):
                        pass
                    break

        elif "foreign" in cat or cat in ("fii", "fpi"):
            if fii is None:
                fii = pct

        elif "domestic institutional" in cat or "dii" in cat:
            if dii is None:
                dii = pct

    if promoter is None and fii is None and dii is None:
        return None

    return {"promoter": promoter, "pledged": pledged, "fii": fii, "dii": dii}


def fetch_shareholding_trend(ticker: str) -> dict:
    """
    Fetch 8-quarter shareholding trend from Screener's #shareholding section.
    Replaces the defunct NSE corporate-share-holdings API (all endpoints now 404).
    Returns point values, trend lists (oldest→newest), QoQ changes, and flags.
    Pledge % is not available from Screener static HTML — always returns None.
    """
    _NONE = {
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

    try:
        sym = ticker.upper()
        soup = None
        for url in (
            f"https://www.screener.in/company/{sym}/consolidated/",
            f"https://www.screener.in/company/{sym}/",
        ):
            try:
                resp = requests.get(url, headers=HEADERS, timeout=10)
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "html.parser")
                    break
            except Exception:
                continue

        if not soup:
            return _NONE

        sh_section = soup.find("section", id="shareholding")
        if not sh_section:
            return _NONE

        # First table is quarterly; second is annual — use first
        tables = sh_section.find_all("table")
        if not tables:
            return _NONE

        qtable = tables[0]
        rows = qtable.find_all("tr")
        if len(rows) < 2:
            return _NONE

        # Build {label → [float_or_None]} from each row.
        # Labels like "Promoters+", "FIIs+", "DIIs+" — strip trailing +
        row_data: dict[str, list] = {}
        for row in rows:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            label = cells[0].get_text(strip=True).lower().rstrip("+").strip()
            if not label:
                continue
            vals: list = []
            for cell in cells[1:]:
                raw = cell.get_text(strip=True).replace(",", "").rstrip("%").strip()
                try:
                    vals.append(round(float(raw), 2))
                except ValueError:
                    vals.append(None)
            row_data[label] = vals

        # How many data columns exist (use FII row as anchor)
        ref_key = next((k for k in row_data if "fii" in k or "foreign" in k), None)
        if not ref_key:
            return _NONE

        n_cols = len(row_data[ref_key])
        n = min(n_cols, 8)

        def _tail8(key_fragments: list[str]) -> list:
            """Return last 8 values for the first row whose label matches any fragment."""
            for k, vals in row_data.items():
                if any(f in k for f in key_fragments):
                    tail = vals[-n:]
                    return [None] * (8 - len(tail)) + tail
            return [None] * 8

        promoter_trend = _tail8(["promoter"])
        fii_trend      = _tail8(["fii", "foreign institutional"])
        dii_trend      = _tail8(["dii", "domestic institutional"])

        def _latest(trend: list):
            for v in reversed(trend):
                if v is not None:
                    return v
            return None

        def _prev(trend: list):
            found = 0
            for v in reversed(trend):
                if v is not None:
                    found += 1
                    if found == 2:
                        return v
            return None

        current_promoter = _latest(promoter_trend)
        current_fii      = _latest(fii_trend)
        current_dii      = _latest(dii_trend)

        def _qoq(new, old):
            return round(new - old, 2) if new is not None and old is not None else None

        promoter_qoq = _qoq(current_promoter, _prev(promoter_trend))
        fii_qoq      = _qoq(current_fii,      _prev(fii_trend))

        # Pledge % is behind a JS expander in Screener — not in static HTML
        flag_declining = (promoter_qoq < -2) if promoter_qoq is not None else None
        flag_fii_acc   = (fii_qoq > 2)       if fii_qoq      is not None else None

        return {
            "promoter_holding_pct":          current_promoter,
            "promoter_pledged_pct":          None,
            "fii_holding_pct":               current_fii,
            "dii_holding_pct":               current_dii,
            "promoter_holding_trend":        promoter_trend,
            "promoter_pledged_trend":        [None] * 8,
            "fii_trend":                     fii_trend,
            "dii_trend":                     dii_trend,
            "promoter_holding_change_qoq":   promoter_qoq,
            "fii_change_qoq":                fii_qoq,
            "flag_promoter_pledging_high":   None,
            "flag_promoter_stake_declining": flag_declining,
            "flag_fii_accumulating":         flag_fii_acc,
        }

    except Exception as e:
        print(f"[sh-trend] fetch_shareholding_trend({ticker!r}) failed: {e}")
        return _NONE


def compute_working_capital_health(financials: dict) -> dict:
    """
    Compute debtor days, creditor days, and cash conversion cycle trend
    from already-scraped Screener history fields. No network calls.
    Returns trend lists, latest CCC, a deterioration flag, and reason string.
    All fields are None on insufficient data or any exception.
    """
    _NONE = {
        "debtor_days_trend": None,
        "creditor_days_trend": None,
        "cash_conversion_cycle_latest": None,
        "ccc_trend": None,
        "flag_working_capital_deteriorating": None,
        "deterioration_reason": None,
    }

    try:
        rec_hist  = financials.get("trade_receivables_history") or []
        pay_hist  = financials.get("trade_payables_history") or []
        rev_hist  = financials.get("revenue_history") or []
        cogs_hist = financials.get("cogs_history") or []

        # Align to the last 3 years common across all four series.
        # All lists are oldest → newest and end at the same fiscal year,
        # so taking [-n:] from each gives correctly aligned years.
        n = min(len(rec_hist), len(pay_hist), len(rev_hist), len(cogs_hist), 3)
        if n < 2:
            return _NONE

        rec  = rec_hist[-n:]
        pay  = pay_hist[-n:]
        rev  = rev_hist[-n:]
        cogs = cogs_hist[-n:]

        # Compute metrics year by year; skip any year where a denominator is zero
        debtor_days_list   = []
        creditor_days_list = []
        ccc_list           = []

        for r, p, s, c in zip(rec, pay, rev, cogs):
            if not (r and p and s and c and s > 0 and c > 0):
                continue
            dd = round(r / s * 365, 1)
            cd = round(p / c * 365, 1)
            debtor_days_list.append(dd)
            creditor_days_list.append(cd)
            ccc_list.append(round(dd - cd, 1))

        if len(debtor_days_list) < 2:
            return _NONE

        # Flag: compare oldest vs latest in the valid subset
        dd_change  = debtor_days_list[-1]  - debtor_days_list[0]
        ccc_change = ccc_list[-1]          - ccc_list[0]
        n_yrs      = len(debtor_days_list)

        dd_bad  = dd_change  > 15
        ccc_bad = ccc_change > 20

        if dd_bad and ccc_bad:
            flag   = True
            reason = "Both debtor days and CCC deteriorating"
        elif dd_bad:
            flag   = True
            reason = f"Debtor days expanding {round(dd_change, 1)} days over {n_yrs} years"
        elif ccc_bad:
            flag   = True
            reason = (
                f"Cash conversion cycle worsening {round(ccc_change, 1)} days"
                f" over {n_yrs} years"
            )
        else:
            flag   = False
            reason = None

        return {
            "debtor_days_trend":                debtor_days_list,
            "creditor_days_trend":              creditor_days_list,
            "cash_conversion_cycle_latest":     ccc_list[-1],
            "ccc_trend":                        ccc_list,
            "flag_working_capital_deteriorating": flag,
            "deterioration_reason":             reason,
        }

    except Exception as e:
        print(f"[wc-health] compute_working_capital_health failed: {e}")
        return _NONE


def extract_dividend_history(financials: dict) -> dict:
    """
    Compute dividend-per-share trend, consistency, and growth flag from
    already-scraped EPS and payout % histories. No network calls.
    DPS is computed as EPS × (Dividend Payout % / 100) for each year.
    """
    _NONE = {
        "dividend_per_share_5yr": None,
        "dividend_yield_latest": None,
        "dividend_consistency": None,
        "dividend_growing": None,
        "buyback_announced": False,
    }

    try:
        eps_hist    = financials.get("eps_history") or []
        payout_hist = financials.get("dividend_payout_history") or []
        dyld        = financials.get("dividend_yield")

        # Align the two series to the last 5 common years (oldest → newest)
        n = min(len(eps_hist), len(payout_hist), 5)
        if n == 0:
            return _NONE

        eps    = eps_hist[-n:]
        payout = payout_hist[-n:]

        dps = [
            round(e * p / 100, 2) if (e is not None and p is not None) else None
            for e, p in zip(eps, payout)
        ]

        # dividend_consistency: True if every available year is non-zero positive.
        # None if fewer than 3 years; otherwise True/False over the available years.
        if n < 3:
            consistency = None
        else:
            consistency = all(d is not None and d > 0 for d in dps)

        # dividend_growing: compare latest year to 3 years prior (needs 4+ data points).
        # dps[-1] = latest; dps[-4] = 3 years before latest.
        if n >= 4 and dps[-1] is not None and dps[-4] is not None:
            growing = dps[-1] > dps[-4]
        else:
            growing = None

        # buyback: check any free-text fields in the scraped dict.
        # The current scraper does not fetch news/notes, so this is always False
        # unless a future field is added.
        buyback = False
        for field in ("notes", "news", "announcements", "company_notes"):
            val = financials.get(field)
            if val and "buyback" in str(val).lower():
                buyback = True
                break

        return {
            "dividend_per_share_5yr":  dps,
            "dividend_yield_latest":   dyld,
            "dividend_consistency":    consistency,
            "dividend_growing":        growing,
            "buyback_announced":       buyback,
        }

    except Exception as e:
        print(f"[dividend] extract_dividend_history failed: {e}")
        return _NONE


_CONCALL_KEYWORDS = ("concall", "conference call", "transcript", "earnings call")

# Indian FY quarter derived from the calendar month of a result announcement.
# (quarter_number, fy_year_offset) where FY = calendar_year + offset
_MONTH_TO_QUARTER = {
    1: (4, 0), 2: (4, 0), 3: (4, 0),    # Jan-Mar → Q4, same FY year
    4: (1, 1), 5: (1, 1), 6: (1, 1),    # Apr-Jun → Q1, next FY year
    7: (2, 1), 8: (2, 1), 9: (2, 1),    # Jul-Sep → Q2, next FY year
    10: (3, 1), 11: (3, 1), 12: (3, 1), # Oct-Dec → Q3, next FY year
}


def _concall_source_label(link_text: str) -> str:
    """
    Extract Q[N] FY[YY] label from concall link text, or fall back to generic label.
    Tries explicit Q-FY pattern first, then derives quarter from month-year date.
    """
    # Explicit "Q4 FY25" / "Q4FY25" / "Q4 FY2025" pattern
    m = re.search(r'[Qq]([1-4])\s*(?:FY|fy)\s*(\d{2,4})', link_text)
    if m:
        q = m.group(1)
        fy = m.group(2)[-2:]  # normalise 4-digit to 2-digit
        return f"Q{q} FY{fy} concall opening remarks via Screener"

    # Month-year date in link text e.g. "30 Jun 2025" or "30 Jun"
    _MONTH_NUM = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    m2 = re.search(r'(\d{1,2})\s+([A-Za-z]{3})\s*(\d{4})?', link_text)
    if m2:
        month_str = m2.group(2).lower()
        month_num = _MONTH_NUM.get(month_str)
        year_str  = m2.group(3)
        if month_num and year_str:
            year = int(year_str)
            q_num, fy_offset = _MONTH_TO_QUARTER[month_num]
            fy = str(year + fy_offset)[-2:]
            return f"Q{q_num} FY{fy} concall opening remarks via Screener"

    return "Latest concall via Screener"


def fetch_concall_snippet(ticker: str) -> dict:
    """
    Fetch the most recent concall/transcript link from the Screener #documents
    section and extract management commentary and guidance via regex.
    Returns all-None if no HTML transcript link is found (PDF links are skipped),
    or on any network/parse failure.
    """
    _NONE = {
        "mgmt_commentary_snippet": None,
        "mgmt_guided_revenue_growth": None,
        "mgmt_guided_margin": None,
        "guidance_text_snippet": None,
        "concall_source": None,
    }

    try:
        sym = ticker.upper()

        # Step 1: Fetch main Screener page; #documents lives there (not on /documents/)
        doc_url = None
        link_text = ""
        for variant in (
            f"https://www.screener.in/company/{sym}/consolidated/",
            f"https://www.screener.in/company/{sym}/",
        ):
            resp = requests.get(variant, headers=HEADERS, timeout=8)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            docs_section = soup.find(id="documents")
            if not docs_section:
                continue
            for a in docs_section.find_all("a", href=True):
                combined = (a.get_text(strip=True) + " " + a["href"]).lower()
                if any(k in combined for k in _CONCALL_KEYWORDS):
                    doc_url = a["href"]
                    link_text = a.get_text(strip=True)
                    break
            if doc_url:
                break

        # Step 2: No link found
        if not doc_url:
            return _NONE

        # Step 3: Skip PDFs — covers direct .pdf URLs and BSE AnnPdfOpen?Pname=*.pdf
        if re.search(r'\.pdf(\?|#|$)', doc_url, re.IGNORECASE):
            return _NONE

        # Fetch the HTML transcript page
        page_resp = requests.get(doc_url, headers=HEADERS, timeout=8)
        if page_resp.status_code != 200:
            return _NONE

        # Guard: server might still return a PDF despite the URL
        if "pdf" in page_resp.headers.get("content-type", "").lower():
            return _NONE

        page_soup = BeautifulSoup(page_resp.text, "html.parser")
        for tag in page_soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        raw_text = page_soup.get_text(" ", strip=True)[:3000]

        if not raw_text.strip():
            return _NONE

        # Step 4: Revenue guidance extraction
        mgmt_guided_revenue_growth = None
        guidance_text_snippet = None

        _rev_patterns = [
            r'(\d+)\s*[-–]\s*(\d+)\s*%\s*(?:growth|revenue\s+growth|top.?line)',
            r'revenue.*?(\d+)\s*%',
            r'grow.*?(\d+)\s*[-–]\s*(\d+)\s*%',
        ]
        for pat in _rev_patterns:
            m = re.search(pat, raw_text, re.IGNORECASE)
            if m:
                lo_s, hi_s = m.group(1), (m.group(2) if len(m.groups()) >= 2 else None)
                if hi_s:
                    mgmt_guided_revenue_growth = round((float(lo_s) + float(hi_s)) / 2, 1)
                else:
                    mgmt_guided_revenue_growth = float(lo_s)
                start = max(0, m.start() - 50)
                end   = min(len(raw_text), m.end() + 100)
                guidance_text_snippet = raw_text[start:end][:200].strip()
                break

        # Margin guidance extraction
        mgmt_guided_margin = None
        _margin_patterns = [
            r'margin.*?(\d+)\s*[-–]\s*(\d+)\s*%',
            r'EBITDA.*?(\d+)\s*%',
        ]
        for pat in _margin_patterns:
            m = re.search(pat, raw_text, re.IGNORECASE)
            if m:
                lo_s = m.group(1)
                hi_s = m.group(2) if len(m.groups()) >= 2 else None
                if hi_s:
                    mgmt_guided_margin = round((float(lo_s) + float(hi_s)) / 2, 1)
                else:
                    mgmt_guided_margin = float(lo_s)
                break

        # Step 5: Concall source label — check link text, fall back to page <title>
        title_el = page_soup.find("title")
        combined_for_label = link_text
        if title_el:
            combined_for_label += " " + title_el.get_text(strip=True)
        concall_source = _concall_source_label(combined_for_label)

        return {
            "mgmt_commentary_snippet":     raw_text[:500],
            "mgmt_guided_revenue_growth":  mgmt_guided_revenue_growth,
            "mgmt_guided_margin":          mgmt_guided_margin,
            "guidance_text_snippet":       guidance_text_snippet,
            "concall_source":              concall_source,
        }

    except Exception as e:
        print(f"[concall] fetch_concall_snippet({ticker!r}) failed: {e}")
        return _NONE


def _parse_explore_table(soup, max_rows=30):
    """
    Parse the results table from Screener.in's /explore/ page.
    Returns list of {name, ticker, market_cap_cr, de}.
    """
    table = (
        soup.find("table", class_="data-table")
        or soup.find("table", id="data-table")
        or soup.find("table")
    )
    if not table:
        return []

    thead = table.find("thead") or table
    raw_headers = [th.get_text(strip=True).lower() for th in thead.find_all(["th", "td"])]

    name_idx  = next((i for i, h in enumerate(raw_headers) if "name" in h), 0)
    mcap_idx  = next((i for i, h in enumerate(raw_headers)
                      if "market cap" in h or "mkt cap" in h or "capitaliz" in h), None)
    de_idx    = next((i for i, h in enumerate(raw_headers)
                      if ("debt" in h and "equity" in h) or "d/e" in h), None)

    peers = []
    tbody = table.find("tbody") or table
    for tr in tbody.find_all("tr")[:max_rows]:
        tds = tr.find_all(["td", "th"])
        if not tds:
            continue

        name_td = tds[name_idx] if name_idx < len(tds) else None
        name = name_td.get_text(strip=True) if name_td else None

        ticker = None
        if name_td:
            link = name_td.find("a")
            if link:
                href = link.get("href", "")
                m = re.search(r"/company/([^/]+)/", href)
                if m:
                    ticker = m.group(1).upper()

        mcap = None
        if mcap_idx is not None and mcap_idx < len(tds):
            mcap = _parse_number(tds[mcap_idx].get_text(strip=True))

        de = None
        if de_idx is not None and de_idx < len(tds):
            raw = tds[de_idx].get_text(strip=True)
            if raw and raw.lower() not in ("", "n/a", "-", "--", "na"):
                de = _parse_number(raw)

        if name:
            peers.append({"name": name, "ticker": ticker, "market_cap_cr": mcap, "de": de})

    return peers


def _fetch_metrics_for_ticker(ticker):
    """
    Fetch D/E, P/E, and EV/Revenue for a peer ticker from Screener.
    Returns dict {de, pe, ev_rev} — any value may be None on parse failure.
    """
    for url in (
        f"https://www.screener.in/company/{ticker}/consolidated/",
        f"https://www.screener.in/company/{ticker}/",
    ):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=8)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")

            # Market Cap (Cr) from top-ratios
            mcap = None
            for li in soup.select("#top-ratios li"):
                spans = li.find_all("span")
                if len(spans) >= 2 and "market cap" in spans[0].get_text(strip=True).lower():
                    mcap = _parse_number(spans[-1].get_text(strip=True))
                    break

            # Borrowings → D/E
            borrowings = (
                _extract_table_latest(soup, "balance-sheet", "Borrowings")
                or _extract_table_latest(soup, "balance-sheet", "Total Debt")
                or 0.0
            )
            de = round((borrowings or 0) / mcap, 4) if mcap and mcap > 0 else None

            # Stock P/E from top-ratios
            pe = _extract_li_value(soup, "Stock P/E") or _extract_li_value(soup, "P/E")

            # EV/Revenue: EV = Mcap + Borrowings - Cash; Revenue from P&L
            ev_rev = None
            if mcap and mcap > 0:
                cash = (
                    _extract_table_latest(soup, "balance-sheet", "Cash Equivalents")
                    or _extract_table_latest(soup, "balance-sheet", "Cash & Bank")
                    or _extract_table_latest(soup, "balance-sheet", "Cash")
                    or 0.0
                )
                revenue = (
                    _extract_table_latest(soup, "profit-loss", "Sales")
                    or _extract_table_latest(soup, "profit-loss", "Revenue")
                )
                if revenue and revenue > 0:
                    ev = mcap + (borrowings or 0) - (cash or 0)
                    ev_rev = round(ev / revenue, 2) if ev > 0 else None

            return {"de": de, "pe": pe, "ev_rev": ev_rev}
        except Exception:
            pass
    return {"de": None, "pe": None, "ev_rev": None}


def _fetch_de_for_ticker(ticker):
    return _fetch_metrics_for_ticker(ticker).get("de")


def _parse_market_page(soup, max_rows=30):
    """Parse a Screener /market/ hierarchy page into {name, ticker, market_cap_cr} list."""
    table = soup.find("table")
    if not table:
        return []

    # Identify market cap column from header
    header_row = table.find("tr")
    headers = [th.get_text(strip=True).lower() for th in header_row.find_all(["th", "td"])] if header_row else []
    mcap_col = next(
        (i for i, h in enumerate(headers) if "cap" in h or "capitaliz" in h),
        None,
    )

    companies = []
    for tr in table.find_all("tr")[1:max_rows + 1]:
        tds = tr.find_all(["td", "th"])
        if not tds:
            continue
        name, ticker = None, None
        for td in tds:
            link = td.find("a", href=re.compile(r"/company/"))
            if link:
                name = link.get_text(strip=True)
                m = re.search(r"/company/([^/]+)/", link["href"])
                if m:
                    ticker = m.group(1).upper()
                break
        if not (name and ticker):
            continue
        mcap = None
        if mcap_col is not None and mcap_col < len(tds):
            mcap = _parse_number(tds[mcap_col].get_text(strip=True))
        companies.append({"name": name, "ticker": ticker, "market_cap_cr": mcap})
    return companies


def fetch_sector_peers(sector, max_peers=30, market_url=None):
    """
    Fetch peer companies for a sector and their D/E ratios.
    Uses Screener's /market/ hierarchy page (reliable) + parallel D/E fetch.
    Returns list of {name, ticker, market_cap_cr, de} dicts.
    Raises ValueError if fewer than 5 peers found.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not market_url:
        raise ValueError(f"No market_url provided for sector '{sector}'")

    base = "https://www.screener.in"
    url = base + market_url if market_url.startswith("/") else market_url

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        raise ValueError(f"Could not fetch market page {url}: {e}")

    soup = BeautifulSoup(resp.text, "html.parser")
    companies = _parse_market_page(soup, max_rows=max_peers)

    if len(companies) < 3:
        raise ValueError(
            f"Too few companies ({len(companies)}) on market page for '{sector}'"
        )

    # Parallel metrics fetch (D/E + P/E + EV/Revenue) — 4 workers, 8s per request
    tickers = [c["ticker"] for c in companies]
    metrics_map: dict = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_metrics_for_ticker, t): t for t in tickers}
        try:
            for fut in as_completed(futures, timeout=60):
                t = futures[fut]
                try:
                    metrics_map[t] = fut.result()
                except Exception:
                    metrics_map[t] = {"de": None, "pe": None, "ev_rev": None}
        except Exception:
            for fut, t in futures.items():
                if t not in metrics_map:
                    metrics_map[t] = fut.result(timeout=0) if fut.done() else {"de": None, "pe": None, "ev_rev": None}

    peers = []
    for c in companies:
        m = metrics_map.get(c["ticker"]) or {}
        peers.append({**c, "de": m.get("de"), "pe": m.get("pe"), "ev_rev": m.get("ev_rev")})

    valid_count = sum(1 for p in peers if p["de"] is not None)
    if valid_count < 3:
        raise ValueError(
            f"Insufficient D/E data: only {valid_count}/{len(peers)} peers have D/E"
        )

    return peers


def _extract_equity_capital_history(soup):
    """Returns equity capital values (Cr) oldest → newest from balance sheet."""
    section = soup.find("section", {"id": "balance-sheet"})
    if not section:
        return []
    for tr in section.find_all("tr"):
        tds = tr.find_all(["td", "th"])
        if not tds:
            continue
        label = tds[0].get_text(strip=True).lower()
        if "equity capital" in label or "share capital" in label:
            vals = []
            for td in tds[1:]:
                v = _parse_number(td.get_text(strip=True))
                if v is not None:
                    vals.append(v)
            return vals
    return []


def _parse_number(text):
    """Convert strings like '1,23,456.78' or '1234.56 Cr' to float."""
    if not text:
        return None
    text = text.strip().replace(",", "")
    # Remove units like Cr, %, etc.
    text = re.sub(r"[^\d.\-]", "", text)
    try:
        return float(text)
    except ValueError:
        return None


def _get_soup(ticker, consolidated=True):
    """Try consolidated (default) first, fall back to standalone, or vice-versa."""
    base = BASE_URL.format(ticker=ticker.upper().strip())
    urls = (
        [(base + "consolidated/", True), (base, False)]
        if consolidated
        else [(base, False), (base + "consolidated/", True)]
    )
    for url, is_cons in urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code == 200:
                return BeautifulSoup(resp.text, "html.parser"), url, is_cons
        except Exception:
            continue
    raise ConnectionError(f"Could not load any page for '{ticker}' on Screener.in")


def _extract_li_value(soup, label_text):
    """Extract value from <li> elements like: <span>Label</span><span>Value</span>."""
    for li in soup.select("ul.company-ratios li, #top-ratios li"):
        spans = li.find_all("span")
        if len(spans) >= 2:
            lbl = spans[0].get_text(strip=True).lower()
            if label_text.lower() in lbl:
                return _parse_number(spans[-1].get_text(strip=True))
    return None


def _extract_table_latest(soup, section_id, row_label):
    """
    Find a table in a section and return the most recent annual value
    for a given row label (first data column after the label).
    """
    section = soup.find("section", {"id": section_id})
    if not section:
        return None
    for tr in section.find_all("tr"):
        tds = tr.find_all(["td", "th"])
        if not tds:
            continue
        if row_label.lower() in tds[0].get_text(strip=True).lower():
            # Return last non-empty numeric column (most recent year)
            values = []
            for td in tds[1:]:
                v = _parse_number(td.get_text(strip=True))
                if v is not None:
                    values.append(v)
            return values[-1] if values else None
    return None


def _extract_table_history(soup, section_id, row_label, n=3):
    """Return last n annual values for row_label (oldest → newest). Returns [] if not found."""
    section = soup.find("section", {"id": section_id})
    if not section:
        return []
    for tr in section.find_all("tr"):
        tds = tr.find_all(["td", "th"])
        if not tds:
            continue
        if row_label.lower() in tds[0].get_text(strip=True).lower():
            values = [_parse_number(td.get_text(strip=True)) for td in tds[1:]]
            values = [v for v in values if v is not None]
            return values[-n:] if values else []
    return []


def _extract_years_listed(soup):
    """Count number of annual columns in P&L table to estimate listing history."""
    section = soup.find("section", {"id": "profit-loss"})
    if not section:
        return 0
    header_row = section.find("tr")
    if not header_row:
        return 0
    ths = header_row.find_all(["th", "td"])
    # First column is label, rest are years
    return max(0, len(ths) - 1)


def _extract_five_year_median_pe(soup):
    """Try to get median PE from the ratios section."""
    for li in soup.select("ul.company-ratios li, #top-ratios li"):
        spans = li.find_all("span")
        if len(spans) >= 2:
            lbl = spans[0].get_text(strip=True).lower()
            if "median" in lbl and "pe" in lbl:
                return _parse_number(spans[-1].get_text(strip=True))
    return None


def _extract_revenue_history(soup):
    """Return all annual revenues found in the P&L table, oldest → newest."""
    section = soup.find("section", {"id": "profit-loss"})
    if not section:
        return []
    for tr in section.find_all("tr"):
        tds = tr.find_all(["td", "th"])
        if not tds:
            continue
        label = tds[0].get_text(strip=True).lower()
        if "sales" in label or "revenue" in label:
            values = []
            for td in tds[1:]:
                v = _parse_number(td.get_text(strip=True))
                if v is not None and v > 0:
                    values.append(v)
            return values
    return []


def _extract_five_year_avg_ev_rev(soup, ev, revenue):
    """
    Approximates 5Y avg EV/Revenue using current EV as a proxy across historical revenues.
    Returns None if fewer than 3 years of revenue data available.
    """
    section = soup.find("section", {"id": "profit-loss"})
    if not section or ev is None:
        return None

    revenue_values = []
    for tr in section.find_all("tr"):
        tds = tr.find_all(["td", "th"])
        if not tds:
            continue
        label = tds[0].get_text(strip=True).lower()
        if "sales" in label or "revenue" in label:
            for td in tds[1:]:
                v = _parse_number(td.get_text(strip=True))
                if v is not None and v > 0:
                    revenue_values.append(v)
            break

    if len(revenue_values) < 3:
        return None

    recent = revenue_values[-5:]
    avg_ev_rev = sum(ev / r for r in recent) / len(recent)
    return avg_ev_rev


def scrape(ticker, consolidated=True):
    """
    Returns a dict with keys:
        company_name, revenue, net_profit, eps, ebitda, debt, cash,
        minority_interest, market_cap, share_price, shares_outstanding,
        enterprise_value, five_yr_avg_ev_rev, revenue_history,
        equity_capital_history, median_pe, years_listed,
        is_consolidated, data_as_of, data_freshness_days, errors
    Values are float or None. 'errors' is a list of field names that failed.
    """
    result = {
        "company_name": None,
        "revenue": None,
        "net_profit": None,
        "eps": None,
        "ebitda": None,
        "debt": None,
        "cash": None,
        "minority_interest": None,
        "market_cap": None,
        "share_price": None,
        "shares_outstanding": None,
        "enterprise_value": None,
        "five_yr_avg_ev_rev": None,
        "revenue_history": [],
        "trade_receivables_history": [],
        "trade_payables_history": [],
        "cogs_history": [],
        "eps_history": [],
        "dividend_payout_history": [],
        "dividend_yield": None,
        "equity_capital_history": [],
        "median_pe": None,
        "years_listed": 0,
        "is_consolidated": None,
        "data_as_of": None,
        "data_freshness_days": None,
        "roce_latest": None,
        "roce_5yr_avg": None,
        "roe_latest": None,
        "roe_5yr_avg": None,
        "net_margin": None,
        "promoter_holding": None,
        "promoter_holding_prev": None,
        "promoter_holding_history": [],
        "promoter_pledged": None,
        "fii_holding": None,
        "fii_holding_prev": None,
        "dii_holding": None,
        "company_sector": None,
        "debt_to_equity": None,
        "bse_code": None,
        "formerly_known_as": False,
        "errors": [],
        "url": None,
    }

    try:
        soup, url, is_cons = _get_soup(ticker, consolidated=consolidated)
        result["url"] = url
        result["is_consolidated"] = is_cons
    except Exception as e:
        result["errors"] = ["all_fields"]
        raise ConnectionError(f"Could not reach Screener.in for '{ticker}': {e}")

    # Company name
    try:
        h1 = soup.find("h1")
        if h1:
            result["company_name"] = h1.get_text(strip=True)
    except Exception:
        pass

    # Market cap and share price from top ratios
    try:
        result["market_cap"] = _extract_li_value(soup, "Market Cap")
    except Exception:
        result["errors"].append("market_cap")

    try:
        result["share_price"] = _extract_li_value(soup, "Current Price")
        if result["share_price"] is None:
            # fallback: look for stock-price-value
            el = soup.find(id="current-price") or soup.select_one(".stock-price-value")
            if el:
                result["share_price"] = _parse_number(el.get_text(strip=True))
    except Exception:
        result["errors"].append("share_price")

    # Revenue / Sales (latest annual)
    try:
        result["revenue"] = _extract_table_latest(soup, "profit-loss", "Sales")
        if result["revenue"] is None:
            result["revenue"] = _extract_table_latest(soup, "profit-loss", "Revenue")
    except Exception:
        result["errors"].append("revenue")

    # Net Profit / PAT
    try:
        result["net_profit"] = _extract_table_latest(soup, "profit-loss", "Net Profit")
    except Exception:
        result["errors"].append("net_profit")

    # EPS
    try:
        result["eps"] = _extract_table_latest(soup, "profit-loss", "EPS")
        if result["eps"] is None:
            result["eps"] = _extract_li_value(soup, "EPS")
    except Exception:
        result["errors"].append("eps")

    # EBITDA: Operating Profit (EBIT proxy on Screener) + Depreciation
    try:
        op = _extract_table_latest(soup, "profit-loss", "Operating Profit")
        depr = _extract_table_latest(soup, "profit-loss", "Depreciation")
        if op is not None:
            result["ebitda"] = op + (depr or 0)
    except Exception:
        pass

    # Debt / Borrowings
    try:
        result["debt"] = _extract_table_latest(soup, "balance-sheet", "Borrowings")
        if result["debt"] is None:
            result["debt"] = _extract_table_latest(soup, "balance-sheet", "Total Debt")
    except Exception:
        result["errors"].append("debt")

    # Cash
    try:
        result["cash"] = _extract_table_latest(soup, "balance-sheet", "Cash")
        if result["cash"] is None:
            result["cash"] = _extract_table_latest(
                soup, "balance-sheet", "Cash Equivalents"
            )
    except Exception:
        result["errors"].append("cash")

    # Minority Interest
    try:
        result["minority_interest"] = _extract_table_latest(
            soup, "balance-sheet", "Minority Interest"
        )
    except Exception:
        pass

    # Shares outstanding = market cap / share price
    try:
        if result["market_cap"] and result["share_price"] and result["share_price"] > 0:
            # Market cap on screener is in Crores, price is in INR
            result["shares_outstanding"] = (result["market_cap"] * 1e7) / result["share_price"]
    except Exception:
        result["errors"].append("shares_outstanding")

    # Years listed
    try:
        result["years_listed"] = _extract_years_listed(soup)
    except Exception:
        pass

    # Revenue history (oldest → newest) — used for DCF reliability scoring
    try:
        result["revenue_history"] = _extract_revenue_history(soup)
    except Exception:
        pass

    # Enterprise Value = Market Cap + Debt - Cash + Minority Interest (all in Crores)
    try:
        mc = result.get("market_cap") or 0
        if mc > 0:
            result["enterprise_value"] = (
                mc
                + (result.get("debt") or 0)
                - (result.get("cash") or 0)
                + (result.get("minority_interest") or 0)
            )
    except Exception:
        pass

    # 5Y avg EV/Revenue (uses current EV as proxy across historical revenues)
    try:
        result["five_yr_avg_ev_rev"] = _extract_five_year_avg_ev_rev(
            soup,
            result.get("enterprise_value") or result.get("market_cap"),
            result["revenue"],
        )
    except Exception:
        pass

    # Latest quarterly result date and data freshness
    try:
        period_text, period_dt = _extract_latest_quarter(soup)
        if period_dt:
            days = (date.today() - period_dt).days
            fy_str = _to_fy_quarter(period_dt)
            month_str = period_dt.strftime("%b %Y")
            result["data_as_of"] = f"{fy_str} ({month_str})" if fy_str else month_str
            result["data_freshness_days"] = days
        elif period_text:
            result["data_as_of"] = period_text
    except Exception:
        pass

    # Equity capital history for share-dilution check
    try:
        result["equity_capital_history"] = _extract_equity_capital_history(soup)
    except Exception:
        pass

    # ROCE — historical table first (for 5Y avg), then top ratios fallback
    try:
        roce_hist = _extract_ratio_history(soup, "return on capital employed", "roce")
        if roce_hist:
            result["roce_latest"] = roce_hist[-1]
            recent = roce_hist[-5:]
            result["roce_5yr_avg"] = round(sum(recent) / len(recent), 2)
        else:
            result["roce_latest"] = _extract_li_value(soup, "ROCE")
    except Exception:
        pass

    # ROE — same pattern
    try:
        roe_hist = _extract_ratio_history(soup, "return on equity", "roe")
        if roe_hist:
            result["roe_latest"] = roe_hist[-1]
            recent = roe_hist[-5:]
            result["roe_5yr_avg"] = round(sum(recent) / len(recent), 2)
        else:
            result["roe_latest"] = _extract_li_value(soup, "ROE")
    except Exception:
        pass

    # Net Profit Margin — computed from already-scraped values
    try:
        rev, np_ = result.get("revenue"), result.get("net_profit")
        if rev and rev > 0 and np_ is not None:
            result["net_margin"] = round(np_ / rev * 100, 2)
    except Exception:
        pass

    # Shareholding — promoter, FII, DII, pledging
    try:
        sh = _extract_shareholding(soup)
        result.update(sh)
    except Exception:
        pass

    # company_sector intentionally not set here — resolved via /api/resolve-sector
    # (NSE API → Screener peers page → BSE API → user dropdown)

    # Debt to Equity (book-value D/E as shown in Screener top ratios)
    try:
        result["debt_to_equity"] = (
            _extract_li_value(soup, "Debt to equity")
            or _extract_li_value(soup, "Debt / Equity")
            or _extract_li_value(soup, "D/E")
        )
    except Exception:
        pass

    # BSE scripcode — needed for BSE sector fallback
    try:
        result["bse_code"] = _extract_bse_code(soup)
    except Exception:
        pass

    # Formerly-known-as flag — warn user sector may reference old name
    try:
        result["formerly_known_as"] = _check_formerly_known_as(soup)
    except Exception:
        pass

    # Working capital history — last 3 years, oldest → newest
    try:
        result["trade_receivables_history"] = (
            _extract_table_history(soup, "balance-sheet", "Trade Receivables")
            or _extract_table_history(soup, "balance-sheet", "Debtors")
        )
    except Exception:
        pass

    try:
        result["trade_payables_history"] = (
            _extract_table_history(soup, "balance-sheet", "Trade Payables")
            or _extract_table_history(soup, "balance-sheet", "Creditors")
        )
    except Exception:
        pass

    try:
        result["cogs_history"] = (
            _extract_table_history(soup, "profit-loss", "Cost of Materials Consumed")
            or _extract_table_history(soup, "profit-loss", "Raw Material Cost")
            or _extract_table_history(soup, "profit-loss", "Material Cost")
            or _extract_table_history(soup, "profit-loss", "Purchase of Stock-in-Trade")
        )
    except Exception:
        pass

    # Dividend history inputs
    try:
        result["eps_history"] = _extract_table_history(soup, "profit-loss", "EPS", n=5)
    except Exception:
        pass

    try:
        result["dividend_payout_history"] = _extract_table_history(
            soup, "profit-loss", "Dividend Payout", n=5
        )
    except Exception:
        pass

    try:
        result["dividend_yield"] = _extract_li_value(soup, "Dividend Yield")
    except Exception:
        pass

    # Extract sector hierarchy from the already-loaded soup (no extra request).
    # Use the most-specific sub-industry URL for peer fetching.
    try:
        _mlinks = []
        for _a in soup.find_all("a", title=True, href=re.compile(r"^/market/")):
            _t = _a.get_text(strip=True)
            if _t and 2 < len(_t) < 80:
                _mlinks.append((_a["title"], _t, _a["href"]))
        if _mlinks:
            result["sector_broad"]   = _mlinks[0][1]   # e.g. "Industrials"
            result["sector_sub_url"] = _mlinks[-1][2]  # e.g. "/market/IN07/.../IN070203002/"
            result["sector_sub_name"] = _mlinks[-1][1] # e.g. "Other Electrical Equipment"
    except Exception:
        pass

    return result
