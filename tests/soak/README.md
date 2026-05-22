# genie-ai-runtime 24h soak test

Harness for [issue #113](https://github.com/GeniePod/genie-claw/issues/113) —
the M1 exit criterion *"genie-ai-runtime v1 backend stable for 24h continuous
run"* on a Jetson Orin Nano Super 8 GB.

The soak proves the runtime stays healthy under continuous load: a pacing
driver emits a chat request every N seconds while `tegrastats`, `/api/health`,
and memory pressure are captured throughout. Crash, hang, OOM, or a silent
backend swap all count as failures.

## What's here

| File | Role |
| --- | --- |
| `genie-soak.sh` | Orchestrator — snapshots systemd state, runs `tegrastats`, drives the workload, scans dmesg/journal for OOM, then scores the run. Jetson-side. |
| `soak_driver.py` | Pacing driver — every N seconds POSTs `/api/chat/stream` (timing first-token latency) and polls `/api/health`; writes `telemetry.jsonl`. Stdlib only. |
| `analyze_soak.py` | Scores a run against all six acceptance criteria → `report.md` + `summary.json`; exits non-zero on any failure. `--self-check` runs offline. |
| `example/` | A **synthetic** fixture (not a real run) that documents the telemetry schema and lets CI exercise the analyzer without hardware. |
| `runs/` | Where live runs land (git-ignored). Commit a real run's artifacts deliberately. |

> **What this harness does and does not do.** The code here is the runnable
> soak harness. It does **not**, by itself, satisfy the issue — the criteria
> are verdicts a *real 24h run on a Jetson* produces. A maintainer runs
> `genie-soak.sh` on hardware and commits the resulting artifact (below).

## Running the soak (on the Jetson)

`genie-core` + `genie-ai-runtime` must already be up under systemd. Run under
`tmux`/`nohup` so an SSH drop doesn't kill a 24h run:

```bash
tmux new -s soak
tests/soak/genie-soak.sh --duration-h 24 --interval 30 \
    --budget-p50-ms <ALPHA_P50> --budget-p99-ms <ALPHA_P99>
```

Useful flags: `--core-url` (default `http://127.0.0.1:3000`), `--runtime-unit`
(default `genie-ai-runtime.service`), `--prompt`, `--tegrastats-interval-ms`,
`--out-dir`. A short dry run first is wise: `--duration-h 0.05 --interval 5`.

Each run writes to `tests/soak/runs/<UTC-timestamp>/`:

- `telemetry.jsonl` — per-tick request + health + RSS samples
- `tegrastats.log` — raw GPU/RAM/EMC trace
- `journal.txt` — dmesg + journal since run start (OOM scan input)
- `meta.json` — host, L4T version, runtime unit state, restart count baseline
- `report.md` / `summary.json` — the scored verdict

## How each acceptance criterion is scored

| Criterion | Measured by |
| --- | --- |
| 24h uptime, zero runtime restarts | `systemctl show <unit> -p NRestarts` delta (must be 0) + telemetry span ≥ target hours |
| Zero OOM kills | `journal.txt` scanned for `oom-kill` / `Out of memory` / `Killed process` markers |
| `/api/health` reachable ≥ 99% | fraction of ticks where `/api/health` reports `llm: connected` **and** `llm_backend` contains `genie-ai-runtime` |
| No monotonic memory growth | runtime RSS (`MemoryCurrent`) first-decile vs last-decile median + least-squares slope; falls back to `mem_available_mb` if RSS is unaccounted |
| First-token p50/p99 within budget at hrs 1/12/24 | first-token latency bucketed into ±30 min windows around each mark, compared to `--budget-p50-ms` / `--budget-p99-ms` |
| Soak script + telemetry artifact committed | this directory + the committed run artifact |

A criterion with no captured input (e.g. no journal, RSS unaccounted on a dev
host) is reported **N/A** and excluded from the pass/fail gate — it never
silently counts as a pass.

> **Why each tick uses a fresh conversation.** The driver sends a new
> `conversation_id` every tick by default (`--conversation fresh`). Reusing one
> conversation would let history accumulate, growing the prefill the runtime
> processes and inflating first-token latency at hours 12/24 for reasons
> unrelated to runtime health — a false latency regression on the very
> criterion this harness scores. Use `--conversation shared` only if you
> deliberately want the growing-context stress profile.

> **Latency budget.** `--budget-p50-ms` / `--budget-p99-ms` default to
> placeholders (1500 / 4000 ms). The repo does not yet pin a numeric alpha
> voice-latency budget — set these to the agreed M1 budget before treating a
> latency PASS as authoritative.

## Committing a real run

After a clean 24h run, copy its directory out of the git-ignored `runs/` and
commit it as the issue's telemetry artifact, e.g.:

```bash
cp -r tests/soak/runs/<timestamp> tests/soak/results/2026-05-20-orin-nano-24h
git add tests/soak/results/2026-05-20-orin-nano-24h
```

Drop `report.md` (and the headline latency/health numbers) into the PR's
**Real Behavior Proof** section. `tegrastats.log` can be large — keep it or
gzip it as the team prefers.

## Validating the harness without a Jetson

```bash
python3 tests/soak/analyze_soak.py --self-check   # scores example/, asserts verdicts
python3 tests/soak/example/make_fixture.py        # regenerate the synthetic fixture
make soak-selfcheck                               # same self-check via make
```

The Python tools are stdlib-only and run on **Python 3.9+** (annotations are
deferred via `from __future__ import annotations`, so the hardware-free
self-check works on the older interpreters some CI images still ship). A real
soak runs against the Jetson Ubuntu 22.04 image's Python 3.10.
