"""Evidence bundles: the only file that knows where market data comes from.

Every consumer (scoring.py, llm.py, app.py) sees the same dict shape, so
swapping yfinance for a broker API means rewriting only the two functions at
the bottom of this file.

EVIDENCE BUNDLE SHAPE
---------------------
{
  "ticker":          "RELIANCE.NS",           # str, yfinance symbol
  "name":            "Reliance Industries",   # str
  "cap_bucket":      "large",                 # "large" | "mid" | "small"
  "price":           2890.5,                  # last traded price, INR
  "day_change_pct":  2.31,
  "week_change_pct": 4.10,
  "month_change_pct": -1.20,
  "sma20":           2810.0,
  "sma50":           2755.4,
  "sma200":          2601.8,
  "rsi14":           58.2,                    # 0-100
  "volume":          8123456,
  "avg_volume_20d":  6100000,
  "week52_high":     3024.9,
  "week52_low":      2220.1,
  "analyst_target":  3150.0,                  # may be None
  "analyst_upside_pct": 8.98,                 # may be None
  "pe":              None,                    # not in yfinance/NSE feed
  "roe":             None,                    # not in yfinance/NSE feed
  "news":            [{"title": str, "date": "YYYY-MM-DD", "source": str}],
  "data_gaps":       ["pe", "roe"],           # names of every None field
  "as_of":           "2026-08-15T09:30:00",
  "source":          "yfinance",
}

Any numeric field that cannot be computed MUST be set to None and its name
appended to data_gaps -- agents are instructed to say "data unavailable"
for anything listed there rather than guess.
"""

import json
import os
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
UNIVERSE_PATH = os.path.join(HERE, "universe.json")

# Fields that must exist on every bundle; used to normalise partial inputs.
NUMERIC_FIELDS = [
    "price", "day_change_pct", "bar_change_pct", "week_change_pct",
    "month_change_pct", "sma20", "sma50", "sma200", "rsi14", "volume",
    "avg_volume_20d", "range_high", "range_low", "week52_high", "week52_low",
    "analyst_target", "analyst_upside_pct", "pe", "roe",
]

# Every moving average and RSI below is measured in BARS of the selected
# timeframe, so "20DMA" on the 15m chart means 20 fifteen-minute bars. The
# period is what yfinance will serve for that interval.
TIMEFRAMES = {
    "15m": {"interval": "15m", "period": "60d",  "label": "15-minute", "resample": None,  "screen_bars": 1},
    "30m": {"interval": "30m", "period": "60d",  "label": "30-minute", "resample": None,  "screen_bars": 1},
    "1h":  {"interval": "1h",  "period": "730d", "label": "hourly",    "resample": None,  "screen_bars": 1},
    "4h":  {"interval": "1h",  "period": "730d", "label": "4-hour",    "resample": "4h",  "screen_bars": 1},
    "1d":  {"interval": "1d",  "period": "1y",   "label": "daily",     "resample": None,  "screen_bars": 1},
    "1wk": {"interval": "1wk", "period": "5y",   "label": "weekly",    "resample": None,  "screen_bars": 5},
    "1mo": {"interval": "1mo", "period": "10y",  "label": "monthly",   "resample": None,  "screen_bars": 21},
}
DEFAULT_TIMEFRAME = "1d"


def timeframe(key):
    return TIMEFRAMES.get(key, TIMEFRAMES[DEFAULT_TIMEFRAME])


def fmt_pct(value):
    """Format a nullable percentage. Unknown stays unknown -- never 0.00%."""
    return f"{value:+.2f}%" if isinstance(value, (int, float)) else "n/a"


def normalise(bundle):
    """Fill missing numeric fields with None and rebuild data_gaps."""
    for f in NUMERIC_FIELDS:
        bundle.setdefault(f, None)
    bundle.setdefault("news", [])
    bundle.setdefault("as_of", datetime.now().isoformat(timespec="seconds"))
    bundle.setdefault("timeframe", DEFAULT_TIMEFRAME)
    bundle.setdefault("timeframe_label", timeframe(bundle["timeframe"])["label"])
    # A daily bundle's high/low range IS the 52-week range; on shorter
    # timeframes it is only the fetched window, so the label has to say which.
    if bundle["range_high"] is None and bundle["week52_high"] is not None:
        bundle["range_high"] = bundle["week52_high"]
        bundle["range_low"] = bundle["week52_low"]
        bundle.setdefault("range_label", "52-week")
    bundle.setdefault("range_label", "52-week")
    if bundle["bar_change_pct"] is None:
        bundle["bar_change_pct"] = bundle["day_change_pct"]
    bundle["data_gaps"] = [f for f in NUMERIC_FIELDS if bundle.get(f) is None]
    return bundle


