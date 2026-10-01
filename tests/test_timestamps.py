import os
import random
import time
from datetime import datetime, timedelta, timezone

import dateutil.parser
import pytest
from google.protobuf.timestamp_pb2 import Timestamp

from proto_converter import ProtoConverter
from proto_converter.timestamps import (
    OUT_OF_RANGE,
    epoch_to_parts,
    parse_epoch,
    parse_iso8601,
    parse_yyyymmdd,
    set_timestamp,
)

ISO_CORPUS = [
    "2024-07-04T09:30:00Z", "2024-07-04T09:30:00", "2024-07-04 09:30:00", "2024-07-04T09:30:00.123Z",
    "2024-07-04T09:30:00.1234567Z", "2024-07-04T09:30:00+05:30", "2024-07-04T09:30:00-0400",
    "2024-07-04T09:30Z", "20240704", "20240704T093000Z", "2024-07-04", "2024-02-30T00:00:00Z",
    "July 4 2024", "2024-07-04T09:30:00 UTC", "09:30", "2024", "garbage",
]


@pytest.fixture
def new_york_time():
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is POSIX-only")
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    yield
    if previous is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = previous
    time.tzset()


@pytest.mark.parametrize("unit, value", [("ms", 1_720_085_400_123), ("s", 1_720_085_400)])
def test_epoch_timestamps_are_utc_whatever_the_host_zone(proto_module, new_york_time, unit, value):
    converter = ProtoConverter({"proto_class": f"{proto_module.__name__}.Sample", "fields": [
        {"json_path": "t", "proto_field": "created_at", "type": "timestamp", "unit": unit}]})
    created = converter.to_proto({"t": value}).created_at
    assert created.seconds == 1_720_085_400
    assert created.nanos == (123_000_000 if unit == "ms" else 0)
    # the reference path (and any non-Timestamp target) agrees
    assert parse_epoch(value, unit == "ms") == datetime(2024, 7, 4, 9, 30, tzinfo=timezone.utc) + timedelta(
        milliseconds=123 if unit == "ms" else 0)


def test_unparseable_and_out_of_range_epochs_leave_the_field_unset(proto_module):
    converter = ProtoConverter({"proto_class": f"{proto_module.__name__}.Sample", "fields": [
        {"json_path": "t", "proto_field": "created_at", "type": "timestamp"}]})
    for value in ("soon", None, 10**20, -(10**20)):
        assert not converter.to_proto({"t": value}).HasField("created_at"), value


def test_epoch_to_parts_matches_datetime_route():
    rng = random.Random(7)
    for _ in range(5000):
        millis = rng.randint(-2_000_000_000_000, 4_000_000_000_000)
        via_datetime = Timestamp()
        via_datetime.FromDatetime(parse_epoch(millis, True))
        assert epoch_to_parts(millis, True) == (via_datetime.seconds, via_datetime.nanos), millis
    assert epoch_to_parts("x", True) is None
    assert epoch_to_parts(10**20, True) is OUT_OF_RANGE


@pytest.mark.parametrize("text", ISO_CORPUS)
def test_iso8601_fast_path_agrees_with_dateutil(text):
    try:
        expected = dateutil.parser.parse(text if len(text) != 10 else text + "T00:00:00Z")
    except (ValueError, OverflowError):
        expected = None
    got = parse_iso8601(text)
    if expected is None:
        assert got is None or isinstance(got, datetime)
    else:
        assert got == expected and got.utcoffset() == expected.utcoffset()


def _dateutil_reference(value):
    """parse_iso8601 as it was at 974baa1: dateutil for everything."""
    text = str(value)
    if len(text) == 10 and text.count("-") == 2 and "T" not in text:
        text += "T00:00:00Z"
    try:
        return dateutil.parser.parse(text)
    except (ValueError, TypeError, OSError):
        return None


