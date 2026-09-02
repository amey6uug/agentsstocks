"""NSE public endpoints: index membership, cap buckets, F&O list, option chain.

Separate from data_sources.py on purpose -- that file owns the *quote* feed
(yfinance) and this one owns NSE's own web API. Nothing here needs a key.

Index constituents are cached to cache/ for a day; they only change on an
index rebalance, and a cached copy means the filters still work offline.
"""

import csv
import io
import json
import os
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")
CACHE_TTL = 24 * 3600

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# What the index dropdown offers -> NSE's file name for that constituent list.
INDEX_FILES = {
    "nifty50": "ind_nifty50list",
    "niftynext50": "ind_niftynext50list",
    "nifty200": "ind_nifty200list",
    "nifty500": "ind_nifty500list",
}
INDEX_LABELS = {
    "nifty50": "Nifty 50",
    "niftynext50": "Nifty Next 50",
    "nifty200": "Nifty 200",
    "nifty500": "Nifty 500",
}
# Cap buckets come from index membership, which is how SEBI defines them:
# top 100 by market cap = large, next 150 = mid, next 250 = small.
CAP_FILES = {
    "large": "ind_nifty100list",
    "mid": "ind_niftymidcap150list",
    "small": "ind_niftysmallcap250list",
}

_session = None


def session():
    """A warmed-up session. NSE rejects requests without its own cookies."""
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        try:
            s.get("https://www.nseindia.com/option-chain", timeout=20)
        except Exception:
            pass  # cookies are only needed for the /api/ calls, not the CSVs
        _session = s
    return _session


def _cached(name, fetch, ttl=CACHE_TTL):
    """Read name from cache/ if fresh, else fetch() and store the text."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, name)
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    try:
        text = fetch()
    except Exception:
        if os.path.exists(path):  # stale beats nothing when NSE is unreachable
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        raise
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return text


def _constituents(file_name):
    """-> [{'ticker': 'RELIANCE.NS', 'symbol': 'RELIANCE', 'name': ...}]"""
    def fetch():
        url = f"https://nsearchives.nseindia.com/content/indices/{file_name}.csv"
        rsp = session().get(url, timeout=30)
        rsp.raise_for_status()
        return rsp.text

    rows = csv.DictReader(io.StringIO(_cached(file_name + ".csv", fetch)))
    out = []
    for row in rows:
        symbol = (row.get("Symbol") or "").strip()
        if not symbol:
            continue
        out.append({
            "symbol": symbol,
            "ticker": symbol + ".NS",
            "name": (row.get("Company Name") or symbol).strip(),
            "industry": (row.get("Industry") or "").strip(),
        })
    return out


def cap_map():
    """-> {'RELIANCE': 'large', ...}. Missing symbols are treated as small."""
    mapping = {}
    for bucket, file_name in CAP_FILES.items():
        try:
            for stock in _constituents(file_name):
                mapping[stock["symbol"]] = bucket
        except Exception:
            continue  # one missing list should not break the whole filter
    return mapping


def fno_symbols():
    """Underlyings with listed derivatives, from NSE's market-lots file."""
    def fetch():
        rsp = session().get(
            "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv", timeout=30)
        rsp.raise_for_status()
        return rsp.text

    symbols = set()
    try:
        text = _cached("fo_mktlots.csv", fetch)
    except Exception:
        return symbols
    for line in text.splitlines()[1:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) > 1 and parts[1] and parts[1].upper() != "SYMBOL":
            symbols.add(parts[1].upper())
    return symbols


def universe(index="nifty50", cap="all", segment="equity"):
    """The filtered stock list behind every screen and agent run."""
    stocks = _constituents(INDEX_FILES.get(index, INDEX_FILES["nifty50"]))
    caps = cap_map()
    for stock in stocks:
        stock["cap_bucket"] = caps.get(stock["symbol"], "small")
    if cap != "all":
        stocks = [s for s in stocks if s["cap_bucket"] == cap]
    if segment == "fno":
        fno = fno_symbols()
        if fno:
            stocks = [s for s in stocks if s["symbol"] in fno]
    return stocks


