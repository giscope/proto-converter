"""
Benchmark proto-converter against the pinned pre-overhaul commit (974baa1).

Usage:
    python bench/bench_converter.py            # medians, spreads and speedups per scenario
    python bench/bench_converter.py --check    # also exit 1 on a missed ratio or an output mismatch
    python bench/bench_converter.py --json results.json

Both versions run in this process on this machine: the baseline is extracted from git
(tests/baseline.py), so every ratio compares like with like. Each scenario is timed
`--repeat` times, alternating old and new, and the median is used. Before timing, each
scenario's output is compared between the two versions (under TZ=UTC, so the epoch
correction does not apply). First use — constructing a converter and converting once,
which now includes compiling the mapping — is reported separately.

The representative workload is `typical_message` (see the Living Plan,
.agent/artifacts/performance-overhaul-plan.md): its gate is >= 15x on the sandboxed
and on the native expression engine.
"""

import argparse
import gc
import hashlib
import json
import logging
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

os.environ["TZ"] = "UTC"
if hasattr(time, "tzset"):
    time.tzset()

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

import proto_converter as new_package  # noqa: E402
from baseline import BASELINE_COMMIT, load_baseline  # noqa: E402
from conftest import _build_test_module  # noqa: E402  (test proto built from descriptors)

# scenario -> (minimum speedup over 974baa1, baseline scenario it is compared with)
GATES = {
    "factory_create": (20.0, "factory_create"),
    "fields_6_plain": (2.5, "fields_6_plain"),
    "expression_sandboxed": (8.0, "expression_sandboxed"),
    "expression_native": (15.0, "expression_sandboxed"),
    "mappings_mixed": (8.0, "mappings_mixed"),
    "mappings_missing_field": (10.0, "mappings_missing_field"),
    "timestamp_iso8601": (10.0, "timestamp_iso8601"),
    "timestamp_epoch": (2.5, "timestamp_epoch"),
    "to_json_5": (1.3, "to_json_5"),
    "typical_message": (15.0, "typical_message"),
    "typical_message_native": (15.0, "typical_message"),
}

STATUS_MAP = {
    "status_map": {
        "active": "STATUS_ACTIVE",
        "inactive": "STATUS_INACTIVE",
        "_default": "STATUS_UNSPECIFIED",
    }
}

SOURCE = {
    "person": {"name": "Ada"},
    "metrics": {"count": "7"},
    "status": "active",
    "created_ms": 1720085400000,
    "created_iso": "2024-07-04T09:30:00Z",
    "tags": ["alpha", "beta"],
    "child": {"note": "nested"},
    "settlement": "2026-01-16",
    "first": "Ada",
    "last": "Lovelace",
    "qty": 3,
    # Real broker payloads are wide; the old expression path copied every key.
    **{f"extra_{i}": {"k": i, "v": [i, i + 1]} for i in range(20)},
}

PLAIN_FIELDS = [
    {"json_path": "person.name", "proto_field": "name"},
    {"json_path": "metrics.count", "proto_field": "count", "transform": "int"},
    {"json_path": "status", "proto_field": "status", "type": "enum", "type_map": "status_map"},
    {"json_path": "tags", "proto_field": "tags"},
    {"json_path": "child.note", "proto_field": "child.note"},
    {"json_path": "settlement", "proto_field": "settlement_date", "transform": "parse_date"},
]

EXPRESSION_FIELD = {"proto_field": "name", "type": "expression", "expression": "first + ' ' + last"}
ISO_FIELD = {"json_path": "created_iso", "proto_field": "created_at", "type": "timestamp", "format": "iso8601"}


def _converter(package, config, engine):
    if engine is None:
        return package.ProtoConverter(config)
    return package.ProtoConverter(config, expression_engine=engine)


def _factory(package, module, path):
    class Factory(package.BaseProtoFactory):
        # Built per call, like real factories (emo-trader returns BASE / "x.yaml")
        proto_class = property(lambda self: module.Sample)
        mappings = property(lambda self: {"source": path.parent / path.name})

    return Factory()


