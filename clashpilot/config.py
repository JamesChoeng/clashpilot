"""Per-user settings, subscription fetching, and the managed mihomo config.

In standalone mode this package owns the whole proxy config: the user supplies a
Clash/Mihomo subscription URL, we download it and inject our own
external-controller, secret, and mixed-port so the rest of the tool (and the OS
proxy) can talk to the core we launched.

YAML is patched textually rather than via PyYAML to keep zero third-party
dependencies -- Clash subscriptions are mappings keyed at column 0, so stripping
and prepending top-level keys is enough.

This module is the single source of truth for the per-user state directory; the
daemon and other modules import STATE_DIR from here.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import sys
import urllib.request
from pathlib import Path

from .subscription_merge import merge_subscription_texts

# LAN / link-local ranges kept off the TUN route table so local services stay reachable.
_TUN_ROUTE_EXCLUDE = (
    "127.0.0.0/8",
    "192.168.0.0/16",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "fc00::/7",
    "fe80::/10",
)


def _default_state_dir() -> Path:
    home = Path.home()
    app = "clashpilot"
    if sys.platform == "win32":
        base = os.getenv("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return Path(base) / app
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / app
    base = os.getenv("XDG_STATE_HOME") or str(home / ".local" / "state")
    return Path(base) / app


STATE_DIR = Path(os.getenv("CLASHPILOT_STATE_DIR") or _default_state_dir())
try:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    STATE_DIR = Path.home()

MANAGED_DIR = STATE_DIR / "managed"
CORE_DIR = STATE_DIR / "core"
SETTINGS_FILE = STATE_DIR / "settings.json"
CONFIG_FILE = MANAGED_DIR / "config.yaml"
SUBSCRIPTION_FILE = MANAGED_DIR / "subscription.yaml"

DEFAULT_MIXED_PORT = 7890
DEFAULT_CONTROLLER_PORT = 9090
# mihomo logs every matched connection at info, which floods the core log. Default
# to warning so the file stays small; override with CLASHPILOT_CORE_LOG_LEVEL.
DEFAULT_CORE_LOG_LEVEL = "warning"
_CORE_LOG_LEVELS = ("silent", "error", "warning", "info", "debug")

# Built-in default subscription so a fresh install connects out of the box, with
# no `set-sub` step. A public, volunteer-run free node list that auto-updates and
# ships proxy-groups + rules (so rule-mode routing and our node-switching work).
# Free public nodes are unstable and see all your traffic -- override with your
# own via `clashpilot set-sub <url>` for anything you care about.
DEFAULT_SUBSCRIPTION_URL = (
    "https://raw.githubusercontent.com/PuddinCat/BestClash/refs/heads/main/proxies.yaml"
)

# Last-resort offline fallback shipped inside the package: used only when neither
# the user's subscription nor the default subscription URL can be fetched.
BUNDLED_CONFIG_FILE = Path(__file__).with_name("default_config.yaml")


class ConfigError(RuntimeError):
    """Raised on missing subscription / unfetchable subscription content."""


# --- Settings ----------------------------------------------------------------


def get_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {}


def save_settings(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _parse_url_list(raw: str) -> list[str]:
    parts = re.split(r"[\n,]+", raw)
    return [p.strip() for p in parts if p.strip()]


def subscription_urls() -> list[str]:
    """User-configured subscription URLs (env or saved settings), or empty."""
    env_multi = os.getenv("CLASHPILOT_SUBSCRIPTIONS")
    if env_multi:
        return _parse_url_list(env_multi)
    env_single = os.getenv("CLASHPILOT_SUBSCRIPTION")
    if env_single:
        urls = _parse_url_list(env_single)
        return urls if len(urls) > 1 else [env_single.strip()]
    s = get_settings()
    urls = s.get("subscription_urls")
    if isinstance(urls, list):
        out = [u.strip() for u in urls if isinstance(u, str) and u.strip()]
        if out:
            return out
    legacy = s.get("subscription_url")
    if isinstance(legacy, str) and legacy.strip():
        return [legacy.strip()]
    return []


def subscription_url() -> str | None:
    """Primary subscription URL for display/backward compatibility."""
    urls = subscription_urls()
    return urls[0] if urls else None


def effective_subscription_urls() -> list[str]:
    """URLs we fetch: the user's list if set, else the built-in default."""
    urls = subscription_urls()
    return urls if urls else [DEFAULT_SUBSCRIPTION_URL]


