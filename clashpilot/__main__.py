"""clashpilot command-line interface.

Run as `clashpilot <cmd>` (installed console script) or `python -m clashpilot <cmd>`.
"""

from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, api, config, core, daemon, pathsetup, service, sysproxy

# Direct GitHub by default; core.download_github falls back to a mirror when the
# direct fetch fails (or honors CLASHPILOT_GH_PROXY if the user set one).
_GEO_BASE = "https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/"
_GEO_FILES = ("geoip.metadb", "geosite.dat")


def _log(msg: str) -> None:
    try:
        with open(config.STATE_DIR / "autostart.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def _ensure_geo() -> None:
    """mihomo refuses to start without the geo DBs the generated rules reference."""
    config.MANAGED_DIR.mkdir(parents=True, exist_ok=True)
    for fn in _GEO_FILES:
        dest = config.MANAGED_DIR / fn
        if dest.exists() and dest.stat().st_size > 0:
            continue
        try:
            core.download_github(_GEO_BASE + fn, dest, timeout=120)
            _log(f"downloaded {fn} ({dest.stat().st_size} bytes)")
        except Exception as e:  # noqa: BLE001
            _log(f"geo {fn} download failed: {e}")


def _ensure_path_once() -> None:
    """First-run only: put the console-scripts dir on PATH, then never auto-touch
    it again (so a user who later removes it isn't fought on every bring-up)."""
    try:
        s = config.get_settings()
        if s.get("path_setup_done"):
            return
        pathsetup.ensure_path_quiet()
        s["path_setup_done"] = True
        config.save_settings(s)
        _log("path setup attempted on first run")
    except Exception as e:  # noqa: BLE001
        _log(f"path setup skipped: {e}")


def _prepare() -> None:
    """First-run prep shared by foreground/background bring-up: PATH, config,
    geo DBs, and the mihomo core binary. All steps are idempotent / cached."""
    _ensure_path_once()
    config.ensure_opus_filtering()
    config.build_managed_config()
    _ensure_geo()
    core.ensure_core()


# --- Subcommands -------------------------------------------------------------


def _console_safe(line: str) -> str:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return line.encode(encoding, errors="replace").decode(encoding, errors="replace")


def _console_notify(line: str) -> None:
    print(_console_safe(line), flush=True)


def _maybe_elevate_for_tun(args: argparse.Namespace) -> int | None:
    """On Windows + TUN, re-launch with UAC if needed. Returns exit code or None."""
    if sys.platform != "win32" or getattr(args, "no_elevate", False):
        return None
    if not config.tun_enabled():
        return None
    from . import win_elevate

    if win_elevate.is_admin():
        return None
    print("clashpilot: TUN on Windows needs Administrator — requesting UAC elevation...")
    return win_elevate.relaunch_elevated(extra_args=["--no-elevate"])


def _cmd_up(args: argparse.Namespace) -> int:
    """Foreground: core + routing + autoswitch loop. Blocks until Ctrl-C."""
    if getattr(args, "tun", False):
        os.environ["CLASHPILOT_TUN"] = "1"
    elif getattr(args, "no_tun", False):
        os.environ["CLASHPILOT_TUN"] = "0"
    elif config.ensure_windows_tun():
        _log("enabled TUN by default on Windows")
    if getattr(args, "persist_tun", False):
        config.set_tun_enabled(config.tun_enabled())

    elevated = _maybe_elevate_for_tun(args)
    if elevated is not None:
        return elevated

    running = daemon.daemon_pid()
    if running:
        print(f"clashpilot already running (pid {running}); nothing to do.")
        print("  stop it with: clashpilot down")
        return 0

    if getattr(args, "detach", False):
        cmd = [str(daemon.PYTHON), "-m", "clashpilot", "up"]
        if getattr(args, "tun", False):
            cmd.append("--tun")
        elif getattr(args, "no_tun", False):
            cmd.append("--no-tun")
        if getattr(args, "persist_tun", False):
            cmd.append("--persist-tun")
        cmd.append("--no-elevate")
        env = os.environ.copy()
        subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            **getattr(daemon, "_NO_WINDOW", {}),
        )
        for _ in range(40):
            time.sleep(0.25)
            pid = daemon.daemon_pid()
            if pid:
                print(f"clashpilot started in background (pid {pid})")
                print(f"  status: clashpilot status")
                print(f"  stop:   clashpilot down")
                print(f"  logs:   {daemon.LOG_FILE}")
                return 0
        print("clashpilot: background start timed out; check logs:", daemon.LOG_FILE, file=sys.stderr)
        return 1

    print("clashpilot: preparing (config, geo databases, core)...")
    _prepare()
    routing = config.proxy_mode()
    print(f"clashpilot up: core + {routing} routing + autoswitch (foreground, Ctrl-C to stop)")
    if routing == "tun":
        print(f"  routing:    TUN (stack={config.tun_stack()})")
        if sys.platform == "darwin":
            print("  note:       macOS TUN may require admin / network permission")
        elif sys.platform == "win32":
            print("  note:       Windows TUN may require admin (approve UAC if prompted)")
    else:
        print(f"  proxy:      127.0.0.1:{config.mixed_port()} (http+socks)")
        if sys.platform == "darwin":
            print("  tip:        Cursor may ignore system proxy; try: clp up --tun --persist-tun")
        elif sys.platform == "win32":
            print("  tip:        system proxy mode; prefer default TUN or: clp up --no-tun --persist-tun")
    if config.opus_filtering_enabled():
        wl = config.opus_whitelist() or []
        print(f"  opus filter: on ({len(wl)} nodes cached; auto-scan on first run if empty)")
    print(f"  controller: 127.0.0.1:{config.controller_port()}")
    print(f"  core:       mihomo {core.core_version()}")
    print(f"  logs:       {daemon.LOG_FILE}")
    print("  tip:        runs in foreground; use `clp up -d` for background")
    daemon.set_console_notify(_console_notify)
    try:
        if not daemon.bring_up():
            print("clashpilot already running; use `clashpilot down` first.", file=sys.stderr)
            return 1
    finally:
        daemon.set_console_notify(None)
    return 0