def build_scenarios(package, module, tmp: Path, native: bool):
    """name -> zero-argument callable converting one message with `package`."""
    sample = f"{module.__name__}.Sample"
    sandboxed = "sandboxed" if native else None
    scenarios = {}

    one = tmp / "one.yaml"
    one.write_text(yaml.safe_dump({"proto_class": sample, "fields": [{"json_path": "first", "proto_field": "name"}]}))
    factory = _factory(package, module, one)
    scenarios["factory_create"] = lambda: factory._create("source", SOURCE)

    plain = _converter(package, {"proto_class": sample, "type_maps": STATUS_MAP, "fields": PLAIN_FIELDS}, sandboxed)
    scenarios["fields_6_plain"] = lambda: plain.to_proto(SOURCE, account_id=1)

    expression = {"proto_class": sample, "fields": [EXPRESSION_FIELD]}
    expr_sandboxed = _converter(package, expression, sandboxed)
    scenarios["expression_sandboxed"] = lambda: expr_sandboxed.to_proto(SOURCE, account_id=1)
    if native:
        expr_native = _converter(package, expression, "native")
        scenarios["expression_native"] = lambda: expr_native.to_proto(SOURCE, account_id=1)

    mixed = _converter(package, {"proto_class": sample, "mappings": {
        "name": "person.name", "count": "qty * 2", "tags": "tags", "child": {"note": "child.note"}}}, sandboxed)
    scenarios["mappings_mixed"] = lambda: mixed.to_proto(SOURCE)

    missing = _converter(package, {"proto_class": sample, "mappings": {"name": "nickname"}}, sandboxed)
    scenarios["mappings_missing_field"] = lambda: missing.to_proto(SOURCE)

    iso = _converter(package, {"proto_class": sample, "fields": [ISO_FIELD]}, sandboxed)
    scenarios["timestamp_iso8601"] = lambda: iso.to_proto(SOURCE)

    epoch = _converter(package, {"proto_class": sample, "fields": [
        {"json_path": "created_ms", "proto_field": "created_at", "type": "timestamp"}]}, sandboxed)
    scenarios["timestamp_epoch"] = lambda: epoch.to_proto(SOURCE)

    to_json = _converter(package, {"proto_class": sample, "type_maps": STATUS_MAP, "fields": PLAIN_FIELDS[:5]}, sandboxed)
    message = to_json.to_proto(SOURCE)
    scenarios["to_json_5"] = lambda: to_json.to_json(message)

    typical_path = tmp / "typical.yaml"
    typical_path.write_text(yaml.safe_dump(typical_config(sample)))
    typical = _factory(package, module, typical_path)
    scenarios["typical_message"] = lambda: typical._create("source", SOURCE, account_id=1)
    if native:
        # Same workload through the factory, its converter compiled on the native engine
        native_path = tmp / "typical_native.yaml"
        native_path.write_text(typical_path.read_text())
        native_factory = _factory(package, module, native_path)
        package.set_default_expression_engine("native")
        try:
            native_factory._create("source", SOURCE, account_id=1)
        finally:
            package.set_default_expression_engine("sandboxed")
        scenarios["typical_message_native"] = lambda: native_factory._create("source", SOURCE, account_id=1)
    return scenarios


def typical_config(sample):
    return {"proto_class": sample, "type_maps": STATUS_MAP, "fields": PLAIN_FIELDS + [EXPRESSION_FIELD, ISO_FIELD]}


def _digest(result) -> str:
    if hasattr(result, "SerializeToString"):
        data = result.SerializeToString(deterministic=True)
    else:
        data = json.dumps(result, sort_keys=True, default=str).encode()
    return hashlib.sha256(data).hexdigest()[:16]