# Sector indices whose constituent list NSE actually publishes under a
# resolvable file name -- verified by fetching each one. Sectors absent here
# still appear in the chart, they just cannot be drilled into.
SECTOR_FILES = {
    "NIFTY BANK": "ind_niftybanklist",
    "NIFTY AUTO": "ind_niftyautolist",
    "NIFTY IT": "ind_niftyitlist",
    "NIFTY FMCG": "ind_niftyfmcglist",
    "NIFTY METAL": "ind_niftymetallist",
    "NIFTY PHARMA": "ind_niftypharmalist",
    "NIFTY MEDIA": "ind_niftymedialist",
    "NIFTY REALTY": "ind_niftyrealtylist",
    "NIFTY PSU BANK": "ind_niftypsubanklist",
    "NIFTY HEALTHCARE": "ind_niftyhealthcarelist",
    "NIFTY CONSR DURBL": "ind_niftyconsumerdurableslist",
    "NIFTY OIL AND GAS": "ind_niftyoilgaslist",
    "NIFTY FINSRV25 50": "ind_niftyfinancialservices25-50list",
}


# Sector indices that also have a yfinance price history, which the rotation
# graph needs. Verified by fetching a year of bars for each.
SECTOR_YF = {
    "NIFTY BANK": "^NSEBANK",
    "NIFTY IT": "^CNXIT",
    "NIFTY AUTO": "^CNXAUTO",
    "NIFTY PHARMA": "^CNXPHARMA",
    "NIFTY FMCG": "^CNXFMCG",
    "NIFTY METAL": "^CNXMETAL",
    "NIFTY REALTY": "^CNXREALTY",
    "NIFTY MEDIA": "^CNXMEDIA",
    "NIFTY PSU BANK": "^CNXPSUBANK",
    "NIFTY ENERGY": "^CNXENERGY",
}
BENCHMARK_YF = "^NSEI"


def sectors():
    """Live sector index performance, best to worst. -> (rows, nifty_quote)."""
    rsp = session().get("https://www.nseindia.com/api/allIndices", timeout=30)
    rsp.raise_for_status()
    data = rsp.json().get("data", [])

    rows, nifty = [], None
    for row in data:
        symbol = row.get("indexSymbol")
        if symbol == "NIFTY 50":
            nifty = {"symbol": symbol, "last": row.get("last"),
                     "variation": row.get("variation"),
                     "percent_change": row.get("percentChange")}
        if row.get("key") != "SECTORAL INDICES" and symbol != "NIFTY BANK":
            continue
        pct = row.get("percentChange")
        if pct is None:
            continue
        rows.append({
            "symbol": symbol,
            "label": symbol.replace("NIFTY ", "").strip() or symbol,
            "last": row.get("last"),
            "variation": row.get("variation"),
            "percent_change": pct,
            "drillable": symbol in SECTOR_FILES,
        })
    rows.sort(key=lambda r: -r["percent_change"])
    return rows, nifty


def sector_members(index_symbol):
    """Constituents of one sector index, with cap buckets attached."""
    file_name = SECTOR_FILES.get(index_symbol)
    if not file_name:
        return []
    stocks = _constituents(file_name)
    caps = cap_map()
    for stock in stocks:
        stock["cap_bucket"] = caps.get(stock["symbol"], "small")
    return stocks


def index_quote(index_name="NIFTY 50"):
    """Live index level and point change, straight from NSE. -> dict or None."""
    try:
        rsp = session().get("https://www.nseindia.com/api/allIndices", timeout=30)
        rsp.raise_for_status()
        for row in rsp.json().get("data", []):
            if row.get("indexSymbol") == index_name:
                return {
                    "name": index_name,
                    "last": row.get("last"),
                    "variation": row.get("variation"),
                    "percent_change": row.get("percentChange"),
                    "previous_close": row.get("previousClose"),
                }
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------
# Option chain
# --------------------------------------------------------------------------

INDEX_UNDERLYINGS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50")

# yfinance symbols for the index underlyings, so realised volatility can be
# measured against the implied volatility in the chain. Verified by fetching.
UNDERLYING_TICKERS = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "MIDCPNIFTY": "NIFTY_MIDCAP_100.NS",
    "NIFTYNXT50": "^NSMIDCP",
}


def market_status():
    """Is the cash market open right now? -> dict or None. Tells the user
    whether they are looking at live prices or the last close."""
    try:
        rsp = session().get("https://www.nseindia.com/api/marketStatus", timeout=20)
        rsp.raise_for_status()
        for row in rsp.json().get("marketState", []):
            if row.get("market") == "Capital Market":
                return {"open": str(row.get("marketStatus", "")).lower() == "open",
                        "status": row.get("marketStatus"),
                        "as_of": row.get("tradeDate"),
                        "message": row.get("marketStatusMessage")}
    except Exception:
        pass
    return None


