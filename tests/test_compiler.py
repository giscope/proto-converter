"""
The compiled converter must behave exactly like the reference implementation.

Each case converts with the generated function and with
`ProtoConverter._reference_to_proto`, then compares the serialized messages (or
the raised exception types), so any shape the compiler specializes is checked
against the code it replaces.
"""

import itertools
import logging
import sys
import types

import pytest
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.protobuf import timestamp_pb2  # noqa: F401 - Timestamp descriptor for the pool

from proto_converter import ProtoConverter, register_expression_function, register_expression_name
from proto_converter.compiler import compile_to_json, compile_to_proto

_MODULE = "proto_converter_rich_test_pb2"
_F = descriptor_pb2.FieldDescriptorProto


def _field(message, name, number, field_type, label=_F.LABEL_OPTIONAL, type_name=None):
    field = message.field.add()
    field.name, field.number, field.type, field.label = name, number, field_type, label
    if type_name:
        field.type_name = type_name


def _build_rich_module() -> types.ModuleType:
    if _MODULE in sys.modules:
        return sys.modules[_MODULE]
    file_proto = descriptor_pb2.FileDescriptorProto()
    file_proto.name = "proto_converter_rich_test.proto"
    file_proto.package = "proto_converter.rich"
    file_proto.syntax = "proto3"
    file_proto.dependency.append("google/protobuf/timestamp.proto")

    side = file_proto.enum_type.add()
    side.name = "Side"
    for name, number in (("SIDE_UNSPECIFIED", 0), ("SIDE_BUY", 1), ("SIDE_SELL", 2)):
        value = side.value.add()
        value.name, value.number = name, number

    date = file_proto.message_type.add()
    date.name = "Date"
    for number, name in enumerate(("year", "month", "day"), 1):
        _field(date, name, number, _F.TYPE_INT32)

    leg = file_proto.message_type.add()
    leg.name = "Leg"
    _field(leg, "symbol", 1, _F.TYPE_STRING)
    _field(leg, "qty", 2, _F.TYPE_INT64)
    _field(leg, "opened_at", 3, _F.TYPE_MESSAGE, type_name=".google.protobuf.Timestamp")
    _field(leg, "expiry", 4, _F.TYPE_MESSAGE, type_name=".proto_converter.rich.Date")
    _field(leg, "codes", 5, _F.TYPE_STRING, _F.LABEL_REPEATED)

    order = file_proto.message_type.add()
    order.name = "Order"
    labels = order.nested_type.add()
    labels.name = "LabelsEntry"
    labels.options.map_entry = True
    _field(labels, "key", 1, _F.TYPE_STRING)
    _field(labels, "value", 2, _F.TYPE_STRING)
    _field(order, "id", 1, _F.TYPE_STRING)
    _field(order, "price", 2, _F.TYPE_DOUBLE)
    _field(order, "qty", 3, _F.TYPE_INT64)
    _field(order, "active", 4, _F.TYPE_BOOL)
    _field(order, "side", 5, _F.TYPE_ENUM, type_name=".proto_converter.rich.Side")
    _field(order, "created_at", 6, _F.TYPE_MESSAGE, type_name=".google.protobuf.Timestamp")
    _field(order, "trade_date", 7, _F.TYPE_MESSAGE, type_name=".proto_converter.rich.Date")
    _field(order, "leg", 8, _F.TYPE_MESSAGE, type_name=".proto_converter.rich.Leg")
    _field(order, "legs", 9, _F.TYPE_MESSAGE, _F.LABEL_REPEATED, ".proto_converter.rich.Leg")
    _field(order, "tags", 10, _F.TYPE_STRING, _F.LABEL_REPEATED)
    _field(order, "labels", 11, _F.TYPE_MESSAGE, _F.LABEL_REPEATED, ".proto_converter.rich.Order.LabelsEntry")
    _field(order, "note", 12, _F.TYPE_STRING)

    pool = descriptor_pool.Default()
    pool.AddSerializedFile(file_proto.SerializeToString())
    module = types.ModuleType(_MODULE)
    for name in ("Order", "Leg", "Date"):
        setattr(module, name, message_factory.GetMessageClass(pool.FindMessageTypeByName(f"proto_converter.rich.{name}")))
    module.SIDE_UNSPECIFIED, module.SIDE_BUY, module.SIDE_SELL = 0, 1, 2
    sys.modules[_MODULE] = module
    return module


