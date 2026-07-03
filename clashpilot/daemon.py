"""clashpilot daemon: process lifecycle + autoswitch main loop.

Standalone mode downloads/supervises mihomo, configures routing, and runs the
health/switch loop. Node selection lives in selector.py; probes in health.py.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import api, config, core, sysproxy
from .api import ControllerError, ControllerUnreachable, get_json, request
from .config import STATE_DIR
from .bench import bench_nodes
from .env_config import (
    ANTHROPIC_FAIL_THRESHOLD,
    ANTHROPIC_OUTAGE_FAILOVERS,
    FULL_SCAN_INTERVAL,
    HEALTH_FAIL_THRESHOLD,
    HEALTH_INTERVAL,
    MAX_HEALTH_DEFER,
    SUB_REFRESH_INTERVAL,
    TARGETS,
)
from .health import (
    health_fail_snapshot,
    health_fail_threshold,
    health_failover_update,
    node_latency,
    reset_health_failures,
)
from .logutil import LOG_FILE, log, notify, set_console_notify, tail_log
from .opus import maybe_refresh_opus_whitelist, refresh_opus_whitelist
from .proxy_ctrl import current_node, current_node_chain, fetch_proxies, has_active_target_connection, target_group
from .selector import format_scan, pick_and_switch, pin_to, switch_to, unpin

PID_FILE = STATE_DIR / "clashpilot.pid"
_TUN_FAIL_MARKER = "Start TUN listening error"
_system_proxy_active = False


def _python_exe() -> Path:
    exe = Path(sys.executable)
    if sys.platform == "win32":
        pyw = exe.with_name("pythonw.exe")
        if pyw.exists():
            return pyw
    return exe


PYTHON = _python_exe()


def _win_no_window() -> dict:
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": si}


_NO_WINDOW = _win_no_window() if sys.platform == "win32" else {}
_WIN_STILL_ACTIVE = 259


# --- Re-exports (stable public API for CLI/tests) --------------------------------

from .health import anthropic_reachable, is_alive  # noqa: E402
from .proxy_ctrl import (  # noqa: E402
    current_mode,
    delay,
    list_nodes,
    set_node,
)
from .selector import score  # noqa: E402

# Backward-compatible alias for tests.
_health_fail_threshold = health_fail_threshold


def format_status() -> str:
    core_line = (
        f"core_running={core.core_running()}\n"
        f"core_version={core.core_version() or 'n/a'}\n"
        f"subscription={'(default)' if config.using_default_subscription() else f'({len(config.subscription_urls())} source(s))' if len(config.subscription_urls()) > 1 else '(set)'}\n"
        f"proxy_mode={config.proxy_mode()}\n"
        f"mixed_port={config.mixed_port()}\n"
    )
    try:
        proxies = fetch_proxies()
        group = target_group(proxies)
        chain = current_node_chain(group, proxies)
        node = chain[-1] if chain else None
        latency = node_latency(node) if node else None
        mode = current_mode()
    except ControllerUnreachable as e:
        return (
            f"controller_unreachable: {e}\n"
            f"{core_line}"
            f"daemon_running={daemon_pid() is not None}"
        )
    daemon = daemon_pid()
    wl = config.opus_whitelist()
    return (
        f"mode={mode}\n"
        f"group={group}\n"
        f"current_node={node}\n"
        f"node_route={' -> '.join(chain) if chain else 'n/a'}\n"
        f"node_latency_ms={(latency or {}).get('average') or 'n/a'}\n"
        f"node_reachable_targets={(latency or {}).get('reachable', 0)}/{(latency or {}).get('total', len(TARGETS))}\n"
        f"{core_line}"
        f"daemon_running={daemon is not None}\n"
        f"daemon_pid={daemon or 'n/a'}\n"
        f"state_dir={STATE_DIR}\n"
        f"targets={TARGETS}\n"
        f"opus_whitelist={'disabled' if wl is None else len(wl)}"
    )


# --- Process management ------------------------------------------------------


def _proc_cmdline(pid: int) -> str:
    try:
        proc = Path(f"/proc/{pid}/cmdline")
        if proc.exists():
            return proc.read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace").strip().lower()
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True, text=True, timeout=10,
        )
        return (r.stdout or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ""


def _win_pid_alive(pid: int) -> bool:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _WIN_STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _win_terminate(pid: int) -> bool:
    PROCESS_TERMINATE = 0x0001
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel32.TerminateProcess.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _is_our_daemon(pid: int) -> bool:
    if not _pid_alive(pid):
        return False
    if sys.platform == "win32":
        return True
    cmd = _proc_cmdline(pid)
    if not cmd:
        return True
    return "clashpilot" in cmd


def daemon_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        return pid if _is_our_daemon(pid) else None
    except Exception:  # noqa: BLE001
        return None


def stop_daemon() -> str:
    pid = daemon_pid()
    if not pid:
        PID_FILE.unlink(missing_ok=True)
        return "not running"
    if sys.platform == "win32":
        _win_terminate(pid)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    PID_FILE.unlink(missing_ok=True)
    return f"stopped (pid {pid})"


def _wait_controller(timeout: int = 20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            get_json("/version")
            return True
        except ControllerError:
            time.sleep(0.5)
    return False


def _wait_mixed_port(timeout: float = 20) -> bool:
    """Wait until mihomo's mixed port accepts TCP (avoids Cursor hitting a dead proxy)."""
    import socket

    host, port = "127.0.0.1", config.mixed_port()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _core_log_tail(max_bytes: int = 16384) -> str:
    try:
        data = core.CORE_LOG_FILE.read_bytes()
    except OSError:
        return ""
    return data[-max_bytes:].decode("utf-8", errors="replace")


