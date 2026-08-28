"""LLM headline sentiment (optional alpha input). NEVER blocks trading.

Role: scores Alpaca news headlines per ticker via the Featherless
OpenAI-compatible endpoint (base_url https://api.featherless.ai/v1, model
openai/gpt-oss-20b) using the openai SDK, temperature 0, strict JSON-only
prompt. Output: {symbol: score} with scores in [-1, 1].

Facts encoded:
- Headlines come from Alpaca's news API via data.MarketData.news(): dicts with
  "headline"/"symbols" (sometimes "title"/"summary"); shapes handled defensively.
- The ENTIRE call path is wrapped in one try/except: missing FEATHERLESS_API_KEY,
  402 (payment), 429 (rate limit), timeouts, malformed JSON, SDK not installed —
  any failure returns {} (neutral fallback), so the decision cycle can never
  hang or die on sentiment.
- 10 second timeout, zero retries (max_retries=0): a slow LLM must not delay
  the 5-minute agent cycle.
"""
from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - types only; config.py is another module
    from quaestor.config import Settings

FEATHERLESS_BASE_URL: str = "https://api.featherless.ai/v1"
SENTIMENT_MODEL: str = "openai/gpt-oss-20b"
REQUEST_TIMEOUT_S: float = 10.0
MAX_HEADLINES: int = 40

_SYSTEM_PROMPT = (
    "You are a financial news sentiment scorer. You will receive recent stock "
    "market headlines, each tagged with the ticker symbols it concerns. "
    "Score the aggregate sentiment for EACH symbol as a float between -1 and 1 "
    "(-1 = very bearish, 0 = neutral, 1 = very bullish). "
    "Respond with ONLY one JSON object mapping symbol to score, e.g. "
    '{"SPY": 0.2, "QQQ": -0.4}. No prose, no markdown, no code fences.'
)


def score_headlines(headlines: list[dict], settings: "Settings") -> dict[str, float]:
    """Score headlines -> {symbol: sentiment in [-1, 1]}. {} on ANY failure."""
    try:
        if not headlines:
            return {}
        key = str(getattr(settings, "featherless_key", "") or "")
        if not key:
            return {}
        lines, symbols = _format_headlines(headlines)
        if not lines:
            return {}

        from openai import OpenAI  # imported lazily: missing SDK -> {} not crash

        client = OpenAI(
            api_key=key,
            base_url=FEATHERLESS_BASE_URL,
            timeout=REQUEST_TIMEOUT_S,
            max_retries=0,
        )
        user_prompt = (
            "Symbols: " + (", ".join(sorted(symbols)) if symbols else "(infer from headlines)")
            + "\nHeadlines:\n" + "\n".join(lines)
            + "\nReturn the JSON object now."
        )
        resp = client.chat.completions.create(
            model=SENTIMENT_MODEL,
            temperature=0,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        text = resp.choices[0].message.content or ""
        return _parse_scores(text, symbols)
    except Exception:
        return {}


# --- internals ---------------------------------------------------------------

def _format_headlines(headlines: list[dict]) -> tuple[list[str], set[str]]:
    """Render headline dicts to prompt lines; collect the tagged symbol set."""
    lines: list[str] = []
    symbols: set[str] = set()
    for h in headlines[:MAX_HEADLINES]:
        if not isinstance(h, dict):
            continue
        text = str(h.get("headline") or h.get("title") or "").strip()
        if not text:
            continue
        raw_syms: Any = h.get("symbols")
        if isinstance(raw_syms, str):
            raw_syms = [raw_syms]
        if not isinstance(raw_syms, list):
            raw_syms = []
        syms = [str(s).strip().upper() for s in raw_syms if s]
        symbols.update(syms)
        line = f"- [{', '.join(syms) if syms else '?'}] {text[:200]}"
        summary = str(h.get("summary") or "").strip()
        if summary:
            line += f" — {summary[:200]}"
        lines.append(line)
    return lines, symbols


def _parse_scores(text: str, symbols: set[str]) -> dict[str, float]:
    """Extract the first {...} JSON object; keep finite floats clamped to [-1, 1].

    When a tagged symbol set exists, unknown keys are dropped (the model must
    not invent tickers). May raise on malformed JSON — the caller catches.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {}
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in data.items():
        sym = str(k).strip().upper()
        if not sym:
            continue
        if symbols and sym not in symbols:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(f):
            continue
        out[sym] = max(-1.0, min(1.0, f))
    return out