@pytest.fixture(scope="module")
def rich():
    return _build_rich_module()


ORDER = f"{_MODULE}.Order"
TYPE_MAPS = {"side": {"buy": "SIDE_BUY", "sell": "SIDE_SELL", "_default": "SIDE_UNSPECIFIED"}}

FIELDS = [
    {"json_path": "id", "proto_field": "id"},
    {"json_path": "px", "proto_field": "price", "transform": "float"},
    {"json_path": "qty", "proto_field": "qty", "transform": "int", "default": 1},
    {"json_path": "active", "proto_field": "active"},
    {"json_path": "side", "proto_field": "side", "type": "enum", "type_map": "side"},
    {"json_path": "side", "proto_field": "note", "type": "enum", "type_map": "missing_map"},
    {"json_path": "ts", "proto_field": "created_at", "type": "timestamp"},
    {"json_path": "ts_s", "proto_field": "leg.opened_at", "type": "timestamp", "unit": "s"},
    {"json_path": "iso", "proto_field": "created_at", "type": "timestamp", "format": "iso8601"},
    {"json_path": "ymd", "proto_field": "trade_date", "type": "timestamp", "format": "date_yyyymmdd"},
    {"json_path": "d", "proto_field": "trade_date", "transform": "parse_date"},
    {"json_path": "d", "proto_field": "leg.expiry"},
    {"json_path": "leg.sym", "proto_field": "leg.symbol"},
    {"json_path": "leg.codes", "proto_field": "leg.codes"},
    {"json_path": "tags", "proto_field": "tags"},
    {"json_path": "legs", "proto_field": "legs"},
    {"json_path": "labels", "proto_field": "labels"},
    {"json_path": "ts", "proto_field": "note", "type": "timestamp"},
    {"proto_field": "note", "type": "constant", "value": "fixed"},
    {"proto_field": "note", "type": "constant", "value": None},
    {"proto_field": "note", "type": "expression", "expression": "str(id) + '-' + str(acct)", "default": "dflt"},
    {"proto_field": "note", "type": "calculated", "expression": "not valid ((", "default": "bad"},
    {"proto_field": "note", "type": "unknown_type", "default": "u"},
    {"json_path": "id", "proto_field": "no_such_field"},
    {"json_path": "id", "proto_field": "leg.no_such"},
    {"json_path": "id", "proto_field": "legs.symbol"},
    {"json_path": "weird.'quote\"\nkey", "proto_field": "note"},
]

MAPPINGS = {
    "id": "id",
    "note": "nickname",
    "price": "px * 2",
    "qty": "acct",
    "active": "data.active",
    "tags": "tags",
    "leg": {"symbol": "leg.sym", "qty": "qty", "codes": "leg.codes"},
    "trade_date": {"proto_class": "x", "mappings": {"year": "dyear", "month": 7, "day": None}},
    "side": "SIDE_SELL_NAME",
    "created_at": "missing.path",
}

PAYLOADS = [
    {},
    {"id": "o1", "px": "12.5", "qty": "3", "active": True, "side": "buy", "ts": 1720085400123,
     "ts_s": 1720085400, "iso": "2024-07-04T09:30:00.250+02:00", "ymd": "20260116", "d": "2026-01-16",
     "leg": {"sym": "AAPL", "codes": ["a", "b"]}, "tags": ["x", "y"], "legs": [], "dyear": 2025,
     "weird": {"'quote\"\nkey": "ok"}},
    {"id": 42, "px": None, "qty": None, "active": "yes", "side": "sell", "ts": "not-a-number",
     "iso": "2024-W27-4", "ymd": "2026-1-1", "d": {"year": 1999, "month": 1, "day": 22},
     "leg": {"sym": 7, "codes": "not-a-list"}, "tags": ("t",), "labels": {"k": "v"}},
    {"id": "o3", "side": "unknown", "ts": -1500, "ts_s": 253402300800, "iso": "2024-07-04",
     "ymd": 20260230, "d": "bad-date-x", "leg": "not-a-dict", "tags": "not-a-list", "nickname": "nick"},
    {"id": "o4", "ts": 10**18, "iso": "July 4 2024", "d": "2026-02", "leg": {"sym": None}},
]