def effective_subscription_url() -> str:
    """Single URL for backward compatibility (first configured source)."""
    return effective_subscription_urls()[0]


def using_default_subscription() -> bool:
    """True when no user subscription is set and we fall back to the default."""
    return not subscription_urls()


def set_subscription_urls(urls: list[str]) -> None:
    cleaned = []
    seen: set[str] = set()
    for url in urls:
        u = url.strip()
        if u and u not in seen:
            cleaned.append(u)
            seen.add(u)
    s = get_settings()
    s["subscription_urls"] = cleaned
    if cleaned:
        s["subscription_url"] = cleaned[0]
    else:
        s.pop("subscription_url", None)
        s.pop("subscription_urls", None)
    save_settings(s)


def set_subscription_url(url: str) -> None:
    """Replace the subscription list with a single URL."""
    set_subscription_urls([url])


def add_subscription_url(url: str) -> bool:
    """Append a subscription URL. Returns False if it was already present."""
    u = url.strip()
    if not u:
        raise ConfigError("subscription URL must not be empty")
    urls = subscription_urls()
    if u in urls:
        return False
    set_subscription_urls(urls + [u])
    return True


def remove_subscription_url(url: str) -> bool:
    """Remove a subscription URL. Returns False if it was not found."""
    u = url.strip()
    urls = subscription_urls()
    if u not in urls:
        return False
    set_subscription_urls([x for x in urls if x != u])
    return True


def _whitelist_env() -> str:
    return (
        os.getenv("CLASHPILOT_OPUS_WHITELIST")
        or os.getenv("CLASHPILOT_CLAUDE_WHITELIST")
        or ""
    ).strip().lower()


def opus_filtering_enabled() -> bool:
    """True when autoswitch should restrict nodes to Opus/Anthropic-eligible exits.

    On by default (Cursor + Anthropic use case). Disable with
    CLASHPILOT_OPUS_WHITELIST=0 or ``opus_filtering: false`` in settings.
    """
    env = _whitelist_env()
    if env in ("0", "false", "off", "no"):
        return False
    if env in ("1", "true", "on", "yes"):
        return True
    s = get_settings()
    if "opus_filtering" in s:
        return bool(s["opus_filtering"])
    return True


def ensure_opus_filtering() -> None:
    """Persist Opus filtering on first run unless the user opted out via env."""
    if _whitelist_env() in ("0", "false", "off", "no"):
        return
    s = get_settings()
    if "opus_filtering" not in s:
        s["opus_filtering"] = True
        save_settings(s)


def opus_whitelist() -> list[str] | None:
    """Return the Opus-region node whitelist when filtering is active.

    Nodes must exit in Anthropic-supported countries (see opus_regions.py).
    Filtering is on by default; set CLASHPILOT_OPUS_WHITELIST=0 to disable.
    An empty list means filtering is active but not yet scanned -- run
    ``whitelist --refresh`` or let ``clashpilot up`` scan on first start.
    """
    if not opus_filtering_enabled():
        return None
    s = get_settings()
    wl = s.get("opus_whitelist")
    if not isinstance(wl, list):
        wl = s.get("claude_whitelist")  # legacy key
    if not isinstance(wl, list):
        wl = []
    return wl


def opus_whitelist_meta() -> dict[str, str]:
    """Node -> ISO country code, populated by the last whitelist refresh."""
    meta = get_settings().get("opus_whitelist_meta")
    return meta if isinstance(meta, dict) else {}


def opus_region_codes() -> frozenset[str]:
    """ISO country codes treated as Opus-eligible; override via CLASHPILOT_OPUS_REGIONS."""
    from .opus_regions import ANTHROPIC_SUPPORTED_REGIONS

    raw = (os.getenv("CLASHPILOT_OPUS_REGIONS") or "").strip()
    if not raw:
        return ANTHROPIC_SUPPORTED_REGIONS
    return frozenset(c.strip().upper() for c in raw.split(",") if c.strip())