def _cmd_down(_args: argparse.Namespace) -> int:
    dmsg = daemon.stop_daemon()  # stop foreground/background loop if any
    stopped = core.stop_core()   # ensure the core is down (idempotent)
    tun_ok = daemon.tun_listening_ok()
    if config.tun_enabled() and tun_ok is not False:
        routing_msg = "TUN stopped"
    else:
        unset = sysproxy.unset_system_proxy()
        routing_msg = f"system proxy {'unset' if unset else 'unchanged'}"
    print(
        f"clashpilot down: daemon {dmsg}; core {'stopped' if stopped else 'not running'}, "
        f"{routing_msg}"
    )
    return 0


def _cmd_status(_args: argparse.Namespace) -> int:
    # Controller host/secret are discovered at import; re-run discovery so we pick
    # up a managed config written after this process started.
    api.reconfigure()
    daemon_pid = daemon.daemon_pid()

    def out(line: str) -> None:
        print(_console_safe(line))

    out(f"clashpilot {__version__}")
    out(f"  autoswitch:   {'running' if daemon_pid else 'stopped'} (pid {daemon_pid or 'n/a'})")
    out(f"  core running: {core.core_running()} (pid {core.core_pid()})")
    out(f"  core version: {core.core_version()}")
    if config.using_default_subscription():
        out(f"  subscription: (default) {config.DEFAULT_SUBSCRIPTION_URL}")
    else:
        urls = config.subscription_urls()
        if len(urls) == 1:
            out(f"  subscription: {urls[0]}")
        else:
            out(f"  subscriptions: {len(urls)} sources (merged for autoswitch)")
            for i, url in enumerate(urls, 1):
                out(f"    [{i}] {url}")
    mode = config.proxy_mode()
    if mode == "tun":
        tun_ok = daemon.tun_listening_ok()
        if tun_ok is False:
            out(f"  routing:      tun (FAILED, stack={config.tun_stack()})")
            out("  tun note:     not listening -- run as admin on Windows, or: clp up --no-tun --persist-tun")
        else:
            out(f"  routing:      tun (stack={config.tun_stack()})")
    else:
        out(f"  routing:      {mode}")
    out(f"  proxy:        127.0.0.1:{config.mixed_port()}")
    out(f"  controller:   127.0.0.1:{config.controller_port()}")
    try:
        proxies = daemon.fetch_proxies()
        clash_mode = daemon.current_mode()
        group = daemon.target_group(proxies)
        chain = daemon.current_node_chain(group, proxies)
        node = chain[-1] if chain else None
        out(f"  mode:         {clash_mode}")
        out(f"  group:        {group}")
        out(f"  node:         {node or 'n/a'}")
        if len(chain) > 1:
            out(f"  node route:   {' -> '.join(chain)}")
        if node:
            latency = daemon.node_latency(node)
            average = latency["average"]
            average_text = f"{average}ms avg" if average is not None else "timeout"
            out(f"  latency:      {average_text} ({latency['reachable']}/{latency['total']} targets)")
            anthropic_ok = daemon.anthropic_reachable(node)
            out(f"  anthropic:    {'ok' if anthropic_ok else 'unreachable (will failover)'}")
        wl = config.opus_whitelist()
        if wl is None:
            out("  opus filter:  off")
        elif wl:
            out(f"  opus wl:      {len(wl)} nodes (Opus-region pool)")
        else:
            out("  opus wl:      active, not scanned yet")
        chain = config.pinned_chain()
        if chain:
            if len(chain) > 1:
                out(f"  pinned:       {' > '.join(chain)} (failover only)")
            else:
                out(f"  pinned:       {chain[0]} (failover only)")
        last = config.last_switch()
        if last:
            import datetime

            ts = last.get("ts")
            when = (
                datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                if isinstance(ts, (int, float))
                else "n/a"
            )
            forced = " (forced)" if last.get("forced") else ""
            out(
                f"  last switch:  {last.get('from') or 'n/a'} -> {last.get('to') or 'n/a'} "
                f"[{last.get('reason') or 'unknown'}]{forced} @ {when}"
            )
    except daemon.ControllerUnreachable as e:
        out(f"  node:         n/a (controller unreachable: {e})")
    except daemon.ControllerError as e:
        out(f"  node:         n/a (controller error: {e})")
    out(f"  state dir:    {config.STATE_DIR}")
    return 0