def _time(fn, n: int) -> float:
    """µs per call over n calls, GC paused as timeit does."""
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        start = time.perf_counter()
        for _ in range(n):
            fn()
        return (time.perf_counter() - start) / n * 1e6
    finally:
        if gc_was_enabled:
            gc.enable()


def _first_use(package, sample, engine, rounds: int) -> float:
    """µs to construct a converter for the typical mapping and convert one message."""
    samples = []
    for _ in range(rounds):
        start = time.perf_counter()
        converter = _converter(package, typical_config(sample), engine)
        converter.to_proto(SOURCE, account_id=1)
        samples.append((time.perf_counter() - start) * 1e6)
    return statistics.median(samples)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="exit 1 on a missed ratio or an output mismatch")
    parser.add_argument("-n", type=int, default=5000, help="calls per sample for the current code")
    parser.add_argument("--repeat", type=int, default=7, help="samples per scenario and version")
    parser.add_argument("--json", type=Path, help="write the results here")
    args = parser.parse_args()

    old_package = load_baseline()
    if old_package is None:
        print(f"Baseline {BASELINE_COMMIT} unavailable (no git history); nothing to compare against.")
        return 1

    logging.disable(logging.WARNING)  # 974baa1 logs a warning per missing mapping value
    module = _build_test_module()
    failures = []
    results = {"baseline": BASELINE_COMMIT, "python": sys.version.split()[0], "scenarios": {}}

    with tempfile.TemporaryDirectory() as tmp_new, tempfile.TemporaryDirectory() as tmp_old:
        new = build_scenarios(new_package, module, Path(tmp_new), native=True)
        old = build_scenarios(old_package, module, Path(tmp_old), native=False)

        # Outputs must match 974baa1 before any timing counts
        for name, fn in new.items():
            baseline_name = GATES[name][1]
            if _digest(fn()) != _digest(old[baseline_name]()):
                failures.append(f"{name}: output differs from {BASELINE_COMMIT}")

        samples = {name: ([], []) for name in new}
        for _ in range(args.repeat):
            for name, fn in new.items():
                samples[name][0].append(_time(fn, args.n))
                if name == GATES[name][1]:
                    samples[name][1].append(_time(old[name], max(args.n // 10, 200)))

        print(f"{'scenario':24s} {'974baa1 µs':>11s} {'now µs':>8s} {'spread':>7s} {'speedup':>8s} {'gate':>6s}")
        for name in new:
            minimum, baseline_name = GATES[name]
            now = statistics.median(samples[name][0])
            before = statistics.median(samples[baseline_name][1])
            spread = (max(samples[name][0]) - min(samples[name][0])) / now
            ratio = before / now
            ok = ratio >= minimum
            if not ok:
                failures.append(f"{name}: {ratio:.1f}x < {minimum}x")
            print(f"{name:24s} {before:11.2f} {now:8.2f} {spread:6.0%} {ratio:7.1f}x {minimum:5.1f}x{'' if ok else '  FAIL'}")
            results["scenarios"][name] = {"baseline_us": round(before, 3), "now_us": round(now, 3),
                                          "spread": round(spread, 3), "speedup": round(ratio, 2), "gate": minimum}

        sample = f"{module.__name__}.Sample"
        rounds = max(args.repeat * 15, 50)
        first_old = _first_use(old_package, sample, None, rounds)
        first_new = _first_use(new_package, sample, "sandboxed", rounds)
        first_native = _first_use(new_package, sample, "native", rounds)
        print(f"\nfirst use (construct + compile + 1 conversion, typical mapping, median of {rounds}):")
        print(f"  974baa1 {first_old:8.1f} µs   now sandboxed {first_new:8.1f} µs   now native {first_native:8.1f} µs")
        results["first_use_us"] = {"baseline": round(first_old, 1), "sandboxed": round(first_new, 1),
                                   "native": round(first_native, 1)}

    if args.json:
        args.json.write_text(json.dumps(results, indent=2) + "\n")
    if failures:
        print("\n" + "\n".join(failures))
        return 1 if args.check else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