def save_opus_whitelist(nodes: list[str], meta: dict[str, str] | None = None) -> None:
    s = get_settings()
    s["opus_whitelist"] = nodes
    if meta is not None:
        s["opus_whitelist_meta"] = meta
    # Drop legacy key so old anthropic-only lists are not reused.
    s.pop("claude_whitelist", None)
    save_settings(s)


def controller_port() -> int:
    try:
        return int(os.getenv("CLASHPILOT_CONTROLLER_PORT") or "")
    except ValueError:
        pass
    return int(get_settings().get("controller_port") or DEFAULT_CONTROLLER_PORT)


def mixed_port() -> int:
    try:
        return int(os.getenv("CLASHPILOT_MIXED_PORT") or "")
    except ValueError:
        pass
    return int(get_settings().get("mixed_port") or DEFAULT_MIXED_PORT)


def core_log_level() -> str:
    """mihomo log verbosity for the managed config (env/settings overridable)."""
    lvl = (
        os.getenv("CLASHPILOT_CORE_LOG_LEVEL")
        or get_settings().get("core_log_level")
        or DEFAULT_CORE_LOG_LEVEL
    ).strip().lower()
    return lvl if lvl in _CORE_LOG_LEVELS else DEFAULT_CORE_LOG_LEVEL


def _env_bool(name: str) -> bool | None:
    raw = (os.getenv(name) or "").strip().lower()
    if raw in ("1", "true", "on", "yes"):
        return True
    if raw in ("0", "false", "off", "no"):
        return False
    return None


def tun_enabled() -> bool:
    """True when TUN mode should route traffic (env overrides saved settings)."""
    env = _env_bool("CLASHPILOT_TUN")
    if env is not None:
        return env
    s = get_settings()
    if "tun_enabled" in s:
        return bool(s["tun_enabled"])
    # Windows: Cursor often misses system/http proxy on startup; TUN is the reliable default.
    return sys.platform == "win32"


def set_tun_enabled(enabled: bool) -> None:
    s = get_settings()
    s["tun_enabled"] = enabled
    save_settings(s)


def ensure_macos_service_tun() -> bool:
    """Enable TUN on first macOS `install-service` unless routing was already configured."""
    if sys.platform != "darwin":
        return False
    if _env_bool("CLASHPILOT_TUN") is not None:
        return False
    s = get_settings()
    if "tun_enabled" in s:
        return False
    set_tun_enabled(True)
    return True


def ensure_windows_tun() -> bool:
    """Persist TUN as the Windows default on first bring-up / service install."""
    if sys.platform != "win32":
        return False
    if _env_bool("CLASHPILOT_TUN") is not None:
        return False
    s = get_settings()
    if "tun_enabled" in s:
        return False
    set_tun_enabled(True)
    return True


def ensure_windows_service_tun() -> bool:
    """Enable TUN on first Windows `install-service` unless routing was already configured."""
    return ensure_windows_tun()


def ensure_service_tun() -> bool:
    """Platform hook for first-time service install TUN defaults."""
    if sys.platform == "darwin":
        return ensure_macos_service_tun()
    if sys.platform == "win32":
        return ensure_windows_service_tun()
    return False


def save_last_switch(
    from_node: str | None,
    to_node: str,
    reason: str,
    *,
    forced: bool = False,
) -> None:
    import time

    s = get_settings()
    s["last_switch"] = {
        "from": from_node,
        "to": to_node,
        "reason": reason,
        "forced": forced,
        "ts": time.time(),
    }
    save_settings(s)


def last_switch() -> dict | None:
    raw = get_settings().get("last_switch")
    return raw if isinstance(raw, dict) else None


def pinned_chain() -> list[str]:
    """Ordered list of manually-pinned nodes: [primary, fallback1, fallback2, ...].

    autoswitch stays on the highest-priority node in this list that's currently
    reachable, ignoring faster candidates outside the chain. Falls back down the
    list only when a higher-priority entry actually goes down, and restores back
    up automatically once it recovers.
    """
    s = get_settings()
    raw = s.get("pinned_chain")
    if isinstance(raw, list):
        chain = [n for n in raw if isinstance(n, str) and n]
        if chain:
            return chain
    legacy = s.get("pinned_node")
    return [legacy] if isinstance(legacy, str) and legacy else []