def _cmd_scan(args: argparse.Namespace) -> int:
    api.reconfigure()
    if not core.core_running():
        print(
            "error: core is not running — start clashpilot first (clashpilot up)",
            file=sys.stderr,
        )
        return 1
    print(
        _console_safe(
            daemon.format_scan(
                top_n=max(1, args.top),
                all_nodes=getattr(args, "all_nodes", False),
            )
        )
    )
    return 0


def _cmd_pin(args: argparse.Namespace) -> int:
    api.reconfigure()
    if not core.core_running():
        print(
            "error: core is not running — start clashpilot first (clashpilot up)",
            file=sys.stderr,
        )
        return 1
    print(_console_safe(daemon.pin_to(*args.nodes)))
    return 0


def _cmd_unpin(_args: argparse.Namespace) -> int:
    print(_console_safe(daemon.unpin()))
    return 0


def _cmd_install_service(args: argparse.Namespace) -> int:
    note = service._service_routing_preamble(args)
    print(service.install_service(extra_note=note))
    return 0


def _cmd_uninstall_service(_args: argparse.Namespace) -> int:
    print(service.uninstall_service())
    return 0


def _cmd_set_sub(args: argparse.Namespace) -> int:
    if getattr(args, "add", False):
        added = config.add_subscription_url(args.url)
        if added:
            print(f"added subscription ({len(config.subscription_urls())} total) -> {config.SETTINGS_FILE}")
        else:
            print("subscription already present; list with: clashpilot list-sub")
        return 0
    config.set_subscription_url(args.url)
    print(f"saved subscription URL -> {config.SETTINGS_FILE}")
    return 0