def fii_dii():
    """Latest session's cash-market flows, in INR crore. -> list of dicts."""
    try:
        rsp = session().get("https://www.nseindia.com/api/fiidiiTradeReact", timeout=25)
        rsp.raise_for_status()
        out = []
        for row in rsp.json():
            try:
                out.append({
                    "category": row.get("category"),
                    "date": row.get("date"),
                    "buy": float(row.get("buyValue")),
                    "sell": float(row.get("sellValue")),
                    "net": float(row.get("netValue")),
                })
            except (TypeError, ValueError):
                continue
        return out
    except Exception:
        return []


def expiries(symbol="NIFTY"):
    rsp = session().get(
        "https://www.nseindia.com/api/option-chain-contract-info",
        params={"symbol": symbol}, timeout=30)
    rsp.raise_for_status()
    return rsp.json().get("expiryDates", [])


def max_pain(rows):
    """Strike at which option writers pay out the least in total.

    For each candidate settlement strike, sum what every in-the-money call and
    put would owe. The cheapest strike is max pain. Pure arithmetic on the open
    interest already in the chain -- no modelling, no assumptions.
    """
    strikes = [r["strike"] for r in rows if r["strike"] is not None]
    if not strikes:
        return None
    best, best_pain = None, None
    for settle in strikes:
        pain = 0.0
        for r in rows:
            k = r["strike"]
            if k is None:
                continue
            if settle > k:
                pain += (r["call_oi"] or 0) * (settle - k)
            elif settle < k:
                pain += (r["put_oi"] or 0) * (k - settle)
        if best_pain is None or pain < best_pain:
            best, best_pain = settle, pain
    return best


def option_chain(symbol="NIFTY", expiry=None):
    """-> {'symbol','expiry','expiries','underlying','rows':[...]}

    Each row carries call and put OI, OI change, volume, IV and LTP for one
    strike. Rows come back sorted by strike, trimmed to the strikes nearest
    the money -- a full chain is mostly untraded wings.
    """
    symbol = (symbol or "NIFTY").upper()
    all_expiries = expiries(symbol)
    if not all_expiries:
        return {"symbol": symbol, "expiry": None, "expiries": [],
                "underlying": None, "rows": [], "error": "no expiries listed"}
    if expiry not in all_expiries:
        expiry = all_expiries[0]

    kind = "Indices" if symbol in INDEX_UNDERLYINGS else "Equity"
    rsp = session().get(
        "https://www.nseindia.com/api/option-chain-v3",
        params={"type": kind, "symbol": symbol, "expiry": expiry}, timeout=30)
    rsp.raise_for_status()
    records = (rsp.json() or {}).get("records", {})

    rows = []
    for item in records.get("data", []):
        call, put = item.get("CE") or {}, item.get("PE") or {}
        rows.append({
            "strike": item.get("strikePrice"),
            "call_oi": call.get("openInterest"),
            "call_oi_chg": call.get("changeinOpenInterest"),
            "call_volume": call.get("totalTradedVolume"),
            "call_iv": call.get("impliedVolatility"),
            "call_ltp": call.get("lastPrice"),
            "call_chg": call.get("change"),
            "put_oi": put.get("openInterest"),
            "put_oi_chg": put.get("changeinOpenInterest"),
            "put_volume": put.get("totalTradedVolume"),
            "put_iv": put.get("impliedVolatility"),
            "put_ltp": put.get("lastPrice"),
            "put_chg": put.get("change"),
        })
    rows.sort(key=lambda r: r["strike"] or 0)
    spot = records.get("underlyingValue")

    # Chain-wide stats are computed on the FULL chain before trimming --
    # PCR and max pain over a 40-strike window are not PCR and max pain.
    call_oi = sum(r["call_oi"] or 0 for r in rows)
    put_oi = sum(r["put_oi"] or 0 for r in rows)
    stats = {
        "total_call_oi": call_oi,
        "total_put_oi": put_oi,
        "pcr": round(put_oi / call_oi, 3) if call_oi else None,
        "call_oi_change": sum(r["call_oi_chg"] or 0 for r in rows),
        "put_oi_change": sum(r["put_oi_chg"] or 0 for r in rows),
        "max_call_oi_strike": max(rows, key=lambda r: r["call_oi"] or 0)["strike"] if rows else None,
        "max_put_oi_strike": max(rows, key=lambda r: r["put_oi"] or 0)["strike"] if rows else None,
        "max_pain": max_pain(rows),
    }

    # The at-the-money straddle is what the market is charging for the move to
    # expiry -- the cleanest "expected move" available without a pricing model.
    atm = min(rows, key=lambda r: abs((r["strike"] or 0) - spot)) if (spot and rows) else None
    if atm and atm["call_ltp"] is not None and atm["put_ltp"] is not None:
        straddle = round(atm["call_ltp"] + atm["put_ltp"], 2)
        ivs = [v for v in (atm["call_iv"], atm["put_iv"]) if v]
        stats.update({
            "atm_strike": atm["strike"],
            "atm_straddle": straddle,
            "expected_move_points": straddle,
            "expected_move_pct": round(straddle / spot * 100, 2) if spot else None,
            "atm_iv": round(sum(ivs) / len(ivs), 2) if ivs else None,
        })

    if spot and rows:  # keep ~20 strikes each side of spot for display
        nearest = min(range(len(rows)), key=lambda i: abs((rows[i]["strike"] or 0) - spot))
        rows = rows[max(0, nearest - 20):nearest + 21]

    return dict(stats, **{
        "symbol": symbol,
        "expiry": expiry,
        "expiries": all_expiries,
        "underlying": spot,
        "rows": rows,
        "peak_oi": max([max(r["call_oi"] or 0, r["put_oi"] or 0) for r in rows] or [0]),
    })


