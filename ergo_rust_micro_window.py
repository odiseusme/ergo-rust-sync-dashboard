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

import json
import os
import subprocess
import threading
import time
import urllib.request
import tkinter as tk
from tkinter import ttk
from typing import Any


# Runtime config. Defaults support a local dashboard reading a local/tunneled node.
NODE_URL = os.environ.get("ERGO_RUST_NODE_URL", "http://127.0.0.1:9052")
REFERENCE_URL = os.environ.get("ERGO_RUST_REFERENCE_URL", "http://159.89.116.15:11088")
REFERENCE_SOURCE_LABEL = os.environ.get(
    "ERGO_RUST_REFERENCE_LABEL",
    REFERENCE_URL.replace("http://", "").replace("https://", "").rstrip("/"),
)

REFRESH_MS = int(os.environ.get("ERGO_RUST_REFRESH_MS", "2000"))

# ExtraIndex is displayed only if the node API exposes extraIndex or extra_index.

# Optional: used to display Rust service uptime.
# If empty, the display first tries local systemd, which works when the node
# service runs on the same machine as the display.
# For a remote node reached through an SSH tunnel, set:
#   ERGO_RUST_UPTIME_SSH_HOST=your-host ./ergo_rust_micro_window.py
SSH_HOST_FOR_UPTIME = os.environ.get("ERGO_RUST_UPTIME_SSH_HOST", "")
SYSTEMD_SERVICE_NAME = os.environ.get("ERGO_RUST_SERVICE_NAME", "ergo-node-rust.service")


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


def detect_extraindex(info: dict[str, Any]) -> str:
    for key in ("extraIndex", "extra_index"):
        if key in info:
            parsed = parse_boolish(info[key])
            if parsed is None:
                return "UNKNOWN"
            return "ON" if parsed else "OFF"
    return "N/A"


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


def _systemd_uptime_command() -> str:
    return (
        f"systemctl show {SYSTEMD_SERVICE_NAME} "
        "--property=ActiveEnterTimestampMonotonic --value && "
        "cut -d' ' -f1 /proc/uptime"
    )


def get_service_uptime_seconds() -> int | None:
    """Return service uptime in seconds.

    Behavior:
    - If ERGO_RUST_UPTIME_SSH_HOST is set, probe the remote host over SSH.
    - If it is not set, try local systemd. This works when the display and node
      service run on the same machine.
    - If local probing fails, return None; the UI renders that as "needs ssh".
    """
    if SSH_HOST_FOR_UPTIME:
        cmd = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5",
            SSH_HOST_FOR_UPTIME,
            _systemd_uptime_command(),
        ]
    else:
        cmd = ["bash", "-c", _systemd_uptime_command()]

    try:
        result = subprocess.run(
            cmd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=8 if SSH_HOST_FOR_UPTIME else 2,
        )
        return _parse_systemd_uptime_output(result.stdout)
    except Exception:
        return None


