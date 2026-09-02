"""AgentDesk -- local Flask server, agent state machine, Telegram, audit log.

Analysis only. This app never places a trade.
"""

import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

import data_sources
import llm
import nse
import scoring

load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "stock_agents.db")

BRAND = os.getenv("BRAND", "AgentDesk")
CONFIDENCE_THRESHOLD = int(os.getenv("CONFIDENCE_THRESHOLD", "7"))
AGENT_DELAY = float(os.getenv("AGENT_DELAY", "0.6"))
# How many stocks a run debates in total, dealt across cap buckets. The UI
# overrides this per run; SHORTLIST_PER_BUCKET is the superseded per-bucket knob
# and is only used to derive a sensible default for anyone who still sets it.
STOCK_COUNT = int(os.getenv("STOCK_COUNT", str(int(os.getenv("SHORTLIST_PER_BUCKET", "4")) * 2)))
STOCK_COUNT_CHOICES = [3, 5, 8, 12, 16, 20, 30]
PORT = int(os.getenv("PORT", "5000"))
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

AGENTS = ["scout", "technician", "fundamentalist", "newsdesk",
          "bull", "bear", "judge", "messenger"]

app = Flask(__name__, static_folder=None)

_lock = threading.Lock()
STATE = {}
# Set by /api/stop. Checked at every step boundary in run_pipeline, so a run
# unwinds cleanly instead of being abandoned mid-write.
STOP = threading.Event()


# --------------------------------------------------------------------------
# Secret scrubbing -- applied to every log line, error and API response
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")


def scrub(text):
    text = str(text)
    if TELEGRAM_BOT_TOKEN:
        text = text.replace(TELEGRAM_BOT_TOKEN, "***")
    return _TOKEN_RE.sub("***", text)


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def resolve_universe(index, cap, segment, sector=None):
    """The filtered stock list for a run or a screen.

    'custom' means universe.json, which stays the hand-editable escape hatch;
    every other index is pulled live from NSE's own constituent lists. A
    sector, when given, replaces the index as the source of names.
    """
    if sector:
        stocks = nse.sector_members(sector)
        if cap != "all":
            stocks = [s for s in stocks if s["cap_bucket"] == cap]
        if segment == "fno":
            fno = nse.fno_symbols()
            if fno:
                stocks = [s for s in stocks if s["symbol"] in fno]
        return stocks
    if index == "custom":
        stocks = []
        for bucket, entries in data_sources.load_universe().items():
            for entry in entries:
                stocks.append(dict(entry, cap_bucket=bucket,
                                   symbol=entry["ticker"].split(".")[0]))
        if cap != "all":
            stocks = [s for s in stocks if s["cap_bucket"] == cap]
        if segment == "fno":
            fno = nse.fno_symbols()
            if fno:
                stocks = [s for s in stocks if s["symbol"] in fno]
        return stocks
    return nse.universe(index, cap, segment)


def reset_state(mode="demo", filters=None):
    with _lock:
        STATE.clear()
        STATE.update({
            "running": False,
            "mode": mode,
            "filters": filters or {},
            "engine": llm.detect_provider(),
            "step": "idle",
            "progress": {"done": 0, "total": 0, "current": None},
            "agents": {a: "offline" for a in AGENTS},
            "log": [],
            "results": [],
            "signals": [],
            "started_at": None,
            "finished_at": None,
            "error": None,
            "stopped": False,
        })


def log(message):
    line = f"{datetime.now().strftime('%H:%M:%S')}  {scrub(message)}"
    print(line, flush=True)
    with _lock:
        STATE["log"].append(line)
        del STATE["log"][:-400]  # keep the tail, this is a live feed not an archive


def set_agents(status, *names):
    with _lock:
        for name in (names or AGENTS):
            STATE["agents"][name] = status


def set_step(step):
    with _lock:
        STATE["step"] = step


