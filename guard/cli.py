"""codex-516-guard CLI entry point (installed via [project.scripts]).

Usage:
  codex-516-guard [--host H] [--port P] [--upstream U] [--log-level L]   run the proxy
  codex-516-guard install-service   [same flags]   opt-in autostart for this platform
  codex-516-guard uninstall-service                remove the autostart entry
"""
from __future__ import annotations

import argparse
import logging
import os

from . import service


def _add_run_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (default: 127.0.0.1; keep it loopback)")
    p.add_argument("--port", type=int, default=8787, help="bind port (default: 8787)")
    p.add_argument("--upstream", default=None,
                   help="upstream base URL (default: https://chatgpt.com/backend-api/codex)")
    p.add_argument("--log-level", default="info",
                   choices=["critical", "error", "warning", "info", "debug"])


def _serve(args) -> int:
    import uvicorn
    if args.upstream:
        os.environ["GUARD_UPSTREAM_BASE"] = args.upstream
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(levelname)s:%(name)s:%(message)s")
    uvicorn.run("guard.server:app", host=args.host, port=args.port,
                log_level=args.log_level)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="codex-516-guard",
        description=(
            "Local Responses proxy for Codex CLI: detects the gpt-5.5 518n-2 "
            "reasoning-truncation fingerprint, auto-continues thinking, and folds "
            "all rounds into one response. Wire Codex to it with the top-level "
            'config key: openai_base_url = "http://127.0.0.1:8787/v1". '
            "Run with no subcommand to start the proxy."
        ),
    )
    _add_run_flags(parser)
    sub = parser.add_subparsers(dest="cmd")

    p_install = sub.add_parser(
        "install-service",
        help="opt-in: register autostart (systemd user / launchd / scheduled task)")
    _add_run_flags(p_install)

    sub.add_parser("uninstall-service", help="remove the autostart entry")
    p_run = sub.add_parser("run", help="start the proxy (default when no subcommand)")
    _add_run_flags(p_run)

    args = parser.parse_args()

    if args.cmd == "install-service":
        raise SystemExit(service.install(args.host, args.port, args.upstream, args.log_level))
    if args.cmd == "uninstall-service":
        raise SystemExit(service.uninstall())
    raise SystemExit(_serve(args))


if __name__ == "__main__":
    main()
