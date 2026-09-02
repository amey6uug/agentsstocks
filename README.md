# AgentDesk — multi-agent Indian stock-analysis dashboard

A local, one-click dashboard where a panel of named agents scans Indian
stocks, debates each pick with an LLM (or a deterministic rules engine if no
LLM is available), and automatically sends BUY signals to your Telegram.
Runs entirely on your own machine — no cloud backend, no accounts beyond the
optional LLM/Telegram integrations.

**This is an analysis tool. It never places trades. Nothing here is
investment advice.**

## What's in the box

| File | Purpose |
|---|---|
| `app.py` | Flask server, background-thread state machine, Telegram sender, SQLite audit log |
| `scoring.py` | Deterministic Bull/Bear/Judge rules engine (always works, no key/network needed) |
| `llm.py` | LLM debate engine — provider auto-detection, prompt, grounding verifier, fallback |
| `data_sources.py` | Demo bundle loader + yfinance live adapter + evidence-bundle builder |
| `nse.py` | NSE index constituents, cap buckets, F&O list, option chain |
| `dashboard.html` | The whole UI — single self-contained file, no build step |
| `universe.json` | Editable ticker list, used by the "Custom" index option |
| `cache/` | Created on first run — day-old copies of the NSE constituent lists |
| `demo_data/*.json` | Nine hand-built evidence bundles (3 large/mid/small cap) for offline demo mode |
| `stock_agents.db` | Created on first run — SQLite audit trail of runs + verdicts |

## The agent panel

Scout (screens the universe for movers) → Technician, Fundamentalist,
Newsdesk (read price action, valuation, and news) → Bull and Bear (argue the
case for/against) → Judge (weighs the debate, issues verdict + confidence) →
Messenger (sends BUY signals to Telegram). All eight animate
offline → working → done as a run progresses.

## 1. Install

```bash
pip install -r requirements.txt
```

Requires Python 3.9+.

## 2. (Optional) Set up the LLM debate engine

The app works with **zero configuration** — with no LLM available it falls
back to a deterministic, rule-based panel that always runs.

To get real LLM-driven debate, pick one:

**Option A — Claude Code CLI (uses your Claude subscription, no API key, no
per-call billing):**

```bash
npm install -g @anthropic-ai/claude-code   # if you don't already have it
claude
# inside the CLI: /login, then sign in with your Claude Pro/Max plan
```

The app auto-detects the `claude` CLI and uses it first. It looks on your
`PATH`, and then inside the VS Code extension
(`~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/`),
which ships its own binary without adding it to `PATH` — so if you use Claude
Code in VS Code, this already works with nothing installed. If your CLI lives
somewhere else, point `CLAUDE_CLI_PATH` at it in `.env`.

Expect roughly 20–25 seconds per stock on this engine (about 3–4 minutes for
the nine demo bundles). The dashboard streams verdicts as each one lands.

**Option B — API key.** Copy `.env.example` to `.env` and set either:

```
ANTHROPIC_API_KEY=sk-ant-...
```
or
```
OPENAI_API_KEY=sk-...
```

You can force a specific provider (skip auto-detection) with:

```
LLM_PROVIDER=claude_code   # or: anthropic | openai | deterministic
```

## 3. (Optional) Set up Telegram

