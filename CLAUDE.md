# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`codexcomp` is a local loopback proxy (127.0.0.1:8787) that sits between the OpenAI Codex CLI and the upstream Responses API. It detects gpt-5.5's `518n − 2` reasoning-truncation fingerprint (516, 1034, 1552, … reasoning tokens — openai/codex#30364), drives the model to continue thinking, and folds all rounds into one complete downstream response. Codex is wired to it via the official top-level `openai_base_url` key — deliberately NOT a `[model_providers]` entry, because changing the provider id re-buckets session history and drops remote compaction/remote-control.

## Commands

```bash
uv sync                        # install deps into .venv
uv run python test_fold.py     # the only test: fold state-machine self-test → "ALL PASS"
uv run codexcomp               # run the proxy locally (foreground, 127.0.0.1:8787)
uv build                       # build sdist + wheel
```

There is no pytest/lint/typecheck setup — `test_fold.py` is a plain script with asserts, run it directly. Run it before any change to `fold.py`.

Release: push a `v*` tag; `.github/workflows/release.yml` builds and publishes to PyPI via Trusted Publishing (OIDC, no stored token). Bump `version` in `pyproject.toml` first.

## Architecture

Four small modules under `codexcomp/`, with one central seam:

- **`fold.py`** — the core: a transport-agnostic state machine. `fold(base_body, open_round)` consumes upstream events as dicts and yields downstream events as dicts; it knows nothing about SSE or WebSocket. Per round it classifies output items: `reasoning` items stream through live (with proxy-owned `sequence_number` and renumbered `output_index`), everything else (messages, tool calls) is **buffered** as tentative. On a `518n−2` terminal it replays the original input + accumulated reasoning items (incl. `encrypted_content`) + a `phase:"commentary"` "Continue thinking..." nudge as the next round's input; only the final clean round's buffered output is flushed. Constants `STEP`/`MIN_N`/`MAX_N`/`MAX_CONTINUE` bound the fold. The `DONE` sentinel object represents SSE `data: [DONE]` across the transport boundary.
- **`server.py`** — Starlette transports around `fold()`. Downstream: WebSocket `/v1/responses` first (Codex's `responses_websockets` protocol: `response.create` envelope frames, connection reused across turns), POST SSE fallback (request body may be zstd/gzip-compressed), plus transparent passthrough for everything else under `/v1/*` (e.g. `GET /v1/models`) and `/healthz`. Upstream is always plain SSE POST — `UpstreamRounds.open` is the `RoundOpener` handed to `fold()`. Upstream base comes from `CODEXCOMP_UPSTREAM_BASE` env (set by the CLI's `--upstream`).
- **`cli.py`** — argparse entry for both `codexcomp` (console) and `codexcompw` (Windows GUI-subsystem, windowless). `_bind_headless_streams()` exists because pythonw starts with `sys.stdout/stderr = None`, which would crash uvicorn at startup — don't remove it. A wired proxy must own its exact port: default behavior is fail-loudly if the port is busy; `--auto-port` is for interactive one-off runs only.
- **`service.py`** — strictly opt-in autostart: systemd user unit (Linux/WSL), launchd LaunchAgent (macOS), manual Startup-shortcut instructions only on Windows (no silent registration — AV heuristics). Installing the package never registers anything.

## Invariants to preserve

- **Auth passthrough only**: the `Authorization` header is forwarded untouched and never read, persisted, or logged. Keep it that way in any logging change.
- **Loopback only**: default bind is 127.0.0.1 and docs tell users to keep it there.
- **Clean rounds pass through byte-for-byte**; the fold path only engages on a detected truncation. Terminal events from a fold report single-response usage (input from round 1, reasoning summed), with the true cumulative cost under `metadata.proxy_billed_usage` and per-round breakdown under `metadata.proxy_rounds`.
- Upstream EOF without a terminal event, mid-stream errors, and failed continuation opens all end in a synthesized `response.incomplete` — never silently drop or fabricate a completed answer.
- `README.md` and `README.zh-CN.md` are maintained in parallel — user-visible changes go to both.

Mechanism credit (neteroster/CodexCont, MIT) is retained in the READMEs and the `fold.py` docstring. `LICENSE` stays pure MIT text with no appended notes.
