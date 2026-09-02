"""Deterministic Bull/Bear/Judge rules engine.

No network, no API key, no LLM. This is the permanent safety net under
llm.py -- if anything there fails, evaluate() still returns a full verdict.

evaluate(bundle) -> {
  "verdict": "BUY" | "HOLD" | "SELL",
  "confidence": 1-10,
  "engine": "deterministic",
  "panel": {"technician": str, "fundamentalist": str, "newsdesk": str,
            "bull": str, "bear": str},
  "judge": str,
  "bull_points": int, "bear_points": int,
}
"""

from data_sources import fmt_pct

POSITIVE_WORDS = (
    "record", "beats", "beat", "surge", "surges", "upgrade", "upgraded", "wins",
    "win", "order", "orders", "profit", "growth", "expansion", "approval",
    "approved", "highest", "strong", "rally", "buyback", "dividend",
)
NEGATIVE_WORDS = (
    "probe", "fraud", "downgrade", "downgraded", "loss", "losses", "fine",
    "penalty", "resign", "resigns", "recall", "cut", "cuts", "slump", "falls",
    "weak", "delay", "delayed", "default", "lawsuit", "raid", "warning",
)


def _headline_sentiment(news):
    """(positive_hits, negative_hits) across all headlines."""
    pos = neg = 0
    for item in news:
        words = item.get("title", "").lower().replace(",", " ").split()
        pos += sum(1 for w in words if w.strip(".:'\"") in POSITIVE_WORDS)
        neg += sum(1 for w in words if w.strip(".:'\"") in NEGATIVE_WORDS)
    return pos, neg


def _technician(b):
    """-> (points, one-line read). Positive points favour the bull."""
    pts, notes = 0, []
    price, s20, s50, s200 = b["price"], b["sma20"], b["sma50"], b["sma200"]

    if None not in (price, s20, s50):
        if price > s20 > s50:
            pts += 2
            notes.append(f"price {price} above 20DMA {s20} above 50DMA {s50} (stacked uptrend)")
        elif price < s20 < s50:
            pts -= 2
            notes.append(f"price {price} below 20DMA {s20} below 50DMA {s50} (stacked downtrend)")
        else:
            notes.append(f"price {price} mixed against 20DMA {s20} / 50DMA {s50}")
    if price is not None and s200 is not None:
        if price > s200:
            pts += 1
            notes.append(f"holding above the 200DMA {s200}")
        else:
            pts -= 1
            notes.append(f"trading under the 200DMA {s200}")

    rsi = b["rsi14"]
    if rsi is not None:
        if rsi >= 70:
            pts -= 2
            notes.append(f"RSI {rsi} is overbought")
        elif rsi <= 30:
            pts += 1
            notes.append(f"RSI {rsi} is oversold, mean-reversion setup")
        elif 50 <= rsi < 70:
            pts += 1
            notes.append(f"RSI {rsi} shows healthy momentum")
        else:
            notes.append(f"RSI {rsi} is soft")

    vol, avg = b["volume"], b["avg_volume_20d"]
    if vol and avg:
        ratio = vol / avg
        if ratio >= 1.5:
            pts += 1
            notes.append(f"volume {vol:,} is {ratio:.1f}x the 20-day average")
        elif ratio <= 0.6:
            pts -= 1
            notes.append(f"volume {vol:,} is only {ratio:.1f}x average, thin participation")

    hi, lo = b.get("range_high"), b.get("range_low")
    label = b.get("range_label", "52-week")
    if price and hi and lo and hi > lo:
        pos = (price - lo) / (hi - lo)
        if pos >= 0.95:
            pts -= 1
            notes.append(f"pinned at the {label} high {hi}, limited headroom")
        elif pos <= 0.15:
            pts -= 1
            notes.append(f"near the {label} low {lo}, no base yet")

    return pts, "; ".join(notes) or "insufficient price history to read a trend"


def _fundamentalist(b):
    pts, notes = 0, []
    up = b["analyst_upside_pct"]
    if up is None:
        notes.append("no analyst target in the feed, valuation call withheld")
    elif up >= 15:
        pts += 2
        notes.append(f"consensus target {b['analyst_target']} implies {up}% upside")
    elif up >= 5:
        pts += 1
        notes.append(f"consensus target {b['analyst_target']} implies a modest {up}% upside")
    elif up <= -5:
        pts -= 2
        notes.append(f"trading {abs(up)}% above the consensus target {b['analyst_target']}")
    else:
        notes.append(f"price sits on top of the consensus target {b['analyst_target']}")

    missing = [f for f in ("pe", "roe") if f in b["data_gaps"]]
    if missing:
        notes.append(f"{'/'.join(x.upper() for x in missing)} data unavailable in this feed")
    return pts, "; ".join(notes)