EXTRAS = [{}, {"acct": 9}, {"acct": "A1", "note": "override", "unknown_extra": 1, "price": "bad"}]


def _outcome(fn):
    try:
        message = fn()
    except Exception as e:  # noqa: BLE001 - both paths must raise the same thing
        return ("raised", type(e))
    return ("ok", message.SerializeToString(deterministic=True))


def _assert_same(converter, payload, extra):
    compiled = _outcome(lambda: converter.to_proto(payload, **extra))
    reference = _outcome(lambda: converter._reference_to_proto(payload, dict(extra)))
    assert compiled == reference, (payload, extra)


@pytest.fixture(autouse=True)
def quiet(caplog):
    caplog.set_level(logging.CRITICAL)


@pytest.mark.parametrize("engine", ["sandboxed", "native"])
def test_fields_format_matches_reference(rich, engine):
    converter = ProtoConverter({"proto_class": ORDER, "type_maps": TYPE_MAPS, "fields": FIELDS}, expression_engine=engine)
    for payload, extra in itertools.product(PAYLOADS, EXTRAS):
        _assert_same(converter, payload, extra)


@pytest.mark.parametrize("engine", ["sandboxed", "native"])
def test_mappings_format_matches_reference(rich, engine):
    register_expression_name("SIDE_SELL_NAME", 2)
    converter = ProtoConverter({"proto_class": ORDER, "mappings": MAPPINGS}, expression_engine=engine)
    for payload, extra in itertools.product(PAYLOADS, EXTRAS):
        _assert_same(converter, payload, extra)


def test_each_field_alone_matches_reference(rich):
    """One field per converter, so an exception in one field cannot mask another."""
    for field in FIELDS:
        converter = ProtoConverter({"proto_class": ORDER, "type_maps": TYPE_MAPS, "fields": [field]})
        for payload, extra in itertools.product(PAYLOADS, EXTRAS):
            _assert_same(converter, payload, extra)


@pytest.mark.parametrize("config", [
    {"fields": None},
    {"fields": "not-a-list"},
    {"fields": [{"json_path": "id"}]},  # no proto_field: raises only when a value exists
    {"fields": [["not", "a", "dict"]]},
    {"fields": [{"json_path": 5, "proto_field": "id"}]},
    {"fields": [{"json_path": "id", "proto_field": "id", "transform": ["unhashable"]}]},
    {"fields": [{"json_path": "side", "proto_field": "side", "type": "enum"}]},  # no type_map
    {"mappings": "not-a-dict"},
    {"mappings": {"leg": {"proto_class": "x"}}},  # no nested mappings
    {"mappings": {"no_such": {"symbol": "id"}}},
    {"mappings": {"leg": {"proto_class": "x", "mappings": "bad"}}},
    {"mappings": {5: "id"}},
])
def test_malformed_configs_fail_the_same_way(rich, config):
    converter = ProtoConverter({"proto_class": ORDER, **config})
    for payload, extra in itertools.product(PAYLOADS, EXTRAS):
        _assert_same(converter, payload, extra)


def test_unspecialized_compile_is_the_reference(rich):
    converter = ProtoConverter({"proto_class": ORDER, "type_maps": TYPE_MAPS, "fields": FIELDS, "mappings": MAPPINGS})
    generic = compile_to_proto(converter, specialize=False)
    for payload, extra in itertools.product(PAYLOADS, EXTRAS):
        assert _outcome(lambda: generic(payload, dict(extra))) == _outcome(
            lambda: converter._reference_to_proto(payload, dict(extra)))


