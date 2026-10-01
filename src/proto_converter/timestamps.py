"""
Timestamp parsing for `type: timestamp` fields.

Each parser returns a `datetime` (or None when the value does not parse), which
`set_timestamp` writes into a `google.protobuf.Timestamp`. `epoch_to_parts`
skips the datetime entirely for epoch values headed for a Timestamp field.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional, Tuple, Union

from google.protobuf.timestamp_pb2 import Timestamp

# Parse failures have always been logged under the converter's logger name.
logger = logging.getLogger("proto_converter.converter")

_EPOCH = datetime(1970, 1, 1)

# google.protobuf.Timestamp's valid range: 0001-01-01T00:00:00Z .. 9999-12-31T23:59:59Z
TIMESTAMP_SECONDS_MIN = -62135596800
TIMESTAMP_SECONDS_MAX = 253402300799

# Returned by epoch_to_parts for values outside Timestamp's range
OUT_OF_RANGE = object()

# Strings on which `datetime.fromisoformat` gives exactly what `dateutil.parser.parse`
# gives (fuzzed over this grammar, including out-of-range field values; the test suite
# keeps it so). Anything else goes to dateutil: e.g. "09:30.5" is 09:30:30 there but
# 09:30:00.5 to fromisoformat, and both parsers accept it.
_FAST_ISO8601 = re.compile(
    r"\d{4}-\d{2}-\d{2}"
    r"(?:[T ](?:[01]\d|2[0-3]):\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?)?",
    re.ASCII,
)


def parse_iso8601(value: Any) -> Optional[datetime]:
    """
    Parse an ISO-8601 string (e.g. "2024-07-04T09:30:00Z").

    A date-only string (YYYY-MM-DD) is taken as UTC midnight. The common RFC 3339
    shapes (`_FAST_ISO8601`) are parsed by `datetime.fromisoformat`, which gives the
    same result as dateutil on them in a small fraction of the time; everything else
    still goes through `dateutil.parser.parse`.
    """
    try:
        val_str = str(value)
        if len(val_str) == 10 and val_str.count("-") == 2 and "T" not in val_str:
            val_str += "T00:00:00Z"
        if _FAST_ISO8601.fullmatch(val_str):
            try:
                return datetime.fromisoformat(val_str)
            except ValueError:
                pass  # e.g. month 13: dateutil decides, as before
        import dateutil.parser
        return dateutil.parser.parse(val_str)
    except (ValueError, TypeError, OSError) as e:
        logger.debug(f"Timestamp parse error: {e}")
        return None


def parse_yyyymmdd(value: Any) -> Optional[datetime]:
    """
    Parse a YYYYMMDD date (e.g. "20260116") from the first 8 characters of the value.

    Eight ASCII digits are split directly, which gives the same result as
    `strptime(..., "%Y%m%d")`; anything else still goes through `strptime`.
    """
    try:
        date_str = str(value)
        if len(date_str) < 8:
            return None
        head = date_str[:8]
        if head.isascii() and head.isdigit():
            return datetime(int(head[:4]), int(head[4:6]), int(head[6:8]))
        return datetime.strptime(head, "%Y%m%d")
    except (ValueError, TypeError, OSError) as e:
        logger.debug(f"Timestamp parse error: {e}")
        return None


def parse_epoch(value: Any, unit_ms: bool) -> Optional[datetime]:
    """
    Parse an epoch value (milliseconds when `unit_ms`, else seconds) as a UTC datetime.

    Returns a timezone-aware datetime: a naive local time would be read back as UTC
    by `Timestamp.FromDatetime` and shift the value by the host's UTC offset.
    """
    try:
        epoch: Union[int, float] = int(value)
        if unit_ms:
            epoch = epoch / 1000
        return datetime.fromtimestamp(epoch, tz=timezone.utc)
    except (ValueError, TypeError, OSError) as e:
        logger.debug(f"Timestamp parse error: {e}")
        return None


def epoch_to_parts(value: Any, unit_ms: bool) -> Union[Tuple[int, int], None, object]:
    """
    Split an epoch value into Timestamp (seconds, nanos) without building a datetime.

    Returns None when the value does not parse (like `parse_epoch`), or `OUT_OF_RANGE`
    when the seconds fall outside Timestamp's range; callers then fall back to
    `parse_epoch` so out-of-range values behave exactly as before.
    """
    try:
        epoch = int(value)
    except (ValueError, TypeError) as e:
        logger.debug(f"Timestamp parse error: {e}")
        return None
    if unit_ms:
        seconds, millis = divmod(epoch, 1000)
        nanos = millis * 1_000_000
    else:
        seconds, nanos = epoch, 0
    if TIMESTAMP_SECONDS_MIN <= seconds <= TIMESTAMP_SECONDS_MAX:
        return seconds, nanos
    return OUT_OF_RANGE


def set_timestamp(ts: Timestamp, dt: datetime) -> None:
    """
    Equivalent of `ts.FromDatetime(dt)`, ~4x faster for naive and UTC datetimes.

    Naive datetimes are taken as UTC, as `FromDatetime` does. Every datetime in that
    case is within Timestamp's range, so no range check is needed.
    """
    if type(dt) is datetime:
        tz = dt.tzinfo
        if tz is None or tz is timezone.utc:
            delta = (dt if tz is None else dt.replace(tzinfo=None)) - _EPOCH
            ts.seconds = delta.days * 86400 + delta.seconds
            ts.nanos = dt.microsecond * 1000
            return
    ts.FromDatetime(dt)
