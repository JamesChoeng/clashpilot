"""Environment-overridable tuning knobs for probes, health checks, and switching."""

from __future__ import annotations

import os
import sys

DEFAULT_TARGETS = (
    "https://api2.cursor.sh",
    "https://api.anthropic.com/v1/messages",
)

INFO_KEYWORDS = ("流量", "剩余", "套餐", "到期", "expire", "重置", "官网", "订阅", "GB", "购买", "续费")
GROUP_TYPES = {
    "Selector", "URLTest", "Fallback", "LoadBalance", "Relay",
    "Direct", "Reject", "RejectDrop", "Compatible", "Pass", "Dns",
}


def env_str(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if v not in (None, "") else default


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, ""))
    except (TypeError, ValueError):
        return default


def _default_health_timeout_ms() -> int:
    return 5000 if sys.platform == "win32" else 2500


def _default_full_scan_interval() -> int:
    return 600


TARGETS = [t.strip() for t in env_str("CLASHPILOT_TARGETS", "").split(",") if t.strip()] or list(DEFAULT_TARGETS)
CLAUDE_TARGET = env_str("CLASHPILOT_CLAUDE_TARGET", "https://api.anthropic.com/v1/messages")

DELAY_TIMEOUT_MS = env_int("CLASHPILOT_DELAY_TIMEOUT_MS", 4000)
HEALTH_TIMEOUT_MS = env_int("CLASHPILOT_HEALTH_TIMEOUT_MS", _default_health_timeout_ms())
DELAY_EXPECTED = env_str("CLASHPILOT_DELAY_EXPECTED", "200-428/430-501/505-599")
CLAUDE_EXPECTED = env_str("CLASHPILOT_CLAUDE_EXPECTED", "200-402/404-428/430-450/452-501/505-599")

FULL_SCAN_INTERVAL = env_int("CLASHPILOT_FULL_SCAN_INTERVAL", _default_full_scan_interval())
HEALTH_INTERVAL = env_int("CLASHPILOT_HEALTH_INTERVAL", 15)
HEALTH_RETRIES = env_int("CLASHPILOT_HEALTH_RETRIES", 3)
# Confirmed-fail health rounds before failover (each round already retries probes).
HEALTH_FAIL_THRESHOLD = env_int("CLASHPILOT_HEALTH_FAIL_THRESHOLD", 1)
ANTHROPIC_FAIL_THRESHOLD = env_int("CLASHPILOT_ANTHROPIC_FAIL_THRESHOLD", 1)
# Consecutive Anthropic failovers (no healthy round in between) that signal an
# upstream/Anthropic-wide outage rather than a bad node. Past this, hold the
# current node instead of benching + jumping through the whole pool. 0 disables.
ANTHROPIC_OUTAGE_FAILOVERS = env_int("CLASHPILOT_ANTHROPIC_OUTAGE_FAILOVERS", 3)
# When the current node is healthy, only switch if a candidate is this much faster
# (latency reduction %) and stays ahead for SWITCH_SUSTAIN_SECONDS.
SWITCH_IMPROVEMENT_PCT = env_int("CLASHPILOT_SWITCH_IMPROVEMENT_PCT", 30)
SWITCH_SUSTAIN_SECONDS = env_int("CLASHPILOT_SWITCH_SUSTAIN_SECONDS", 180)
MAX_WORKERS = env_int("CLASHPILOT_MAX_WORKERS", 10)
SWITCH_COOLDOWN = env_int("CLASHPILOT_SWITCH_COOLDOWN", 60)
MAX_DEFER = env_int("CLASHPILOT_MAX_DEFER", 5)
# Health failover defers while Cursor/Anthropic is active; force after this many defers.
MAX_HEALTH_DEFER = env_int("CLASHPILOT_MAX_HEALTH_DEFER", 5)
# Legacy sliding-window knobs (unused; failover uses consecutive confirmed-fail rounds).
HEALTH_WINDOW_SIZE = env_int("CLASHPILOT_HEALTH_WINDOW_SIZE", 5)
HEALTH_WINDOW_FAILS = env_int("CLASHPILOT_HEALTH_WINDOW_FAILS", 3)
# EMA smoothing factor for rank scores (0 disables smoothing).
SCORE_EMA_ALPHA = env_int("CLASHPILOT_SCORE_EMA_ALPHA", 30)  # stored as pct, /100 at use
# When 1, skip periodic full rank if the current node passes a quick liveness check.
IDLE_SCAN = env_int("CLASHPILOT_IDLE_SCAN", 1)
# Re-probe a switch candidate this many extra times before committing the
# switch, requiring every probe to pass, so autoswitch doesn't land on a node
# that only works intermittently. 0 disables the re-check.
SWITCH_CONFIRM_ATTEMPTS = env_int("CLASHPILOT_SWITCH_CONFIRM_ATTEMPTS", 1)
# Cap how many top-ranked candidates get re-probed when the best one fails, so
# a scan where everything is flaky can't blow up into a probe storm.
SWITCH_CONFIRM_CANDIDATES = env_int("CLASHPILOT_SWITCH_CONFIRM_CANDIDATES", 5)
LOG_MAX_BYTES = env_int("CLASHPILOT_LOG_MAX_BYTES", 1_000_000)
SUB_REFRESH_INTERVAL = env_int("CLASHPILOT_SUB_REFRESH_INTERVAL", 21600)
NODE_BENCH_SECONDS = env_int("CLASHPILOT_NODE_BENCH_SECONDS", 600)
# Minimum seconds between automatic Opus whitelist rescans (avoids scan thrashing).
OPUS_RESCAN_COOLDOWN = env_int("CLASHPILOT_OPUS_RESCAN_COOLDOWN", 600)

GEO_IP_URL = env_str("CLASHPILOT_GEO_IP_URL", "")
GEO_PROBE_DELAY_S = env_int("CLASHPILOT_GEO_PROBE_DELAY_MS", 350) / 1000.0

ACTIVE_HOSTS = ("cursor.sh", "cursor.com", "anthropic")