def pinned_node() -> str | None:
    chain = pinned_chain()
    return chain[0] if chain else None


def set_pinned_chain(nodes: list[str]) -> None:
    s = get_settings()
    s["pinned_chain"] = list(nodes)
    s.pop("pinned_node", None)
    save_settings(s)


def set_pinned_node(node: str) -> None:
    set_pinned_chain([node])


def clear_pinned_node() -> None:
    s = get_settings()
    changed = False
    for key in ("pinned_chain", "pinned_node"):
        if key in s:
            del s[key]
            changed = True
    if changed:
        save_settings(s)


def tun_stack() -> str:
    """mihomo TUN stack: system / gvisor / mixed (platform-aware default)."""
    raw = (os.getenv("CLASHPILOT_TUN_STACK") or get_settings().get("tun_stack") or "").strip().lower()
    if raw in ("system", "gvisor", "mixed"):
        return raw
    # gvisor is generally more reliable on macOS; system elsewhere.
    return "gvisor" if sys.platform == "darwin" else "system"


def tun_mtu() -> int | None:
    """Optional TUN MTU override (macOS often benefits from 9000)."""
    env = os.getenv("CLASHPILOT_TUN_MTU")
    saved = get_settings().get("tun_mtu")
    raw = env if env not in (None, "") else saved
    if raw in (None, ""):
        return 9000 if sys.platform == "darwin" else None
    if isinstance(raw, int):
        return raw
    raw = str(raw).strip()
    if not raw:
        return 9000 if sys.platform == "darwin" else None
    try:
        return int(raw)
    except ValueError:
        return None


def proxy_mode() -> str:
    return "tun" if tun_enabled() else "system"


def _tun_config_block() -> str:
    """YAML fragment injected into the managed config when TUN mode is on."""
    lines = [
        "tun:",
        "  enable: true",
        f"  stack: {tun_stack()}",
        "  auto-route: true",
        "  auto-detect-interface: true",
        "  dns-hijack:",
        "    - any:53",
        "    - tcp://any:53",
        "  route-exclude-address:",
    ]
    lines.extend(f"    - {cidr}" for cidr in _TUN_ROUTE_EXCLUDE)
    mtu = tun_mtu()
    if mtu:
        lines.append(f"  mtu: {mtu}")
    if sys.platform == "linux" and _env_bool("CLASHPILOT_TUN_AUTO_REDIRECT") is True:
        lines.extend(["  auto-redirect: true"])
    return "\n".join(lines) + "\n"


def get_secret() -> str:
    """Stable controller secret, generated once and persisted in settings."""
    env = os.getenv("CLASH_SECRET")
    if env:
        return env
    s = get_settings()
    sec = s.get("secret")
    if not sec:
        sec = secrets.token_hex(16)
        s["secret"] = sec
        save_settings(s)
    return sec


# --- Subscription + managed config -------------------------------------------


def _opener() -> urllib.request.OpenerDirector:
    # Ignore any system/env proxy: we may be downloading *before* the proxy is up.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _decode_subscription_body(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace")
    if "proxies:" not in text and "proxy-providers:" not in text:
        stripped = text.strip()
        try:
            decoded = base64.b64decode(stripped + "=" * (-len(stripped) % 4)).decode("utf-8", "replace")
            if "proxies:" in decoded or "proxy-providers:" in decoded:
                text = decoded
        except Exception:  # noqa: BLE001
            pass
    return text


def fetch_subscription_url(url: str) -> str:
    """Download one subscription body (decoding base64 panels when needed)."""
    req = urllib.request.Request(url, headers={"User-Agent": "clash-verge/v2.0.0"})
    try:
        with _opener().open(req, timeout=30) as r:
            raw = r.read()
    except Exception as e:  # noqa: BLE001
        raise ConfigError(f"failed to fetch subscription {url!r}: {e}") from e
    return _decode_subscription_body(raw)


