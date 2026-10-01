"""
Differential tests against the pinned pre-overhaul commit (see `baseline.py`).

Under TZ=UTC the current converter must produce exactly what `974baa1` produced for
every case here. The epoch-timestamp correction (wrong by the host's UTC offset
before) is tested separately in non-UTC zones, as is the documented float-rounding
difference for epoch milliseconds outside 1833–2106.
"""

import itertools
import logging
import os
import random
import time

import pytest

import proto_converter
from baseline import BASELINE_COMMIT, load_baseline
from test_compiler import EXTRAS, FIELDS, MAPPINGS, ORDER, PAYLOADS, TYPE_MAPS, _build_rich_module

old = load_baseline()
pytestmark = [
    pytest.mark.skipif(old is None, reason=f"git history with {BASELINE_COMMIT} unavailable"),
    pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset is POSIX-only"),
]


def _set_zone(zone):
    if zone is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = zone
    time.tzset()


@pytest.fixture
def zone():
    previous = os.environ.get("TZ")
    yield _set_zone
    _set_zone(previous)


@pytest.fixture(autouse=True)
def registrations(caplog):
    caplog.set_level(logging.CRITICAL)
    _build_rich_module()
    for package in (proto_converter, old):
        package.register_expression_name("SIDE_SELL_NAME", 2)
        package.register_expression_function("tag", lambda value: f"<{value}>")
        package.register_transform("shout", lambda value: str(value).upper())
    yield
    for package in (proto_converter, old):
        package.converter._expression_names.clear()
        package.converter._expression_functions.clear()
        package.TRANSFORMS.pop("shout", None)


def _outcome(fn):
    try:
        message = fn()
    except Exception as e:  # noqa: BLE001 - both versions must fail the same way
        return ("raised", type(e).__name__)
    return ("ok", message.SerializeToString(deterministic=True))


def _pair(config, engine="sandboxed"):
    return proto_converter.ProtoConverter(config, expression_engine=engine), old.ProtoConverter(config)


CONFIGS = {
    "fields": {"proto_class": ORDER, "type_maps": TYPE_MAPS, "fields": FIELDS + [
        {"json_path": "id", "proto_field": "note", "transform": "shout"},
        {"proto_field": "note", "type": "expression", "expression": "tag(id) + f'{px}'"}]},
    "mappings": {"proto_class": ORDER, "mappings": MAPPINGS},
}


@pytest.mark.parametrize("engine", ["sandboxed", "native"])
@pytest.mark.parametrize("name", list(CONFIGS))
def test_same_messages_as_baseline_under_utc(zone, name, engine):
    zone("UTC")
    new, before = _pair(CONFIGS[name], engine)
    for payload, extra in itertools.product(PAYLOADS, EXTRAS):
        assert _outcome(lambda: new.to_proto(payload, **extra)) == _outcome(
            lambda: before.to_proto(payload, **extra)), (name, payload, extra)


