"""LLM debate engine: provider auto-detection, prompt, grounding verifier.

run_panel(bundle) returns the same dict shape as scoring.evaluate(), so
app.py never has to care which engine produced a verdict. If no provider is
available, the call fails, the reply will not parse, or any cited number
cannot be traced back to the evidence bundle, this module falls back to
scoring.evaluate() rather than showing an invented number.
"""

import glob
import json
import os
import re
import shutil
import subprocess
import threading
from functools import lru_cache

import scoring

CLAUDE_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "180"))

SEATS = ("technician", "fundamentalist", "newsdesk", "bull", "bear")

# Numbers an agent may write that are labels, not claims about this stock:
# "20DMA", "RSI14", "52-week", "12-month target", confidence out of 10.
STRUCTURAL_NUMBERS = {12, 14, 20, 50, 52, 100, 200, 30, 70, 1, 2, 3, 4, 5, 6, 7,
                      8, 9, 10, 0, 2024, 2025, 2026, 2027}


# --------------------------------------------------------------------------
# Provider detection
# --------------------------------------------------------------------------

@lru_cache(maxsize=1)
def claude_cli():
    """Path to the claude CLI, or None. Restart the app after installing one.

    The VS Code extension ships its own binary and never puts it on PATH, so
    a machine can have a perfectly good CLI that shutil.which() cannot see.
    The glob is version-agnostic on purpose -- the extension folder name
    carries its version and changes on every update.
    """
    override = (os.getenv("CLAUDE_CLI_PATH") or "").strip()
    if override:
        return override if os.path.exists(override) else None
    found = shutil.which("claude")
    if found:
        return found
    candidates = glob.glob(os.path.expanduser(
        "~/.vscode*/extensions/anthropic.claude-code-*/resources/native-binary/claude*"))
    return max(candidates, key=os.path.getmtime) if candidates else None


def detect_provider():
    """-> 'claude_code' | 'anthropic' | 'openai' | 'deterministic'."""
    forced = (os.getenv("LLM_PROVIDER") or "").strip().lower()
    if forced in ("claude_code", "anthropic", "openai", "deterministic"):
        return forced
    if claude_cli():
        return "claude_code"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    return "deterministic"


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------

PROMPT = """You are a six-seat trading desk reviewing one Indian listed stock \
on the {tf} timeframe. You have exactly one source of truth: the EVIDENCE \
bundle below. This is analysis only -- no orders are placed by anyone.

WHAT THIS DESK IS FOR
A short-horizon momentum and technical screen on the {tf} chart. You are \
judging the setup in front of you over the next few {tf} bars -- not whether \
this is a good business to own for a decade.

HARD RULES ON NUMBERS
1. Every number you write must appear in the EVIDENCE bundle. Do not compute, \
estimate, annualise or infer new numbers.
2. Any field listed in data_gaps is unknown. Write "data unavailable" for it. \
Never guess a P/E, ROE, or any figure that is null.
3. No number in your output that is absent from EVIDENCE. If you want to make a \
point you cannot support with a bundle number, make it in words.

ABOUT THE MISSING FUNDAMENTALS
This feed never carries P/E or ROE, for any stock, ever. Note it once and move \
on. Their absence is normal and is NOT on its own a reason to sit on the \
fence -- if you treat it as one you will return HOLD for every stock forever, \
which is useless to the desk. Judge what you do have: trend, moving averages, \
momentum, participation, range position, the analyst target and the news.

Treat the analyst target as a 12-month view. It is context, not a veto: a \
small percentage upside to a one-year target does not by itself outweigh a \
clean setup on the {tf} chart, and a large one does not rescue a broken one.

VERDICT CRITERIA -- commit to the one that fits
BUY   price is above its short and medium averages, or reclaiming them; \
momentum is positive without being exhausted; participation confirms the move; \
no dominant negative catalyst in the news.
SELL  price is below its short and medium averages; momentum is weak or \
rolling over; and/or a clear negative catalyst is driving it.
HOLD  the signals genuinely contradict each other, or too much of the bundle \
is missing to read the setup at all. HOLD is the honest answer when the \
evidence is mixed -- it is not the safe default when the evidence is clear.

CONFIDENCE
8-10 the signals agree with each other and volume confirms the move.
5-7  the setup is there but carries a real caveat.
1-4  thin, conflicting, or built on very little data.

SEATS
- technician: trend, moving averages, RSI, volume, range position.
- fundamentalist: the analyst target and what is unavailable. Be brief.
- newsdesk: what the headlines imply; no headline means say so.
- bull: strongest honest case to buy.
- bear: strongest honest case against, including cap-size and liquidity risk.
- judge: weighs the debate, issues verdict and confidence.

EVIDENCE
{evidence}

Reply with ONE JSON object and nothing else -- no markdown fence, no preamble:
{{"technician": "...", "fundamentalist": "...", "newsdesk": "...",
  "bull": "...", "bear": "...", "judge": "...",
  "verdict": "BUY" | "HOLD" | "SELL", "confidence": <integer 1-10>}}

Each seat gets 1-3 sentences. The judge explains the verdict in 2-3 sentences.
"""


