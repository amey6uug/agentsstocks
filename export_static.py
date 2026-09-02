"""Generate a static snapshot of the latest analysis for GitHub Pages.

The interactive app needs a Python server: it calls NSE server-side, shells
out to an LLM and writes SQLite. GitHub Pages serves static files only, so
none of that can run there. What CAN run is this script, on a schedule, in
GitHub Actions -- it does the work on the runner and writes one self-contained
HTML file with the results baked in.

The result is a read-only snapshot: real data, no buttons. Anything that
cannot be fetched is omitted with a reason rather than faked.
"""

import datetime as dt
import html
import json
import os
import sys
import traceback

import data_sources as ds
import llm
import nse
import scoring

# Written to the repo root, not docs/. GitHub Pages "deploy from a branch"
# renders README.md as the index only when no index.html exists there, so a
# root index.html is what makes the snapshot the page you actually land on --
# no repo setting needs changing.
OUT_DIR = os.getenv("EXPORT_OUT") or os.path.dirname(os.path.abspath(__file__))
INDEX = os.getenv("EXPORT_INDEX", "nifty50")
COUNT = int(os.getenv("EXPORT_COUNT", "6"))
TIMEFRAME = os.getenv("EXPORT_TIMEFRAME", "1d")
BRAND = os.getenv("BRAND", "AgentDesk")