def tun_listening_ok() -> bool | None:
    """True when the latest core session started TUN cleanly; False on logged TUN errors."""
    if not config.tun_enabled():
        return None
    tail = _core_log_tail()
    if not tail:
        return None
    marker = "Start initial configuration in progress"
    idx = tail.rfind(marker)
    session = tail[idx:] if idx >= 0 else tail
    if _TUN_FAIL_MARKER in session:
        return False
    if "[TUN]" in session:
        return True
    return None


def _apply_system_proxy() -> bool:
    global _system_proxy_active
    host, port = "127.0.0.1", config.mixed_port()
    if sysproxy.set_system_proxy(host, port):
        _system_proxy_active = True
        log(f"== system proxy set -> {host}:{port}")
        return True
    log("!! could not set system proxy automatically (set it manually)")
    return False


def _clear_system_proxy() -> None:
    global _system_proxy_active
    if _system_proxy_active and sysproxy.unset_system_proxy():
        log("== system proxy removed")
    _system_proxy_active = False


def _recover_core(group: str | None = None) -> bool:
    if core.core_running():
        return False
    log("!! mihomo core not running -- restarting")
    try:
        core.start_core()
        api.reconfigure()
        if _wait_controller(20):
            log("== core restarted; controller reachable")
            if group:
                pick_and_switch(group, emergency=True)
            return True
        log("!! core restarted but controller still unreachable -- check core log")
    except Exception as e:  # noqa: BLE001
        log(f"!! core restart failed: {type(e).__name__}: {e}")
    return False


def _reload_core_config() -> bool:
    try:
        status, _ = request(
            "PUT", "/configs?force=true",
            body=json.dumps({"path": str(config.CONFIG_FILE)}),
        )
        return status in (204, 200)
    except ControllerError:
        return False


def bring_up() -> bool:
    if not _acquire_singleton():
        log("already running (another clashpilot up owns the pid file)")
        return False

    def _cleanup(*_args) -> None:
        bring_down()
        PID_FILE.unlink(missing_ok=True)
        sys.exit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _cleanup)
        except (ValueError, OSError):
            pass

    try:
        log("== standalone up: ensuring mihomo core")
        config.ensure_opus_filtering()
        core.ensure_core()
        if config.using_default_subscription():
            log("== no user subscription set -- using built-in default "
                "(set your own with: clashpilot set-sub <url>)")
        config.build_managed_config()
        api.reconfigure()
        pid = core.start_core()
        log(f"== core started (pid {pid}); version {core.core_version()}")
        if not _wait_controller(20):
            log("!! controller not reachable after start -- check core log")
        if config.tun_enabled():
            log(f"== TUN mode enabled (stack={config.tun_stack()}) -- skipping system proxy")
            if sys.platform == "darwin":
                log("   macOS: TUN may require admin; grant network permission if prompted")
            elif sys.platform == "win32":
                log("   Windows: TUN needs admin; approve UAC if prompted")
            time.sleep(0.5)
            tun_ok = tun_listening_ok()
            if tun_ok is False:
                reason = "access denied (run clashpilot as admin)" if sys.platform == "win32" else "see core log"
                log(f"!! TUN failed to start ({reason}) -- falling back to system proxy")
                if not _wait_mixed_port():
                    log("!! mixed port not ready -- system proxy may fail for early clients")
                _apply_system_proxy()
        else:
            if not _wait_mixed_port():
                log("!! mixed port not ready -- system proxy may fail for early clients")
            _apply_system_proxy()
        _run_loop(manage_subscription=True)
        return True
    finally:
        bring_down()
        PID_FILE.unlink(missing_ok=True)


def bring_down() -> None:
    _clear_system_proxy()
    if core.stop_core():
        log("== core stopped")