# --------------------------------------------------------------------------
# SQLite audit trail
# --------------------------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT, finished_at TEXT, mode TEXT, engine TEXT,
            n_evaluated INTEGER, n_signals INTEGER
        );
        CREATE TABLE IF NOT EXISTS verdicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER, created_at TEXT, ticker TEXT, name TEXT,
            cap_bucket TEXT, price REAL, verdict TEXT, confidence INTEGER,
            engine TEXT, signalled INTEGER, payload TEXT
        );
        """)


def start_run_row(mode, engine):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO runs (started_at, mode, engine, n_evaluated, n_signals)"
            " VALUES (?,?,?,0,0)",
            (datetime.now().isoformat(timespec="seconds"), mode, engine))
        return cur.lastrowid


def finish_run_row(run_id, n_evaluated, n_signals):
    with db() as conn:
        conn.execute(
            "UPDATE runs SET finished_at=?, n_evaluated=?, n_signals=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), n_evaluated, n_signals, run_id))


def save_verdict(run_id, bundle, result, signalled):
    with db() as conn:
        conn.execute(
            "INSERT INTO verdicts (run_id, created_at, ticker, name, cap_bucket, price,"
            " verdict, confidence, engine, signalled, payload) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, datetime.now().isoformat(timespec="seconds"), bundle["ticker"],
             bundle["name"], bundle["cap_bucket"], bundle["price"], result["verdict"],
             result["confidence"], result["engine"], int(signalled),
             json.dumps({"evidence": bundle, "result": result}, default=str)))


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def telegram_configured():
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def send_telegram(text):
    """-> True if delivered. Never raises, never leaks the token."""
    if not telegram_configured():
        return False
    try:
        rsp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15)
        if rsp.status_code != 200:
            log(f"Telegram rejected the message ({rsp.status_code}): {scrub(rsp.text)[:180]}")
            return False
        return True
    except Exception as exc:
        log(f"Telegram send failed: {scrub(exc)[:180]}")
        return False


def levels_text(levels):
    """Reference levels for Telegram, each naming the field it came from."""
    if not levels or not levels.get("entry"):
        return ""
    rows = [f"<b>Entry</b> {levels['entry']} — {levels['entry_basis']}"]
    if levels.get("target"):
        rows.append(f"<b>Target</b> {levels['target']} "
                    f"(+{levels['upside_pct']}%) — {levels['target_basis']}")
    if levels.get("stop"):
        rows.append(f"<b>Stop</b> {levels['stop']} "
                    f"({levels['downside_pct']}%) — {levels['stop_basis']}")
    if levels.get("risk_reward"):
        rows.append(f"<b>Risk/reward</b> {levels['risk_reward']} : 1")
    return "\n".join(rows) + "\n\n"


def signal_message(bundle, result):
    gaps = ", ".join(bundle["data_gaps"]) or "none"
    return (
        f"<b>{BRAND} — BUY signal</b>\n"
        f"<b>{bundle['name']}</b> ({bundle['ticker']}, {bundle['cap_bucket']}-cap)\n"
        f"Price {bundle['price']}  ({data_sources.fmt_pct(bundle['day_change_pct'])} today)\n"
        f"Confidence {result['confidence']}/10 · engine: {result['engine']}\n\n"
        f"<b>Judge:</b> {result['judge']}\n\n"
        f"<b>Bull:</b> {result['panel']['bull']}\n"
        f"<b>Bear:</b> {result['panel']['bear']}\n\n"
        f"{levels_text(result.get('levels'))}"
        f"<i>Data unavailable: {gaps}. Analysis only — not investment advice.</i>")


def summary_message(mode, signals, n_evaluated):
    head = (f"<b>{BRAND} — daily summary</b>\n"
            f"{datetime.now().strftime('%d %b %Y, %H:%M')} · {mode} mode · "
            f"{n_evaluated} stocks debated\n\n")
    if not signals:
        return head + "No BUY signals cleared the confidence threshold today."
    lines = "\n".join(
        f"• <b>{s['name']}</b> ({s['ticker']}) — {s['price']}, confidence {s['confidence']}/10"
        for s in signals)
    return head + f"{len(signals)} signal(s) fired:\n{lines}\n\n<i>Analysis only — not investment advice.</i>"


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------

def pace():
    if AGENT_DELAY > 0:
        time.sleep(AGENT_DELAY)


def run_pipeline(mode, filters):
    provider = llm.detect_provider()
    index = filters.get("index", "nifty50")
    cap = filters.get("cap", "all")
    segment = filters.get("segment", "equity")
    tf_key = filters.get("timeframe", data_sources.DEFAULT_TIMEFRAME)
    intent = filters.get("intent", "any")
    count = max(1, min(int(filters.get("count") or STOCK_COUNT), 50))
    with _lock:
        STATE["running"] = True
        STATE["engine"] = provider
        STATE["started_at"] = datetime.now().isoformat(timespec="seconds")
    run_id = start_run_row(mode, provider)
    signals = []
    bundles = []
    stopped = False

    try:
        log(f"{BRAND} run started — {mode} mode, engine: {provider}")
        if provider == "deterministic":
            log("No LLM provider detected, running the deterministic rules panel.")

        # 1. Scout
        set_step("screening")
        set_agents("working", "scout")
        pace()
        if mode == "live":
            stocks = resolve_universe(index, cap, segment, filters.get("sector"))
            log(f"Scout: {nse.INDEX_LABELS.get(index, index)} · {cap} cap · "
                f"{segment} · {data_sources.timeframe(tf_key)['label']} bars "
                f"— {len(stocks)} stocks to screen"
                f"{'' if intent == 'any' else f', hunting {intent} candidates'}"
                f" — keeping {count}.")
            bundles = data_sources.get_live_evidence_bundles(
                stocks, count, tf_key, log, intent)
        else:
            log(f"Scout: loading the offline demo bundles, keeping {count}...")
            bundles = data_sources.deal_shortlist(
                data_sources.get_demo_evidence_bundles(), count)
        set_agents("done", "scout")
        log(f"Scout: {len(bundles)} stocks shortlisted for debate.")
        if STOP.is_set():
            bundles = []
        with _lock:
            STATE["progress"]["total"] = len(bundles)

        if not bundles:
            log("Nothing to debate — the screen returned no usable data.")

        # 2. Panel + judge, one stock at a time
        for i, bundle in enumerate(bundles, 1):
            if STOP.is_set():
                stopped = True
                log(f"Stopped by request after {i - 1} of {len(bundles)} stocks.")
                break
            with _lock:
                STATE["progress"]["current"] = bundle["ticker"]
            set_step(f"debating {bundle['ticker']}")
            log(f"[{i}/{len(bundles)}] {bundle['name']} ({bundle['ticker']}) "
                f"{bundle['price']} {data_sources.fmt_pct(bundle['day_change_pct'])}")

            set_agents("working", "technician", "fundamentalist", "newsdesk")
            pace()
            set_agents("working", "bull", "bear")
            pace()
            set_agents("working", "judge")

            result = llm.run_panel(bundle, provider, log)
            if STOP.is_set():
                # The in-flight call was killed; whatever came back is not a
                # verdict anyone asked for, so it is not recorded.
                stopped = True
                log(f"Stopped by request during {bundle['ticker']} — "
                    f"its evaluation was discarded.")
                break

            set_agents("done", "technician", "fundamentalist", "newsdesk",
                       "bull", "bear", "judge")
            if result.get("fallback_reason"):
                log(f"  fell back to the deterministic engine: {result['fallback_reason']}")

            fired = (result["verdict"] == "BUY"
                     and result["confidence"] >= CONFIDENCE_THRESHOLD)
            log(f"  Judge: {result['verdict']} at {result['confidence']}/10"
                f"{' — signal' if fired else ''}")

            row = {
                "ticker": bundle["ticker"], "name": bundle["name"],
                "cap_bucket": bundle["cap_bucket"], "price": bundle["price"],
                "day_change_pct": bundle["day_change_pct"],
                "bar_change_pct": bundle["bar_change_pct"],
                "timeframe_label": bundle.get("timeframe_label"),
                "moving": bundle.get("moving"),
                "momentum": bundle.get("momentum"),
                "vol_ratio": bundle.get("vol_ratio"),
                "verdict": result["verdict"], "confidence": result["confidence"],
                "engine": result["engine"], "panel": result["panel"],
                "judge": result["judge"], "data_gaps": bundle["data_gaps"],
                "levels": (scoring.trade_levels(bundle)
                           if result["verdict"] == "BUY" else None),
                "news": bundle.get("news", []), "signalled": fired,
                "evidence": bundle,
            }
            with _lock:
                STATE["results"].append(row)
                STATE["progress"]["done"] = i
            save_verdict(run_id, bundle, result, fired)
            if fired:
                signals.append(row)
            pace()

        # 3. Messenger
        set_step("messaging")
        set_agents("working", "messenger")
        pace()
        if stopped:
            log(f"Messenger: run was stopped, not sending "
                f"{len(signals)} pending signal(s).")
        elif not telegram_configured():
            log(f"Messenger: Telegram not configured, skipping "
                f"{len(signals)} signal message(s) and the daily summary.")
        else:
            for row in signals:
                sent = send_telegram(signal_message(row["evidence"],
                                                    {"confidence": row["confidence"],
                                                     "engine": row["engine"],
                                                     "judge": row["judge"],
                                                     "panel": row["panel"],
                                                     "levels": row.get("levels")}))
                log(f"Messenger: {row['ticker']} signal "
                    f"{'sent' if sent else 'not delivered'}.")
            if send_telegram(summary_message(mode, signals, len(bundles))):
                log("Messenger: daily summary sent.")
        set_agents("done", "messenger")

        with _lock:
            STATE["signals"] = [{k: v for k, v in s.items() if k != "evidence"}
                                for s in signals]
        done = STATE["progress"]["done"]
        log(f"Run {'stopped' if stopped else 'complete'} — {done} debated, "
            f"{len(signals)} BUY signal(s) at confidence >= {CONFIDENCE_THRESHOLD}.")
    except Exception as exc:
        with _lock:
            STATE["error"] = scrub(f"{exc.__class__.__name__}: {exc}")
        log(f"Run failed: {scrub(exc)}")
    finally:
        finish_run_row(run_id, STATE.get("progress", {}).get("done", 0), len(signals))
        set_step("stopped" if stopped else "done")
        with _lock:
            STATE["running"] = False
            STATE["stopped"] = stopped
            STATE["finished_at"] = datetime.now().isoformat(timespec="seconds")
            STATE["progress"]["current"] = None


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(HERE, "dashboard.html")


@app.get("/api/config")
def api_config():
    return jsonify({
        "brand": BRAND,
        "engine": llm.detect_provider(),
        "confidence_threshold": CONFIDENCE_THRESHOLD,
        "telegram": telegram_configured(),
        "agents": AGENTS,
        "stock_count": STOCK_COUNT,
        "market": nse.market_status(),
    })


@app.get("/api/filters")
def api_filters():
    """Everything the dropdowns need, so the UI hardcodes no lists."""
    return jsonify({
        "pages": [
            {"key": "agents", "label": "Agent panel"},
            {"key": "screener", "label": "Top stocks"},
            {"key": "chart", "label": "Chart"},
            {"key": "contrib", "label": "Index movers"},
            {"key": "sectors", "label": "Sector flow"},
            {"key": "rrg", "label": "Sector rotation"},
            {"key": "heatmap", "label": "Heatmap"},
            {"key": "history", "label": "History"},
            {"key": "options", "label": "F&O option chain"},
        ],
        "indices": ([{"key": k, "label": v} for k, v in nse.INDEX_LABELS.items()]
                    + [{"key": "custom", "label": "Custom (universe.json)"}]),
        "caps": [{"key": "all", "label": "All caps"},
                 {"key": "large", "label": "Large cap"},
                 {"key": "mid", "label": "Mid cap"},
                 {"key": "small", "label": "Small cap"}],
        "timeframes": [{"key": k, "label": v["label"]}
                       for k, v in data_sources.TIMEFRAMES.items()],
        "segments": [{"key": "equity", "label": "Equity"},
                     {"key": "fno", "label": "F&O"}],
        "counts": [{"key": str(n), "label": f"{n} stocks"} for n in STOCK_COUNT_CHOICES],
        "default_count": STOCK_COUNT,
        "intents": [{"key": "any", "label": "Biggest movers"},
                    {"key": "buy", "label": "Buy candidates"},
                    {"key": "sell", "label": "Sell candidates"}],
        # Sectors we can actually resolve to a constituent list. Static map,
        # so this costs no network call.
        "sectors": ([{"key": "", "label": "Whole index"}]
                    + [{"key": k, "label": k.replace("NIFTY ", "").title()}
                       for k in sorted(nse.SECTOR_FILES)]),
        "option_underlyings": list(nse.INDEX_UNDERLYINGS),
    })


@app.get("/api/screen")
def api_screen():
    """Top movers for the current filters -- no LLM, no debate, just a screen."""
    index = request.args.get("index", "nifty50")
    cap = request.args.get("cap", "all")
    segment = request.args.get("segment", "equity")
    tf_key = request.args.get("timeframe", data_sources.DEFAULT_TIMEFRAME)
    intent = request.args.get("intent", "any")
    sector = request.args.get("sector") or None
    limit = min(int(request.args.get("limit", 25)), 100)
    try:
        stocks = resolve_universe(index, cap, segment, sector)
        ranked = data_sources.screen_movers(stocks, tf_key, lambda m: None, intent)
    except Exception as exc:
        return jsonify({"error": scrub(f"{exc.__class__.__name__}: {exc}")}), 502
    advances = sum(1 for s in ranked if s["change_pct"] > 0)
    declines = sum(1 for s in ranked if s["change_pct"] < 0)
    return jsonify({
        "universe_size": len(stocks),
        "sector": sector,
        "timeframe": tf_key,
        "timeframe_label": data_sources.timeframe(tf_key)["label"],
        "breadth": {
            "advances": advances,
            "declines": declines,
            "unchanged": len(ranked) - advances - declines,
            # Classic A/D ratio. None when nothing declined -- not infinity.
            "ad_ratio": round(advances / declines, 2) if declines else None,
            "moving": sum(1 for s in ranked if s.get("moving")),
        },
        "gainers": ranked and sorted(
            [s for s in ranked if s["change_pct"] > 0],
            key=lambda s: -s["change_pct"])[:limit] or [],
        "losers": ranked and sorted(
            [s for s in ranked if s["change_pct"] < 0],
            key=lambda s: s["change_pct"])[:limit] or [],
        # Exactly-flat names belong to neither list, but the heatmap claims to
        # show the whole universe, so they get their own bucket.
        "flat": [s for s in ranked if s["change_pct"] == 0][:limit],
    })


@app.get("/api/sectors")
def api_sectors():
    """Live sector index performance for the SectorFlow chart."""
    try:
        rows, nifty = nse.sectors()
    except Exception as exc:
        return jsonify({"error": scrub(f"{exc.__class__.__name__}: {exc}")}), 502
    return jsonify({"sectors": rows, "nifty": nifty})


@app.get("/api/contributors")
def api_contributors():
    """Who pushed the index up and down. Restricted to the 50-name indices --
    this needs one slow float-share lookup per constituent on a cold cache."""
    index = request.args.get("index", "nifty50")
    if index not in ("nifty50", "niftynext50"):
        return jsonify({"error": "contributors is available for Nifty 50 and "
                                 "Nifty Next 50 only"}), 400
    try:
        members = nse.universe(index)
        quote = nse.index_quote("NIFTY 50" if index == "nifty50" else "NIFTY NEXT 50")
        result = data_sources.index_contributions(members, quote, log)
    except Exception as exc:
        return jsonify({"error": scrub(f"{exc.__class__.__name__}: {exc}")}), 502
    result["index"] = nse.INDEX_LABELS.get(index, index)
    result["quote"] = quote
    return jsonify(result)


@app.get("/api/chart")
def api_chart():
    ticker = request.args.get("ticker", "RELIANCE.NS").upper()
    if "." not in ticker:
        ticker += ".NS"
    tf_key = request.args.get("timeframe", data_sources.DEFAULT_TIMEFRAME)
    try:
        bars = data_sources.fetch_bars(ticker, tf_key)
    except Exception as exc:
        return jsonify({"error": scrub(f"{exc.__class__.__name__}: {exc}")}), 502
    if bars is None or bars.empty:
        return jsonify({"error": f"no data for {ticker}"}), 404
    bars = bars.tail(180)
    return jsonify({
        "ticker": ticker,
        "timeframe": tf_key,
        "timeframe_label": data_sources.timeframe(tf_key)["label"],
        "bars": [{"t": str(idx)[:16], "o": round(float(row.Open), 2),
                  "h": round(float(row.High), 2), "l": round(float(row.Low), 2),
                  "c": round(float(row.Close), 2), "v": int(row.Volume or 0)}
                 for idx, row in bars.iterrows()],
    })


@app.get("/api/optionchain")
def api_optionchain():
    symbol = request.args.get("symbol", "NIFTY")
    try:
        chain = nse.option_chain(symbol, request.args.get("expiry"))
    except Exception as exc:
        return jsonify({"error": scrub(f"{exc.__class__.__name__}: {exc}")}), 502
    # Implied vs realised: is the chain pricing more movement than the index
    # has actually delivered? Unavailable underlyings stay null, never guessed.
    underlying = nse.UNDERLYING_TICKERS.get(symbol.upper())
    chain["hv30"] = data_sources.historical_volatility(underlying) if underlying else None
    if chain.get("atm_iv") and chain["hv30"]:
        chain["iv_hv_ratio"] = round(chain["atm_iv"] / chain["hv30"], 2)
    return jsonify(chain)


@app.get("/api/rrg")
def api_rrg():
    """Relative rotation of the sector indices against Nifty 50."""
    try:
        raw = data_sources.rrg(list(nse.SECTOR_YF.values()), nse.BENCHMARK_YF)
    except Exception as exc:
        return jsonify({"error": scrub(f"{exc.__class__.__name__}: {exc}")}), 502
    label = {v: k for k, v in nse.SECTOR_YF.items()}
    return jsonify({
        "benchmark": "NIFTY 50",
        "sectors": [{"symbol": label[t],
                     "label": label[t].replace("NIFTY ", ""),
                     **v} for t, v in raw.items()],
    })


@app.get("/api/flows")
def api_flows():
    """FII/DII cash-market flows and whether the market is currently open."""
    return jsonify({"flows": nse.fii_dii(), "market": nse.market_status()})


@app.post("/api/start")
def api_start():
    if STATE.get("running"):
        return jsonify({"ok": False, "error": "a run is already in progress"}), 409
    body = request.get_json(silent=True) or {}
    mode = body.get("mode", "demo")
    if mode not in ("demo", "live"):
        return jsonify({"ok": False, "error": "mode must be demo or live"}), 400
    filters = {
        "index": body.get("index", "nifty50"),
        "cap": body.get("cap", "all"),
        "segment": body.get("segment", "equity"),
        "timeframe": body.get("timeframe", data_sources.DEFAULT_TIMEFRAME),
        "intent": body.get("intent", "any"),
        "sector": body.get("sector") or None,
        "count": body.get("count") or STOCK_COUNT,
    }
    STOP.clear()
    llm.reset_cancel()
    reset_state(mode, filters)
    STATE["running"] = True  # set before the thread starts so a double click 409s
    threading.Thread(target=run_pipeline, args=(mode, filters), daemon=True).start()
    return jsonify({"ok": True, "mode": mode, "filters": filters})


@app.post("/api/stop")
def api_stop():
    """Ask the running panel to stop. Kills the in-flight LLM call so this
    takes effect now rather than at the end of the current stock."""
    if not STATE.get("running"):
        return jsonify({"ok": False, "error": "no run in progress"}), 409
    STOP.set()
    llm.cancel_running_call()
    log("Stop requested — finishing up.")
    return jsonify({"ok": True})


@app.get("/api/state")
def api_state():
    with _lock:
        return jsonify(json.loads(json.dumps(STATE, default=str)))


@app.get("/api/history")
def api_history():
    limit = min(int(request.args.get("limit", 50)), 500)
    with db() as conn:
        rows = conn.execute(
            "SELECT created_at, ticker, name, cap_bucket, price, verdict,"
            " confidence, engine, signalled FROM verdicts"
            " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        runs = conn.execute(
            "SELECT id, started_at, finished_at, mode, engine, n_evaluated,"
            " n_signals FROM runs ORDER BY id DESC LIMIT 20").fetchall()
    return jsonify({"verdicts": [dict(r) for r in rows],
                    "runs": [dict(r) for r in runs]})


if __name__ == "__main__":
    init_db()
    reset_state()
    print(f"\n  {BRAND} — http://127.0.0.1:{PORT}")
    print(f"  engine: {llm.detect_provider()} · telegram: "
          f"{'configured' if telegram_configured() else 'not configured'}")
    print("  analysis only, no orders are ever placed\n")
    app.run(host="127.0.0.1", port=PORT, threaded=True)