def test_to_json_plan_matches_reference(rich):
    fields = FIELDS + [{"json_path": "out.ts_s", "proto_field": "created_at", "unit": "s"},
                       {"json_path": "out.side", "proto_field": "side", "type": "enum", "type_map": "side"},
                       {"json_path": "out.leg", "proto_field": "leg.symbol"},
                       {"json_path": "", "proto_field": "id"}]
    converter = ProtoConverter({"proto_class": ORDER, "type_maps": TYPE_MAPS, "fields": fields})
    plan = compile_to_json(converter)
    for payload in PAYLOADS:
        try:
            message = converter.to_proto(payload, acct=1)
        except Exception:  # noqa: BLE001 - payloads that cannot convert have nothing to reverse
            continue
        assert plan(message) == converter._reference_to_json(message)


def test_generated_source_is_inspectable_and_keys_are_literals(rich):
    converter = ProtoConverter({"proto_class": ORDER, "fields": [
        {"json_path": "weird.'quote\"\nkey", "proto_field": "note"}]})
    message = converter.to_proto({"weird": {"'quote\"\nkey": "ok"}})
    assert message.note == "ok"
    assert "def to_proto(src, extra):" in converter._to_proto_source
    assert "src.get('weird')" in converter._to_proto_source


def test_extra_field_named_source_no_longer_crashes(rich):
    converter = ProtoConverter({"proto_class": ORDER, "fields": [{"json_path": "id", "proto_field": "id"}]})
    assert converter.to_proto({"id": "x"}, source="ibkr").id == "x"


def test_replacing_config_recompiles(rich):
    converter = ProtoConverter({"proto_class": ORDER, "fields": [{"json_path": "id", "proto_field": "id"}]})
    assert converter.to_proto({"id": "a", "note": "n"}).note == ""
    converter.config = {"proto_class": ORDER, "fields": [{"json_path": "note", "proto_field": "note"}]}
    assert converter.to_proto({"id": "a", "note": "n"}).note == "n"


def test_recompile_picks_up_in_place_config_changes(rich):
    config = {"proto_class": ORDER, "fields": [{"json_path": "id", "proto_field": "id"}]}
    converter = ProtoConverter(config)
    assert converter.to_proto({"id": "a", "note": "n"}).note == ""
    config["fields"].append({"json_path": "note", "proto_field": "note"})
    assert converter.to_proto({"id": "a", "note": "n"}).note == ""  # compiled before the change
    converter.recompile()
    assert converter.to_proto({"id": "a", "note": "n"}).note == "n"


def test_config_replacement_resets_class_enum_maps_and_engine(rich, proto_module):
    from proto_converter import set_default_expression_engine

    expression = {"proto_field": "note", "type": "expression", "expression": "first"}
    converter = ProtoConverter({"proto_class": ORDER, "type_maps": TYPE_MAPS, "fields": [
        {"json_path": "side", "proto_field": "side", "type": "enum", "type_map": "side"}, expression]})
    assert converter.to_proto({"side": "buy", "first": "x"}).side == rich.SIDE_BUY
    assert converter.expression_engine == "sandboxed"

    try:
        set_default_expression_engine("native")
        converter.config = {"proto_class": ORDER,
                            "type_maps": {"side": {"buy": "SIDE_SELL", "_default": "SIDE_UNSPECIFIED"}},
                            "fields": [{"json_path": "side", "proto_field": "side", "type": "enum", "type_map": "side"},
                                       expression]}
        assert converter.to_proto({"side": "buy", "first": "x"}).side == rich.SIDE_SELL
        assert converter.expression_engine == "native"
    finally:
        set_default_expression_engine("sandboxed")

    converter.config = {"proto_class": f"{proto_module.__name__}.Sample",
                        "fields": [{"json_path": "n", "proto_field": "name"}]}
    assert type(converter.to_proto({"n": "s"})) is proto_module.Sample
    assert converter.type_maps == {}
    converter.recompile()
    assert converter.expression_engine == "sandboxed"


def test_late_registrations_reach_compiled_converters(rich):
    converter = ProtoConverter({"proto_class": ORDER, "fields": [
        {"json_path": "id", "proto_field": "id", "transform": "shout"},
        {"proto_field": "note", "type": "expression", "expression": "tag(id)"}]})
    assert converter.to_proto({"id": "a"}).id == "a"

    from proto_converter import register_transform
    register_transform("shout", str.upper)
    register_expression_function("tag", lambda value: f"<{value}>")
    message = converter.to_proto({"id": "a"})
    assert (message.id, message.note) == ("A", "<a>")