1. Message [@BotFather](https://t.me/BotFather) on Telegram, run `/newbot`,
   and copy the bot token it gives you.
2. Message [@userinfobot](https://t.me/userinfobot) to get your numeric chat
   ID.
3. Copy `.env.example` to `.env` (if you haven't already) and fill in:

```
TELEGRAM_BOT_TOKEN=123456789:AA...
TELEGRAM_CHAT_ID=123456789
```

If these are left blank, the app runs fine — it just skips the Telegram
step and logs that it was skipped. The bot token is never printed to logs
or the UI.

## 4. Run it

```bash
python app.py
```

Open **http://127.0.0.1:5000**, pick a mode, and click **Start agents**.

- **Demo** — runs entirely offline against the nine bundled evidence files
  in `demo_data/`. Good for a first run, or when markets are closed.
- **Live** — pulls real NSE data via `yfinance` for the tickers listed in
  `universe.json` (edit that file to change your universe). Use this during
  NSE market hours: **Mon–Fri, 09:15–15:30 IST**. Outside those hours you'll
  still get data, just stale/after-hours prices.

Everything the app does — every run and every verdict — is logged to
`stock_agents.db` (SQLite) for your own auditing.

## Pages

The **Page** dropdown in the toolbar switches between four views:

| Page | What it does |
|---|---|
| **Agent panel** | The eight-agent run: screen → debate → verdict → Telegram, with a **Stop** button. The BUY/SELL/HOLD chips filter what's displayed — HOLD is off by default so you only see actionable calls |
| **Top stocks** | Ranks the filtered universe by move, gainers and losers side by side. No LLM — it's a plain screen, and it's fast. Click any symbol to open its chart |
| **Chart** | Candlestick + volume for one ticker on the selected timeframe |
| **Index movers** | Which constituents pushed the index up and down, as diverging bars — gainers one side, losers the other, sorted by size. Nifty 50 / Nifty Next 50 |
| **Sector flow** | All 22 NSE sector indices ranked best to worst as vertical bars, with Nifty as the reference. Click a sector to screen its constituents |
| **Sector rotation** | Relative rotation graph — each sector's strength and momentum against Nifty 50 over 8 weeks, plotted into Leading / Weakening / Lagging / Improving quadrants with its tail |
| **Heatmap** | Every stock in the filtered universe as a tile, shaded by size of move, MOVING tagged. Click for its chart |
| **History** | The full audit trail from `stock_agents.db` — every run and every verdict, with the engine that produced it |
| **F&O option chain** | Live NSE chain for NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY or NIFTYNXT50 — strike-by-strike OI, OI change, volume, IV and LTP with OI bars, plus max pain, expected move, implied range, PCR, call/put walls and net OI change |

## Sector rotation (RRG)

The formula is written out rather than hidden, so the chart is reproducible:

```
RS          = sector close / Nifty 50 close      (weekly bars)
RS-Ratio    = 100 * RS / EMA(RS, 10)
RS-Momentum = 100 * RS-Ratio / EMA(RS-Ratio, 10)
```

Both oscillate around 100. Ratio above 100 means the sector is outperforming
the benchmark; momentum above 100 means that outperformance is still building.
The quadrants follow directly: Leading (both above), Weakening (strength
fading), Lagging (both below), Improving (recovering). Each sector's tail is
its last 8 weekly readings.

This is a simplified construction in the spirit of the published RRG method,
not the trademarked JdK implementation — the absolute numbers will not match a
vendor's chart, and the rotation pattern is the point. Ten sector indices have
the price history this needs; the rest are omitted rather than approximated.

## Market status and institutional flows

The header carries a **market open / closed** badge from NSE, with the last
trade timestamp when closed — so it is always obvious whether a number is live
or the previous session's close.

The Sector flow page also shows **FII/DII cash-market flows**: buy, sell and
net value in INR crore for foreign and domestic institutions, for the latest
session NSE has published.

## Sector flow

Live percentage change for every NSE sector index, sorted best to worst, read
straight from NSE's own index feed — these are the published index values, not
averages the app computed.

Clicking a bar screens that sector's constituents on the Top stocks page, with
its own breadth line. Thirteen of the twenty-two sectors can be drilled into:
NSE publishes a constituent list for those under a resolvable file name, and
each one was verified by fetching it. Sectors without a published list still
appear in the chart, shown dimmed and not clickable, rather than being hidden
or given made-up membership.

## F&O analytics

Everything on the option chain page is arithmetic on the chain NSE already
returns — no pricing model, no estimated inputs.

| Metric | What it is |
|---|---|
| **Max pain** | The settlement strike at which option writers pay out least in total, summed across every in-the-money call and put |
| **Expected move** | The at-the-money straddle premium (ATM call + ATM put) — what the market is charging for the move to expiry — as points and as a % of spot |
| **Implied range** | Spot ± the straddle premium |
| **PCR (OI)** | Put OI ÷ call OI |
| **Call wall / put wall** | Heaviest call OI (resistance) and heaviest put OI (support) |
| **Net OI change** | Total call vs put OI added or removed since the previous session |
| **OI bars** | Each OI figure is backed by a bar scaled to the chain's peak, so the walls are visible without reading digits |
| **Volatility skew** | Call and put implied volatility plotted across every strike, with spot marked — the IV the chain already reports, drawn, not modelled |
| **IV / HV₃₀** | ATM implied volatility against the underlying's 30-day realised volatility (annualised stdev of daily log returns). A ratio above 1 means options are pricing more movement than the index has actually delivered |

PCR and max pain are computed on the **full chain**, then the table is trimmed
to ±20 strikes around spot for display — a PCR measured only over the strikes
that happen to be on screen is not PCR.

The screener also reports **market breadth** for whatever universe is
filtered: advances, declines, the A/D ratio, and how many names are moving on
volume.

## Filters

| Filter | Options | Applies to |
|---|---|---|
| **Index** | Nifty 50, Nifty Next 50, Nifty 200, Nifty 500, Custom (`universe.json`) | Agent panel, Top stocks |
| **Cap** | All, Large, Mid, Small | Agent panel, Top stocks |
| **Segment** | Equity (everything), F&O (only underlyings with listed derivatives) | Agent panel, Top stocks |
| **Timeframe** | 15m, 30m, 1h, 4h, 1d, 1wk, 1mo | Agent panel, Top stocks, Chart |
| **Looking for** | Biggest movers, Buy candidates, Sell candidates | Agent panel, Top stocks |

**"Looking for" changes what Scout hunts, not what the Judge concludes.** Ask
for buy candidates and the shortlist is built from the strongest advances, so
the panel is handed stocks that could plausibly earn a BUY. The Judge is still
free to reject every one of them — biasing the *input* is legitimate
screening, while forcing the *output* would just be manufacturing signals.

## Index points are estimated, and the page says so

The **Index movers** page offers two measures.

**% change (exact).** Straight from the price data. Ignores index weight, so a
small constituent moving 4% outranks a heavyweight moving 0.5%.

**Index points (estimated).** How many index points each stock contributed.
This needs every constituent's free-float weight, and NSE's official
Investible Weight Factor — banded and capped — is not published in any free
feed. The app substitutes yfinance free-float share counts, which are close
but not identical.

Because the decomposition can be checked against the index's real move, the
page always shows the reconciliation: the modelled total, NSE's actual point
change, and the gap between them. On a recent Nifty 50 session that was
−23.31 modelled against −29.85 actual, about 6.5 points out. **Treat the
ranking as reliable and the exact figures as indicative** — if you need
numbers that tie out to NSE's own, use the % measure. Nothing in the app
presents the estimate as an official figure.

Free-float share counts are cached in `cache/float_shares.json` for a week,
since they only change on a corporate action. The first load of the page takes
about a minute while they are fetched; after that it is a couple of seconds.

## Momentum and the MOVING tag

Every screened stock carries a **momentum** score (0–100) built from three
things that already happened: the size of the move, whether volume confirmed
it (last bar versus its 20-day average), and whether it broke the edge of its
recent range. Rows tagged **MOVING** are moves of at least 1.5% on at least
1.5× average volume — a move real money is behind, rather than a drift on thin
trade. Those rows are highlighted in the screener and on the verdict cards.

This is a description of what has happened, not a forecast. Nothing in this
app predicts the next bar, the next day, or the next minute, and a high
momentum score is not a claim that a move will continue.

Index membership is pulled from NSE's own published constituent lists and
cached in `cache/` for a day, so the filters keep working offline and a
rebalance is picked up automatically. Cap buckets are not guessed — they come
from index membership the way SEBI defines them: Nifty 100 = large, Midcap 150
= mid, Smallcap 250 = small. That partitions the Nifty 500 exactly
100/150/250.

**The timeframe is real, not cosmetic.** Every moving average and the RSI are
measured in *bars* of the selected timeframe, so the 20-period average on the
15m chart is 20 fifteen-minute bars. Anything that genuinely cannot be
computed on a timeframe becomes a declared data gap rather than a fudge:
`week52_high`/`week52_low` are only populated on the daily chart (a 60-day
intraday window is not a 52-week range), `sma200` drops out on the monthly
chart (only ~120 monthly bars exist), and `day_change_pct` drops out on
weekly/monthly bars. 4h is resampled from 1h, which yfinance does not serve
natively.

## How a run works

1. **Scout** screens the universe. Demo mode loads the pre-built bundles and
   ignores the filters. Live mode resolves your index/cap/segment filters into
   a stock list, ranks the whole list in one batched download, and keeps the
   top `SHORTLIST_PER_BUCKET` movers per cap bucket. The batching matters: a
   Nifty 500 screen takes about 40 seconds, where fetching 500 tickers one at
   a time takes roughly ten minutes. Full evidence bundles are built only for
   the shortlist.
2. For each shortlisted stock, one combined evaluation is run: a six-seat
   panel (Bull, Bear, Fundamentalist, Technician, Newsdesk) plus a Judge, via
   whichever LLM provider was detected — or the deterministic rules engine if
   none is available or the LLM call fails for any reason.
3. Every number an agent cites is checked against the evidence bundle it was
   given (`llm.py`'s grounding verifier). If a citation can't be traced back
   to real data, that stock silently falls back to the deterministic engine
   rather than showing you an invented number.
   The Judge returns **BUY**, **SELL** or **HOLD**. HOLD is a real answer, not
   a cop-out — it's what an honest panel says when the evidence does not
   support a direction, and it is deliberately still available to the Judge.
   The dashboard just hides it by default. SELL means the panel is negative on
   the stock; it is not a recommendation to short anything, and no order is
   ever placed either way.
4. Stocks that get a **BUY** verdict with confidence ≥ `CONFIDENCE_THRESHOLD`
   (default 7) fire a Telegram message. After the run, one daily summary
   message is sent listing every fired signal (or saying none fired).

## Entry, target and stop

Every **BUY** verdict carries reference levels, shown on the card and included
in the Telegram signal. Each one is derived from a figure already in the
evidence bundle and states which field it came from:

| Level | Where it comes from |
|---|---|
| **Entry** | The 20-bar average when price sits above it — a pullback entry. Otherwise the last price, since there is nothing to wait for |
| **Target** | The analyst consensus target when it clears the entry, otherwise the top of the range the bundle covers |
| **Stop** | The 50-bar average when it sits below the entry, otherwise the range low |
| **Risk / reward** | (target − entry) ÷ (entry − stop) |

Anything the bundle cannot support comes back empty rather than invented — no
analyst target and no range high means no target, not a guessed one.

**These are traceable reference levels, not advice and not a price forecast.**
The app places no orders. Two limits worth knowing: the risk/reward ratio
flatters setups where the 20- and 50-bar averages happen to sit close
together, because a tight stop shrinks the denominator; and a pullback entry
below the current price may simply never fill.

## Stopping a run

**Stop** halts the panel immediately — it kills the in-flight LLM call rather
than waiting for the current stock to finish, so it lands in well under a
second instead of the ~20 seconds a debate takes.

A stopped run unwinds cleanly rather than being abandoned:

- verdicts already completed are kept, on screen and in `stock_agents.db`
- the stock being evaluated when you pressed Stop is **discarded**, not saved
  with a half-formed verdict
- no Telegram messages are sent, including for signals that had already fired
  earlier in the run — the log says how many were held back
- the run's audit row records what actually completed, not what was planned
- the cancelled call is logged as "cancelled", never as an engine failure and
  fallback, because that is not what happened

## Configuration reference (`.env`)

| Key | Default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | — | Your chat id from @userinfobot |
| `LLM_PROVIDER` | auto-detect | Force `claude_code` \| `anthropic` \| `openai` \| `deterministic` |
| `CLAUDE_CLI_PATH` | auto-detect | Explicit path to the `claude` binary, if it is neither on `PATH` nor in the VS Code extension |
| `ANTHROPIC_API_KEY` | — | Only needed for the `anthropic` provider |
| `OPENAI_API_KEY` | — | Only needed for the `openai` provider |
| `BRAND` | `AgentDesk` | Name shown in the header/footer and Telegram summary |
| `CONFIDENCE_THRESHOLD` | `7` | Minimum Judge confidence (1–10) required to fire a BUY signal |
| `AGENT_DELAY` | `0.6` | Seconds of pacing between pipeline steps, purely visual |
| `STOCK_COUNT` | `8` | Default number of stocks a run debates, dealt across cap buckets. The **Stocks** dropdown overrides it per run |
| `SHORTLIST_PER_BUCKET` | `4` | Superseded by `STOCK_COUNT`; still read to derive its default |
| `PORT` | `5000` | Local port the Flask server listens on |

## Swapping the data source

`data_sources.py` is intentionally the only file that knows about
`yfinance`. If you have a broker API, a paid market-data feed, or an MCP
connector you'd rather use, replace `build_evidence_bundle_from_yf` and
`get_live_evidence_bundles` with your own implementation — as long as the
function returns an evidence bundle in the same shape (documented at the top
of `data_sources.py`), `scoring.py` and `llm.py` don't need to change at all.

## Notes on data completeness

`yfinance`/NSE data does not include raw fundamental ratios like P/E or ROE
— the evidence bundle only carries price/technical/analyst-target/news
data. Any field that can't be computed for a given stock is set to `null`
and its name is added to that bundle's `data_gaps` list; agents are
instructed to say "data unavailable" rather than guess.

## Safety notes

- No orders are ever placed. This is analysis output only.
- The Telegram bot token is read from `.env`, never hardcoded, and is
  scrubbed from every log line and error message before it's written or
  displayed.
- The deterministic engine has no external dependencies and cannot fail due
  to a missing key, an expired login, or no internet — it's the permanent
  safety net under the LLM engine.

## Self-checks

Each engine file runs its own assertions:

```bash
python scoring.py   # rules engine: BUY/SELL cases + an all-null bundle
python llm.py       # grounding verifier, JSON extraction, provider detection
```

The demo bundles are synthetic, hand-built data for offline demonstration —
they are not real quotes.