def fetch_subscription(url: str | None = None) -> str:
    """Download subscription content and cache the merged result.

    When multiple URLs are configured, each source is fetched and merged so
    autoswitch can pick the fastest node across all of them. Partial failures
    are tolerated when at least one source succeeds.
    """
    if url is not None:
        text = fetch_subscription_url(url)
        MANAGED_DIR.mkdir(parents=True, exist_ok=True)
        SUBSCRIPTION_FILE.write_text(text, encoding="utf-8")
        return text

    urls = effective_subscription_urls()
    if len(urls) == 1:
        text = fetch_subscription_url(urls[0])
    else:
        bodies: list[str] = []
        errors: list[str] = []
        for sub_url in urls:
            try:
                bodies.append(fetch_subscription_url(sub_url))
            except ConfigError as e:
                errors.append(str(e))
        if not bodies:
            detail = "; ".join(errors) if errors else "no subscription URLs configured"
            raise ConfigError(f"failed to fetch any subscription: {detail}")
        text = merge_subscription_texts(bodies)

    MANAGED_DIR.mkdir(parents=True, exist_ok=True)
    SUBSCRIPTION_FILE.write_text(text, encoding="utf-8")
    return text


# Top-level keys we own; any subscription-provided copy is stripped so ours win
# and mihomo never sees a duplicate top-level key.
_OVERRIDE_KEYS = frozenset({
    "external-controller", "external-controller-tls", "secret", "external-ui",
    "mixed-port", "port", "socks-port", "redir-port", "tproxy-port",
    "allow-lan", "bind-address", "mode", "log-level", "tun",
})

_TOP_KEY_RE = re.compile(r"^([A-Za-z0-9_.-]+):")


def _strip_top_level_keys(yaml_text: str, keys: frozenset) -> str:
    """Drop the listed top-level mapping keys (and their indented blocks)."""
    out: list[str] = []
    skipping = False
    for line in yaml_text.splitlines():
        m = _TOP_KEY_RE.match(line)
        if m:  # a new top-level key starts here
            skipping = m.group(1) in keys
        if not skipping:
            out.append(line)
    return "\n".join(out)


def _bundled_config_text() -> str | None:
    """The packaged offline-fallback node list, or None if it's missing."""
    try:
        return BUNDLED_CONFIG_FILE.read_text(encoding="utf-8")
    except OSError:
        return None


def build_managed_config(subscription_text: str | None = None) -> Path:
    """Regenerate the managed config.yaml from the subscription + our overrides.

    Source precedence: explicit text > cached subscription > freshly fetched
    subscription (user's, else the built-in default) > bundled offline fallback.
    """
    text = subscription_text
    if text is None:
        if SUBSCRIPTION_FILE.exists():
            text = SUBSCRIPTION_FILE.read_text(encoding="utf-8")
        else:
            try:
                text = fetch_subscription()
            except ConfigError:
                # Network unreachable on first run: fall back to bundled nodes so
                # the core can still start and the user is online out of the box.
                text = _bundled_config_text()
                if text is None:
                    raise
    base = _strip_top_level_keys(text, _OVERRIDE_KEYS).lstrip("\n")
    header = (
        "# Managed by clashpilot -- do not edit; regenerated from your subscription.\n"
        f"mixed-port: {mixed_port()}\n"
        "allow-lan: false\n"
        "bind-address: 127.0.0.1\n"
        "mode: rule\n"
        f"log-level: {core_log_level()}\n"
        f"external-controller: 127.0.0.1:{controller_port()}\n"
        # json.dumps yields a safely-quoted scalar so a CLASH_SECRET containing
        # quotes/spaces can't break the generated YAML.
        f"secret: {json.dumps(get_secret())}\n"
    )
    if tun_enabled():
        header += "\n" + _tun_config_block()
    MANAGED_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(header + "\n" + base + "\n", encoding="utf-8")
    return CONFIG_FILE


def update_subscription() -> Path:
    """Re-fetch the subscription and rebuild the managed config."""
    return build_managed_config(fetch_subscription())


def ensure_config() -> Path:
    """Make sure a managed config exists, building it from the subscription."""
    if CONFIG_FILE.exists():
        return CONFIG_FILE
    return build_managed_config()