def build_prompt(bundle):
    return PROMPT.format(
        tf=bundle.get("timeframe_label", "daily"),
        evidence=json.dumps(bundle, indent=2, default=str))


# --------------------------------------------------------------------------
# Grounding verifier
# --------------------------------------------------------------------------

# Digits preceded by a letter are part of a name, not a cited figure:
# CNBC-TV18, Q1, H2. Digits *followed* by letters stay in scope, so a made-up
# "27x earnings" is still caught. The alnum lookbehind also stops a hyphen
# from being read as a minus sign when it is really a range separator, as in
# "1570.10-2174.50" or a date -- otherwise a real high reads as a fake
# negative and the whole panel needlessly falls back.
NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])-?\d[\d,]*\.?\d*")


def _allowed_numbers(bundle):
    """Every number an agent is allowed to cite, plus scale variants.

    Indian reporting often converts raw volume into lakh/crore, and analysts
    round, so each evidence number is allowed at several scales.
    """
    allowed = set(float(n) for n in STRUCTURAL_NUMBERS)
    raw = []

    def harvest(text):
        for token in NUMBER_RE.findall(str(text)):
            try:
                raw.append(float(token.replace(",", "")))
            except ValueError:
                pass

    for key, value in bundle.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            raw.append(float(value))
        elif isinstance(value, str):
            harvest(value)  # e.g. range_label "60d window", timeframe "15m"
    for item in bundle.get("news") or []:
        harvest(f"{item.get('title', '')} {item.get('date', '')} {item.get('source', '')}")
    for v in raw:
        for scale in (1, 1e3, 1e5, 1e6, 1e7):
            allowed.add(round(v / scale, 4))
            allowed.add(round(abs(v) / scale, 4))
    return allowed


def _matches(n, allowed):
    return any(abs(n - a) <= max(abs(a) * 0.01, 0.51) for a in allowed)


def verify_grounding(text, bundle):
    """-> (ok, [numbers that are not traceable to the bundle])."""
    allowed = _allowed_numbers(bundle)
    offenders = []
    for token in NUMBER_RE.findall(text):
        cleaned = token.replace(",", "").rstrip(".")
        if not cleaned or cleaned in ("-", "."):
            continue
        try:
            n = float(cleaned)
        except ValueError:
            continue
        if not _matches(n, allowed):
            offenders.append(n)
    return (not offenders), offenders


# --------------------------------------------------------------------------
# Provider calls -- each returns raw text or raises
# --------------------------------------------------------------------------

_current_proc = None
_proc_lock = threading.Lock()
_cancelled = False


def reset_cancel():
    """Clear the cancelled flag at the start of a fresh run."""
    global _cancelled
    _cancelled = False


def cancel_running_call():
    """Kill an in-flight CLI call. Without this a stop request only lands once
    the current stock finishes, which on this engine is ~20 seconds of nothing
    happening after the user asked it to stop."""
    global _cancelled
    _cancelled = True
    with _proc_lock:
        proc = _current_proc
    if proc is not None and proc.poll() is None:
        try:
            proc.kill()
        except Exception:
            pass


