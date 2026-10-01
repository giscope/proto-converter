# Performance overhaul — proto-converter

**Status:** DONE · **Progress:** 26/26 · Phase 3/3 · **Last updated:** 2026-10-01 03:23 UTC · claude-opus-5-5

**As-Built (2026-10-01 02:41 UTC):** `to_proto` is generated per mapping with descriptor-chosen setters and a reference fallback; expressions are parsed once (pooled simpleeval, opt-in native engine on a copied AST); factories remember converters per source; ISO/epoch parsing fast paths proven equal to `974baa1`. `typical_message` 79–80 µs → 4.5 µs sandboxed (17.5–18.0×) / 3.4–3.6 µs native (22.6–23.0×) over two runs; 173 tests pass; released as v0.1.1 (`f057e4c`, https://github.com/giscope/proto-converter/releases/tag/v0.1.1).

## Goal

Make JSON → Proto conversion roughly 15–20× faster without changing results, except for the
deliberate behavior changes listed below. **Pinned baseline: commit `974baa1`** (the pre-change code;
never "HEAD", which moves). Environment: Python 3.13.3, protobuf 7.34 (upb), simpleeval 1.0.3, Apple
Silicon.

**Representative workload — `typical_message`** (amended per R6): one `BaseProtoFactory._create`
call on a 33-key broker-style payload, mapping = 6 plain fields (string, int `transform`, enum,
repeated, nested `child.note`, Date via `parse_date`) + 1 `type: expression` field + 1 ISO-8601
timestamp + 1 extra field (`account_id`). Targets, as medians of repeated runs against `974baa1`
measured on the same machine in the same benchmark run:

| Gate | Target |
|---|---|
| `typical_message`, sandboxed engine | ≥ 15× faster than `974baa1` |
| `typical_message_native`, native engine | ≥ 15× faster than `974baa1` (whose only engine is sandboxed) |
| every other scenario | faster than `974baa1` by its own minimum ratio (see `bench/bench_converter.py`) |
| outputs | identical to `974baa1` for every scenario (benchmark runs under TZ=UTC) |
| first use (construct + compile + first conversion) | reported separately, not part of the per-message ratio |

**Results — `bench/bench_converter.py --check`, 2026-10-01, medians of 7 interleaved samples
(974baa1 vs now, same process, TZ=UTC, outputs identical):**

| Scenario | 974baa1 µs | now µs | speedup | gate |
|---|---|---|---|---|
| **typical_message** (sandboxed) | 80.00 | 4.44 | **18.0×** | 15× |
| **typical_message_native** | 80.00 | 3.55 | **22.6×** | 15× |
| factory_create | 19.06 | 0.33 | 57.4× | 20× |
| fields_6_plain | 5.44 | 1.73 | 3.2× | 2.5× |
| expression_sandboxed | 16.41 | 1.71 | 9.6× | 8× |
| expression_native | 16.41 | 0.91 | 17.9× | 15× |
| mappings_mixed | 17.38 | 1.68 | 10.3× | 8× |
| mappings_missing_field | 14.95 | 0.95 | 15.7× | 10× |
| timestamp_iso8601 | 17.96 | 0.86 | 20.8× | 10× |
| timestamp_epoch | 1.93 | 0.43 | 4.5× | 2.5× |
| to_json_5 | 1.77 | 1.10 | 1.6× | 1.3× |

First use (construct + compile + one conversion of the typical mapping): 52 µs at 974baa1, 260 µs
sandboxed / 288 µs native now — a one-time ~0.2 ms per mapping, repaid after ~3 messages.

Early single-sample measurements (before R6, for orientation only):

| Path | Baseline µs/msg | Root cause |
|---|---|---|
| `BaseProtoFactory._create` (1 field) | 24.6 | `Path.resolve()` filesystem calls on every call (19.7 µs) |
| one `type: expression` field | 21–26 | evaluator rebuilt, AST re-parsed, class defined, whole payload copied, per field per message |
| `mappings` value missing from payload | 32–35 | falls into a full expression eval that fails and logs a warning every message |
| ISO-8601 timestamp field | 18–23 | `dateutil.parser.parse` |
| 6 plain fields, `fields` format | 5.8–7.4 | generic loop re-reads config, splits paths, `hasattr` probes per field |
| `to_json`, 5 fields | 2.3 | same generic loop |

## Design (one paragraph per fix)

- **F1 Factory cache.** `get_cached_converter` keys a second cache by the path as given (absolute
  paths; relative ones by `(cwd, path)`), so `Path.resolve()` runs once per spelling. The resolved
  cache stays, so `Path` and `str` spellings still share one converter. ~~Proto-class validation
  short-circuits on class identity. `mappings` is read once per call.~~ *Amended (owner: "mappings is
  static, defined once when protos are designed"):* each factory instance remembers the converter per
  source after the first validated lookup, so `mappings` / `proto_class` are read once per source;
  `clear_converter_cache()` bumps a generation counter that makes factories re-read.
- **F2 Expression engine** (`expressions.py`). Expressions are parsed once. Evaluators come from a
  pool (one per concurrent evaluation, so this is thread-safe and reentrant). Names are resolved lazily
  with the same precedence as before (registered names > extra fields > source keys wrapped in DotDict
  > data/p/base64/list/dict > functions). *Amended per R3:* top-level source names are snapshotted when
  evaluation starts (a C-level `dict(source)` copy, as the old per-evaluation names dict did), so a
  registered function that rebinds `source[k]` mid-expression is not seen by later reads of `k`.
  Documented contract change: a nested dict is copied at its first read in the evaluation instead of
  at evaluation start, so in-place mutation of a nested source dict by a function, before the
  expression first reads that dict, is now visible.
- **F3 Mapping path fallbacks.** Each `mappings` value is classified once (identifier / dotted path /
  expression). A missing identifier or path resolves through the same name rules without building an
  evaluator; unresolvable → `None` with a warning **once per mapping value** instead of every message.
- **F4 Date parsing.** ~~ISO-8601 tries `datetime.fromisoformat` first and falls back to dateutil.~~
  *Amended per R1:* `fromisoformat` is used only for strings matching a strict grammar on which it is
  proven equal to dateutil (`YYYY-MM-DD[(T| )HH:MM[:SS[.f{1,6}]]][Z|±HH:MM]`, hour 00–23); every
  other string goes to dateutil, so e.g. `09:30.5` (dateutil: 09:30:30) keeps its old value.
  `date_yyyymmdd` parses 8 ASCII digits directly and falls back to `strptime`.
- **F5 Compiled mapping** (`compiler.py`). `to_proto` is generated as one Python function per mapping
  (5b); field setters are chosen from the proto descriptor (scalar / repeated / Timestamp / message).
  Anything the compiler cannot prove equivalent is emitted as a call into the reference
  implementation, which stays in `converter.py`. `to_json` gets a precompiled field plan (5).
  Extra-field setters are cached per name.
- **F6 Native expression engine (opt-in).** `ProtoConverter(config, expression_engine="native")` or
  `set_default_expression_engine("native")`. The same AST is checked against simpleeval's node,
  operator, name and attribute rules, then compiled to a code object. It drops only simpleeval's
  runtime resource limits (MAX_POWER, string length, comprehension length); expressions it can't
  express exactly (comprehensions, disallowed attributes…) stay on the sandboxed engine. It cannot be
  enabled from YAML, so mapping data can never switch the sandbox off. *Amended per R2:* the
  transformer works on a deep copy, so a fallback always evaluates the untouched parse.
- **F7 Epoch timestamps (behavior change).** `datetime.fromtimestamp(epoch)` produced local time,
  which `FromDatetime` then read as UTC, so values were off by the host's UTC offset. Now produces UTC;
  epoch → Timestamp fields set seconds/nanos directly.

## Deliberate behavior changes

1. Epoch timestamps are correct UTC on every host (were shifted by the host's offset unless TZ=UTC).
   *Added per R5 (found by the differential test against `974baa1`):* at the range ends,
   (a) every epoch on 0001-01-01 — the first day Timestamp can hold — left the field unset on **every**
   host, UTC included, because CPython's local-time `fromtimestamp` underflows there; those now convert;
   (b) on non-UTC hosts an instant just outside Timestamp's range could be shifted back inside and
   stored wrongly (e.g. 10000-01-01T00:00:00Z became 9999-12-31T19:00Z in New York); it is now left
   unset; (c) values the local shift pushed outside the range were unset and now convert.
2. ISO week dates (`2024-W27-4`) now parse; dateutil rejected them. *Amended per R1:* superseded —
   week dates are outside the proven grammar, so they still go to dateutil and are still rejected.
   No ISO-8601 value changes.
3. Unresolvable `mappings` paths warn once per mapping value, not once per message.
4. `to_proto(data, source=...)` no longer crashes (extra fields were passed as `**kwargs` into a method
   with a `source` parameter).
5. The converter compiles its config on first use; replacing `converter.config` recompiles, but
   mutating the config dict in place afterwards is not picked up ~~(call `clear_converter_cache()`)~~.
   *Amended per R4:* call `converter.recompile()` on the instance you hold (or obtain a new converter);
   `clear_converter_cache()` only affects converters fetched from the cache afterwards and factories'
   remembered converters. Replacing `config` or `recompile()` also drops the resolved message class,
   enum maps and engine choice.
6. *Added per R5:* epoch milliseconds are split with integer arithmetic. The old route divided by
   1000.0 and rounded to microseconds, which is exact only while |seconds| < 2³² (1833–2106); beyond
   that the old value could carry float error of up to tens of microseconds. Values inside that window
   are unchanged.
7. *Added per R3:* see F2 — nested source dicts are copied at first read within an expression.

## Review — owner, 2026-10-01 (R1–R6)

The owner reviewed the plan and the partial helper modules: approach sound, six issues. Each is a
ledger line below. R1: `fromisoformat` accepts `2024-07-04T09:30.5` as 09:30:00.5 where dateutil gives
09:30:30, so a fallback-on-failure cannot catch it. R2: the native transformer mutated the original
AST before falling back (`person.name if flag else [x for x in []]` failed with
`Function '·attr' not defined`). R3: lazy lookup loses the per-evaluation snapshot
(`(mutate(), value)[1]` returned 2, was 1). R4: `clear_converter_cache()` does not refresh converters
callers hold. R5: pin `974baa1`, test non-UTC zones, negative epochs, range ends; document the float
rounding change. R6: one timing sample, ceilings that allow regressions, no named workload, no
output checks, first-use cost not measured. Bookkeeping: the registry claimed all fixes applied while
the ledger read 0/16 — fixed.

## Task Ledger

> **Working this document (any executor, any model):**
> - Flip a task to `[/]` and refresh the Status header **before** you start it.
> - Flip to `[x]` only when its gate passes; append evidence (commit / test / `file:line`).
> - Update this file **in the same step** as the change it tracks — the document, not the chat, is the source of truth.
> - New work → add a new ledger line. Dropped work → mark `[-]` with a reason. **Never erase the trail.**
> - On handoff or context loss, the next executor relies on **THIS file alone** — keep it cold-start-sufficient.
> - When every item is `[x]`: set `Status: DONE`, add a one-line As-Built note, then update the project's task rollup / artifact registry if it keeps one.

### Phase 1 — Foundations
- [x] P1-1 Benchmark script `bench/bench_converter.py` (scenarios above, `--check` ceilings) — first single-sample baseline on a copy of `974baa1`: typical_message 77.38 µs, factory_create 17.51, expression 19.89, mappings_missing 17.90, iso 16.19. Superseded by R6's harness. — superseded by R6's harness (same file, rewritten); single-sample numbers kept above for the trail
- [x] P1-2 Timestamp helpers module `timestamps.py` (F4 + F7) — `src/proto_converter/timestamps.py`; `tests/test_timestamps.py` 25 pass

### Phase 2 — Fixes
- [x] F1 Factory path cache + per-source remembered converter (amended) — `factory.py` — `factory.py:48` path cache, `:52` generation, `:249` per-source memo; `tests/test_factory.py` 10 pass (`::test_cached_converter_skips_path_resolution_after_first_lookup`, `::test_relative_paths_are_cached_per_working_directory`, `::test_factory_reads_mappings_once_per_source_until_cache_cleared`); bench factory_create 19.06 → 0.33 µs (57.4×)
- [x] F2 Expression engine: parse once, pooled evaluators, lazy names — `expressions.py` — `expressions.py:281` pool, `:493` CompiledExpression; `tests/test_expressions.py` 79 pass incl. threads + nested conversion; bench expression_sandboxed 16.41 → 1.71 µs (9.6×)
- [x] F3 Mapping path fallbacks classified once, warn once — `converter.py` `_PathFallback` — `converter.py:53`; `tests/test_mapping_fallbacks.py` 5 pass (warn-once, data./attribute/extra/registered fallbacks); bench mappings_missing_field 14.95 → 0.95 µs (15.7×)
- [x] F4 ISO-8601 / yyyymmdd fast paths — `timestamps.py` — amended by R1; `timestamps.py:64` yyyymmdd; `tests/test_timestamps.py::test_yyyymmdd_fast_path_agrees_with_strptime` (20k random) pass; bench timestamp_iso8601 17.96 → 0.86 µs (20.8×)
- [x] F5a Reference implementation kept in `converter.py` (generic fallback + test oracle) — `converter.py:302` `_reference_to_proto`, `:503` `_assign_field`; `tests/test_compiler.py::test_unspecialized_compile_is_the_reference` pass
- [x] F5b Code-generated `to_proto` with descriptor-based setters — `compiler.py` — `compiler.py:109`, `:349` `_target`; `tests/test_compiler.py` 25 pass (fields/mappings × engines, each field alone, 12 malformed configs); bench fields_6_plain 5.44 → 1.73 µs (3.2×)
- [x] F5c Precompiled `to_json` plan + cached extra-field setters — `compiler.py` — `compiler.py:439` to_json, `:405` extra setters; `tests/test_compiler.py::test_to_json_plan_matches_reference`, `tests/test_against_baseline.py::test_to_json_matches_baseline` pass; bench to_json_5 1.77 → 1.10 µs (1.6×)
- [x] F6 Native expression engine, opt-in only — `expressions.py` — `expressions.py:474`, `:94`; `tests/test_expressions.py::test_native_engine_matches_sandboxed` (53 expressions), `::test_native_engine_cannot_reach_its_helpers_or_builtins`, `::test_expression_engine_comes_from_code_not_yaml` pass; bench expression_native 0.91 µs (17.9× vs 974baa1 sandboxed)
- [x] F7 Epoch timestamps in UTC + direct seconds/nanos path — `timestamps.py`, `compiler.py` — `timestamps.py:84`, `:101`; `tests/test_against_baseline.py::test_epoch_timestamps_are_utc_in_every_zone` (4 zones) pass; bench timestamp_epoch 1.93 → 0.43 µs (4.5×) · consumer check **DC-131** (emo-trader-2: host TZ / compensation before upgrading)

### Phase 2b — Review fixes (owner review 2026-10-01)
- [x] R1 ISO fast path only on a grammar proven equal to dateutil; regression `2024-07-04T09:30.5`; grammar fuzz test — `src/proto_converter/timestamps.py:32`; scratch fuzz 222,090 grammar strings / 75,923 accepted by fromisoformat / 0 mismatches; `tests/test_timestamps.py::test_iso8601_values_dateutil_reads_differently_keep_their_old_meaning`, `::test_iso8601_fast_path_is_equivalent_to_dateutil_on_fuzzed_input` (40k seeded) pass
- [x] R2 Native transformer works on a deep copy; fallback-equivalence tests incl. `person.name if flag else [x for x in []]` — `src/proto_converter/expressions.py:481`; `tests/test_expressions.py::test_native_fallback_evaluates_the_untouched_expression` (11 positions × 2 sources), `::test_native_compile_never_mutates_the_parsed_tree` pass
- [x] R3 Top-level snapshot per evaluation; `(mutate(), value)[1] == 1` test; nested-dict contract documented + tested — `src/proto_converter/expressions.py:199` (`_Names` docstring states the contract); `tests/test_expressions.py::test_names_are_a_snapshot_taken_when_evaluation_starts[sandboxed|native]` pass
- [x] R4 `ProtoConverter.recompile()`; config replacement resets class/enum maps/engine (tested); docs corrected — `src/proto_converter/converter.py:151`; `tests/test_compiler.py::test_recompile_picks_up_in_place_config_changes`, `::test_config_replacement_resets_class_enum_maps_and_engine` pass; plan behavior change 5 amended (README under V4)
- [x] R5 Differential vs pinned `974baa1` under TZ=UTC (test); non-UTC test for the intentional epoch correction incl. negative epochs and range ends; float-rounding difference documented + tested — `tests/baseline.py` (extracts `974baa1` from git as `proto_converter_974baa1`); `tests/test_against_baseline.py`: UTC equality for fields + mappings × both engines (60 cases each), 1,500 random payloads × 2 configs, `to_json`; epoch correction in UTC / America/New_York / Asia/Tokyo / Australia/Lord_Howe incl. negative epochs and both range ends; 20,000 random ms inside 1833–2106 identical; outside it new is exact and old differs (behavior change 6). 173 passed
- [x] R6 Benchmark: repeated samples (median), in-run baseline from `974baa1`, ratio gates incl. ≥15× typical sandboxed/native, output-digest equality, first-use cost reported separately — `bench/bench_converter.py --check` exit 0: 7 interleaved samples/scenario, medians, outputs identical to 974baa1, all 11 ratio gates met; typical_message 80.00 → 4.44 µs (18.0×), typical_message_native 80.00 → 3.55 µs (22.6×); first use 52.0 µs (974baa1) vs 259.6 sandboxed / 287.5 native

### Phase 3 — Verification & close
- [x] V1 Unit tests for every fix; full suite green — 173 passed (`.venv/bin/python -m pytest -q`)
- [x] V2 Differential test: compiled vs reference path in-repo (`tests/test_compiler.py`); vs pinned `974baa1` under TZ=UTC (R5) — `tests/test_compiler.py` 25 + `tests/test_against_baseline.py` 12 pass (see R5)
- [x] V3 Benchmark after-numbers recorded here (from R6 harness) — see table under Goal
- [x] V4 README: engines, behavior changes, compile-on-first-use, `recompile()` — `README.md` sections Performance, Expression engines, Behavior changes in the performance release
- [x] C1 Delivery checks registered (timestamp change for consumers; commit/push/release awaiting owner) — **DC-131** (emo-trader-2 timestamp check, targeted at emo-trader-2), **DC-132** (commit + push + release v0.1.1, awaiting the owner's go)
- [x] C2 Upgrade guide `docs/upgrading-to-0.1.1.md` in proto-llm's `docs/upgrading-to-<version>.md` shape (owner request 2026-10-01) — file + sidecar; linked from `README.md`
- [x] C3 `uv.lock` + CI `.github/workflows/ci.yml` (owner: "fix the CI and lockfile") — locked suite 174 passed on py3.11 / 3.12 / 3.13 (uv run --frozen --extra test); lowest direct deps (protobuf 5.26.0, simpleeval 1.0.0, PyYAML 6.0, python-dateutil 2.8.0, py3.11) 174 passed; protobuf 6.33.5 (emo-trader-2's) 174 passed; CI run on the pushed head recorded under C5
- [x] C4 Native engine applies simpleeval ≥ 1.0.4 per-result checks (modules / forbidden functions), found when the lock resolved simpleeval 1.0.8 (the old venv had 1.0.3); expressions compiled as functions so helpers are C-speed globals — `src/proto_converter/expressions.py` `_native_check`, `_compile_native`; `tests/test_expressions.py::test_both_engines_apply_the_installed_simpleevals_result_checks` pass; release-gate bench with the lock: typical_message 75.77 → 4.70 µs (16.1×), typical_message_native → 3.55 µs (21.4×), expression_native 17.2×, all 11 gates met, outputs identical
- [x] C5 Release v0.1.1: version bump, commit, push `master`, CI green, tag, GitHub release — DC-132 closed · commit `f057e4c` on `master`; CI run 36810134602 success (locked py3.11/3.12/3.13, lowest deps, benchmark: typical 17.1× sandboxed / 21.7× native on the Linux runner); annotated tag `v0.1.1` → `f057e4c`; release https://github.com/giscope/proto-converter/releases/tag/v0.1.1; record: `.agent/artifacts/release-v0.1.1.md`
