"""
Fetch Nifty Options Chain data from NSE India.
Usage:
    python fetch_nifty_options.py                        # Nearest expiry
    python fetch_nifty_options.py --expiry 2026-04-02    # Specific expiry
    python fetch_nifty_options.py --strikes 23000-24000  # Filter strike range
"""

import argparse
import json
import sys
from datetime import datetime

from curl_cffi import requests as cfreq

NSE_OPTIONS_URL = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"
NSE_MARKET_STATUS_URL = "https://www.nseindia.com/api/marketStatus"
NSE_BASE_URL = "https://www.nseindia.com"


def create_session():
    """Create and return an HTTP session with NSE India cookies established.

    Uses curl_cffi with Chrome TLS impersonation to bypass NSE's bot detection.
    An initial GET request to the NSE homepage is made to obtain session cookies
    (required for subsequent API calls). If the session cannot be established
    (e.g., network error, NSE blocking), the script prints an error JSON and exits.

    Returns:
        cfreq.Session: A configured session object with valid NSE cookies.
    """
    session = cfreq.Session(impersonate="chrome")
    try:
        resp = session.get(NSE_BASE_URL, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(json.dumps({"error": f"Failed to establish NSE session: {e}"}))
        sys.exit(1)
    return session


def fetch_market_status(session):
    """Fetch the current market status from the NSE market status API.

    Retrieves live market state information including whether the Capital Market
    segment is open/closed, last traded price, trade date, and any status messages.
    This is used as a fallback when the options chain API returns empty data
    (typically after market hours, post 3:30 PM IST).

    Args:
        session (cfreq.Session): An authenticated NSE session with cookies.

    Returns:
        dict or None: Parsed JSON response containing marketState array with
            market status details, or None if the request fails.
    """
    try:
        resp = session.get(NSE_MARKET_STATUS_URL, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def fetch_options_chain(session):
    """Fetch the complete Nifty 50 options chain from the NSE India API.

    Makes an authenticated GET request to the NSE option-chain-indices endpoint
    for the NIFTY symbol. The response includes all available expiry dates,
    strike prices, and option data (OI, change in OI, LTP, IV, volume) for
    both Call (CE) and Put (PE) options.

    Note: NSE returns empty data after market hours (post 3:30 PM IST).
    A Referer header is set to mimic browser navigation from the NSE
    option chain page.

    Args:
        session (cfreq.Session): An authenticated NSE session with cookies.

    Returns:
        dict: Parsed JSON response containing 'records' (all strikes across
            all expiries) and 'filtered' (nearest expiry data) keys.
            Exits with error JSON if the request fails.
    """
    try:
        resp = session.get(NSE_OPTIONS_URL, timeout=15, headers={
            "Referer": "https://www.nseindia.com/option-chain",
        })
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(json.dumps({"error": f"Failed to fetch options chain: {e}"}))
        sys.exit(1)


def process_data(data, expiry_filter=None, strike_range=None):
    """Process raw NSE options chain data into a structured analysis summary.

    Filters the raw option chain records by expiry date and/or strike price range,
    then builds a comprehensive summary including:
    - Per-strike option chain rows (OI, change in OI, LTP, IV, volume for CE/PE)
    - Total CE and PE open interest
    - Put-Call Ratio (PCR) based on total OI
    - Max Pain strike price (where option writers face minimum loss)
    - Highest CE OI strike (key resistance) and highest PE OI strike (key support)
    - ATM (At The Money) strike nearest to the underlying spot price

    If no expiry is specified, defaults to the nearest available expiry date.
    Supports NSE's date format (DD-Mon-YYYY) and ISO format (YYYY-MM-DD)
    for expiry matching.

    Args:
        data (dict): Raw JSON response from the NSE options chain API.
        expiry_filter (str, optional): Target expiry date in YYYY-MM-DD format.
            Falls back to nearest expiry if None or not found.
        strike_range (tuple, optional): (low, high) strike price bounds for
            filtering. Includes both endpoints.

    Returns:
        dict: Structured result containing timestamp, underlyingValue,
            expiryDates, selectedExpiry, chain (list of strike rows),
            and summary (PCR, maxPain, highestCE_OI, highestPE_OI, ATM_strike).
    """
    records = data.get("records", {})
    filtered = data.get("filtered", {})

    timestamp = records.get("timestamp", "N/A")
    underlying_value = records.get("underlyingValue", 0)
    expiry_dates = records.get("expiryDates", [])

    all_data = records.get("data", [])

    # Filter by expiry if specified
    if expiry_filter:
        target = expiry_filter
        all_data = [r for r in all_data if r.get("expiryDate") == target]
        if not all_data:
            # Try matching with different date formats from NSE
            for rec in records.get("data", []):
                try:
                    exp_date = datetime.strptime(rec["expiryDate"], "%d-%b-%Y")
                    if exp_date.strftime("%Y-%m-%d") == target:
                        expiry_filter = rec["expiryDate"]
                        break
                except (ValueError, KeyError):
                    continue
            all_data = [r for r in records.get("data", []) if r.get("expiryDate") == expiry_filter]
    else:
        # Use nearest expiry
        if expiry_dates:
            nearest = expiry_dates[0]
            all_data = [r for r in all_data if r.get("expiryDate") == nearest]

    # Filter by strike range if specified
    if strike_range:
        low, high = strike_range
        all_data = [r for r in all_data if low <= r.get("strikePrice", 0) <= high]

    # Build option chain rows
    chain = []
    total_ce_oi = 0
    total_pe_oi = 0
    max_ce_oi = {"strike": 0, "oi": 0}
    max_pe_oi = {"strike": 0, "oi": 0}

    for rec in all_data:
        strike = rec.get("strikePrice", 0)
        ce = rec.get("CE", {})
        pe = rec.get("PE", {})

        ce_oi = ce.get("openInterest", 0)
        pe_oi = pe.get("openInterest", 0)
        total_ce_oi += ce_oi
        total_pe_oi += pe_oi

        if ce_oi > max_ce_oi["oi"]:
            max_ce_oi = {"strike": strike, "oi": ce_oi}
        if pe_oi > max_pe_oi["oi"]:
            max_pe_oi = {"strike": strike, "oi": pe_oi}

        row = {
            "strikePrice": strike,
            "CE_OI": ce_oi,
            "CE_changeOI": ce.get("changeinOpenInterest", 0),
            "CE_LTP": ce.get("lastPrice", 0),
            "CE_IV": ce.get("impliedVolatility", 0),
            "CE_volume": ce.get("totalTradedVolume", 0),
            "PE_OI": pe_oi,
            "PE_changeOI": pe.get("changeinOpenInterest", 0),
            "PE_LTP": pe.get("lastPrice", 0),
            "PE_IV": pe.get("impliedVolatility", 0),
            "PE_volume": pe.get("totalTradedVolume", 0),
        }
        chain.append(row)

    # Calculate PCR
    pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 0

    # Calculate Max Pain
    max_pain = calculate_max_pain(chain, underlying_value)

    # Find ATM strike
    atm_strike = min(chain, key=lambda x: abs(x["strikePrice"] - underlying_value))["strikePrice"] if chain else 0

    result = {
        "timestamp": timestamp,
        "underlyingValue": underlying_value,
        "expiryDates": expiry_dates,
        "selectedExpiry": expiry_filter or (expiry_dates[0] if expiry_dates else "N/A"),
        "chain": chain,
        "summary": {
            "totalCE_OI": total_ce_oi,
            "totalPE_OI": total_pe_oi,
            "PCR": pcr,
            "maxPain": max_pain,
            "highestCE_OI": max_ce_oi,
            "highestPE_OI": max_pe_oi,
            "ATM_strike": atm_strike,
        },
    }
    return result


def calculate_max_pain(chain, spot):
    """Calculate the Max Pain strike price for the given options chain.

    Max Pain is the strike price at which the combined value of outstanding
    call and put options causes the maximum financial loss (pain) to option
    holders — or equivalently, the minimum loss to option writers. It is
    computed by iterating over each strike as a hypothetical expiry price
    and summing the intrinsic value losses:
    - CE pain at strike S for test price T = CE_OI * max(0, S - T)
    - PE pain at strike S for test price T = PE_OI * max(0, T - S)

    The strike with the lowest total pain is identified as Max Pain.
    This level often acts as a magnet for price near expiry.

    Args:
        chain (list): List of option chain row dicts, each containing
            'strikePrice', 'CE_OI', and 'PE_OI' keys.
        spot (float): Current underlying spot price (unused in calculation
            but kept for potential future weighting).

    Returns:
        int: The Max Pain strike price, or 0 if the chain is empty.
    """
    if not chain:
        return 0

    strikes = [r["strikePrice"] for r in chain]
    min_pain = float("inf")
    max_pain_strike = 0

    for test_strike in strikes:
        total_pain = 0
        for row in chain:
            s = row["strikePrice"]
            # CE writers pain: CE OI * max(0, s - test_strike) * lot_size
            ce_pain = row["CE_OI"] * max(0, s - test_strike)
            # PE writers pain: PE OI * max(0, test_strike - s) * lot_size
            pe_pain = row["PE_OI"] * max(0, test_strike - s)
            total_pain += ce_pain + pe_pain

        if total_pain < min_pain:
            min_pain = total_pain
            max_pain_strike = test_strike

    return max_pain_strike


def main():
    """Entry point for the Nifty options chain fetcher CLI.

    Parses command-line arguments for optional expiry date and strike range
    filters, establishes an authenticated NSE session, fetches market status
    and the full options chain, processes the data into a structured summary,
    and prints the result as formatted JSON to stdout.

    If the options chain API returns empty data (typically after market hours),
    falls back to displaying market status information (last price, trade date,
    market status message) instead of an empty chain.

    CLI Arguments:
        --expiry (str): Filter by expiry date in YYYY-MM-DD format.
        --strikes (str): Filter by strike range, e.g., '23000-24000'.

    Output:
        JSON to stdout with keys: timestamp, underlyingValue, expiryDates,
        selectedExpiry, chain, summary. Or market_closed fallback JSON.
    """
    parser = argparse.ArgumentParser(description="Fetch Nifty Options Chain from NSE")
    parser.add_argument("--expiry", type=str, help="Expiry date (YYYY-MM-DD)")
    parser.add_argument("--strikes", type=str, help="Strike range (e.g., 23000-24000)")
    args = parser.parse_args()

    expiry_filter = None
    if args.expiry:
        expiry_filter = args.expiry

    strike_range = None
    if args.strikes:
        parts = args.strikes.split("-")
        if len(parts) == 2:
            try:
                strike_range = (int(parts[0]), int(parts[1]))
            except ValueError:
                print(json.dumps({"error": "Invalid strike range format. Use: 23000-24000"}))
                sys.exit(1)

    print("Fetching Nifty options chain from NSE...", file=sys.stderr)
    session = create_session()

    # Check market status
    market_status = fetch_market_status(session)

    data = fetch_options_chain(session)

    # If API returned empty data, report market status instead
    if not data or not data.get("records", {}).get("data"):
        result = {"market_closed": True, "chain": [], "summary": {}}
        if market_status:
            for m in market_status.get("marketState", []):
                if m.get("market") == "Capital Market":
                    result["marketStatus"] = m.get("marketStatus", "Unknown")
                    result["tradeDate"] = m.get("tradeDate", "N/A")
                    result["lastPrice"] = m.get("last", 0)
                    result["variation"] = m.get("variation", 0)
                    result["percentChange"] = m.get("percentChange", 0)
                    result["message"] = m.get("marketStatusMessage", "")
                    break
        print(json.dumps(result, indent=2))
        return

    result = process_data(data, expiry_filter, strike_range)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
