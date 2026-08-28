"""Configuration loading for quaestor: env-driven Settings plus YAML policy/calendar.

Role: the ONLY module that touches os.environ. Everything else receives a Settings
instance or a parsed policy/calendar dict.

Alpaca facts encoded here:
- Paper trading host is https://paper-api.alpaca.markets, market data host is
  https://data.alpaca.markets. This agent is paper-only: load_settings() refuses to
  start (SystemExit) if the trading base does not resolve to the paper host, or if
  the ALPACA_LIVE_TRADE env var is set truthy (fail-closed paper gate).
- Credential env var names follow the Alpaca CLI/MCP convention:
  ALPACA_API_KEY / ALPACA_SECRET_KEY (sent as APCA-API-KEY-ID / APCA-API-SECRET-KEY
  headers by broker.py). FEATHERLESS_API_KEY is optional (sentiment path only).
- configs/policy.yaml is hashed (sha256 of the raw bytes) into every RiskVerdict and
  bulla receipt via load_policy()["digest"] — editing the policy file visibly changes
  the audit trail.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

__all__ = ["Settings", "load_settings", "load_policy", "load_calendar"]

PAPER_HOST = "paper-api.alpaca.markets"

# Values of ALPACA_LIVE_TRADE that still count as "not enabled". Anything else is
# treated as a request for live trading and refused.
_FALSY = {"", "0", "false", "no", "off", "none"}


def _repo_root() -> Path:
    """Repository root = parent of the quaestor package directory."""
    return Path(__file__).resolve().parents[1]


@dataclass(kw_only=True)
class Settings:
    """Runtime settings loaded from the environment (+ optional .env). Fail-closed:
    construction via load_settings() guarantees paper-only operation."""

    api_key: str
    api_secret: str
    paper: bool                     # MUST be True; enforced in load_settings()
    trading_base: str = "https://paper-api.alpaca.markets"
    data_base: str = "https://data.alpaca.markets"
    featherless_key: str = ""       # optional; empty string disables sentiment path
    repo_root: Path = field(default_factory=_repo_root)
    runs_dir: Path = field(default_factory=lambda: _repo_root() / "runs")
    receipts_dir: Path = field(default_factory=lambda: _repo_root() / "receipts")


def _refuse(reason: str) -> None:
    """Fail-closed exit. Never echoes secret values."""
    raise SystemExit(f"quaestor: refusing to start — {reason}")


def load_settings() -> Settings:
    """Build Settings from the environment, loading .env from the repo root first.

    Fail-closed guarantees:
    - ALPACA_LIVE_TRADE set to anything truthy -> SystemExit (paper gate).
    - Missing ALPACA_API_KEY / ALPACA_SECRET_KEY -> SystemExit.
    - trading_base not on the paper host -> SystemExit.
    Side effect: creates runs/ and receipts/ directories if absent.
    """
    root = _repo_root()
    env_file = root / ".env"
    if env_file.is_file():
        # Never override real environment values with .env contents.
        load_dotenv(dotenv_path=env_file, override=False)

    live_flag = os.environ.get("ALPACA_LIVE_TRADE", "")
    if live_flag.strip().lower() not in _FALSY:
        _refuse(
            "ALPACA_LIVE_TRADE is set. This agent is paper-only; "
            "unset ALPACA_LIVE_TRADE entirely."
        )

    api_key = os.environ.get("ALPACA_API_KEY", "").strip()
    api_secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if not api_key:
        _refuse("ALPACA_API_KEY is not set (put it in .env or the environment)")
    if not api_secret:
        _refuse("ALPACA_SECRET_KEY is not set (put it in .env or the environment)")

    settings = Settings(
        api_key=api_key,
        api_secret=api_secret,
        paper=True,
        featherless_key=os.environ.get("FEATHERLESS_API_KEY", "").strip(),
        repo_root=root,
        runs_dir=root / "runs",
        receipts_dir=root / "receipts",
    )

    # Belt-and-braces: even a code change to the default must not slip past this.
    if PAPER_HOST not in settings.trading_base:
        _refuse(
            f"trading base {settings.trading_base!r} does not resolve to the paper "
            f"host ({PAPER_HOST}); live trading is forbidden"
        )
    if not settings.paper:
        _refuse("Settings.paper is False; live trading is forbidden")

    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    settings.receipts_dir.mkdir(parents=True, exist_ok=True)
    return settings


def load_policy() -> dict:
    """Parse configs/policy.yaml and add "digest": sha256 hex of the RAW file bytes.

    The digest is computed over the bytes on disk (not the parsed structure), so any
    edit — even whitespace — changes every subsequent RiskVerdict.policy_digest.
    """
    path = _repo_root() / "configs" / "policy.yaml"
    raw = path.read_bytes()
    policy: dict = yaml.safe_load(raw)
    if not isinstance(policy, dict):
        raise ValueError(f"policy file {path} did not parse to a mapping")
    policy["digest"] = hashlib.sha256(raw).hexdigest()
    return policy


def load_calendar() -> dict:
    """Parse configs/calendar.yaml (catalyst calendar; times are US/Eastern)."""
    path = _repo_root() / "configs" / "calendar.yaml"
    calendar: dict = yaml.safe_load(path.read_bytes())
    if not isinstance(calendar, dict):
        raise ValueError(f"calendar file {path} did not parse to a mapping")
    return calendar