class MicroWindow:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("ergo-node-rust sync")
        self.root.geometry("700x360")
        self.root.minsize(660, 330)

        self.colors = {
            "bg": "#5a2413",        # reddish rust background
            "bg2": "#3d170c",
            "group": "#1a0f0b",
            "card": "#25140d",
            "line": "#8f4a24",
            "rust": "#ff8a3d",
            "rust2": "#d7692f",
            "text": "#f9ead8",
            "muted": "#c7a891",
            "ok": "#2ff08f",
            "blue": "#48b8ff",
            "bad": "#ff5264",
            "bar_bg": "#120b08",
        }

        self.root.configure(bg=self.colors["bg"])

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
        c = self.colors
        style = ttk.Style()
        style.theme_use("clam")

        style.configure("Root.TFrame", background=c["bg"])
        style.configure("Header.TFrame", background=c["bg"])
        style.configure("Group.TFrame", background=c["group"], relief="flat")
        style.configure("Card.TFrame", background=c["card"], relief="flat")

        style.configure(
            "Title.TLabel",
            background=c["bg"],
            foreground=c["text"],
            font=("Sans", 18, "bold"),
        )
        style.configure(
            "RustTitle.TLabel",
            background=c["bg"],
            foreground=c["rust"],
            font=("Sans", 18, "bold"),
        )
        style.configure(
            "Sub.TLabel",
            background=c["bg"],
            foreground=c["muted"],
            font=("Sans", 9),
        )
        style.configure(
            "GroupTitle.TLabel",
            background=c["group"],
            foreground=c["rust"],
            font=("Sans", 12, "bold"),
        )
        style.configure(
            "GroupSub.TLabel",
            background=c["group"],
            foreground=c["muted"],
            font=("Sans", 9),
        )
        style.configure(
            "StatusOk.TLabel",
            background=c["bg"],
            foreground=c["ok"],
            font=("Sans", 12, "bold"),
        )
        style.configure(
            "StatusBad.TLabel",
            background=c["bg"],
            foreground=c["bad"],
            font=("Sans", 12, "bold"),
        )
        style.configure(
            "CardLabel.TLabel",
            background=c["card"],
            foreground=c["muted"],
            font=("Sans", 8),
        )
        style.configure(
            "CardValue.TLabel",
            background=c["card"],
            foreground=c["text"],
            font=("Sans", 14, "bold"),
        )
        style.configure(
            "CardValueSmall.TLabel",
            background=c["card"],
            foreground=c["text"],
            font=("Sans", 11, "bold"),
        )
        style.configure(
            "Ok.TLabel",
            background=c["card"],
            foreground=c["ok"],
            font=("Sans", 14, "bold"),
        )
        style.configure(
            "Blue.TLabel",
            background=c["card"],
            foreground=c["blue"],
            font=("Sans", 14, "bold"),
        )
        style.configure(
            "BlueBig.TLabel",
            background=c["card"],
            foreground=c["blue"],
            font=("Sans", 17, "bold"),
        )
        style.configure(
            "BlueSmall.TLabel",
            background=c["card"],
            foreground=c["blue"],
            font=("Sans", 11, "bold"),
        )
        style.configure(
            "Rust.TLabel",
            background=c["card"],
            foreground=c["rust"],
            font=("Sans", 14, "bold"),
        )
        style.configure(
            "Bad.TLabel",
            background=c["card"],
            foreground=c["bad"],
            font=("Sans", 14, "bold"),
        )
        style.configure(
            "Horizontal.TProgressbar",
            troughcolor=c["bar_bg"],
            background=c["blue"],
            bordercolor=c["line"],
            lightcolor=c["blue"],
            darkcolor=c["blue"],
        )
        style.configure(
            "TCheckbutton",
            background=c["bg"],
            foreground=c["muted"],
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
            if delta_minutes > 3 * (REFRESH_MS / 60000.0):
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
            self.cached_uptime = get_service_uptime_seconds()
            self.last_uptime_check = now
        return self.cached_uptime

    def _collect_snapshot(self) -> dict[str, Any]:
        """Collect data in a background thread.

        The Rust node API is required. The reference node is optional: if it
        fails, the Rust node display remains live and the reference panel is
        marked stale.
        """
        rust = fetch_json(NODE_URL)

        ref = None
        ref_error = None
        try:
            ref = fetch_json(REFERENCE_URL, timeout=5)
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
        rust_downloaded = int(rust.get("downloadedHeight") or 0)
        peers = int(rust.get("peersCount") or 0)
        mempool = int(rust.get("unconfirmedCount") or 0)
        extraindex_value = detect_extraindex(rust)

        self._update_rate(rust_full)

        ref_full = int(ref.get("fullHeight") or 0) if isinstance(ref, dict) else 0
        behind = max(ref_full - rust_full, 0) if ref_full else 0

        progress_headers = (rust_full / rust_headers * 100.0) if rust_headers else 0.0
        progress_ref = (rust_full / ref_full * 100.0) if ref_full else 0.0

        progress_headers = max(0.0, min(progress_headers, 100.0))
        progress_ref = max(0.0, min(progress_ref, 100.0))

        status_suffix = "REF STALE" if ref_error else "REF OK"
        network = str(rust.get("network", "—"))
        state_type = str(rust.get("stateType", "—")).upper()

        self.status.configure(
            text=f"RUST OK · {network} · {state_type} · {status_suffix}",
            style="StatusOk.TLabel" if not ref_error else "StatusBad.TLabel",
        )

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
        self._set_label(
            self.extraindex,
            extraindex_value,
            "Rust.TLabel" if extraindex_value == "ON" else ("Ok.TLabel" if extraindex_value == "OFF" else "Blue.TLabel"),
        )
        self._set_label(self.uptime, fmt_duration(uptime_seconds) if uptime_seconds is not None else "needs ssh", "BlueSmall.TLabel")

        self._set_label(self.rate, fmt_rate(self.blocks_per_min), "BlueSmall.TLabel")
        self._set_label(self.eta, fmt_eta(behind, self.blocks_per_min), "BlueSmall.TLabel")
        self._set_label(self.node_kind, "mining" if rust.get("isMining") else "non-mining", "CardValueSmall.TLabel")

        if isinstance(ref, dict):
            self._set_label(self.reference_height, fmt_int(ref_full), "Blue.TLabel")
            self._set_label(self.reference_source, REFERENCE_SOURCE_LABEL, "BlueSmall.TLabel")
            self._set_label(self.reference_version, str(ref.get("appVersion", "—")), "BlueSmall.TLabel")
            self._set_label(self.reference_name, str(ref.get("name", "reference")).replace("mainnet-", ""), "BlueSmall.TLabel")
        else:
            self._set_label(self.reference_height, "STALE", "Bad.TLabel")
            self._set_label(self.reference_source, REFERENCE_SOURCE_LABEL, "BlueSmall.TLabel")
            self._set_label(self.reference_version, "—", "Bad.TLabel")
            self._set_label(self.reference_name, ref_error or "unavailable", "Bad.TLabel")

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

        self.root.after(REFRESH_MS, self._refresh)

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    MicroWindow().run()


if __name__ == "__main__":
    main()