def load_universe():
    with open(UNIVERSE_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------
# Live mode -- yfinance. Replace the two functions below to swap data source.
# --------------------------------------------------------------------------

def _rsi14(closes):
    """Wilder RSI on a pandas Series of closes. None if not enough history."""
    if len(closes) < 15:
        return None
    delta = closes.diff().dropna()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    last_loss = float(loss.iloc[-1])
    if last_loss == 0:
        return 100.0
    rs = float(gain.iloc[-1]) / last_loss
    return round(100 - 100 / (1 + rs), 2)


def _pct(now, then):
    if then in (None, 0) or now is None:
        return None
    return round((now - then) / then * 100, 2)


def _sma(closes, n):
    return round(float(closes.tail(n).mean()), 2) if len(closes) >= n else None


def fetch_bars(ticker, tf_key=DEFAULT_TIMEFRAME):
    """OHLCV DataFrame on the requested timeframe. 4h is resampled from 1h,
    which yfinance does not serve natively."""
    import yfinance as yf

    tf = timeframe(tf_key)
    hist = yf.Ticker(ticker).history(
        period=tf["period"], interval=tf["interval"], auto_adjust=False)
    if hist is None or hist.empty:
        return hist
    if tf["resample"]:
        hist = hist.resample(tf["resample"]).agg(
            {"Open": "first", "High": "max", "Low": "min",
             "Close": "last", "Volume": "sum"}).dropna(how="all")
    return hist.dropna(subset=["Close"])


def build_evidence_bundle_from_yf(ticker, name, cap_bucket, tf_key=DEFAULT_TIMEFRAME):
    """One ticker -> one evidence bundle. Returns None if the fetch is empty."""
    import yfinance as yf

    tf = timeframe(tf_key)
    tk = yf.Ticker(ticker)
    hist = fetch_bars(ticker, tf_key)
    if hist is None or hist.empty or len(hist) < 2:
        return None

    closes = hist["Close"].dropna()
    price = round(float(closes.iloc[-1]), 2)
    prev = float(closes.iloc[-2])

    # Session change needs daily bars. Intraday series can be rolled up to
    # them; weekly/monthly bars cannot, so it stays unknown there.
    if tf["interval"] in ("15m", "30m", "1h"):
        daily = closes.resample("1D").last().dropna()
        day_change = _pct(price, float(daily.iloc[-2])) if len(daily) > 1 else None
    elif tf["interval"] == "1d":
        day_change = _pct(price, prev)
    else:
        day_change = None

    info = {}
    try:
        info = tk.info or {}
    except Exception:
        pass  # info is best-effort; the bundle is still useful without it

    target = info.get("targetMeanPrice")
    target = round(float(target), 2) if isinstance(target, (int, float)) else None

    news = []
    try:
        for item in (tk.news or [])[:5]:
            content = item.get("content", item)
            title = content.get("title") or item.get("title")
            if not title:
                continue
            pub = content.get("pubDate") or item.get("providerPublishTime")
            if isinstance(pub, (int, float)):
                pub = datetime.fromtimestamp(pub).strftime("%Y-%m-%d")
            news.append({
                "title": title,
                "date": str(pub)[:10] if pub else "",
                "source": (content.get("provider") or {}).get("displayName")
                          or item.get("publisher") or "yfinance",
            })
    except Exception:
        pass  # news is optional; newsdesk will report it as a gap

    vol = hist["Volume"].dropna()
    is_daily_range = tf["interval"] in ("1d", "1wk", "1mo")
    range_high = round(float(hist["High"].max()), 2)
    range_low = round(float(hist["Low"].min()), 2)
    return normalise({
        "ticker": ticker,
        "name": name,
        "cap_bucket": cap_bucket,
        "timeframe": tf_key,
        "timeframe_label": tf["label"],
        "price": price,
        "day_change_pct": day_change,
        "bar_change_pct": _pct(price, prev),
        "week_change_pct": _pct(price, float(closes.iloc[-6])) if len(closes) > 6 else None,
        "month_change_pct": _pct(price, float(closes.iloc[-22])) if len(closes) > 22 else None,
        "sma20": _sma(closes, 20),
        "sma50": _sma(closes, 50),
        "sma200": _sma(closes, 200),
        "rsi14": _rsi14(closes),
        "volume": int(vol.iloc[-1]) if len(vol) else None,
        "avg_volume_20d": int(vol.tail(20).mean()) if len(vol) >= 20 else None,
        "range_high": range_high,
        "range_low": range_low,
        "range_label": "52-week" if tf["interval"] == "1d" else f"{tf['period']} window",
        # Only a one-year daily window genuinely is the 52-week range.
        "week52_high": range_high if tf["interval"] == "1d" else None,
        "week52_low": range_low if tf["interval"] == "1d" else None,
        "analyst_target": target,
        "analyst_upside_pct": _pct(target, price) if target else None,
        "pe": None,   # NSE feed via yfinance does not carry a reliable P/E
        "roe": None,  # ...nor ROE
        "news": news,
        "source": "yfinance",
        "bars": len(hist),
    })


def _momentum(change, vol_ratio, near_high, near_low):
    """0-100 strength of the move. Describes what already happened -- size of
    the move, whether volume confirmed it, whether it broke the recent range.
    It is not a forecast, and nothing here predicts the next bar."""
    score = min(50.0, abs(change) * 10)
    if vol_ratio and vol_ratio > 1:
        score += min(30.0, (vol_ratio - 1) * 30)
    if (change > 0 and near_high) or (change < 0 and near_low):
        score += 20
    return round(min(100.0, score))


def _sort_key(intent):
    """buy hunts the strongest advances, sell the sharpest declines, any the
    biggest move in either direction. Momentum breaks ties."""
    if intent == "buy":
        return lambda s: (-s["change_pct"], -s["momentum"])
    if intent == "sell":
        return lambda s: (s["change_pct"], -s["momentum"])
    return lambda s: (-abs(s["change_pct"]), -s["momentum"])


def screen_movers(stocks, tf_key=DEFAULT_TIMEFRAME, log=print, intent="any"):
    """Rank a stock list by its move over the timeframe's lookback.

    One batched daily download for the whole list. Screening 500 tickers one
    at a time costs ~10 minutes; this costs seconds, and full evidence
    bundles are built afterwards only for the handful that survive.
    """
    import yfinance as yf

    lookback = timeframe(tf_key)["screen_bars"]
    tickers = [s["ticker"] for s in stocks]
    frames = {}
    for i in range(0, len(tickers), 200):
        chunk = tickers[i:i + 200]
        data = yf.download(chunk, period="3mo", interval="1d", group_by="ticker",
                           threads=True, progress=False, auto_adjust=False)
        if data is None or data.empty:
            continue
        for ticker in chunk:
            try:
                frames[ticker] = data[ticker] if len(chunk) > 1 else data
            except KeyError:
                continue
        log(f"  screened {min(i + len(chunk), len(tickers))}/{len(tickers)}")

    ranked = []
    for stock in stocks:
        frame = frames.get(stock["ticker"])
        if frame is None:
            continue
        closes = frame["Close"].dropna()
        if len(closes) < lookback + 1:
            continue
        price = round(float(closes.iloc[-1]), 2)
        change = _pct(price, float(closes.iloc[-1 - lookback]))
        if change is None:
            continue

        # Participation and range position: is the move actually being bought,
        # and is it happening at the edge of the recent range?
        vols = frame["Volume"].dropna()
        vol_ratio = None
        if len(vols) >= 20 and float(vols.tail(20).mean()) > 0:
            vol_ratio = round(float(vols.iloc[-1]) / float(vols.tail(20).mean()), 2)
        highs, lows = frame["High"].dropna().tail(20), frame["Low"].dropna().tail(20)
        near_high = bool(len(highs) and price >= float(highs.max()) * 0.995)
        near_low = bool(len(lows) and price <= float(lows.min()) * 1.005)

        ranked.append(dict(
            stock, price=price, change_pct=change, vol_ratio=vol_ratio,
            near_high=near_high, near_low=near_low,
            # Last 30 daily closes, for the row sparkline. Already downloaded,
            # so this costs nothing beyond the bytes on the wire.
            spark=[round(float(c), 2) for c in closes.tail(30)],
            momentum=_momentum(change, vol_ratio, near_high, near_low),
            # "Moving" means a real move on real volume, not a drift.
            moving=bool(vol_ratio and vol_ratio >= 1.5 and abs(change) >= 1.5),
        ))

    ranked.sort(key=_sort_key(intent))
    log(f"Screened {len(ranked)} of {len(stocks)} with usable data"
        f"{'' if intent == 'any' else f', hunting {intent} candidates'}.")
    return ranked


def historical_volatility(ticker, window=30, period="6mo"):
    """Annualised realised volatility, % -- the stdev of daily log returns over
    the window, scaled by sqrt(252). None when there is not enough history.

    This is what the underlying actually did. Compared against the implied
    volatility in the option chain, it says whether options are pricing more
    or less movement than has recently been delivered.
    """
    import numpy as np
    import yfinance as yf

    try:
        hist = yf.Ticker(ticker).history(period=period)
    except Exception:
        return None
    if hist is None or hist.empty or len(hist) < window + 1:
        return None
    rets = np.log(hist["Close"] / hist["Close"].shift(1)).dropna()
    if len(rets) < window:
        return None
    return round(float(rets.tail(window).std() * np.sqrt(252) * 100), 2)


QUADRANTS = {(True, True): "leading", (True, False): "weakening",
             (False, False): "lagging", (False, True): "improving"}


def rrg(tickers, benchmark, weeks=8, span=10):
    """Relative rotation of each ticker against a benchmark.

    Explicit formula, so this is reproducible rather than a black box:
        RS           = ticker close / benchmark close   (weekly bars)
        RS-Ratio     = 100 * RS / EMA(RS, span)
        RS-Momentum  = 100 * RS-Ratio / EMA(RS-Ratio, span)
    Both oscillate around 100. Above 100 on the ratio means outperforming the
    benchmark; above 100 on momentum means that outperformance is building.
    The four quadrants follow from the signs.

    This is a simplified construction in the spirit of the published RRG
    method, not the trademarked JdK implementation -- the numbers will not
    match a vendor's chart exactly, and the tails are the point anyway.

    -> {ticker: {"points": [{"ratio","momentum"}...], "quadrant": str}}
    """
    import yfinance as yf

    data = yf.download(list(tickers) + [benchmark], period="2y", interval="1wk",
                       group_by="ticker", threads=True, progress=False,
                       auto_adjust=False)
    try:
        bench = data[benchmark]["Close"].dropna()
    except KeyError:
        return {}
    if len(bench) < span * 2:
        return {}

    out = {}
    for ticker in tickers:
        try:
            closes = data[ticker]["Close"].dropna()
        except KeyError:
            continue
        pair = closes.align(bench, join="inner")
        series, base = pair[0], pair[1]
        if len(series) < span * 2 + weeks:
            continue
        rs = series / base
        ratio = 100 * rs / rs.ewm(span=span, adjust=False).mean()
        momentum = 100 * ratio / ratio.ewm(span=span, adjust=False).mean()
        pts = [{"ratio": round(float(r), 3), "momentum": round(float(m), 3)}
               for r, m in zip(ratio.tail(weeks), momentum.tail(weeks))]
        if not pts:
            continue
        last = pts[-1]
        out[ticker] = {
            "points": pts,
            "quadrant": QUADRANTS[(last["ratio"] >= 100, last["momentum"] >= 100)],
        }
    return out


def float_shares(tickers, log=print):
    """{ticker: free-float share count}. Cached on disk -- these change only on
    a corporate action, and fetching them is one slow .info call per ticker."""
    import json as _json
    import yfinance as yf

    os.makedirs(os.path.join(HERE, "cache"), exist_ok=True)
    path = os.path.join(HERE, "cache", "float_shares.json")
    cache = {}
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 7 * 86400:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cache = _json.load(fh)
        except Exception:
            cache = {}

    missing = [t for t in tickers if t not in cache]
    if missing:
        log(f"Fetching free-float share counts for {len(missing)} stocks (cached for a week)...")
        for ticker in missing:
            try:
                cache[ticker] = yf.Ticker(ticker).info.get("floatShares")
            except Exception:
                cache[ticker] = None
        with open(path, "w", encoding="utf-8") as fh:
            _json.dump(cache, fh)
    return {t: cache.get(t) for t in tickers}


def index_contributions(members, index_quote, log=print):
    """Decompose an index move into per-stock contributions.

    Two numbers per stock. change_pct is exact. points is an ESTIMATE: it
    needs each stock's free-float weight, and NSE's official IWF (banded and
    capped) is not published in any free feed, so this uses yfinance float
    share counts instead. The reconciliation error is returned alongside so
    the caller can show exactly how far off the decomposition is -- never
    present the points as if they were NSE's own figure.
    """
    import yfinance as yf

    tickers = [m["ticker"] for m in members]
    data = yf.download(tickers, period="5d", interval="1d", group_by="ticker",
                       threads=True, progress=False, auto_adjust=False)
    floats = float_shares(tickers, log)

    rows, prev_ffmc = [], 0.0
    for m in members:
        shares = floats.get(m["ticker"])
        try:
            closes = data[m["ticker"]]["Close"].dropna()
        except KeyError:
            continue
        if len(closes) < 2:
            continue
        last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
        if prev <= 0:
            continue
        rows.append({"symbol": m["symbol"], "name": m["name"], "ticker": m["ticker"],
                     "price": round(last, 2), "prev": prev, "shares": shares,
                     "change_pct": round((last / prev - 1) * 100, 2)})
        if shares:
            prev_ffmc += prev * shares

    prev_close = (index_quote or {}).get("previous_close")
    divisor = prev_ffmc / prev_close if (prev_ffmc and prev_close) else None
    for row in rows:
        row["points"] = (round((row["price"] - row["prev"]) * row["shares"] / divisor, 2)
                         if divisor and row["shares"] else None)
        row.pop("prev", None)
        row.pop("shares", None)

    modelled = sum(r["points"] for r in rows if r["points"] is not None) or None
    actual = (index_quote or {}).get("variation")
    return {
        "rows": rows,
        "modelled_change": round(modelled, 2) if modelled is not None else None,
        "actual_change": actual,
        "error_points": (round(modelled - actual, 2)
                         if modelled is not None and actual is not None else None),
    }


def deal_shortlist(ranked, count):
    """Top `count` names, dealt round-robin across cap buckets.

    Taking the top N outright lets one bucket swallow the whole list; dealing
    a turn at a time keeps a large/mid/small spread while still following the
    ranking inside each bucket. Buckets that run dry just stop taking turns.
    """
    by_bucket = {}
    for stock in ranked:                      # ranked is already in priority order
        by_bucket.setdefault(stock.get("cap_bucket", "small"), []).append(stock)
    out = []
    while len(out) < count:
        dealt = False
        for bucket in list(by_bucket):
            if by_bucket[bucket] and len(out) < count:
                out.append(by_bucket[bucket].pop(0))
                dealt = True
        if not dealt:
            break
    return out


def get_live_evidence_bundles(stocks, count=8,
                              tf_key=DEFAULT_TIMEFRAME, log=print, intent="any"):
    """Screen the filtered universe, then build bundles for the top movers
    in each cap bucket. `stocks` carries a cap_bucket per nse.universe().

    `count` is the total number of stocks to debate, dealt across cap buckets.
    `intent` decides what Scout shortlists, so asking for buy candidates
    changes which stocks reach the panel. It never touches what the panel
    concludes about them -- the Judge is free to reject every one.
    """
    ranked = screen_movers(stocks, tf_key, log, intent)

    kept = deal_shortlist(ranked, count)
    spread = {}
    for stock in kept:
        spread[stock.get("cap_bucket", "small")] = spread.get(stock.get("cap_bucket", "small"), 0) + 1
    log(f"Shortlist: {len(kept)} stocks — "
        + ", ".join(f"{b} {n}" for b, n in sorted(spread.items())))

    bundles = []
    for stock in kept:
        try:
            bundle = build_evidence_bundle_from_yf(
                stock["ticker"], stock["name"], stock.get("cap_bucket", "small"), tf_key)
        except Exception as exc:
            log(f"  {stock['ticker']}: fetch failed ({exc.__class__.__name__})")
            continue
        if bundle is None:
            log(f"  {stock['ticker']}: no data returned")
            continue
        # Carry the screen's read of the move through to the panel and the UI.
        bundle["momentum"] = stock.get("momentum")
        bundle["moving"] = stock.get("moving")
        bundle["vol_ratio"] = stock.get("vol_ratio")
        bundles.append(bundle)
        log(f"  {stock['ticker']} {bundle['price']} ({fmt_pct(bundle['bar_change_pct'])})")
    return bundles