def _run_loop(manage_subscription: bool = False) -> None:
    group: str | None = None
    log(f"== clashpilot start | targets={TARGETS}")
    failovers = 0
    health_defer_count = 0
    last_full = time.time()
    last_sub = time.time()

    try:
        group = target_group()
        log(f"== mode group='{group}'")
        maybe_refresh_opus_whitelist(force=False)
        notify("probing nodes for best route...")
        result = pick_and_switch(group)
        node = result.get("to") or result.get("node")
        if node:
            notify(f"ready: autoswitch running on '{node}' (Ctrl-C to stop)")
        else:
            notify("ready: autoswitch running (Ctrl-C to stop)")
    except ControllerUnreachable:
        log("startup: controller unreachable -- will retry in loop")
        group = None
    except Exception as e:  # noqa: BLE001
        log(f"!! startup scan error: {type(e).__name__}: {e}")

    while True:
        time.sleep(HEALTH_INTERVAL)
        try:
            if (
                manage_subscription
                and SUB_REFRESH_INTERVAL > 0
                and time.time() - last_sub >= SUB_REFRESH_INTERVAL
            ):
                last_sub = time.time()
                try:
                    config.update_subscription()
                    if _reload_core_config():
                        log("subscription refreshed + core reloaded")
                        if config.opus_whitelist() is not None:
                            refresh_opus_whitelist(incremental=True)
                    else:
                        log("subscription refreshed (core reload failed)")
                except Exception as e:  # noqa: BLE001
                    log(f"!! subscription refresh failed: {type(e).__name__}: {e}")

            if group is None:
                group = target_group()
                log(f"== mode group='{group}'")

            try:
                cur = current_node(group)
            except ControllerUnreachable:
                if _recover_core(group):
                    reset_health_failures()
                    health_defer_count = 0
                    continue
                log("health: controller unreachable -- skipping round (not counted)")
                continue

            unhealthy, fail_threshold = health_fail_threshold(cur)
            should_failover = health_failover_update(unhealthy, fail_threshold)
            consecutive_fails = health_fail_snapshot()
            if unhealthy:
                anthropic_issue = fail_threshold == ANTHROPIC_FAIL_THRESHOLD
                reason = "Anthropic unreachable" if anthropic_issue else "unhealthy"
                log(
                    f"health: current '{cur}' {reason} "
                    f"(confirmed-fail {consecutive_fails}/{fail_threshold})"
                )
                if should_failover:
                    if (
                        anthropic_issue
                        and ANTHROPIC_OUTAGE_FAILOVERS > 0
                        and failovers >= ANTHROPIC_OUTAGE_FAILOVERS
                    ):
                        log(
                            f"suspected Anthropic-wide outage ({failovers} consecutive "
                            f"failovers) -> holding '{cur}', not switching"
                        )
                        reset_health_failures()
                        health_defer_count = 0
                    else:
                        force_failover = False
                        if has_active_target_connection() and health_defer_count < MAX_HEALTH_DEFER:
                            health_defer_count += 1
                            log(
                                f"defer failover from '{cur}' ({reason}): active connection "
                                f"({health_defer_count}/{MAX_HEALTH_DEFER})"
                            )
                        else:
                            force_failover = True
                            if health_defer_count >= MAX_HEALTH_DEFER:
                                log(
                                    f"forced failover from '{cur}' ({reason}) "
                                    f"after {health_defer_count} defers"
                                )
                            health_defer_count = 0
                        if force_failover:
                            log(f"current node confirmed DOWN ({reason}) -> failover")
                            if cur:
                                bench_nodes(cur, f"failed health loop ({reason})")
                            result = pick_and_switch(group, emergency=True)
                            if result.get("action") == "switched":
                                failovers += 1
                            reset_health_failures()
                            last_full = time.time()
            else:
                if failovers or health_defer_count:
                    log(f"current '{cur}' recovered, reset health counters")
                    reset_health_failures()
                    health_defer_count = 0
                    failovers = 0
                if time.time() - last_full >= FULL_SCAN_INTERVAL:
                    pick_and_switch(group, idle=True)
                    last_full = time.time()
        except ControllerUnreachable:
            if _recover_core(group):
                continue
            log("controller unreachable mid-iteration -- retrying next round")
            group = None
        except Exception as e:  # noqa: BLE001
            log(f"!! loop error: {type(e).__name__}: {e}")


def _acquire_singleton() -> bool:
    for _ in range(2):
        try:
            fd = os.open(str(PID_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            if daemon_pid() is not None:
                return False
            PID_FILE.unlink(missing_ok=True)
    return False


__all__ = [
    "ANTHROPIC_FAIL_THRESHOLD",
    "ControllerError",
    "ControllerUnreachable",
    "HEALTH_FAIL_THRESHOLD",
    "LOG_FILE",
    "PID_FILE",
    "PYTHON",
    "TARGETS",
    "_NO_WINDOW",
    "_health_fail_threshold",
    "anthropic_reachable",
    "bring_down",
    "bring_up",
    "current_mode",
    "current_node",
    "current_node_chain",
    "daemon_pid",
    "delay",
    "fetch_proxies",
    "format_scan",
    "format_status",
    "is_alive",
    "list_nodes",
    "node_latency",
    "notify",
    "pick_and_switch",
    "refresh_opus_whitelist",
    "score",
    "set_console_notify",
    "set_node",
    "stop_daemon",
    "switch_to",
    "pin_to",
    "unpin",
    "tail_log",
    "target_group",
]
