#!/usr/bin/env python3
"""Pacing driver for the genie-ai-runtime 24h soak test (issue #113).

Emits one chat/voice-equivalent request every N seconds against a running
`genie-core`, and on the same tick polls `/api/health` to confirm the
`genie-ai-runtime` backend is reachable. Each tick is appended as one JSON
object to a JSONL telemetry file that `analyze_soak.py` later scores against
the M1 exit criteria.

Why genie-core and not genie-ai-runtime directly: a real voice cycle reaches
the runtime *through* genie-core (`POST /api/chat/stream`), and `/api/health`
is genie-core's own reachability view of the backend. Driving genie-core
exercises the same path the acceptance criteria are written against.

Stdlib only — this runs on the Python that ships with the Jetson Ubuntu 22.04
image, with no pip install. Designed to run for hours under systemd, so it
flushes every record, survives transient request failures, and shuts down
cleanly on SIGTERM/SIGINT.

Telemetry record schema (one JSON object per line):

    {
      "ts": 1716200000.123,          # epoch seconds
      "iso": "2026-05-20T12:00:00Z",
      "elapsed_s": 30.0,             # seconds since driver start
      "tick": 1,
      "conversation_id": "soak-1716200000-4242-1",
      "request": {
        "ok": true,
        "status": 200,
        "first_token_ms": 412.7,     # null if no token streamed
        "total_ms": 1873.4,
        "tokens": 64,
        "error": null
      },
      "health": {
        "ok": true,                  # status present, llm connected, backend matches
        "http_status": 200,
        "status": "ok",
        "llm": "connected",
        "llm_backend": "genie-ai-runtime",
        "mem_available_mb": 1234,
        "error": null
      },
      "runtime_rss_bytes": 1610612736 # MemoryCurrent of the runtime unit, or null
    }
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from http.client import HTTPConnection
from urllib.parse import urlsplit

# A small rotation of short, deterministic prompts. Varying the prompt keeps
# the runtime from trivially serving an identical cached completion every tick,
# while staying cheap enough to pace at a fixed interval.
DEFAULT_PROMPTS = [
    "What time is it?",
    "Give me a one-sentence status of the house.",
    "Say a short hello.",
    "What's the weather like, briefly?",
    "Reply with a single word: ok.",
]

# Substring that marks a health sample as "genie-ai-runtime reachable". The
# backend reports itself as "genie-ai-runtime" in /api/health.llm_backend.
DEFAULT_BACKEND_MATCH = "genie-ai-runtime"

_STOP = False


def _handle_signal(_signum, _frame):
    global _STOP
    _STOP = True


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def host_port(url: str) -> tuple[str, int]:
    parts = urlsplit(url if "://" in url else f"http://{url}")
    return parts.hostname or "127.0.0.1", parts.port or 3000


def stream_chat(url: str, prompt: str, conversation_id: str, timeout: float) -> dict:
    """POST /api/chat/stream and measure first-token + total latency.

    The endpoint replies with newline-delimited JSON events (start / token /
    replace / done / error). First-token latency is the wall time from request
    send to the first `token` or `replace` event — the moment the runtime's
    output becomes observable.

    `conversation_id` controls how much history rides along: a fresh id per tick
    keeps the prefill (and therefore first-token latency) comparable across the
    hour-1/12/24 buckets the soak is scored on; reusing one id lets history
    accumulate and would inflate later-hour latency for reasons unrelated to
    runtime health.
    """
    host, port = host_port(url)
    payload: dict = {"message": prompt}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    body = json.dumps(payload).encode("utf-8")
    result: dict = {
        "ok": False,
        "status": None,
        "first_token_ms": None,
        "total_ms": None,
        "tokens": 0,
        "error": None,
    }

    start = time.monotonic()
    conn = HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request(
            "POST",
            "/api/chat/stream",
            body=body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        resp = conn.getresponse()
        result["status"] = resp.status
        if resp.status != 200:
            resp.read()
            result["error"] = f"http {resp.status}"
            return result

        saw_error = None
        while True:
            line = resp.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype in ("token", "replace") and result["first_token_ms"] is None:
                result["first_token_ms"] = round((time.monotonic() - start) * 1000.0, 1)
            if etype == "token":
                result["tokens"] += 1
            elif etype == "error":
                saw_error = event.get("message", "stream error")
            elif etype == "done":
                break

        result["total_ms"] = round((time.monotonic() - start) * 1000.0, 1)
        if saw_error is not None:
            result["error"] = saw_error
        else:
            result["ok"] = result["first_token_ms"] is not None
            if not result["ok"]:
                result["error"] = "no token streamed"
    except (OSError, TimeoutError) as exc:
        result["total_ms"] = round((time.monotonic() - start) * 1000.0, 1)
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()
    return result


def poll_health(url: str, backend_match: str, timeout: float) -> dict:
    """GET /api/health and decide whether the runtime is reachable this tick."""
    host, port = host_port(url)
    out: dict = {
        "ok": False,
        "http_status": None,
        "status": None,
        "llm": None,
        "llm_backend": None,
        "mem_available_mb": None,
        "error": None,
    }
    conn = HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", "/api/health")
        resp = conn.getresponse()
        out["http_status"] = resp.status
        payload = resp.read()
        if resp.status != 200:
            out["error"] = f"http {resp.status}"
            return out
        data = json.loads(payload)
        out["status"] = data.get("status")
        out["llm"] = data.get("llm")
        out["llm_backend"] = data.get("llm_backend")
        out["mem_available_mb"] = data.get("mem_available_mb")
        # "Reachable" = genie-core answered, the LLM is connected, and the
        # backend serving it is genie-ai-runtime (not a fallback like llama.cpp).
        backend_ok = backend_match in (out["llm_backend"] or "")
        out["ok"] = out["llm"] == "connected" and backend_ok
        if not out["ok"]:
            out["error"] = f"llm={out['llm']} backend={out['llm_backend']}"
    except (OSError, TimeoutError, json.JSONDecodeError) as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()
    return out


def sample_runtime_rss(unit: str) -> int | None:
    """Read MemoryCurrent of the runtime systemd unit, in bytes.

    Returns None off-systemd (dev hosts) or when cgroup accounting is absent —
    the analyzer treats a column of nulls as "RSS not captured" rather than a
    failure, and falls back to tegrastats RAM for the plateau check.
    """
    try:
        proc = subprocess.run(
            ["systemctl", "show", unit, "--property=MemoryCurrent", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = proc.stdout.strip()
    if not value or not value.isdigit():
        # systemd reports "[not set]" as the sentinel u64::MAX when unaccounted.
        return None
    rss = int(value)
    if rss >= 2**63:
        return None
    return rss


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--core-url",
        default="http://127.0.0.1:3000",
        help="genie-core base URL (default: http://127.0.0.1:3000)",
    )
    parser.add_argument(
        "--interval", type=float, default=30.0, help="seconds between ticks (default: 30)"
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="total run seconds; 0 runs until SIGTERM/SIGINT (default: 0)",
    )
    parser.add_argument(
        "--out", default="telemetry.jsonl", help="telemetry JSONL output path"
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="fixed prompt to send each tick (default: rotate a small built-in set)",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=60.0,
        help="per-request timeout in seconds (default: 60)",
    )
    parser.add_argument(
        "--runtime-unit",
        default="genie-ai-runtime.service",
        help="systemd unit whose MemoryCurrent is sampled as runtime RSS",
    )
    parser.add_argument(
        "--backend-match",
        default=DEFAULT_BACKEND_MATCH,
        help='substring required in /api/health.llm_backend (default: "genie-ai-runtime")',
    )
    parser.add_argument(
        "--conversation",
        choices=["fresh", "shared"],
        default="fresh",
        help="fresh = new conversation id per tick, keeping prefill/TTFT "
        "comparable across hours (default); shared = reuse one growing conversation",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    prompts = [args.prompt] if args.prompt else DEFAULT_PROMPTS
    # Unique per run so conversation ids never collide with a previous soak's
    # rows in genie-core's store.
    run_id = f"soak-{int(time.time())}-{os.getpid()}"
    start = time.monotonic()
    tick = 0

    print(
        f"soak driver: core={args.core_url} interval={args.interval}s "
        f"duration={'unbounded' if args.duration <= 0 else f'{args.duration}s'} "
        f"out={args.out}",
        file=sys.stderr,
        flush=True,
    )

    with open(args.out, "a", encoding="utf-8") as sink:
        while not _STOP:
            tick_start = time.monotonic()
            elapsed = tick_start - start
            if args.duration > 0 and elapsed >= args.duration:
                break
            tick += 1
            prompt = prompts[(tick - 1) % len(prompts)]
            conv_id = f"{run_id}-{tick}" if args.conversation == "fresh" else run_id

            request = stream_chat(args.core_url, prompt, conv_id, args.request_timeout)
            health = poll_health(args.core_url, args.backend_match, min(args.request_timeout, 10.0))
            rss = sample_runtime_rss(args.runtime_unit)

            record = {
                "ts": round(time.time(), 3),
                "iso": iso_now(),
                "elapsed_s": round(elapsed, 1),
                "tick": tick,
                "conversation_id": conv_id,
                "request": request,
                "health": health,
                "runtime_rss_bytes": rss,
            }
            sink.write(json.dumps(record) + "\n")
            sink.flush()

            if (tick % 10 == 0) or not request["ok"] or not health["ok"]:
                print(
                    f"tick {tick} t+{elapsed / 3600:.2f}h "
                    f"req={'ok' if request['ok'] else 'FAIL'}({request['first_token_ms']}ms) "
                    f"health={'ok' if health['ok'] else 'FAIL'} "
                    f"rss={rss}",
                    file=sys.stderr,
                    flush=True,
                )

            # Pace on a fixed grid: sleep the remainder of the interval, in
            # short slices so a SIGTERM during the gap stops us promptly.
            next_tick = tick_start + args.interval
            while not _STOP and time.monotonic() < next_tick:
                time.sleep(min(0.5, next_tick - time.monotonic()))

    print(f"soak driver: stopped after {tick} ticks", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
