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
import tkinter.font as tkfont
from tkinter import ttk
from typing import Any


FONT_SANS = ("Inter", "Cantarell", "DejaVu Sans", "Liberation Sans", "sans")
FONT_MONO = ("Inter Mono", "DejaVu Sans Mono", "Liberation Mono", "monospace")


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
        f"eta {fmt_eta(behind, blocks_per_min)}",
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
        self.root.minsize(640, 480)

        self.theme_name = config["theme"] if config.get("theme") in THEMES else "dark"
        self.theme = THEMES[self.theme_name]

        self.font_sans = self._resolve_font_family(FONT_SANS)
        self.font_mono = self._resolve_font_family(FONT_MONO)

        self.root.configure(bg=self.theme["bg_window"])

        self.always_on_top = tk.BooleanVar(value=False)

        self.last_full_height: int | None = None
        self.last_sample_time: float | None = None
        self.blocks_per_min: float | None = None

        self.last_uptime_check = 0.0
        self.cached_uptime: int | None = None
        self.refresh_in_flight = False

        # Hero zone state for redraws on theme change.
        self.hero_state_key: str = "starting"
        self.hero_progress: float = 0.0

        # Non-ttk widgets whose `bg` follows a theme token. Each entry is
        # (widget, theme_key); apply_theme iterates this list on swap.
        self._themed_widgets: list[tuple[tk.Widget, str]] = []

        self._setup_style()
        self._build_ui()
        self._fit_window_to_content()
        self._refresh()

    @staticmethod
    def _resolve_font_family(candidates: tuple[str, ...]) -> str:
        """Return the first available font family from candidates."""
        available = set(tkfont.families())
        for name in candidates:
            if name in available:
                return name
        return candidates[-1]

    def _track_themed(self, widget: tk.Widget, theme_key: str) -> tk.Widget:
        """Register a non-ttk widget whose background follows a theme token.

        ttk styles refresh themselves when _setup_style runs again. Plain
        tk.Frame and tk.Canvas widgets do not — they keep whatever bg they
        were created with — so we re-set them explicitly on theme change.
        """
        self._themed_widgets.append((widget, theme_key))
        return widget

    def apply_theme(self, name: str) -> None:
        """Swap the active palette and repaint every theme-dependent surface."""
        if name not in THEMES:
            return
        self.theme_name = name
        self.theme = THEMES[name]
        self.root.configure(bg=self.theme["bg_window"])

        self._setup_style()

        for widget, key in self._themed_widgets:
            try:
                widget.configure(bg=self.theme[key])
            except tk.TclError:
                pass

        # Hero state label foreground is set per-state in _render_hero, so
        # repaint it explicitly to match the new palette without waiting for
        # the next refresh tick.
        state_color = self.theme.get(
            f"state_{self.hero_state_key}", self.theme["state_starting"]
        )
        if hasattr(self, "hero_state_label"):
            self.hero_state_label.configure(foreground=state_color)

        self._redraw_ring(self.hero_state_key, self.hero_progress)

        if hasattr(self, "theme_toggle_button"):
            self.theme_toggle_button.configure(text=self._theme_toggle_label())

    def _theme_toggle_label(self) -> str:
        """The button shows the name of the theme it would switch to."""
        other = "light" if self.theme_name == "dark" else "dark"
        return other.capitalize()

    def _toggle_theme(self) -> None:
        next_name = "light" if self.theme_name == "dark" else "dark"
        self.apply_theme(next_name)

    def _setup_style(self) -> None:
        t = self.theme
        style = ttk.Style()
        style.theme_use("clam")

        style.configure("Root.TFrame", background=t["bg_window"])
        style.configure("Header.TFrame", background=t["bg_window"])
        style.configure("Hero.TFrame", background=t["bg_window"])
        style.configure("Card.TFrame", background=t["bg_card"], relief="flat")

        style.configure(
            "HeroState.TLabel",
            background=t["bg_window"],
            foreground=t["state_starting"],
            font=(self.font_sans, 26, "bold"),
        )
        style.configure(
            "HeroSubtitle.TLabel",
            background=t["bg_window"],
            foreground=t["text_secondary"],
            font=(self.font_sans, 12),
        )
        style.configure(
            "SectionLabel.TLabel",
            background=t["bg_window"],
            foreground=t["text_muted"],
            font=(self.font_sans, 10),
        )
        style.configure(
            "CardLabelV2.TLabel",
            background=t["bg_card"],
            foreground=t["text_muted"],
            font=(self.font_sans, 10),
        )
        style.configure(
            "CardValueMono.TLabel",
            background=t["bg_card"],
            foreground=t["text_primary"],
            font=(self.font_mono, 14),
        )
        style.configure(
            "CardValueText.TLabel",
            background=t["bg_card"],
            foreground=t["text_primary"],
            font=(self.font_sans, 14),
        )
        style.configure(
            "CardValueMuted.TLabel",
            background=t["bg_card"],
            foreground=t["text_muted"],
            font=(self.font_sans, 14),
        )
        style.configure(
            "CardValueAccent.TLabel",
            background=t["bg_card"],
            foreground=t["accent"],
            font=(self.font_sans, 14),
        )
        style.configure(
            "CardValueSlow.TLabel",
            background=t["bg_card"],
            foreground=t["state_slow"],
            font=(self.font_sans, 14),
        )
        style.configure(
            "Footer.TLabel",
            background=t["bg_window"],
            foreground=t["text_muted"],
            font=(self.font_sans, 11),
        )

        style.configure(
            "TCheckbutton",
            background=t["bg_window"],
            foreground=t["text_secondary"],
            font=(self.font_sans, 9),
        )
        style.configure(
            "ThemeToggle.TButton",
            background=t["bg_window"],
            foreground=t["text_secondary"],
            font=(self.font_sans, 9),
            borderwidth=0,
            focusthickness=0,
            padding=(6, 2),
            relief="flat",
        )
        style.map(
            "ThemeToggle.TButton",
            background=[("active", t["bg_card"]), ("pressed", t["bg_card"])],
            foreground=[("active", t["text_primary"])],
        )

    def _card(
        self,
        parent: tk.Widget,
        row: int,
        col: int,
        label: str,
        mono: bool = True,
        colspan: int = 1,
    ) -> ttk.Label:
        frame = ttk.Frame(parent, style="Card.TFrame", padding=(12, 10))
        frame.grid(row=row, column=col, columnspan=colspan, sticky="nsew", padx=4, pady=4)

        ttk.Label(frame, text=label, style="CardLabelV2.TLabel").pack(anchor="w")
        value_style = "CardValueMono.TLabel" if mono else "CardValueText.TLabel"
        value = ttk.Label(frame, text="—", style=value_style)
        value.pack(anchor="w", pady=(4, 0))
        return value

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, style="Root.TFrame", padding=10)
        outer.pack(fill="both", expand=True)
        self._outer = outer

        top = ttk.Frame(outer, style="Header.TFrame")
        top.pack(fill="x")

        self.theme_toggle_button = ttk.Button(
            top,
            text=self._theme_toggle_label(),
            style="ThemeToggle.TButton",
            command=self._toggle_theme,
            takefocus=False,
        )
        self.theme_toggle_button.pack(side="right", padx=(8, 0))

        ttk.Checkbutton(
            top,
            text="top",
            variable=self.always_on_top,
            command=self._toggle_top,
            style="TCheckbutton",
        ).pack(side="right")

        self._build_hero(outer)
        self._build_rust_grid(outer)
        self._build_reference(outer)
        self._build_footer(outer)

    def _build_rust_grid(self, parent: tk.Widget) -> None:
        """Rust node section: section label + 3x3 card grid."""
        ttk.Label(
            parent, text="Rust node", style="SectionLabel.TLabel",
        ).pack(anchor="w", pady=(16, 8))

        grid = ttk.Frame(parent, style="Root.TFrame")
        grid.pack(fill="x")
        for c in range(3):
            grid.columnconfigure(c, weight=1, uniform="rustcol")

        # Row 1: Full / Headers / Downloaded
        self.full_height = self._card(grid, 0, 0, "Full")
        self.headers = self._card(grid, 0, 1, "Headers")
        self.downloaded = self._card(grid, 0, 2, "Downloaded")

        # Row 2: Peers / Mempool / Uptime
        self.peers = self._card(grid, 1, 0, "Peers")
        self.mempool = self._card(grid, 1, 1, "Mempool")
        self.uptime = self._card(grid, 1, 2, "Uptime", mono=False)

        # Row 3: Mining / Extra index / Version
        self.mining = self._card(grid, 2, 0, "Mining", mono=False)
        self.extraindex = self._card(grid, 2, 1, "Extra index", mono=False)
        self.version = self._card(grid, 2, 2, "Version", mono=False)

    def _build_reference(self, parent: tk.Widget) -> None:
        """Reference node: full-width card matching the Rust card pattern.

        Label on top, value below. The value row is justified: the block
        height anchors the left edge (to compare with the Rust Full card
        directly above), the node name anchors the right. Layout:

            Reference
            block <height>                                   <name>

        No fixed pixel widths anywhere — the inline row uses fill=x so
        the empty middle absorbs slack as the window resizes.
        """
        card = ttk.Frame(parent, style="Card.TFrame", padding=(12, 10))
        card.pack(fill="x", expand=True, padx=4, pady=(8, 0))

        ttk.Label(card, text="Reference", style="CardLabelV2.TLabel").pack(anchor="w")

        inline = ttk.Frame(card, style="Card.TFrame")
        inline.pack(fill="x", expand=True, pady=(4, 0))

        # Pack the right side first so the name anchors the right edge
        # without being squeezed by anything on the left.
        self.ref_name_label = ttk.Label(inline, text="—", style="CardValueMuted.TLabel")
        self.ref_name_label.pack(side="right")

        self.ref_block_label = ttk.Label(inline, text="block ", style="CardValueText.TLabel")
        self.ref_block_label.pack(side="left")
        self.ref_height_label = ttk.Label(inline, text="—", style="CardValueMuted.TLabel")
        self.ref_height_label.pack(side="left")

        # Widgets that toggle visibility when ref goes stale.
        self._ref_live_widgets = (
            self.ref_block_label,
            self.ref_height_label,
            self.ref_name_label,
        )

        # Shown only when ref is stale; hidden initially.
        self.ref_stale_label = ttk.Label(
            inline, text="unavailable", style="CardValueSlow.TLabel",
        )

    def _build_footer(self, parent: tk.Widget) -> None:
        """Footer: small refresh-cadence status line."""
        seconds = self.config["refresh_ms"] / 1000.0
        text = f"auto-refresh {seconds:.0f}s"
        self.footer_label = ttk.Label(parent, text=text, style="Footer.TLabel")
        self.footer_label.pack(anchor="e", pady=(12, 0))

    def _build_hero(self, parent: tk.Widget) -> None:
        """Hero zone: large ring + state label + subtitle."""
        hero = ttk.Frame(parent, style="Hero.TFrame", padding=(14, 14, 14, 18))
        hero.pack(fill="x")

        self.hero_ring = tk.Canvas(
            hero,
            width=80,
            height=80,
            bg=self.theme["bg_window"],
            highlightthickness=0,
            bd=0,
        )
        self._track_themed(self.hero_ring, "bg_window")
        self.hero_ring.pack(side="left", padx=(0, 16))

        text_block = ttk.Frame(hero, style="Hero.TFrame")
        text_block.pack(side="left", fill="both", expand=True)

        self.hero_state_label = ttk.Label(
            text_block,
            text="Starting",
            style="HeroState.TLabel",
            anchor="w",
        )
        self.hero_state_label.pack(anchor="w", pady=(8, 0))

        self.hero_subtitle = ttk.Label(
            text_block,
            text="warming up",
            style="HeroSubtitle.TLabel",
            anchor="w",
        )
        self.hero_subtitle.pack(anchor="w", pady=(2, 0))

        # 0.5px separator below the hero block.
        self.hero_separator = tk.Frame(
            parent,
            height=1,
            bg=self.theme["border"],
            bd=0,
            highlightthickness=0,
        )
        self._track_themed(self.hero_separator, "border")
        self.hero_separator.pack(fill="x", pady=(0, 8))

        self._redraw_ring("starting", 0.0)

    def _redraw_ring(self, state_key: str, progress_percent: float) -> None:
        """Render the hero ring for the given state."""
        if not hasattr(self, "hero_ring"):
            return

        c = self.hero_ring
        t = self.theme
        c.configure(bg=t["bg_window"])
        c.delete("all")

        size = 80
        stroke = 4.5
        inset = stroke / 2 + 1
        x0, y0, x1, y1 = inset, inset, size - inset, size - inset

        # Outer track.
        c.create_oval(x0, y0, x1, y1, outline=t["track"], width=stroke)

        state_color = t.get(f"state_{state_key}", t["state_starting"])

        if state_key == "offline":
            center_text = "—"
            center_color = state_color
        else:
            extent = -360 * max(0.0, min(progress_percent / 100.0, 1.0))
            arc_color = t["text_muted"] if state_key == "starting" else state_color
            if state_key == "synced":
                # Drawing an arc of exactly 360 produces nothing in Tk; use an
                # oval for the closed ring instead.
                c.create_oval(x0, y0, x1, y1, outline=arc_color, width=stroke)
            elif extent != 0:
                c.create_arc(
                    x0, y0, x1, y1,
                    start=90,
                    extent=extent,
                    style=tk.ARC,
                    outline=arc_color,
                    width=stroke,
                )

            if state_key == "starting" and progress_percent <= 0:
                center_text = "—"
            else:
                center_text = f"{progress_percent:.1f}%"
            center_color = state_color

        c.create_text(
            size / 2,
            size / 2,
            text=center_text,
            fill=center_color,
            font=(self.font_mono, 13),
        )

        self.hero_state_key = state_key
        self.hero_progress = progress_percent

    def _fit_window_to_content(self) -> None:
        """Open the window at the size its packed children request.

        Without this, Tk picks an initial geometry that can clip the
        bottom-most widgets (Progress bars, Footer) on first paint. We let
        every child publish its requested size first, then set the geometry
        to that — floored at the minsize, capped at the screen size so we
        never open larger than the display.
        """
        self.root.update_idletasks()

        width = max(640, self.root.winfo_reqwidth())
        height = max(480, self.root.winfo_reqheight())

        screen_width = self.root.winfo_screenwidth() - 80
        screen_height = self.root.winfo_screenheight() - 80

        width = min(width, screen_width)
        height = min(height, screen_height)

        self.root.geometry(f"{width}x{height}")

    def _toggle_top(self) -> None:
        self.root.attributes("-topmost", bool(self.always_on_top.get()))

    def _set_label(self, label: ttk.Label, text: str, style: str = "CardValueMono.TLabel") -> None:
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
        ref_full = int(ref.get("fullHeight") or 0) if isinstance(ref, dict) else 0
        behind = max(ref_full - rust_full, 0) if ref_full else 0

        self._update_rate(rust_full)

        self._render_hero(rust, ref, uptime_seconds, ref_error)
        self._render_rust(rust, uptime_seconds, behind)
        self._render_reference(ref, ref_error)

    def _render_hero(
        self,
        rust: dict[str, Any] | None,
        ref: dict[str, Any] | None,
        uptime_seconds: int | None,
        ref_error: str | None,
    ) -> None:
        state_key, state_label, state_subtitle, progress = compute_health_state(
            rust,
            ref,
            uptime_seconds,
            ref_error,
            self.blocks_per_min,
            peers_min=self.config["peers_min"],
        )
        state_color = self.theme.get(f"state_{state_key}", self.theme["state_starting"])
        self.hero_state_label.configure(text=state_label, foreground=state_color)
        self.hero_subtitle.configure(text=state_subtitle)
        self._redraw_ring(state_key, progress)

    def _render_rust(self, rust: dict[str, Any], uptime_seconds: int | None, behind: int) -> None:
        rust_full = int(rust.get("fullHeight") or 0)
        rust_headers = int(rust.get("headersHeight") or 0)
        rust_downloaded = int(rust.get("downloadedHeight") or 0)
        peers = int(rust.get("peersCount") or 0)
        mempool = int(rust.get("unconfirmedCount") or 0)

        self._set_label(self.full_height, fmt_int(rust_full), "CardValueMono.TLabel")
        self._set_label(self.headers, fmt_int(rust_headers), "CardValueMono.TLabel")
        self._set_label(self.downloaded, fmt_int(rust_downloaded), "CardValueMono.TLabel")

        peers_style = "CardValueMono.TLabel" if peers > 0 else "CardValueSlow.TLabel"
        self._set_label(self.peers, fmt_int(peers), peers_style)
        self._set_label(self.mempool, fmt_int(mempool), "CardValueMono.TLabel")
        self._set_label(self.version, str(rust.get("appVersion", "—")), "CardValueText.TLabel")

        is_mining = bool(rust.get("isMining"))
        self._set_label(
            self.mining,
            "On" if is_mining else "Off",
            "CardValueAccent.TLabel" if is_mining else "CardValueMuted.TLabel",
        )
        extraindex_label, extraindex_value_style = self._extraindex_card_style(rust)
        self._set_label(self.extraindex, extraindex_label, extraindex_value_style)
        uptime_text, uptime_style = self._uptime_card_style(uptime_seconds)
        self._set_label(self.uptime, uptime_text, uptime_style)

    def _uptime_card_style(self, uptime_seconds: int | None) -> tuple[str, str]:
        """Pick the (text, style) for the uptime card.

        The ambiguous case is uptime_seconds=None: differentiate "SSH host not
        configured" (so we never tried) from "SSH host configured but the
        probe failed" (so something is wrong).
        """
        if uptime_seconds is not None:
            return (fmt_duration(uptime_seconds), "CardValueText.TLabel")
        if self.config["uptime_ssh_host"]:
            return ("probe failed", "CardValueSlow.TLabel")
        return ("needs ssh", "CardValueMuted.TLabel")

    @staticmethod
    def _extraindex_card_style(rust: dict[str, Any]) -> tuple[str, str]:
        """Map detect_extraindex output onto the new card value styles."""
        label, _ = detect_extraindex(rust)
        if label == "ON":
            return ("On", "CardValueAccent.TLabel")
        if label == "OFF":
            return ("Off", "CardValueMuted.TLabel")
        if label == "N/A":
            return ("n/a", "CardValueMuted.TLabel")
        return ("unknown", "CardValueMuted.TLabel")

    def _render_reference(self, ref: dict[str, Any] | None, ref_error: str | None) -> None:
        if not isinstance(ref, dict):
            for w in self._ref_live_widgets:
                w.pack_forget()
            self.ref_stale_label.configure(text="unavailable")
            if not self.ref_stale_label.winfo_ismapped():
                self.ref_stale_label.pack(side="left")
            return

        if self.ref_stale_label.winfo_ismapped():
            self.ref_stale_label.pack_forget()
        # Re-pack in the original justification: right side first, then left.
        if not self.ref_name_label.winfo_ismapped():
            self.ref_name_label.pack(side="right")
        if not self.ref_block_label.winfo_ismapped():
            self.ref_block_label.pack(side="left")
        if not self.ref_height_label.winfo_ismapped():
            self.ref_height_label.pack(side="left")

        self._set_ref_segment(
            self.ref_name_label,
            str(ref.get("name")) if ref.get("name") else "",
            mono=False,
        )

        height_raw = ref.get("fullHeight")
        try:
            height_text = fmt_int(height_raw) if height_raw is not None else ""
        except Exception:
            height_text = ""
        self._set_ref_segment(self.ref_height_label, height_text, mono=True)

    def _set_ref_segment(self, label: ttk.Label, text: str, mono: bool) -> None:
        """Drive a single inline reference segment, falling back to muted '—'."""
        if text:
            style = "CardValueMono.TLabel" if mono else "CardValueText.TLabel"
            label.configure(text=text, style=style)
        else:
            label.configure(text="—", style="CardValueMuted.TLabel")

    def _apply_refresh_error(self, error: str) -> None:
        self.refresh_in_flight = False
        offline_color = self.theme["state_offline"]
        self.hero_state_label.configure(text="Offline", foreground=offline_color)
        self.hero_subtitle.configure(text=f"refresh failed: {error}")
        self._redraw_ring("offline", 0.0)
        self._set_label(self.peers, "err", "CardValueSlow.TLabel")

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
