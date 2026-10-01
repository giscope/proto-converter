from proto_converter.helpers import get_nested_value
from proto_converter.helpers import set_nested_value


def test_get_nested_value_returns_value():
    data = {"profile": {"address": {"city": "Austin"}}}
    assert get_nested_value(data, "profile.address.city") == "Austin"


def test_get_nested_value_returns_default_for_missing_path():
    data = {"profile": {"name": "Ada"}}
    assert get_nested_value(data, "profile.address.city", default="unknown") == "unknown"


def test_set_nested_value_creates_missing_levels():
    data = {}
    set_nested_value(data, "meta.audit.created_by", "system")
    assert data == {"meta": {"audit": {"created_by": "system"}}}