def _call_claude_code(prompt):
    global _current_proc
    exe = claude_cli()
    if not exe:
        raise RuntimeError("claude CLI not found")
    proc = subprocess.Popen(
        [exe, "-p", "--output-format", "text"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace")
    with _proc_lock:
        _current_proc = proc
    try:
        out, err = proc.communicate(prompt, timeout=LLM_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    finally:
        with _proc_lock:
            _current_proc = None
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI exit {proc.returncode}: {(err or '')[:200]}")
    return out


def _call_anthropic(prompt):
    import anthropic
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=CLAUDE_MODEL, max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in msg.content if block.type == "text")


def _call_openai(prompt):
    from openai import OpenAI
    client = OpenAI()
    rsp = client.chat.completions.create(
        model=OPENAI_MODEL, max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return rsp.choices[0].message.content or ""


CALLERS = {
    "claude_code": _call_claude_code,
    "anthropic": _call_anthropic,
    "openai": _call_openai,
}


def _extract_json(text):
    """Pull the first JSON object out of a reply, fence or prose included."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unterminated JSON object in reply")


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def run_panel(bundle, provider=None, log=print):
    """Debate one stock. Always returns a verdict dict -- never raises."""
    provider = provider or detect_provider()
    if provider == "deterministic":
        return scoring.evaluate(bundle)

    try:
        raw = CALLERS[provider](build_prompt(bundle))
        data = _extract_json(raw)
    except Exception as exc:
        if _cancelled:
            # The call was killed on purpose. Reporting that as an engine
            # failure and a fallback would be a lie about what happened.
            log(f"  {bundle['ticker']}: call cancelled")
            return _fallback(bundle, "cancelled")
        log(f"  {bundle['ticker']}: {provider} failed ({exc.__class__.__name__}: "
            f"{str(exc)[:120]}), using deterministic engine")
        return _fallback(bundle, f"{provider} unavailable")

    missing = [k for k in SEATS + ("judge", "verdict", "confidence") if k not in data]
    if missing:
        log(f"  {bundle['ticker']}: reply missing {missing}, using deterministic engine")
        return _fallback(bundle, "incomplete panel reply")

    prose = " ".join(str(data[k]) for k in SEATS + ("judge",))
    ok, offenders = verify_grounding(prose, bundle)
    if not ok:
        log(f"  {bundle['ticker']}: ungrounded numbers {offenders[:5]}, "
            f"using deterministic engine")
        return _fallback(bundle, f"ungrounded figures {offenders[:5]}")

    verdict = str(data["verdict"]).strip().upper()
    if verdict == "AVOID":
        verdict = "SELL"  # same call, different word -- not worth a fallback
    if verdict not in ("BUY", "HOLD", "SELL"):
        log(f"  {bundle['ticker']}: bad verdict {verdict!r}, using deterministic engine")
        return _fallback(bundle, "unrecognised verdict")

    try:
        confidence = max(1, min(10, int(float(data["confidence"]))))
    except (TypeError, ValueError):
        confidence = 5

    return {
        "verdict": verdict,
        "confidence": confidence,
        "engine": provider,
        "panel": {seat: str(data[seat]) for seat in SEATS},
        "judge": str(data["judge"]),
        "bull_points": None,
        "bear_points": None,
    }


def _fallback(bundle, reason):
    result = scoring.evaluate(bundle)
    result["fallback_reason"] = reason
    return result


if __name__ == "__main__":
    b = {
        "ticker": "T.NS", "name": "T Co", "price": 1000.0, "rsi14": 58.2,
        "volume": 8_123_456, "analyst_target": 1150.0, "pe": None,
        "news": [{"title": "T Co wins 450 crore order", "date": "2026-08-14"}],
        "data_gaps": ["pe"], "cap_bucket": "mid", "day_change_pct": 2.0,
    }
    ok, bad = verify_grounding(
        "Price 1,000 with RSI 58.2 on 81.23 lakh shares against a 1150 target; "
        "the 450 crore order lands above the 200DMA. P/E data unavailable.", b)
    assert ok, bad
    ok, bad = verify_grounding("Book value works out to 372.4 per share.", b)
    assert not ok and 372.4 in bad, (ok, bad)
    # A number inside a name is not a claim; a fabricated multiple still is.
    ok, bad = verify_grounding("Reported by CNBC-TV18 in Q1; H2 guidance held.", b)
    assert ok, bad
    ok, bad = verify_grounding("It trades at 27x earnings.", b)
    assert not ok and 27.0 in bad, (ok, bad)
    # Label numbers the prompt itself introduces, and numbers living in bundle
    # strings, are citable -- they are not claims about the stock.
    ok, bad = verify_grounding(
        "The 12-month target sits above the 60d window high.",
        dict(b, range_label="60d window"))
    assert ok, bad
    # A hyphen between two figures is a range, not a minus sign.
    ok, bad = verify_grounding("The range is 1000-1150 on the year.", b)
    assert ok, bad
    # A genuine negative that is not in the bundle is still caught, and after
    # a space the minus sign is still a minus sign.
    ok, bad = verify_grounding("Margins fell by -38.4 basis points.", b)
    assert not ok and -38.4 in bad, (ok, bad)
    assert _extract_json('noise ```json\n{"a": {"b": "}"}}\n``` tail') == {"a": {"b": "}"}}
    assert detect_provider() in ("claude_code", "anthropic", "openai", "deterministic")
    assert run_panel(dict(b, sma20=None, sma50=None, sma200=None, week52_high=None,
                          week52_low=None, avg_volume_20d=None, roe=None,
                          week_change_pct=None, month_change_pct=None,
                          analyst_upside_pct=15.0),
                     provider="deterministic")["verdict"] in ("BUY", "HOLD", "SELL")
    print("llm.py self-check OK, provider:", detect_provider())
