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

## Configuration

See `.env.example` for supported environment variables.

## Suggested upstream placement

If integrated into the `ergo-node-rust` project, the natural home is `tools/micro-display/`.

## License

MIT - see [LICENSE](LICENSE).
