# ergo-rust-sync-dashboard

A small native Python/Tkinter sync monitor for `ergo-node-rust`. Shows Rust node sync status alongside a reference Ergo node - no browser required.

> **Status:** early. Code-quality pass and UI overhaul in progress.

## Requirements

Ubuntu/Debian:

    sudo apt update
    sudo apt install -y python3 python3-tk curl openssh-client

No external Python packages beyond `python3-tk`.

## Quick start

    cp .env.example .env
    # edit .env to match your node setup
    ./run_micro_window.sh

For guided setup (SSH tunnel mode, etc.):

    ./setup_dashboard.sh

## Command-line options

The dashboard accepts CLI flags that override `.env` values. Precedence is
CLI > environment variable > built-in default.

    --node-url URL          Rust node API URL (env: ERGO_RUST_NODE_URL)
    --reference-url URL     Reference Ergo node API URL (env: ERGO_RUST_REFERENCE_URL)
    --reference-label LBL   Label shown in the UI for the reference (env: ERGO_RUST_REFERENCE_LABEL)
    --refresh-ms MS         Refresh interval in milliseconds (env: ERGO_RUST_REFRESH_MS)
    --uptime-ssh-host HOST  SSH host for remote uptime probing (env: ERGO_RUST_UPTIME_SSH_HOST)
    --service-name NAME     systemd unit name for uptime probing (env: ERGO_RUST_SERVICE_NAME)
    --theme {dark,light}    Initial color theme (env: ERGO_RUST_THEME)
    --peers-min N           Minimum healthy peer count (env: ERGO_RUST_PEERS_MIN)

Use `-h` or `--help` to print the full list with descriptions.

## Themes

Two palettes ship: a warm-neutral `dark` (default) and a paper-toned `light`.
A small button in the top-right of the window cycles between them at runtime.

The toggle is session-only — it does not write back to `.env`. To persist a
preference, set `ERGO_RUST_THEME=light` in `.env` or pass `--theme light` on
the command line.

## Desktop launcher and autostart

`./setup_dashboard.sh` can optionally create:

- an XDG `.desktop` launcher in `~/.local/share/applications` so the
  dashboard appears in the system app menu
- a user systemd unit at `~/.config/systemd/user/ergo-rust-sync-dashboard.service`
  that auto-starts the dashboard on login

Both prompts default to "no" and are independent of the SSH-tunnel option.

## Configuration

See `.env.example` for supported environment variables.

## Suggested upstream placement

If integrated into the `ergo-node-rust` project, the natural home is `tools/micro-display/`.

## License

MIT - see [LICENSE](LICENSE).