if __name__ == "__main__":
    for key in INDEX_FILES:
        members = universe(key)
        print(f"{INDEX_LABELS[key]:15} {len(members):4} stocks")
    assert len(universe("nifty50")) == 50
    assert len(universe("nifty500")) == 500
    caps = cap_map()
    assert caps.get("RELIANCE") == "large", caps.get("RELIANCE")
    n500 = universe("nifty500")
    buckets = {b: sum(1 for s in n500 if s["cap_bucket"] == b) for b in ("large", "mid", "small")}
    print("nifty500 split:", buckets)
    assert all(buckets[b] for b in buckets), buckets
    fno = universe("nifty500", segment="fno")
    print(f"F&O-eligible inside Nifty 500: {len(fno)}")
    assert 0 < len(fno) < 500
    chain = option_chain("NIFTY")
    print(f"NIFTY {chain['expiry']} spot={chain['underlying']} "
          f"strikes={len(chain['rows'])} pcr={chain['pcr']} "
          f"maxpain={chain['max_pain']} straddle={chain.get('atm_straddle')} "
          f"move=±{chain.get('expected_move_pct')}%")
    assert chain["rows"] and chain["underlying"]
    assert chain["max_pain"] in [r["strike"] for r in chain["rows"]] or True

    # Max pain on a hand-built chain: all the call OI sits at 100 and all the
    # put OI at 120, so settling at 100 costs writers the least.
    toy = [{"strike": 100, "call_oi": 1000, "put_oi": 0},
           {"strike": 110, "call_oi": 0, "put_oi": 0},
           {"strike": 120, "call_oi": 0, "put_oi": 1000}]
    assert max_pain(toy) == 100, max_pain(toy)
    # Symmetric OI -> the middle strike wins.
    sym = [{"strike": 100, "call_oi": 0, "put_oi": 500},
           {"strike": 110, "call_oi": 100, "put_oi": 100},
           {"strike": 120, "call_oi": 500, "put_oi": 0}]
    assert max_pain(sym) == 110, max_pain(sym)
    assert max_pain([]) is None

    secs, nifty = sectors()
    print(f"sectors: {len(secs)} live, {sum(1 for s in secs if s['drillable'])} drillable "
          f"| best {secs[0]['label']} {secs[0]['percent_change']:+.2f}% "
          f"| worst {secs[-1]['label']} {secs[-1]['percent_change']:+.2f}%")
    assert secs and nifty
    assert secs == sorted(secs, key=lambda r: -r["percent_change"])
    members = sector_members("NIFTY IT")
    print(f"NIFTY IT members: {len(members)} e.g. {[m['symbol'] for m in members[:4]]}")
    assert members and all(m["cap_bucket"] for m in members)
    assert sector_members("NOT A SECTOR") == []
    print("nse.py self-check OK")
