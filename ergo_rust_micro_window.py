#!/usr/bin/env python3
"""
Tiny native micro-window for ergo-node-rust sync status.

No browser.
No terminal UI.
No external Python packages.
Requires python3-tk.

Defaults:
- Rust node API: http://127.0.0.1:9052
  This can be either a same-machine node or a local SSH tunnel.
- Reference node API: http://159.89.116.15:11088

Useful environment variables:
- ERGO_RUST_NODE_URL
- ERGO_RUST_REFERENCE_URL
- ERGO_RUST_REFERENCE_LABEL
- ERGO_RUST_REFRESH_MS
- ERGO_RUST_UPTIME_SSH_HOST
- Optional uptime source: ssh YOUR_SSH_HOST systemctl show ergo-node-rust.service

Note:
- ExtraIndex is shown only if the node /info response exposes extraIndex or extra_index.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
import urllib.request
import tkinter as tk
from tkinter import ttk
from typing import Any


# ExtraIndex is displayed only if the node API exposes extraIndex or extra_index.

# Service-uptime probing:
# - If config["uptime_ssh_host"] is set, probe the remote host over SSH.
# - Otherwise, try local systemd. That works when the node service runs on the
#   same machine as the display.


THEMES: dict[str, dict[str, str]] = {
    "dark": {
        "bg_window": "#1a1814",
        "bg_card": "#252220",
        "text_primary": "#f0eee5",
        "text_secondary": "#b8b0a0",
        "text_muted": "#7a7268",
        "accent": "#d97757",
        "state_synced": "#7ba87b",
        "state_syncing": "#d99c5a",
        "state_slow": "#c97870",
        "state_offline": "#5e5852",
        "state_starting": "#b8b0a0",
        "border": "#2a2724",
        "track": "#2d2a26",
    },
    "light": {
        "bg_window": "#faf7f2",
        "bg_card": "#f1ece2",
        "text_primary": "#2a2520",
        "text_secondary": "#5a5048",
        "text_muted": "#8a7e72",
        "accent": "#c45a3a",
        "state_synced": "#4f7a4f",
        "state_syncing": "#a8714a",
        "state_slow": "#9a4f3e",
        "state_offline": "#b0aa9e",
        "state_starting": "#5a5048",
        "border": "#ddd5c5",
        "track": "#e0d8c8",
    },
}


def fetch_json(base_url: str, path: str = "/info", timeout: int = 2) -> dict[str, Any]:
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fmt_int(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except Exception:
        return "—"


def fmt_rate(blocks_per_min: float | None) -> str:
    if blocks_per_min is None:
        return "warming"
    if blocks_per_min >= 1000:
        return f"{blocks_per_min:,.0f}/min"
    if blocks_per_min >= 100:
        return f"{blocks_per_min:,.1f}/min"
    return f"{blocks_per_min:,.2f}/min"


def fmt_eta(blocks_behind: int, blocks_per_min: float | None) -> str:
    if blocks_per_min is None or blocks_per_min <= 0:
        return "—"
    minutes = blocks_behind / blocks_per_min
    if minutes < 60:
        return f"{minutes:.0f}m"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f}h"
    days = hours / 24
    return f"{days:.1f}d"


def fmt_duration(seconds: int | None) -> str:
    if seconds is None or seconds < 0:
        return "unknown"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def parse_boolish(value: Any) -> bool | None:
    """Parse bool-like API values safely.

    Returns:
    - True for true/1/yes/on/enabled
    - False for false/0/no/off/disabled/empty
    - None for unknown/unparseable values
    """
    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return value != 0

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on", "enabled"}:
            return True
        if normalized in {"false", "0", "no", "n", "off", "disabled", ""}:
            return False

    return None


def compute_health_state(
    rust: dict[str, Any] | None,
    ref: dict[str, Any] | None,
    uptime_seconds: int | None,
    ref_error: str | None,
    blocks_per_min: float | None,
    peers_min: int = 1,
) -> tuple[str, str, str, float]:
    """Classify node health into one of five states.

    Returns (state_key, state_label, state_subtitle, progress_percent).
    state_key is one of: offline, starting, synced, slow, syncing.
    First match wins; the order matters.
    """
    # 1. offline — no data from the Rust API
    if not isinstance(rust, dict) or rust.get("fullHeight") is None:
        return ("offline", "Offline", "unable to reach API", 0.0)

    rust_full = int(rust.get("fullHeight") or 0)
    peers = int(rust.get("peersCount") or 0)

    ref_full = int(ref.get("fullHeight") or 0) if isinstance(ref, dict) else 0
    behind = max(ref_full - rust_full, 0) if ref_full else 0

    progress = 0.0
    if ref_full:
        progress = max(0.0, min(rust_full / ref_full * 100.0, 100.0))

    # 2. starting — too early to score
    if (uptime_seconds is not None and uptime_seconds < 60) or blocks_per_min is None:
        return ("starting", "Starting", f"warming up · {peers} peers", progress)

    # 3. slow — disconnected, or trailing the tip without speed to recover
    if peers < peers_min:
        return (
            "slow",
            "Slow",
            f"{peers} peers · not connected",
            progress,
        )
    if behind > 5 and blocks_per_min < 5:
        return (
            "slow",
            "Slow",
            f"behind {fmt_int(behind)} blocks · {fmt_rate(blocks_per_min)}",
            progress,
        )

    # 4. synced — within touching distance of the tip (requires a reference)
    if ref_full and behind <= 5:
        return (
            "synced",
            "Synced",
            f"block {fmt_int(rust_full)} · {peers} peers",
            100.0,
        )

    # 5. syncing — fallback
    return (
        "syncing",
        "Syncing",
        f"{progress:.2f}% to tip · eta {fmt_eta(behind, blocks_per_min)} · {peers} peers · {fmt_rate(blocks_per_min)}",
        progress,
    )


def detect_extraindex(info: dict[str, Any]) -> tuple[str, str]:
    for key in ("extraIndex", "extra_index"):
        if key in info:
            parsed = parse_boolish(info[key])
            if parsed is None:
                return ("UNKNOWN", "Blue.TLabel")
            return ("ON", "Rust.TLabel") if parsed else ("OFF", "Ok.TLabel")
    return ("N/A", "Blue.TLabel")


def _parse_systemd_uptime_output(output: str) -> int | None:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) < 2:
        return None

    active_enter_us = int(lines[0])
    uptime_seconds = float(lines[1])

    if active_enter_us <= 0:
        return None

    # systemd monotonic timestamp is in microseconds since boot.
    active_enter_seconds = active_enter_us / 1_000_000.0
    return int(max(0, uptime_seconds - active_enter_seconds))


def _systemd_uptime_command(service_name: str) -> str:
    return (
        f"systemctl show {service_name} "
        "--property=ActiveEnterTimestampMonotonic --value && "
        "cut -d' ' -f1 /proc/uptime"
    )


def get_service_uptime_seconds(ssh_host: str, service_name: str) -> int | None:
    """Return service uptime in seconds, or None if probing failed."""
    cmd_text = _systemd_uptime_command(service_name)
    if ssh_host:
        cmd = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5",
            ssh_host,
            cmd_text,
        ]
        timeout = 8
    else:
        cmd = ["bash", "-c", cmd_text]
        timeout = 2

    try:
        result = subprocess.run(
            cmd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
        return _parse_systemd_uptime_output(result.stdout)
    except Exception:
        return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ergo_rust_micro_window.py",
        description="Native sync monitor for ergo-node-rust.",
    )
    parser.add_argument(
        "--node-url",
        default=None,
        help="Rust node API URL. Overrides ERGO_RUST_NODE_URL.",
    )
    parser.add_argument(
        "--reference-url",
        default=None,
        help="Reference Ergo node API URL. Overrides ERGO_RUST_REFERENCE_URL.",
    )
    parser.add_argument(
        "--reference-label",
        default=None,
        help="Label shown in the UI for the reference source. Overrides ERGO_RUST_REFERENCE_LABEL.",
    )
    parser.add_argument(
        "--refresh-ms",
        type=int,
        default=None,
        help="Refresh interval in milliseconds. Overrides ERGO_RUST_REFRESH_MS.",
    )
    parser.add_argument(
        "--uptime-ssh-host",
        default=None,
        help="SSH host alias used to probe remote service uptime. Overrides ERGO_RUST_UPTIME_SSH_HOST.",
    )
    parser.add_argument(
        "--service-name",
        default=None,
        help="systemd unit name used for uptime probing. Overrides ERGO_RUST_SERVICE_NAME.",
    )
    parser.add_argument(
        "--theme",
        choices=sorted(THEMES.keys()),
        default=None,
        help="Initial color theme. Overrides ERGO_RUST_THEME.",
    )
    parser.add_argument(
        "--peers-min",
        type=int,
        default=None,
        help="Minimum healthy peer count. Below this the dashboard shows the Slow state. Overrides ERGO_RUST_PEERS_MIN.",
    )
    return parser.parse_args(argv)


def load_config(args: argparse.Namespace) -> dict[str, Any]:
    """Merge CLI args, environment variables, and built-in defaults.

    Precedence: CLI > env > default.
    """

    def pick(arg_val: Any, env_key: str, default: str) -> str:
        if arg_val is not None:
            return str(arg_val)
        env_val = os.environ.get(env_key, "")
        if env_val:
            return env_val
        return default

    node_url = pick(args.node_url, "ERGO_RUST_NODE_URL", "http://127.0.0.1:9052")
    reference_url = pick(
        args.reference_url, "ERGO_RUST_REFERENCE_URL", "http://159.89.116.15:11088"
    )
    derived_label = (
        reference_url.replace("http://", "").replace("https://", "").rstrip("/")
    )
    reference_label = pick(args.reference_label, "ERGO_RUST_REFERENCE_LABEL", derived_label)
    refresh_ms = int(pick(args.refresh_ms, "ERGO_RUST_REFRESH_MS", "2000"))
    uptime_ssh_host = pick(args.uptime_ssh_host, "ERGO_RUST_UPTIME_SSH_HOST", "")
    service_name = pick(args.service_name, "ERGO_RUST_SERVICE_NAME", "ergo-node-rust.service")
    theme = pick(args.theme, "ERGO_RUST_THEME", "dark").strip().lower()
    if theme not in THEMES:
        theme = "dark"
    peers_min = int(pick(args.peers_min, "ERGO_RUST_PEERS_MIN", "1"))
    if peers_min < 0:
        peers_min = 0

    return {
        "node_url": node_url,
        "reference_url": reference_url,
        "reference_label": reference_label,
        "refresh_ms": refresh_ms,
        "uptime_ssh_host": uptime_ssh_host,
        "service_name": service_name,
        "theme": theme,
        "peers_min": peers_min,
    }


class MicroWindow:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.root = tk.Tk()
        self.root.title("ergo-node-rust sync")
        self.root.minsize(660, 330)

        self.theme_name = config["theme"] if config.get("theme") in THEMES else "dark"
        self.theme = THEMES[self.theme_name]

        self.root.configure(bg=self.theme["bg_window"])

        self.always_on_top = tk.BooleanVar(value=False)

        self.last_full_height: int | None = None
        self.last_sample_time: float | None = None
        self.blocks_per_min: float | None = None

        self.last_uptime_check = 0.0
        self.cached_uptime: int | None = None
        self.refresh_in_flight = False

        self._setup_style()
        self._build_ui()
        self._fit_window_to_content()
        self._refresh()

    def _setup_style(self) -> None:
        t = self.theme
        style = ttk.Style()
        style.theme_use("clam")

        style.configure("Root.TFrame", background=t["bg_window"])
        style.configure("Header.TFrame", background=t["bg_window"])
        style.configure("Group.TFrame", background=t["bg_card"], relief="flat")
        style.configure("Card.TFrame", background=t["bg_card"], relief="flat")

        style.configure(
            "Title.TLabel",
            background=t["bg_window"],
            foreground=t["text_primary"],
            font=("Sans", 18, "bold"),
        )
        style.configure(
            "RustTitle.TLabel",
            background=t["bg_window"],
            foreground=t["accent"],
            font=("Sans", 18, "bold"),
        )
        style.configure(
            "Sub.TLabel",
            background=t["bg_window"],
            foreground=t["text_secondary"],
            font=("Sans", 9),
        )
        style.configure(
            "GroupTitle.TLabel",
            background=t["bg_card"],
            foreground=t["accent"],
            font=("Sans", 12, "bold"),
        )
        style.configure(
            "GroupSub.TLabel",
            background=t["bg_card"],
            foreground=t["text_secondary"],
            font=("Sans", 9),
        )
        style.configure(
            "StatusOk.TLabel",
            background=t["bg_window"],
            foreground=t["state_synced"],
            font=("Sans", 12, "bold"),
        )
        style.configure(
            "StatusBad.TLabel",
            background=t["bg_window"],
            foreground=t["state_slow"],
            font=("Sans", 12, "bold"),
        )
        style.configure(
            "CardLabel.TLabel",
            background=t["bg_card"],
            foreground=t["text_muted"],
            font=("Sans", 8),
        )
        style.configure(
            "CardValue.TLabel",
            background=t["bg_card"],
            foreground=t["text_primary"],
            font=("Sans", 14, "bold"),
        )
        style.configure(
            "CardValueSmall.TLabel",
            background=t["bg_card"],
            foreground=t["text_primary"],
            font=("Sans", 11, "bold"),
        )
        style.configure(
            "Ok.TLabel",
            background=t["bg_card"],
            foreground=t["state_synced"],
            font=("Sans", 14, "bold"),
        )
        style.configure(
            "Blue.TLabel",
            background=t["bg_card"],
            foreground=t["text_secondary"],
            font=("Sans", 14, "bold"),
        )
        style.configure(
            "BlueBig.TLabel",
            background=t["bg_card"],
            foreground=t["text_secondary"],
            font=("Sans", 17, "bold"),
        )
        style.configure(
            "BlueSmall.TLabel",
            background=t["bg_card"],
            foreground=t["text_secondary"],
            font=("Sans", 11, "bold"),
        )
        style.configure(
            "Rust.TLabel",
            background=t["bg_card"],
            foreground=t["accent"],
            font=("Sans", 14, "bold"),
        )
        style.configure(
            "Bad.TLabel",
            background=t["bg_card"],
            foreground=t["state_slow"],
            font=("Sans", 14, "bold"),
        )
        style.configure(
            "Horizontal.TProgressbar",
            troughcolor=t["track"],
            background=t["accent"],
            bordercolor=t["border"],
            lightcolor=t["accent"],
            darkcolor=t["accent"],
        )
        style.configure(
            "TCheckbutton",
            background=t["bg_window"],
            foreground=t["text_secondary"],
            font=("Sans", 9),
        )

    def _card(self, parent: tk.Widget, row: int, col: int, label: str, small: bool = False, colspan: int = 1) -> ttk.Label:
        frame = ttk.Frame(parent, style="Card.TFrame", padding=(9, 7))
        frame.grid(row=row, column=col, columnspan=colspan, sticky="nsew", padx=4, pady=4)

        ttk.Label(frame, text=label, style="CardLabel.TLabel").pack(anchor="w")
        value_style = "CardValueSmall.TLabel" if small else "CardValue.TLabel"
        value = ttk.Label(frame, text="—", style=value_style)
        value.pack(anchor="w", pady=(2, 0))
        return value

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, style="Root.TFrame", padding=10)
        outer.pack(fill="both", expand=True)

        top = ttk.Frame(outer, style="Header.TFrame")
        top.pack(fill="x")

        ttk.Checkbutton(
            top,
            text="top",
            variable=self.always_on_top,
            command=self._toggle_top,
            style="TCheckbutton",
        ).pack(side="right")

        self.status = ttk.Label(outer, text="starting…", style="Sub.TLabel")
        self.status.pack(anchor="w", pady=(2, 8))

        body = ttk.Frame(outer, style="Root.TFrame")
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)

        rust_group = ttk.Frame(body, style="Group.TFrame", padding=10)
        rust_group.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        rust_group.columnconfigure(0, weight=1)
        rust_group.columnconfigure(1, weight=1)
        rust_group.columnconfigure(2, weight=1)

        ref_group = ttk.Frame(body, style="Group.TFrame", padding=10)
        ref_group.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        ref_group.columnconfigure(0, weight=1)
        ref_group.columnconfigure(1, weight=1)

        ttk.Label(rust_group, text="RUST NODE", style="GroupTitle.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 4)
        )

        self.full_height = self._card(rust_group, 1, 0, "Full")
        self.headers = self._card(rust_group, 1, 1, "Headers")
        self.downloaded = self._card(rust_group, 1, 2, "Downloaded")

        self.peers = self._card(rust_group, 2, 0, "Peers")
        self.mempool = self._card(rust_group, 2, 1, "Mempool")
        self.uptime = self._card(rust_group, 2, 2, "Uptime", small=True)

        self.mining = self._card(rust_group, 3, 0, "Mining")
        self.extraindex = self._card(rust_group, 3, 1, "ExtraIndex")
        self.version = self._card(rust_group, 3, 2, "Version", small=True)

        self.rate = self._card(rust_group, 4, 0, "Speed", small=True)
        self.eta = self._card(rust_group, 4, 1, "ETA to tip", small=True)
        self.node_kind = self._card(rust_group, 4, 2, "Role", small=True)

        ttk.Label(ref_group, text="REFERENCE NODE", style="GroupTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )

        self.reference_height = self._card(ref_group, 1, 0, "Height", colspan=2)
        self.reference_source = self._card(ref_group, 2, 0, "Source", small=True, colspan=2)
        self.reference_version = self._card(ref_group, 3, 0, "Version", small=True, colspan=2)
        self.reference_name = self._card(ref_group, 4, 0, "Name", small=True, colspan=2)

        bars = ttk.Frame(outer, style="Root.TFrame")
        bars.pack(fill="x", pady=(8, 0))

        self.headers_progress_label = ttk.Label(bars, text="Known headers: —", style="Sub.TLabel")
        self.headers_progress_label.pack(anchor="w")
        self.headers_progress = ttk.Progressbar(
            bars,
            orient="horizontal",
            mode="determinate",
            maximum=100,
            style="Horizontal.TProgressbar",
        )
        self.headers_progress.pack(fill="x", pady=(2, 6))

        self.ref_progress_label = ttk.Label(bars, text="Network tip: —", style="Sub.TLabel")
        self.ref_progress_label.pack(anchor="w")
        self.ref_progress = ttk.Progressbar(
            bars,
            orient="horizontal",
            mode="determinate",
            maximum=100,
            style="Horizontal.TProgressbar",
        )
        self.ref_progress.pack(fill="x", pady=(2, 0))

    def _fit_window_to_content(self) -> None:
        """Open large enough to show all widgets without clipping."""
        self.root.update_idletasks()

        requested_width = self.root.winfo_reqwidth()
        requested_height = self.root.winfo_reqheight()

        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()

        width = min(max(720, requested_width + 20), screen_width - 80)
        height = min(max(520, requested_height + 20), screen_height - 80)

        self.root.geometry(f"{width}x{height}")

    def _toggle_top(self) -> None:
        self.root.attributes("-topmost", bool(self.always_on_top.get()))

    def _set_label(self, label: ttk.Label, text: str, style: str = "CardValue.TLabel") -> None:
        label.configure(text=text, style=style)

    def _update_rate(self, full_height: int) -> None:
        now = time.monotonic()
        if self.last_full_height is not None and self.last_sample_time is not None:
            delta_blocks = full_height - self.last_full_height
            delta_minutes = (now - self.last_sample_time) / 60.0
            # If the gap between samples is far longer than the refresh interval,
            # this sample reflects a stall recovery (e.g. machine sleep, network
            # drop). The block delta then represents catch-up, not steady-state
            # sync speed, so we reset the baseline instead of folding the burst
            # into the EWMA.
            if delta_minutes > 3 * (self.config["refresh_ms"] / 60000.0):
                self.last_full_height = full_height
                self.last_sample_time = now
                return
            if delta_blocks >= 0 and delta_minutes > 0:
                instant = delta_blocks / delta_minutes
                if self.blocks_per_min is None:
                    self.blocks_per_min = instant
                else:
                    self.blocks_per_min = (self.blocks_per_min * 0.70) + (instant * 0.30)

        self.last_full_height = full_height
        self.last_sample_time = now

    def _get_cached_uptime_in_worker(self) -> int | None:
        """Return cached service uptime.

        This may run in the background worker. It must not touch Tk widgets.
        If no SSH host is configured, local systemd is tried first.
        """
        now = time.monotonic()
        if now - self.last_uptime_check > 15:
            self.cached_uptime = get_service_uptime_seconds(
                self.config["uptime_ssh_host"],
                self.config["service_name"],
            )
            self.last_uptime_check = now
        return self.cached_uptime

    def _collect_snapshot(self) -> dict[str, Any]:
        """Collect data in a background thread.

        The Rust node API is required. The reference node is optional: if it
        fails, the Rust node display remains live and the reference panel is
        marked stale.
        """
        rust = fetch_json(self.config["node_url"])

        ref = None
        ref_error = None
        try:
            ref = fetch_json(self.config["reference_url"], timeout=5)
        except Exception as e:
            ref_error = str(e)

        return {
            "rust": rust,
            "ref": ref,
            "ref_error": ref_error,
            "uptime_seconds": self._get_cached_uptime_in_worker(),
        }

    def _apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.refresh_in_flight = False

        rust = snapshot["rust"]
        ref = snapshot.get("ref")
        ref_error = snapshot.get("ref_error")
        uptime_seconds = snapshot.get("uptime_seconds")

        rust_full = int(rust.get("fullHeight") or 0)
        rust_headers = int(rust.get("headersHeight") or 0)
        ref_full = int(ref.get("fullHeight") or 0) if isinstance(ref, dict) else 0
        behind = max(ref_full - rust_full, 0) if ref_full else 0

        self._update_rate(rust_full)

        self._render_status(rust, ref_error, uptime_seconds)
        self._render_rust(rust, uptime_seconds, behind)
        self._render_reference(ref, ref_error)
        self._render_progress(rust_full, rust_headers, ref_full)

    def _render_status(self, rust: dict[str, Any], ref_error: str | None, uptime_seconds: int | None) -> None:
        status_suffix = "REF STALE" if ref_error else "REF OK"
        network = str(rust.get("network", "—"))
        state_type = str(rust.get("stateType", "—")).upper()

        text = f"RUST OK · {network} · {state_type} · {status_suffix}"
        if uptime_seconds is None and not self.config["uptime_ssh_host"]:
            text += " · UPTIME ?"

        self.status.configure(
            text=text,
            style="StatusOk.TLabel" if not ref_error else "StatusBad.TLabel",
        )

    def _render_rust(self, rust: dict[str, Any], uptime_seconds: int | None, behind: int) -> None:
        rust_full = int(rust.get("fullHeight") or 0)
        rust_headers = int(rust.get("headersHeight") or 0)
        rust_downloaded = int(rust.get("downloadedHeight") or 0)
        peers = int(rust.get("peersCount") or 0)
        mempool = int(rust.get("unconfirmedCount") or 0)
        extraindex_label, extraindex_style = detect_extraindex(rust)

        self._set_label(self.full_height, fmt_int(rust_full))
        self._set_label(self.headers, fmt_int(rust_headers))
        self._set_label(self.downloaded, fmt_int(rust_downloaded))

        self._set_label(self.peers, fmt_int(peers), "Ok.TLabel" if peers > 0 else "Bad.TLabel")
        self._set_label(self.mempool, fmt_int(mempool), "Blue.TLabel" if mempool > 0 else "Ok.TLabel")
        self._set_label(self.version, str(rust.get("appVersion", "—")), "CardValueSmall.TLabel")

        self._set_label(
            self.mining,
            "ON" if rust.get("isMining") else "OFF",
            "Rust.TLabel" if rust.get("isMining") else "Ok.TLabel",
        )
        self._set_label(self.extraindex, extraindex_label, extraindex_style)
        self._set_label(self.uptime, fmt_duration(uptime_seconds) if uptime_seconds is not None else "needs ssh", "BlueSmall.TLabel")

        self._set_label(self.rate, fmt_rate(self.blocks_per_min), "BlueSmall.TLabel")
        self._set_label(self.eta, fmt_eta(behind, self.blocks_per_min), "BlueSmall.TLabel")
        self._set_label(self.node_kind, "mining" if rust.get("isMining") else "non-mining", "CardValueSmall.TLabel")

    def _render_reference(self, ref: dict[str, Any] | None, ref_error: str | None) -> None:
        ref_label = self.config["reference_label"]
        if isinstance(ref, dict):
            ref_full = int(ref.get("fullHeight") or 0)
            self._set_label(self.reference_height, fmt_int(ref_full), "Blue.TLabel")
            self._set_label(self.reference_source, ref_label, "BlueSmall.TLabel")
            self._set_label(self.reference_version, str(ref.get("appVersion", "—")), "BlueSmall.TLabel")
            self._set_label(self.reference_name, str(ref.get("name", "reference")), "BlueSmall.TLabel")
        else:
            self._set_label(self.reference_height, "STALE", "Bad.TLabel")
            self._set_label(self.reference_source, ref_label, "BlueSmall.TLabel")
            self._set_label(self.reference_version, "—", "Bad.TLabel")
            self._set_label(self.reference_name, ref_error or "unavailable", "Bad.TLabel")

    def _render_progress(self, rust_full: int, rust_headers: int, ref_full: int) -> None:
        progress_headers = (rust_full / rust_headers * 100.0) if rust_headers else 0.0
        progress_ref = (rust_full / ref_full * 100.0) if ref_full else 0.0

        progress_headers = max(0.0, min(progress_headers, 100.0))
        progress_ref = max(0.0, min(progress_ref, 100.0))

        self.headers_progress["value"] = progress_headers
        self.ref_progress["value"] = progress_ref

        self.headers_progress_label.configure(text=f"Rust vs known headers: {progress_headers:.2f}%")
        self.ref_progress_label.configure(
            text=f"Rust vs network tip: {progress_ref:.2f}%" if ref_full else "Rust vs network tip: reference stale"
        )

    def _apply_refresh_error(self, error: str) -> None:
        self.refresh_in_flight = False
        self.status.configure(text=f"STALE · Rust node refresh failed: {error}", style="StatusBad.TLabel")
        self._set_label(self.peers, "ERR", "Bad.TLabel")

    def _refresh_worker(self) -> None:
        try:
            snapshot = self._collect_snapshot()
        except Exception as e:
            self.root.after(0, lambda: self._apply_refresh_error(str(e)))
            return

        self.root.after(0, lambda: self._apply_snapshot(snapshot))

    def _refresh(self) -> None:
        if not self.refresh_in_flight:
            self.refresh_in_flight = True
            threading.Thread(target=self._refresh_worker, daemon=True).start()

        self.root.after(self.config["refresh_ms"], self._refresh)

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    args = parse_args()
    config = load_config(args)
    MicroWindow(config).run()


if __name__ == "__main__":
    main()