def _newsdesk(b):
    news = b.get("news") or []
    if not news:
        return 0, "no headlines in the window, news is not a factor here"
    pos, neg = _headline_sentiment(news)
    pts = (1 if pos > neg else 0) + (1 if pos >= 3 else 0) - (2 if neg > pos else 0)
    lead = news[0]["title"]
    tone = "constructive" if pos > neg else ("negative" if neg > pos else "neutral")
    return pts, (f"{len(news)} headlines, tone {tone} ({pos} positive / {neg} negative "
                 f"keywords); lead story: \"{lead}\"")


def evaluate(bundle):
    b = bundle
    t_pts, t_note = _technician(b)
    f_pts, f_note = _fundamentalist(b)
    n_pts, n_note = _newsdesk(b)

    bull_points = sum(p for p in (t_pts, f_pts, n_pts) if p > 0)
    bear_points = -sum(p for p in (t_pts, f_pts, n_pts) if p < 0)
    net = t_pts + f_pts + n_pts

    move = b.get("bar_change_pct", b["day_change_pct"])
    bull = (f"The case to buy {b['name']} rests on {fmt_pct(move)} on the "
            f"{b.get('timeframe_label', 'daily')} bar at {b['price']}. {t_note}. "
            f"On valuation: {f_note}.")
    bear = (f"Against {b['name']}: {n_note}. Risk markers score {bear_points} against "
            f"{bull_points} in favour, and the stock is a {b['cap_bucket']}-cap, "
            f"so position sizing matters.")

    if net >= 4:
        verdict, confidence = "BUY", min(10, 6 + net - 3)
    elif net >= 2:
        verdict, confidence = "BUY", 6
    elif net >= -1:
        verdict, confidence = "HOLD", 5
    else:
        verdict, confidence = "SELL", min(10, 5 + abs(net))

    if b["data_gaps"] and confidence > 3:
        confidence -= 1  # never fully confident on a bundle with holes

    judge = (f"Net signal score {net:+d} (bull {bull_points} / bear {bear_points}). "
             f"{verdict} at confidence {confidence}/10. "
             f"Unavailable fields: {', '.join(b['data_gaps']) or 'none'}.")

    return {
        "verdict": verdict,
        "confidence": int(confidence),
        "engine": "deterministic",
        "panel": {
            "technician": t_note,
            "fundamentalist": f_note,
            "newsdesk": n_note,
            "bull": bull,
            "bear": bear,
        },
        "judge": judge,
        "bull_points": bull_points,
        "bear_points": bear_points,
    }


def trade_levels(bundle):
    """Entry, target and stop for a BUY, derived only from figures already in
    the evidence bundle. Every level names the field it came from, and
    anything the bundle cannot support comes back None.

    These are reference levels you can trace back to real data, not a
    recommendation and not a price forecast. No order is ever placed.

    entry  the 20-bar average when price sits above it -- a pullback entry --
           otherwise the last price, because there is nothing to wait for.
    target the analyst consensus target when it clears the entry, otherwise
           the top of the range the bundle actually covers.
    stop   the 50-bar average when it sits below entry, otherwise the range low.
    """
    price = bundle.get("price")
    if not price:
        return {}
    sma20, sma50 = bundle.get("sma20"), bundle.get("sma50")
    hi, lo = bundle.get("range_high"), bundle.get("range_low")
    rng = bundle.get("range_label", "range")

    if sma20 and sma20 < price:
        entry, entry_basis = sma20, "pullback to the 20-bar average"
    else:
        entry, entry_basis = price, "at market, price is already at or below the 20-bar average"

    target = bundle.get("analyst_target")
    target_basis = "analyst consensus target"
    if not target or target <= entry:
        if hi and hi > entry:
            target, target_basis = hi, f"top of the {rng} range"
        else:
            target, target_basis = None, None

    if sma50 and sma50 < entry:
        stop, stop_basis = sma50, "50-bar average"
    elif lo and lo < entry:
        stop, stop_basis = lo, f"{rng} low"
    else:
        stop, stop_basis = None, None

    reward = (target - entry) if target else None
    risk = (entry - stop) if stop else None
    return {
        "entry": round(entry, 2), "entry_basis": entry_basis,
        "target": round(target, 2) if target else None, "target_basis": target_basis,
        "stop": round(stop, 2) if stop else None, "stop_basis": stop_basis,
        "upside_pct": round(reward / entry * 100, 2) if reward else None,
        "downside_pct": round(-risk / entry * 100, 2) if risk else None,
        "risk_reward": round(reward / risk, 2) if (reward and risk and risk > 0) else None,
    }