def _cmd_list_sub(_args: argparse.Namespace) -> int:
    if config.using_default_subscription():
        print(f"(default) {config.DEFAULT_SUBSCRIPTION_URL}")
        return 0
    urls = config.subscription_urls()
    print(f"{len(urls)} subscription source(s):")
    for i, url in enumerate(urls, 1):
        print(f"  [{i}] {url}")
    return 0


def _cmd_remove_sub(args: argparse.Namespace) -> int:
    if config.using_default_subscription():
        print("no user subscriptions configured", file=sys.stderr)
        return 1
    urls = config.subscription_urls()
    target = args.url
    if getattr(args, "index", None) is not None:
        idx = args.index
        if idx < 1 or idx > len(urls):
            print(f"invalid index {idx}; use 1..{len(urls)}", file=sys.stderr)
            return 1
        target = urls[idx - 1]
    if not target:
        print("provide a URL or --index N", file=sys.stderr)
        return 1
    if not config.remove_subscription_url(target):
        print(f"subscription not found: {target!r}", file=sys.stderr)
        return 1
    remaining = config.subscription_urls()
    if remaining:
        print(f"removed; {len(remaining)} subscription(s) remain -> {config.SETTINGS_FILE}")
    else:
        print(f"removed; no subscriptions left (will use built-in default) -> {config.SETTINGS_FILE}")
    return 0


def _cmd_setup_path(_args: argparse.Namespace) -> int:
    for line in pathsetup.setup_path():
        print(line)
    # Don't auto-redo on the next bring-up: the user has configured it explicitly.
    try:
        s = config.get_settings()
        s["path_setup_done"] = True
        config.save_settings(s)
    except Exception:  # noqa: BLE001
        pass
    return 0


def _cmd_update(_args: argparse.Namespace) -> int:
    api.reconfigure()
    path = config.update_subscription()
    urls = config.subscription_urls()
    if len(urls) > 1:
        print(f"merged {len(urls)} subscription sources; managed config rebuilt at {path}")
    else:
        print(f"subscription refreshed; managed config rebuilt at {path}")
    if config.opus_whitelist() is not None and core.core_running():
        ok = daemon.refresh_opus_whitelist(incremental=True)
        print(f"opus whitelist updated (incremental): {len(ok)} nodes")
    if core.core_running():
        print("note: restart the core to apply (clashpilot down && clashpilot up)")
    return 0


def _cmd_whitelist(args: argparse.Namespace) -> int:
    api.reconfigure()
    if getattr(args, "refresh", False):
        if not core.core_running():
            print("error: core is not running -- start clashpilot first (clashpilot up)", file=sys.stderr)
            return 1
        ok = daemon.refresh_opus_whitelist(full=True)
        meta = config.opus_whitelist_meta()
        print(f"opus whitelist: {len(ok)} nodes saved -> {config.SETTINGS_FILE}")
        print("  (full geo scan: exit country must be Anthropic-supported + Anthropic API reachable)")
        for name in ok:
            cc = meta.get(name, "?")
            print(_console_safe(f"  + {name} [{cc}]"))
        if not ok:
            print("warning: no Opus-region nodes found; try other subscription nodes", file=sys.stderr)
            return 1
        return 0

    wl = config.opus_whitelist()
    meta = config.opus_whitelist_meta()
    if wl is None:
        print("opus whitelist: filtering disabled")
        print("  enable:  runs by default on `clp up`; or export CLASHPILOT_OPUS_WHITELIST=1")
        return 0
    if not wl:
        print("opus whitelist: filtering active, no nodes scanned yet")
        print("  scan nodes: clashpilot whitelist --refresh  (or start `clp up` to auto-scan)")
        return 0
    print(f"opus whitelist: {len(wl)} nodes (Opus-region filtering active)")
    for name in wl:
        cc = meta.get(name, "?")
        print(_console_safe(f"  {name} [{cc}]"))
    return 0


