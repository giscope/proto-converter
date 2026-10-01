from proto_converter.transforms import TRANSFORMS
from proto_converter.transforms import register_transform


def test_float_transform_handles_none_like_values():
    assert TRANSFORMS["float"](None) == 0.0
    assert TRANSFORMS["float"]("None") == 0.0
    assert TRANSFORMS["float"]("1.25") == 1.25


def test_parse_date_transform():
    assert TRANSFORMS["parse_date"]("2026-01-16") == {"year": 2026, "month": 1, "day": 16}
    assert TRANSFORMS["parse_date"]("20260116") is None


def test_register_transform_adds_new_transform():
    register_transform("double", lambda x: x * 2)
    assert TRANSFORMS["double"](7) == 14