if __name__ == "__main__":
    strong = {
        "ticker": "TEST.NS", "name": "Test Co", "cap_bucket": "large",
        "price": 100.0, "day_change_pct": 3.0, "week_change_pct": 5.0,
        "month_change_pct": 8.0, "sma20": 95.0, "sma50": 90.0, "sma200": 80.0,
        "rsi14": 61.0, "volume": 2_000_000, "avg_volume_20d": 1_000_000,
        "week52_high": 110.0, "week52_low": 60.0, "analyst_target": 130.0,
        "analyst_upside_pct": 30.0, "pe": None, "roe": None,
        "news": [{"title": "Test Co wins record order, profit surges", "date": "2026-08-14", "source": "x"}],
        "data_gaps": ["pe", "roe"],
    }
    weak = dict(strong, price=70.0, sma20=75.0, sma50=80.0, sma200=90.0, rsi14=28.0,
                volume=400_000, analyst_target=65.0, analyst_upside_pct=-7.1,
                news=[{"title": "Regulator opens probe, shares slump on downgrade",
                       "date": "2026-08-14", "source": "x"}])

    s, w = evaluate(strong), evaluate(weak)
    assert s["verdict"] == "BUY" and s["confidence"] >= 7, s
    assert w["verdict"] == "SELL", w
    assert 1 <= s["confidence"] <= 10 and 1 <= w["confidence"] <= 10
    assert set(s["panel"]) == {"technician", "fundamentalist", "newsdesk", "bull", "bear"}
    # A bundle with everything missing must still return a verdict, not crash.
    empty = {"ticker": "E.NS", "name": "Empty", "cap_bucket": "small", "price": None,
             "news": [], "data_gaps": ["price", "day_change_pct"]}
    for f in ("day_change_pct", "week_change_pct", "month_change_pct", "sma20", "sma50", "sma200",
              "rsi14", "volume", "avg_volume_20d", "week52_high", "week52_low",
              "analyst_target", "analyst_upside_pct", "pe", "roe"):
        empty[f] = None
    assert evaluate(empty)["verdict"] in ("BUY", "HOLD", "SELL")
    # Trade levels must be traceable to bundle fields, never invented.
    lv = trade_levels(strong)
    assert lv["entry"] == 95.0 and "pullback" in lv["entry_basis"], lv
    assert lv["target"] == 130.0 and lv["target_basis"] == "analyst consensus target", lv
    assert lv["stop"] == 90.0 and lv["stop_basis"] == "50-bar average", lv
    assert lv["risk_reward"] == 7.0, lv          # (130-95)/(95-90)
    assert lv["upside_pct"] == 36.84, lv
    # Price already below its 20-bar average -> enter at market, not above.
    at_mkt = trade_levels(dict(strong, price=94.0))
    assert at_mkt["entry"] == 94.0 and "at market" in at_mkt["entry_basis"], at_mkt
    # No analyst target -> fall back to the range top, and say so.
    no_tgt = trade_levels(dict(strong, analyst_target=None, range_high=120.0,
                               range_label="52-week"))
    assert no_tgt["target"] == 120.0 and "52-week" in no_tgt["target_basis"], no_tgt
    # Nothing above entry anywhere -> no target rather than a made-up one.
    none_tgt = trade_levels(dict(strong, analyst_target=None, range_high=None))
    assert none_tgt["target"] is None and none_tgt["risk_reward"] is None, none_tgt
    assert trade_levels({"price": None}) == {}
    print("scoring.py self-check OK:", s["verdict"], s["confidence"], "|",
          w["verdict"], w["confidence"], "| levels traceable")