def safe(label, fn, default=None):
    """Run one section. A failure is reported on the page, never hidden."""
    try:
        return fn(), None
    except Exception as exc:
        print(f"  {label}: FAILED {exc.__class__.__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        return default, f"{exc.__class__.__name__}: {exc}"


def esc(x):
    return html.escape(str(x if x is not None else "—"))


def pct(v):
    return f"{v:+.2f}%" if isinstance(v, (int, float)) else "n/a"


def cls(v):
    return "up" if isinstance(v, (int, float)) and v >= 0 else ("down" if isinstance(v, (int, float)) else "")


def build():
    print(f"Exporting {INDEX} · {COUNT} stocks · {TIMEFRAME}")
    market, _ = safe("market status", nse.market_status, None)
    provider = llm.detect_provider()

    def screen_and_debate():
        stocks = nse.universe(INDEX)
        bundles = ds.get_live_evidence_bundles(stocks, COUNT, TIMEFRAME, print, "any")
        rows = []
        for b in bundles:
            r = llm.run_panel(b, provider, print)
            rows.append({"bundle": b, "result": r,
                         "levels": scoring.trade_levels(b) if r["verdict"] == "BUY" else None})
        return rows

    verdicts, verdict_err = safe("verdicts", screen_and_debate, [])
    sectors, sector_err = safe("sectors", lambda: nse.sectors()[0], [])
    flows, _ = safe("flows", nse.fii_dii, [])

    os.makedirs(OUT_DIR, exist_ok=True)
    # Stop Jekyll touching the output; it is already plain HTML.
    open(os.path.join(OUT_DIR, ".nojekyll"), "w").close()
    page = render(verdicts, verdict_err, sectors, sector_err, flows, market, provider)
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(page)
    with open(os.path.join(OUT_DIR, "data.json"), "w", encoding="utf-8") as fh:
        json.dump({"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "index": INDEX, "timeframe": TIMEFRAME, "market": market,
                   "engine": provider,
                   "verdicts": [{"ticker": v["bundle"]["ticker"],
                                 "name": v["bundle"]["name"],
                                 "price": v["bundle"]["price"],
                                 "verdict": v["result"]["verdict"],
                                 "confidence": v["result"]["confidence"],
                                 "levels": v["levels"]} for v in verdicts],
                   "sectors": sectors, "flows": flows}, fh, indent=1, default=str)
    print(f"wrote {os.path.join(OUT_DIR, 'index.html')}  ({len(page):,} bytes, "
          f"{len(verdicts)} verdicts, {len(sectors)} sectors)")
    return len(verdicts)


CSS = """
:root{color-scheme:light dark;
  --bg:#FBF8F2;--panel:#FFF;--panel2:#F4EFE6;--line:#E4DDD0;--line-soft:#EFE9DE;
  --text:#1B1712;--muted:#6E6559;--accent:#8A5E12;
  --buy:#0F6B3F;--up-line:#B6D9C4;--up-fill:#E4F2EA;
  --avoid:#B3392C;--down-line:#EEC2BC;--down-fill:#FAE9E7;
  --hold:#8A5E12;--flat-line:#E3CFA6;--flat-fill:#F7EFDD;
  --fd:ui-monospace,"SF Mono","Cascadia Mono",Menlo,Consolas,monospace;
  --fu:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  --fs:ui-serif,"Iowan Old Style",Charter,Georgia,serif}
@media (prefers-color-scheme:dark){:root{
  --bg:#141210;--panel:#1D1A16;--panel2:#171410;--line:#302B24;--line-soft:#262119;
  --text:#F2EDE3;--muted:#9A9083;--accent:#E0AA4E;
  --buy:#43C77E;--up-line:#1F5238;--up-fill:#15251C;
  --avoid:#F2796C;--down-line:#5A2A24;--down-fill:#251614;
  --hold:#E3B35C;--flat-line:#4E3D1C;--flat-fill:#261E10}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 var(--fu);
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:26px 20px 60px}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:baseline;
  border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:8px}
h1{margin:0;font:600 26px/1.15 var(--fs);letter-spacing:-.01em}
h1 span{color:var(--muted);font:400 13px/1.4 var(--fu);margin-left:8px}
h2{font:600 12px/1.3 var(--fs);text-transform:uppercase;letter-spacing:.14em;
  color:var(--muted);margin:30px 0 12px}
.badges{margin-left:auto;display:flex;gap:8px;flex-wrap:wrap}
.badge{font-size:12px;padding:4px 10px;border-radius:999px;border:1px solid var(--line);
  background:var(--panel);color:var(--muted);white-space:nowrap}
.badge.on{border-color:var(--up-line);color:var(--buy)}
.badge.off{border-color:var(--down-line);color:var(--avoid)}
.note{color:var(--muted);font-size:12.5px;margin:10px 0 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  margin-bottom:10px;overflow:hidden}
.head{display:flex;gap:12px;align-items:center;padding:14px 16px;flex-wrap:wrap}
.tk{font-weight:600}.tk small{display:block;color:var(--muted);font-weight:400;font-size:12px}
.chg{font:13px/1.4 var(--fd);font-variant-numeric:tabular-nums}
.up{color:var(--buy)}.down{color:var(--avoid)}
.spacer{margin-left:auto}
.cap{font-size:11px;color:var(--muted);border:1px solid var(--line);padding:2px 8px;border-radius:999px}
.v{font-weight:700;font-size:13px;padding:4px 12px;border-radius:999px}
.v.BUY{background:var(--up-fill);color:var(--buy)}
.v.HOLD{background:var(--flat-fill);color:var(--hold)}
.v.SELL{background:var(--down-fill);color:var(--avoid)}
.conf{font:12px/1.4 var(--fd);color:var(--muted)}
.levels{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;
  padding:12px 16px;border-top:1px solid var(--line);background:var(--panel2)}
.lvl{display:flex;flex-direction:column;gap:1px;min-width:0}
.lvl .lk{font-size:10px;text-transform:uppercase;letter-spacing:.09em;color:var(--muted)}
.lvl b{font:600 16px/1.2 var(--fd);font-variant-numeric:tabular-nums}
.lvl em{font-style:normal;font-size:10.5px;color:var(--muted);line-height:1.35}
.seats{padding:0 16px 14px;border-top:1px solid var(--line)}
.seat{margin-top:12px}
.seat b{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.08em;
  color:var(--muted);margin-bottom:2px}
.seat p{margin:0;font-size:14px}
.meta{padding:0 16px 14px;font-size:12px;color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:13.5px}
.tbl{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:auto}
th{text-align:left;font-size:10.5px;text-transform:uppercase;letter-spacing:.07em;
  color:var(--muted);padding:8px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:8px 12px;border-bottom:1px solid var(--line-soft);font:13px/1.45 var(--fd);
  font-variant-numeric:tabular-nums;white-space:nowrap}
td.nm{font-family:var(--fu)}
.secbar{display:flex;align-items:center;gap:10px;padding:3px 0}
.secbar .l{width:130px;flex:none;font-size:12px;font-weight:600;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.secbar .t{flex:1;height:13px;background:var(--panel);border:1px solid var(--line);
  border-radius:3px;display:flex}
.secbar .t i{display:block;height:100%;border-radius:2px}
.secbar.pos .t{justify-content:flex-start}.secbar.pos .t i{background:var(--buy)}
.secbar.neg .t{justify-content:flex-end}.secbar.neg .t i{background:var(--avoid)}
.secbar .v2{width:62px;flex:none;text-align:right;font:12px/1.4 var(--fd);color:var(--muted)}
.err{background:var(--down-fill);border:1px solid var(--down-line);color:var(--avoid);
  border-radius:10px;padding:10px 14px;font-size:13px;margin-bottom:10px}
footer{margin-top:34px;padding-top:16px;border-top:1px solid var(--line);
  color:var(--muted);font-size:12.5px}
footer a{color:var(--accent)}
"""


def _level_cells(lv):
    def cell(label, val, sub):
        if val is None:
            return ""
        return ('<div class="lvl"><span class="lk">' + esc(label) + '</span><b>'
                + esc(val) + '</b><em>' + esc(sub) + '</em></div>')

    up = ("+" + str(lv["upside_pct"]) + "% · ") if lv.get("upside_pct") else ""
    down = (str(lv["downside_pct"]) + "% · ") if lv.get("downside_pct") else ""
    out = ('<div class="levels">'
           + cell("Entry", lv.get("entry"), lv.get("entry_basis"))
           + cell("Target", lv.get("target"), up + (lv.get("target_basis") or ""))
           + cell("Stop", lv.get("stop"), down + (lv.get("stop_basis") or "")))
    if lv.get("risk_reward"):
        out += ('<div class="lvl"><span class="lk">Risk / reward</span><b>'
                + str(lv["risk_reward"]) + ' : 1</b><em>reward per unit risked</em></div>')
    return out + "</div>"


def _card(v):
    b, r, lv = v["bundle"], v["result"], v["levels"]
    chg = b.get("bar_change_pct")
    levels = _level_cells(lv) if (lv and lv.get("entry")) else ""
    seats = "".join(
        '<div class="seat"><b>' + esc(k) + '</b><p>' + esc(t) + '</p></div>'
        for k, t in [("Judge", r["judge"]), ("Bull", r["panel"]["bull"]),
                     ("Bear", r["panel"]["bear"]), ("Technician", r["panel"]["technician"])])
    return (
        '<div class="card"><div class="head">'
        '<div class="tk">' + esc(b["ticker"]) + '<small>' + esc(b["name"]) + '</small></div>'
        '<div class="chg ' + cls(chg) + '">' + esc(b["price"]) + ' &nbsp;' + pct(chg) + '</div>'
        '<div class="spacer"></div>'
        '<span class="cap">' + esc(b["cap_bucket"]) + '-cap</span>'
        '<span class="cap">' + esc(b.get("timeframe_label")) + '</span>'
        '<span class="conf">' + esc(r["confidence"]) + '/10</span>'
        '<span class="v ' + esc(r["verdict"]) + '">' + esc(r["verdict"]) + '</span>'
        '</div>' + levels +
        '<div class="seats">' + seats + '</div>'
        '<div class="meta">engine: ' + esc(r["engine"]) + ' · data unavailable: '
        + esc(", ".join(b["data_gaps"]) or "none") + '</div></div>')


def render(verdicts, verdict_err, sectors, sector_err, flows, market, provider):
    now = dt.datetime.now(dt.timezone.utc)
    ist = now + dt.timedelta(hours=5, minutes=30)
    mopen = bool(market and market.get("open"))
    mtxt = ("market " + str(market.get("status", "?")).lower() + " · "
            + str(market.get("as_of", ""))) if market else "market status unavailable"

    cards = "".join(_card(v) for v in verdicts)

    scale = max([abs(s["percent_change"]) for s in sectors] or [1]) or 1
    sec_html = "".join(
        '<div class="secbar ' + ("pos" if s["percent_change"] >= 0 else "neg") + '">'
        '<span class="l">' + esc(s["label"]) + '</span>'
        '<span class="t"><i style="width:%.1f%%"></i></span>' % (abs(s["percent_change"]) / scale * 100)
        + '<span class="v2">' + pct(s["percent_change"]) + '</span></div>'
        for s in sectors)

    flow_rows = "".join(
        '<tr><td class="nm">' + esc(f["category"]) + '</td><td>' + esc(f["date"]) + '</td>'
        '<td class="' + cls(f["net"]) + '">' + format(f["net"], "+,.2f") + '</td>'
        '<td>' + format(f["buy"], ",.2f") + '</td>'
        '<td>' + format(f["sell"], ",.2f") + '</td></tr>' for f in flows)

    verdict_block = ('<div class="err">Verdicts unavailable: ' + esc(verdict_err) + '</div>'
                     ) if verdict_err else ""
    sector_block = ('<div class="err">Sector data unavailable: ' + esc(sector_err)
                    + '<br>NSE blocks many datacenter IP ranges, which is the usual cause '
                      'when this works locally but not here.</div>') if sector_err else ""
    flow_block = ('<div class="tbl"><table><thead><tr><th>Category</th><th>Date</th>'
                  '<th>Net</th><th>Buy</th><th>Sell</th></tr></thead><tbody>'
                  + flow_rows + '</tbody></table></div>') if flow_rows else \
                 '<p class="note">No flow data published for the latest session.</p>'

    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        '<title>' + esc(BRAND) + ' — ' + esc(INDEX) + ' snapshot</title>\n'
        '<style>' + CSS + '</style></head><body><div class="wrap">\n'
        '<header><h1>' + esc(BRAND) + '<span>' + esc(INDEX) + ' snapshot · '
        + esc(TIMEFRAME) + '</span></h1><div class="badges">'
        '<div class="badge">engine ' + esc(provider) + '</div>'
        '<div class="badge ' + ("on" if mopen else "off") + '">' + esc(mtxt) + '</div>'
        '</div></header>\n'
        '<p class="note">Generated ' + ist.strftime("%d %b %Y, %H:%M") + ' IST ('
        + now.strftime("%H:%M") + ' UTC) by a scheduled GitHub Action. This is a '
        '<b>read-only snapshot</b> — the interactive dashboard needs a Python server '
        'and cannot run on GitHub Pages.</p>\n'
        '<h2>Verdicts</h2>\n' + verdict_block
        + (cards or '<p class="note">No verdicts in this run.</p>') + '\n'
        '<h2>Sector flow</h2>\n' + sector_block
        + (sec_html or '<p class="note">No sector data.</p>') + '\n'
        '<h2>Institutional flows (INR crore)</h2>\n' + flow_block + '\n'
        '<footer><b>' + esc(BRAND) + '</b> — analysis output only. It never places a '
        'trade, and nothing here is investment advice. Entry, target and stop are reference '
        'levels derived from moving averages, the analyst consensus target and the price '
        'range; they are not a forecast. Quotes from Yahoo Finance, index and F&amp;O data '
        'from NSE.<br>Source: <a href="https://github.com/amey6uug/agentsstocks">'
        'github.com/amey6uug/agentsstocks</a></footer>\n'
        '</div></body></html>')


if __name__ == "__main__":
    sys.exit(0 if build() >= 0 else 1)
