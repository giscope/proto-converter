# Release v0.1.1

**Status:** PUBLISHED · 2026-10-01 03:23 UTC

| | |
|---|---|
| Version | 0.1.1 (`pyproject.toml`, `uv.lock`); previous: 0.1.0 at `974baa1`, untagged |
| Commit to pin | `f057e4c` on `master` |
| Tag | `v0.1.1` (annotated) → `f057e4c` |
| GitHub release | https://github.com/giscope/proto-converter/releases/tag/v0.1.1 |
| CI | run 36810134602 — success: tests py3.11 / 3.12 / 3.13 (locked), lowest direct deps (py3.11), benchmark vs `974baa1` |
| Release gates (local) | 174 tests passed; `bench/bench_converter.py --check` exit 0, all 11 gates met, outputs identical to `974baa1` |
| Upgrade guide | `docs/upgrading-to-0.1.1.md` |
| Source plan | `.agent/artifacts/performance-overhaul-plan.md` (DONE) |

## What it ships

Compiled mappings, parse-once expressions with an opt-in native engine, factory caching, ISO-8601 /
epoch fast paths. Typical factory conversion: 75.8 → 4.7 µs sandboxed (16.1×) / 3.6 µs native (21.4×)
on Apple Silicon; 195 → 11.4 µs (17.1×) / 9.0 µs (21.7×) on the CI runner.

## Behavior changes

Epoch timestamps UTC on every host; epoch milliseconds exact outside 1833–2106; unresolvable mapping
values warn once; extra field `source=` works; nested payload dicts copied at first read in
expressions. Details in the upgrade guide.

## Open after release

- DC-131 — emo-trader-2: check host time zone / data converted under its `local` profile before it
  refreshes its proto-converter lock (it is pinned to `974baa1` via `branch = "master"`).