def _prog_name() -> str:
    """Reflect how the user invoked us (e.g. `clp`) in help/usage text."""
    name = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else "clashpilot"
    if name.endswith(".py") or name in ("__main__.py", "-c", ""):
        return "clashpilot"
    return name


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=_prog_name(), description="Standalone Clash/Mihomo client.")
    p.add_argument("--version", action="version", version=f"clashpilot {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="Core + routing + autoswitch in the foreground (Ctrl-C to stop).")
    up.add_argument(
        "--tun",
        action="store_true",
        help="route all traffic via mihomo TUN (no system proxy)",
    )
    up.add_argument(
        "--no-tun",
        action="store_true",
        help="force system-proxy mode even if TUN is saved in settings",
    )
    up.add_argument(
        "--persist-tun",
        action="store_true",
        help="save the TUN on/off choice from this run to settings",
    )
    up.add_argument(
        "--no-elevate",
        action="store_true",
        help="do not request UAC elevation on Windows (TUN may fail)",
    )
    up.add_argument(
        "-d", "--detach",
        action="store_true",
        help="start in background and return immediately",
    )
    up.set_defaults(func=_cmd_up)
    sub.add_parser("down", help="Stop the daemon/core and tear down routing.").set_defaults(func=_cmd_down)
    sub.add_parser("status", help="Show core / proxy / subscription status.").set_defaults(func=_cmd_status)

    scan = sub.add_parser(
        "scan",
        help="Probe nodes and rank by latency (no switch). Requires a running core.",
    )
    scan.add_argument(
        "-n", "--top",
        type=int,
        default=10,
        metavar="N",
        help="show top N nodes (default: 10)",
    )
    scan.add_argument(
        "--all",
        dest="all_nodes",
        action="store_true",
        help="scan every subscription node, not just the Opus whitelist pool",
    )
    scan.set_defaults(func=_cmd_scan)

    pin = sub.add_parser(
        "pin",
        help="Pin to a node (autoswitch only on failure); optionally add fallbacks in priority order.",
    )
    pin.add_argument(
        "nodes",
        nargs="+",
        help="primary node, then optional fallback nodes in priority order, e.g. 美国01 日本专线01",
    )
    pin.set_defaults(func=_cmd_pin)

    sub.add_parser("unpin", help="Clear the pinned node.").set_defaults(func=_cmd_unpin)

    sp = sub.add_parser("set-sub", help="Save your Clash/Mihomo subscription URL.")
    sp.add_argument("url")
    sp.add_argument(
        "--add",
        action="store_true",
        help="append this URL to existing subscriptions instead of replacing",
    )
    sp.set_defaults(func=_cmd_set_sub)

    sub.add_parser("list-sub", help="List configured subscription URLs.").set_defaults(func=_cmd_list_sub)

    rm = sub.add_parser("remove-sub", help="Remove a subscription URL from the list.")
    rm.add_argument("url", nargs="?", help="subscription URL to remove")
    rm.add_argument(
        "--index",
        type=int,
        metavar="N",
        help="remove the Nth subscription (see list-sub)",
    )
    rm.set_defaults(func=_cmd_remove_sub)

    sub.add_parser("update", help="Re-fetch the subscription and rebuild the config.").set_defaults(func=_cmd_update)

    wl = sub.add_parser(
        "whitelist",
        help="Show or refresh the Opus-region node whitelist used by autoswitch.",
    )
    wl.add_argument(
        "--refresh",
        action="store_true",
        help="probe exit country + Anthropic; keep Anthropic-supported regions only",
    )
    wl.set_defaults(func=_cmd_whitelist)
    sub.add_parser(
        "setup-path",
        help="Add the clashpilot/clp scripts dir to your user PATH (idempotent).",
    ).set_defaults(func=_cmd_setup_path)
    svc = sub.add_parser(
        "install-service",
        help="Run clashpilot in the background at login (restarts on crash).",
    )
    svc.add_argument(
        "--tun",
        action="store_true",
        help="persist TUN routing in settings before installing the service",
    )
    svc.add_argument(
        "--no-tun",
        action="store_true",
        help="persist system-proxy routing before installing the service",
    )
    svc.set_defaults(func=_cmd_install_service)
    sub.add_parser(
        "uninstall-service",
        help="Remove the login-launched background service.",
    ).set_defaults(func=_cmd_uninstall_service)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return args.func(args)
    except config.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except core.CoreError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
