import pytest

from proto_converter.converter import ProtoConverter
from proto_converter.converter import register_expression_function
from proto_converter.converter import register_expression_name


def _base_config(proto_module_name: str) -> dict:
    return {
        "proto_class": f"{proto_module_name}.Sample",
        "type_maps": {
            "status_map": {
                "active": "STATUS_ACTIVE",
                "inactive": "STATUS_INACTIVE",
                "_default": "STATUS_UNSPECIFIED",
            }
        },
    }


def test_to_proto_handles_simple_enum_timestamp_expression_and_overrides(proto_module):
    config = _base_config(proto_module.__name__)
    config["fields"] = [
        {"json_path": "person.name", "proto_field": "name"},
        {"json_path": "metrics.count", "proto_field": "count", "transform": "int"},
        {
            "json_path": "status",
            "proto_field": "status",
            "type": "enum",
            "type_map": "status_map",
        },
        {
            "json_path": "created_at",
            "proto_field": "created_at",
            "type": "timestamp",
            "format": "iso8601",
        },
        {"json_path": "tags", "proto_field": "tags"},
        {"json_path": "child.note", "proto_field": "child.note"},
        {
            "json_path": "settlement",
            "proto_field": "settlement_date",
            "transform": "parse_date",
        },
        {
            "proto_field": "name",
            "type": "expression",
            "expression": "get('person.name') + '-' + str(account_id)",
        },
    ]

    source = {
        "person": {"name": "Ada"},
        "metrics": {"count": "7"},
        "status": "active",
        "created_at": "2024-07-04T09:30:00Z",
        "tags": ["alpha", "beta"],
        "child": {"note": "nested"},
        "settlement": "2026-01-16",
    }

    converter = ProtoConverter(config)
    proto = converter.to_proto(source, account_id=42, count=99)

    assert proto.name == "Ada-42"
    assert proto.count == 99
    assert proto.status == proto_module.STATUS_ACTIVE
    assert proto.created_at.seconds == 1_720_085_400
    assert list(proto.tags) == ["alpha", "beta"]
    assert proto.child.note == "nested"
    assert proto.settlement_date.year == 2026
    assert proto.settlement_date.month == 1
    assert proto.settlement_date.day == 16


def test_to_json_reverses_enum_and_timestamp(proto_module):
    config = _base_config(proto_module.__name__)
    config["fields"] = [
        {"json_path": "person.name", "proto_field": "name"},
        {"json_path": "metrics.count", "proto_field": "count"},
        {
            "json_path": "status",
            "proto_field": "status",
            "type": "enum",
            "type_map": "status_map",
        },
        {
            "json_path": "created_at_seconds",
            "proto_field": "created_at",
            "type": "timestamp",
            "unit": "s",
        },
        {"json_path": "child.note", "proto_field": "child.note"},
    ]

    proto = proto_module.Sample()
    proto.name = "Grace"
    proto.count = 3
    proto.status = proto_module.STATUS_INACTIVE
    proto.created_at.FromSeconds(1_720_085_400)
    proto.child.note = "hello"

    converter = ProtoConverter(config)
    as_json = converter.to_json(proto)

    assert as_json == {
        "person": {"name": "Grace"},
        "metrics": {"count": 3},
        "status": "inactive",
        "created_at_seconds": 1_720_085_400,
        "child": {"note": "hello"},
    }


def test_to_proto_supports_mappings_dictionary_format(proto_module):
    config = {
        "proto_class": f"{proto_module.__name__}.Sample",
        "mappings": {
            "name": "person.name",
            "count": "metrics.count + bump",
            "tags": "tags",
            "child": {
                "note": "child.note",
            },
        },
    }
    source = {
        "person": {"name": "Map Mode"},
        "metrics": {"count": 2},
        "tags": ["one", "two"],
        "child": {"note": "from mappings"},
    }

    converter = ProtoConverter(config)
    proto = converter.to_proto(source, bump=5)

    assert proto.name == "Map Mode"
    assert proto.count == 7
    assert list(proto.tags) == ["one", "two"]
    assert proto.child.note == "from mappings"


def test_expression_registry_supports_custom_functions_and_names(proto_module):
    register_expression_function("make_label", lambda prefix, value: f"{prefix}-{value}")
    register_expression_name("PREFIX", "acct")
    converter = ProtoConverter(
        {
            "proto_class": f"{proto_module.__name__}.Sample",
            "fields": [
                {
                    "proto_field": "name",
                    "type": "expression",
                    "expression": "make_label(PREFIX, str(id))",
                }
            ],
        }
    )

    proto = converter.to_proto({"id": 10})
    assert proto.name == "acct-10"


def test_unknown_enum_uses_type_map_default(proto_module):
    converter = ProtoConverter(
        {
            "proto_class": f"{proto_module.__name__}.Sample",
            "type_maps": {
                "status_map": {
                    "active": "STATUS_ACTIVE",
                    "_default": "STATUS_UNSPECIFIED",
                }
            },
            "fields": [
                {
                    "json_path": "status",
                    "proto_field": "status",
                    "type": "enum",
                    "type_map": "status_map",
                }
            ],
        }
    )

    proto = converter.to_proto({"status": "not-configured"})
    assert proto.status == proto_module.STATUS_UNSPECIFIED


def test_converter_getattr_proxies_config_values(proto_module):
    converter = ProtoConverter(
        {
            "proto_class": f"{proto_module.__name__}.Sample",
            "mapping_id": "sample_map",
        }
    )
    assert converter.mapping_id == "sample_map"

    with pytest.raises(AttributeError):
        _ = converter.not_a_real_config_key