def _random_value(rng, depth=0):
    roll = rng.random()
    if roll < 0.12:
        return None
    if roll < 0.24:
        return rng.randint(-(2**31), 2**32 - 1)  # also epoch seconds/ms inside 1833–2106
    if roll < 0.32:
        return rng.random() * 1e3
    if roll < 0.40:
        return rng.choice([True, False])
    if roll < 0.52:
        return rng.choice(["buy", "sell", "x", "", "2024-07-04", "20260116", "2026-01-16",
                           "2024-07-04T09:30:00Z", "2024-07-04T09:30.5", "2024-W27-4", "12.5", "None"])
    if roll < 0.62:
        return "".join(rng.choice("abc-019T:. ") for _ in range(rng.randint(0, 12)))
    if roll < 0.80 and depth < 2:
        return {key: _random_value(rng, depth + 1) for key in rng.sample(["sym", "codes", "year", "month", "day", "name"], 3)}
    if depth < 2:
        return [_random_value(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    return rng.randint(0, 100)


def test_random_payloads_match_baseline_under_utc(zone):
    zone("UTC")
    rng = random.Random(974)
    keys = ["id", "px", "qty", "active", "side", "ts", "ts_s", "iso", "ymd", "d", "leg", "tags", "legs",
            "labels", "acct", "nickname", "dyear", "first"]
    pairs = {name: _pair(config) for name, config in CONFIGS.items()}
    for _ in range(1500):
        payload = {key: _random_value(rng) for key in rng.sample(keys, rng.randint(0, len(keys)))}
        extra = rng.choice(EXTRAS)
        for name, (new, before) in pairs.items():
            assert _outcome(lambda: new.to_proto(payload, **extra)) == _outcome(
                lambda: before.to_proto(payload, **extra)), (name, payload, extra)


def test_to_json_matches_baseline(zone):
    zone("UTC")
    fields = [f for f in FIELDS if f.get("json_path")] + [
        {"json_path": "out.created_s", "proto_field": "created_at", "unit": "s"},
        {"json_path": "out.side", "proto_field": "side", "type": "enum", "type_map": "side"}]
    new, before = _pair({"proto_class": ORDER, "type_maps": TYPE_MAPS, "fields": fields})
    for payload in PAYLOADS:
        try:
            message = new.to_proto(payload)
        except Exception:  # noqa: BLE001
            continue
        assert new.to_json(message) == before.to_json(message)


EPOCH_FIELDS = [
    {"json_path": "ms", "proto_field": "created_at", "type": "timestamp"},
    {"json_path": "s", "proto_field": "leg.opened_at", "type": "timestamp", "unit": "s"},
]
SECONDS_MIN, SECONDS_MAX = -62135596800, 253402300799
EPOCHS_MS = [1_720_085_400_123, 0, -1, -1500, -86_400_000, SECONDS_MIN * 1000, SECONDS_MAX * 1000 + 999,
             (SECONDS_MIN - 1) * 1000, (SECONDS_MAX + 1) * 1000]
EPOCHS_S = [1_720_085_400, 0, -1, -86_400, SECONDS_MIN, SECONDS_MAX, SECONDS_MIN - 1, SECONDS_MAX + 1]


def _expected_utc(epoch, unit_ms):
    seconds, millis = divmod(epoch, 1000) if unit_ms else (epoch, 0)
    if not SECONDS_MIN <= seconds <= SECONDS_MAX:
        return None
    return seconds, millis * 1_000_000


@pytest.mark.parametrize("host_zone", ["UTC", "America/New_York", "Asia/Tokyo", "Australia/Lord_Howe"])
def test_epoch_timestamps_are_utc_in_every_zone(zone, host_zone):
    zone(host_zone)
    new, before = _pair({"proto_class": ORDER, "fields": EPOCH_FIELDS})
    for ms, s in itertools.zip_longest(EPOCHS_MS, EPOCHS_S):
        payload = {key: value for key, value in (("ms", ms), ("s", s)) if value is not None}
        message = new.to_proto(payload)
        for value, unit_ms, ts, present in ((ms, True, message.created_at, message.HasField("created_at")),
                                            (s, False, message.leg.opened_at, message.leg.HasField("opened_at"))):
            if value is None:
                continue
            expected = _expected_utc(value, unit_ms)
            assert (present and (ts.seconds, ts.nanos)) == (expected is not None and expected), (host_zone, value, unit_ms)

        # The only differences from 974baa1 are the intended corrections: the old value
        # was the true instant shifted by the host's UTC offset, and on 0001-01-01 (the
        # first day Timestamp can hold) CPython's local-time conversion underflows, so
        # the old code left the field unset on every host.
        try:
            previous = before.to_proto(payload)
        except Exception:  # noqa: BLE001 - 974baa1 raised OverflowError far outside the range
            continue
        for value, unit_ms, old_ts, old_present in (
            (ms, True, previous.created_at, previous.HasField("created_at")),
            (s, False, previous.leg.opened_at, previous.leg.HasField("opened_at")),
        ):
            expected = None if value is None else _expected_utc(value, unit_ms)
            if expected is None:
                # the true instant is outside Timestamp's range; a non-UTC host could
                # shift it back inside, and 974baa1 stored that wrong value
                assert not old_present or host_zone != "UTC", value
            elif expected[0] < SECONDS_MIN + 86_400:
                assert not old_present, (host_zone, value)
            elif old_present:
                shift = 0 if host_zone == "UTC" else time.localtime(expected[0]).tm_gmtoff
                old_nanos = (old_ts.seconds - shift) * 10**9 + old_ts.nanos
                new_nanos = expected[0] * 10**9 + expected[1]
                # outside 1833–2106 the old float route was off by up to tens of µs
                tolerance = 0 if not unit_ms or abs(expected[0]) < 2**32 else 100_000
                assert abs(old_nanos - new_nanos) <= tolerance, (host_zone, value)
            else:
                assert host_zone != "UTC", value  # shifted past a range end on a non-UTC host


def test_epoch_milliseconds_rounding_matches_baseline_inside_1833_to_2106(zone):
    zone("UTC")
    rng = random.Random(42)
    new, before = _pair({"proto_class": ORDER, "fields": EPOCH_FIELDS[:1]})
    for _ in range(20000):
        ms = rng.randint(-(2**32) * 1000 + 1000, (2**32) * 1000 - 1)
        assert new.to_proto({"ms": ms}) == before.to_proto({"ms": ms}), ms


def test_epoch_milliseconds_outside_1833_to_2106_are_now_exact(zone):
    """Documented difference: 974baa1 divided by 1000.0 and rounded to microseconds."""
    zone("UTC")
    new, before = _pair({"proto_class": ORDER, "fields": EPOCH_FIELDS[:1]})
    differing = 0
    for ms in range(SECONDS_MAX * 1000 - 999, SECONDS_MAX * 1000 + 1000, 7):
        created = new.to_proto({"ms": ms}).created_at
        assert (created.seconds, created.nanos) == _expected_utc(ms, True)
        differing += before.to_proto({"ms": ms}).created_at != created
    assert differing > 0  # the old float route really was inexact here