def _offset(dt):
    try:
        return dt.utcoffset()
    except ValueError:  # dateutil can build offsets of 24h or more, which datetime rejects
        return repr(dt.tzinfo)


def _same(got, expected):
    if expected is None or got is None:
        return got is expected
    return (
        got.replace(tzinfo=None) == expected.replace(tzinfo=None)
        and _offset(got) == _offset(expected)
        and (got.tzinfo is None) == (expected.tzinfo is None)
    )


def test_iso8601_values_dateutil_reads_differently_keep_their_old_meaning():
    # Both parsers accept these; fromisoformat would read 09:30.5 as 09:30:00.5
    assert parse_iso8601("2024-07-04T09:30.5") == datetime(2024, 7, 4, 9, 30, 30)
    for text in ("2024-07-04T09:30.5", "2024-07-04+01:00", "2024-W27-4", "2024-W27-4T10:00:00",
                 "20240704T093000Z", "2024-07-04T09:30:00.1234567Z", "2024-07-04t09:30:00z"):
        assert _same(parse_iso8601(text), _dateutil_reference(text)), text


def _grammar_samples(rng, count):
    """Strings shaped like the fast-path grammar, with out-of-range fields and near misses."""
    def two(lo, hi):
        return f"{rng.randint(lo, hi):02d}"
    for _ in range(count):
        text = f"{rng.randint(0, 9999):04d}-{two(0, 19)}-{two(0, 39)}"
        if rng.random() < 0.85:
            text += rng.choice("T t") + two(0, 29) + ":" + two(0, 69)
            if rng.random() < 0.7:
                text += ":" + two(0, 69)
                if rng.random() < 0.5:
                    text += rng.choice(".,") + "".join(rng.choice("0123456789") for _ in range(rng.randint(1, 8)))
            elif rng.random() < 0.2:
                text += "." + rng.choice("05")
        roll = rng.random()
        if roll < 0.3:
            text += rng.choice(["Z", "z"])
        elif roll < 0.6:
            text += rng.choice("+-") + two(0, 29) + rng.choice([":", ""]) + two(0, 69)
        yield text


def test_iso8601_fast_path_is_equivalent_to_dateutil_on_fuzzed_input():
    rng = random.Random(20261001)
    for text in _grammar_samples(rng, 40000):
        assert _same(parse_iso8601(text), _dateutil_reference(text)), text


def test_yyyymmdd_fast_path_agrees_with_strptime():
    rng = random.Random(11)
    samples = ["20260116", "20261315", "20260230", "00000101", "2026+1+1", "2026 1 1", "2026-1-1", "20260116xyz",
               "２０２６０１１６", "1234567", 20260116]
    samples += ["".join(rng.choice("0123456789") for _ in range(8)) for _ in range(20000)]
    for value in samples:
        text = str(value)
        try:
            expected = datetime.strptime(text[:8], "%Y%m%d") if len(text) >= 8 else None
        except ValueError:
            expected = None
        assert parse_yyyymmdd(value) == expected, value


def test_set_timestamp_matches_from_datetime():
    rng = random.Random(3)
    zones = [None, timezone.utc, timezone(timedelta(hours=5, minutes=30)), timezone(timedelta(hours=-8))]
    for _ in range(5000):
        dt = datetime(1, 1, 1) + timedelta(seconds=rng.randint(0, 315_537_897_599), microseconds=rng.randint(0, 999_999))
        tz = rng.choice(zones)
        if tz is not None:
            if tz.utcoffset(None) < timedelta(0) and dt.year == 1 or tz.utcoffset(None) > timedelta(0) and dt.year == 9999:
                continue  # the offset would push FromDatetime itself out of range
            dt = dt.replace(tzinfo=tz)
        fast, slow = Timestamp(), Timestamp()
        set_timestamp(fast, dt)
        slow.FromDatetime(dt)
        assert (fast.seconds, fast.nanos) == (slow.seconds, slow.nanos), dt
