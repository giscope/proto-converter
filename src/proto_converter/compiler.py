"""
Mapping compiler: turns a ProtoConverter config into one generated function.

`compile_to_proto` emits straight-line Python for each field shape it can prove
equivalent to the converter's reference implementation (dict lookups, and direct
attribute sets chosen from the proto descriptor), and a call into the reference
implementation for anything else. A result therefore never depends on which path
a field took; the test suite runs both paths against each other.

Generated code contains only names this module creates, attribute names that are
plain identifiers checked against the descriptor, and `repr()` of path keys; every
other value from the mapping is passed in through the function's globals.

The generated source is registered with `linecache`, so tracebacks through it show
real lines, and kept on the converter as `_to_proto_source` for inspection.
"""

import itertools
import keyword
import linecache
import weakref
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from google.protobuf.message import Message
from google.protobuf.timestamp_pb2 import Timestamp

from proto_converter.expressions import report_failure
from proto_converter.timestamps import (
    OUT_OF_RANGE,
    epoch_to_parts,
    parse_epoch,
    parse_iso8601,
    parse_yyyymmdd,
    set_timestamp,
)
from proto_converter.transforms import TRANSFORMS

if TYPE_CHECKING:
    from proto_converter.converter import ProtoConverter

# How a value reaches a proto field, decided from the descriptor plus the same
# checks the reference setter (`ProtoConverter._assign_field`) makes at runtime
_SCALAR = "scalar"        # plain assignment
_REPEATED = "repeated"    # clear + extend for lists/tuples
_TIMESTAMP = "timestamp"  # datetime -> seconds/nanos
_MESSAGE = "message"      # the reference assignment logic, minus path resolution
_GENERIC = "generic"      # the full reference setter

_GENERIC_TARGET = (_GENERIC, (), "")

# Characters that make a mappings value an expression rather than a dotted path
_EXPRESSION_CHARS = "()+-*/"

_compile_ids = itertools.count(1)


class _Code:
    """Accumulates the lines and globals of one generated function."""

    def __init__(self, converter: "ProtoConverter", specialize: bool):
        self.converter = converter
        self.specialize = specialize
        self.lines: List[str] = []
        self._ids = itertools.count()
        self.namespace: Dict[str, Any] = {
            "datetime": datetime,
            "_transforms": TRANSFORMS,
            "_report_failure": report_failure,
            "_epoch_to_parts": epoch_to_parts,
            "_OUT_OF_RANGE": OUT_OF_RANGE,
            "_parse_epoch": parse_epoch,
            "_parse_iso8601": parse_iso8601,
            "_parse_yyyymmdd": parse_yyyymmdd,
            "_set_timestamp": set_timestamp,
            "_set": converter._set_proto_field,
            "_assign": converter._assign_field,
            "_field": converter._reference_field,
            "_fields": converter._reference_fields,
            "_mappings": converter._apply_mappings,
        }

    def const(self, value: Any) -> str:
        """Pass a value in through the function's globals; returns its name."""
        name = f"_k{next(self._ids)}"
        self.namespace[name] = value
        return name

    def var(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids)}"

    def emit(self, depth: int, line: str) -> None:
        self.lines.append("    " * depth + line)

    def build(self, name: str, params: str, label: str) -> Tuple[Callable, str]:
        source = f"def {name}({params}):\n" + "\n".join(self.lines) + "\n"
        filename = f"<proto_converter {label} #{next(_compile_ids)}>"
        exec(compile(source, filename, "exec"), self.namespace)
        fn = self.namespace[name]
        linecache.cache[filename] = (len(source), None, source.splitlines(True), filename)
        weakref.finalize(fn, linecache.cache.pop, filename, None)
        return fn, source


# =============================================================================
# to_proto
# =============================================================================

def compile_to_proto(converter: "ProtoConverter", specialize: bool = True) -> Callable[[Any, Dict[str, Any]], Message]:
    """
    Generate `to_proto(src, extra)` for the converter's current config.

    Args:
        converter: The converter whose config to compile.
        specialize: False emits reference-implementation calls for every field
            (used by tests to compare the two paths).
    """
    config = converter.config
    proto_class = converter.proto_class
    sample = proto_class()

    code = _Code(converter, specialize)
    code.namespace["_new"] = proto_class
    code.namespace["_apply_extra"] = _extra_field_applier(converter, specialize)
    code.emit(1, "msg = _new()")
    code.emit(1, "src_is_dict = isinstance(src, dict)")

    if "fields" in config:
        fields = config["fields"]
        if specialize and isinstance(fields, (list, tuple)):
            for field_config in fields:
                _emit_field(code, sample, field_config)
        else:
            code.emit(1, f"_fields(msg, {code.const(fields)}, src, extra)")

    if "mappings" in config:
        _emit_mappings(code, 1, "msg", sample, config["mappings"])

    code.emit(1, "if extra:")
    code.emit(2, "_apply_extra(msg, extra)")
    code.emit(1, "return msg")

    fn, source = code.build("to_proto", "src, extra", f"to_proto {proto_class.DESCRIPTOR.full_name}")
    converter._to_proto_source = source
    return fn


def _emit_reference_field(code: _Code, field_config: Any) -> None:
    code.emit(1, f"_field(msg, {code.const(field_config)}, src, extra)")


def _emit_get(code: _Code, depth: int, var: str, keys: Optional[List[str]]) -> None:
    """`var = get_nested_value(src, ".".join(keys))`, unrolled."""
    if not keys:
        code.emit(depth, f"{var} = None")
        return
    code.emit(depth, f"{var} = src.get({keys[0]!r}) if src_is_dict else None")
    for key in keys[1:]:
        code.emit(depth, f"{var} = {var}.get({key!r}) if isinstance({var}, dict) else None")


def _emit_field(code: _Code, sample: Message, field_config: Any) -> None:
    """One entry of the `fields` list (see `ProtoConverter._convert_field_to_proto`)."""
    if not isinstance(field_config, dict):
        return _emit_reference_field(code, field_config)

    converter = code.converter
    field_type = field_config.get("type", "simple")
    json_path = field_config.get("json_path", "")
    default = field_config.get("default")
    proto_field = field_config.get("proto_field")
    if not isinstance(proto_field, str) or (json_path and not isinstance(json_path, str)):
        return _emit_reference_field(code, field_config)
    keys = json_path.split(".") if json_path else None

    if field_type == "simple" or field_type is None:
        transform = field_config.get("transform")
        if transform is not None and not isinstance(transform, str):
            return _emit_reference_field(code, field_config)
        _emit_get(code, 1, "v", keys)
        if default is not None:
            code.emit(1, "if v is None:")
            code.emit(2, f"v = {code.const(default)}")
        if transform:
            if default is not None:
                code.emit(1, f"elif {transform!r} in _transforms:")
            else:
                code.emit(1, f"if v is not None and {transform!r} in _transforms:")
            code.emit(2, f"v = _transforms[{transform!r}](v)")

    elif field_type == "enum":
        if "type_map" not in field_config:
            return _emit_reference_field(code, field_config)
        try:
            tmap = converter.type_maps.get(field_config["type_map"])
        except TypeError:
            return _emit_reference_field(code, field_config)
        _emit_get(code, 1, "v", keys)
        if tmap:
            code.emit(1, f"v = {code.const(tmap['forward'])}.get(v, {code.const(tmap['default'])})")
        else:
            code.emit(1, "v = 0")

    elif field_type == "timestamp":
        fmt = field_config.get("format", "epoch")
        _emit_get(code, 1, "v", keys)
        if fmt == "iso8601":
            code.emit(1, "if v is not None:")
            code.emit(2, "v = _parse_iso8601(v)")
        elif fmt == "date_yyyymmdd":
            code.emit(1, "if v is not None:")
            code.emit(2, "v = _parse_yyyymmdd(v)")
        else:
            unit_ms = field_config.get("unit", "ms") == "ms"
            kind, parents, leaf = _target(sample, proto_field)
            if kind == _TIMESTAMP:
                return _emit_epoch_timestamp(code, sample, proto_field, parents, leaf, unit_ms)
            code.emit(1, "if v is not None:")
            code.emit(2, f"v = _parse_epoch(v, {unit_ms})")

    elif field_type == "constant":
        value = field_config.get("value")
        if value is None:
            return
        code.emit(1, f"v = {code.const(value)}")

    elif field_type in ("expression", "calculated"):
        expr = field_config.get("expression", "")
        code.emit(1, "try:")
        code.emit(2, f"v = {code.const(converter._expression(expr))}.evaluate(src, extra)")
        code.emit(1, "except Exception as e:")
        code.emit(2, f"v = _report_failure({code.const(expr)}, e, {code.const(default)})")

    else:
        if default is None:
            return
        code.emit(1, f"v = {code.const(default)}")

    code.emit(1, "if v is not None:")
    _emit_set(code, 2, "msg", sample, proto_field, "v")


def _emit_epoch_timestamp(code: _Code, sample: Message, proto_field: str, parents: Tuple[str, ...], leaf: str, unit_ms: bool) -> None:
    """Epoch value into a Timestamp field: seconds/nanos directly, no datetime."""
    code.emit(1, "if v is not None:")
    code.emit(2, f"parts = _epoch_to_parts(v, {unit_ms})")
    code.emit(2, "if parts is _OUT_OF_RANGE:")
    code.emit(3, f"v = _parse_epoch(v, {unit_ms})")
    code.emit(3, "if v is not None:")
    _emit_set(code, 4, "msg", sample, proto_field, "v")
    code.emit(2, "elif parts is not None:")
    code.emit(3, f"ts = {'.'.join(('msg',) + parents)}.{leaf}")
    code.emit(3, "ts.seconds = parts[0]")
    code.emit(3, "ts.nanos = parts[1]")


def _emit_mappings(code: _Code, depth: int, target: str, sample: Any, mappings: Any) -> None:
    """A `mappings` dict (see `ProtoConverter._apply_mappings`)."""
    if not code.specialize or not isinstance(mappings, dict):
        code.emit(depth, f"_mappings({target}, {code.const(mappings)}, src, extra)")
        return
    for field_name, config in mappings.items():
        if not isinstance(config, dict):
            _emit_mapping_value(code, depth, target, sample, field_name, config)
            continue
        nested_sample = _nested_sample(sample, field_name)
        if nested_sample is None or ("proto_class" in config and "mappings" not in config):
            # Let the reference raise exactly what it raises for this entry
            code.emit(depth, f"_mappings({target}, {code.const({field_name: config})}, src, extra)")
            continue
        nested = code.var("t")
        code.emit(depth, f"{nested} = {target}.{field_name}")
        if "proto_class" in config:
            _emit_mappings(code, depth, nested, nested_sample, config["mappings"])
        else:
            for sub_field, sub_config in config.items():
                _emit_mapping_value(code, depth, nested, nested_sample, sub_field, sub_config)


def _emit_mapping_value(code: _Code, depth: int, target: str, sample: Any, field_name: Any, config: Any) -> None:
    """One `field: value` of a mappings dict (see `ProtoConverter._eval_config_value`)."""
    converter = code.converter
    if not isinstance(config, str):
        if config is None:
            return
        code.emit(depth, f"v = {code.const(config)}")
    elif config.isidentifier():
        code.emit(depth, f"if {config!r} in src:")
        code.emit(depth + 1, f"v = src[{config!r}]")
        code.emit(depth, "else:")
        code.emit(depth + 1, f"v = {code.const(converter._path_fallback(config))}(src, extra)")
    elif "." in config and " " not in config and not any(c in config for c in _EXPRESSION_CHARS):
        _emit_get(code, depth, "v", config.split("."))
        code.emit(depth, "if v is None:")
        code.emit(depth + 1, f"v = {code.const(converter._path_fallback(config))}(src, extra)")
    else:
        code.emit(depth, "try:")
        code.emit(depth + 1, f"v = {code.const(converter._expression(config))}.evaluate(src, extra)")
        code.emit(depth, "except Exception as e:")
        code.emit(depth + 1, f"v = _report_failure({code.const(config)}, e, None)")
    code.emit(depth, "if v is not None:")
    _emit_set(code, depth + 1, target, sample, field_name, "v")


def _nested_sample(sample: Any, field_name: Any) -> Optional[Any]:
    """`getattr(sample, field_name)` when the generated code can do the same, else None."""
    if not isinstance(sample, Message) or not _is_attribute_name(field_name):
        return None
    try:
        return getattr(sample, field_name)
    except AttributeError:
        return None


# =============================================================================
# Field setters
# =============================================================================

def _emit_set(code: _Code, depth: int, base: str, sample: Any, path: Any, value: str) -> None:
    """`ProtoConverter._set_proto_field(base, path, value)`, specialized by field kind."""
    kind, parents, leaf = _target(sample, path) if code.specialize else _GENERIC_TARGET
    if kind == _GENERIC:
        code.emit(depth, f"_set({base}, {code.const(path)}, {value})")
        return
    owner = ".".join((base,) + parents)
    fallback = f"_set({base}, {code.const(path)}, {value})"
    if kind == _SCALAR:
        code.emit(depth, "try:")
        code.emit(depth + 1, f"{owner}.{leaf} = {value}")
        code.emit(depth, "except (TypeError, AttributeError):")
        code.emit(depth + 1, fallback)
    elif kind == _REPEATED:
        code.emit(depth, f"if isinstance({value}, (list, tuple)):")
        code.emit(depth + 1, f"items = {owner}.{leaf}")
        code.emit(depth + 1, "del items[:]")
        code.emit(depth + 1, f"items.extend({value})")
        code.emit(depth, "else:")
        code.emit(depth + 1, fallback)
    elif kind == _TIMESTAMP:
        code.emit(depth, f"if isinstance({value}, datetime):")
        code.emit(depth + 1, f"_set_timestamp({owner}.{leaf}, {value})")
        code.emit(depth, "else:")
        code.emit(depth + 1, fallback)
    else:  # _MESSAGE
        code.emit(depth, f"owner = {owner}")
        code.emit(depth, f"_assign(owner, {leaf!r}, owner.{leaf}, {value})")


def _target(sample: Any, path: Any) -> Tuple[str, Tuple[str, ...], str]:
    """
    Classify where `path` lands on messages shaped like `sample`.

    Returns (kind, parent attribute names, leaf name). Only singular message
    parents are walked; anything unusual is `_GENERIC`, i.e. the reference setter.
    """
    if not isinstance(sample, Message) or not isinstance(path, str):
        return _GENERIC_TARGET
    parts = path.split(".")
    if not all(_is_attribute_name(part) for part in parts):
        return _GENERIC_TARGET
    owner = sample
    for part in parts[:-1]:
        field = owner.DESCRIPTOR.fields_by_name.get(part)
        if field is None or _is_repeated(field) or field.message_type is None:
            return _GENERIC_TARGET
        owner = getattr(owner, part)
    parents, leaf = tuple(parts[:-1]), parts[-1]
    field = owner.DESCRIPTOR.fields_by_name.get(leaf)
    if field is None:
        return _GENERIC_TARGET

    # The reference setter branches on these properties of the current field value;
    # for a given field they never change, so the branch can be chosen here.
    field_obj = getattr(owner, leaf)
    is_timestamp = isinstance(field_obj, Timestamp)
    is_date_like = hasattr(field_obj, "year") and hasattr(field_obj, "month") and hasattr(field_obj, "day")
    can_extend = hasattr(field_obj, "extend")

    if _is_repeated(field):
        if _is_map(field) or not can_extend or is_timestamp or is_date_like:
            return _GENERIC_TARGET
        return _REPEATED, parents, leaf
    if field.message_type is not None:
        return (_TIMESTAMP if is_timestamp else _MESSAGE), parents, leaf
    if is_timestamp or is_date_like or can_extend:
        return _GENERIC_TARGET
    return _SCALAR, parents, leaf


def _is_attribute_name(name: Any) -> bool:
    return isinstance(name, str) and name.isidentifier() and not keyword.iskeyword(name)


def _is_repeated(field: Any) -> bool:
    is_repeated = getattr(field, "is_repeated", None)  # protobuf >= 6; `label` before
    if is_repeated is not None:
        return bool(is_repeated)
    return field.label == field.LABEL_REPEATED


def _is_map(field: Any) -> bool:
    return field.message_type is not None and field.message_type.GetOptions().map_entry


def _extra_field_applier(converter: "ProtoConverter", specialize: bool) -> Callable[[Message, Dict[str, Any]], None]:
    """
    Set extra fields passed to `to_proto` (they override mapped values).

    Each name is checked and compiled once: names that are not attributes of the
    message are skipped, as before; the rest get a setter specialized like mapped fields.
    """
    setters = converter._extra_setters

    def apply_extra(msg: Message, extra: Dict[str, Any]) -> None:
        for name, value in extra.items():
            try:
                setter = setters[name]
            except KeyError:
                setter = setters[name] = _extra_field_setter(converter, msg, name, specialize)
            if setter is not None:
                setter(msg, value)

    return apply_extra


def _extra_field_setter(converter: "ProtoConverter", msg: Message, name: str, specialize: bool) -> Optional[Callable[[Message, Any], None]]:
    if not hasattr(msg, name):
        return None
    code = _Code(converter, specialize)
    _emit_set(code, 1, "msg", converter.proto_class(), name, "v")
    fn, _ = code.build("set_extra", "msg, v", f"extra field {name!r}")
    return fn


# =============================================================================
# to_json
# =============================================================================

def compile_to_json(converter: "ProtoConverter", specialize: bool = True) -> Callable[[Message], Dict[str, Any]]:
    """
    Precompile `to_json` into one closure per field (see `ProtoConverter._reference_json_field`).
    """
    fields = converter.config.get("fields", [])
    if not specialize or not isinstance(fields, (list, tuple)):
        return converter._reference_to_json
    ops = [op for op in (_json_field_op(converter, fc) for fc in fields) if op is not None]

    def to_json(proto: Message) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for op in ops:
            op(proto, result)
        return result

    return to_json


def _json_field_op(converter: "ProtoConverter", field_config: Any) -> Optional[Callable[[Message, Dict[str, Any]], None]]:
    reference = converter._reference_json_field

    def reference_op(proto: Message, result: Dict[str, Any]) -> None:
        reference(proto, field_config, result)

    if not isinstance(field_config, dict):
        return reference_op
    json_path = field_config.get("json_path")
    if not json_path:
        return None
    proto_field = field_config.get("proto_field")
    if not isinstance(json_path, str) or not isinstance(proto_field, str):
        return reference_op

    parts = proto_field.split(".")
    keys = json_path.split(".")
    parent_keys, last_key = keys[:-1], keys[-1]
    unit_ms = field_config.get("unit", "ms") == "ms"
    reverse = None
    if field_config.get("type") == "enum":
        type_map_name = field_config.get("type_map")
        try:
            if type_map_name and type_map_name in converter.type_maps:
                reverse = converter.type_maps[type_map_name]["reverse"]
        except TypeError:
            return reference_op

    def op(proto: Message, result: Dict[str, Any]) -> None:
        value = proto
        for part in parts:
            if value is None:
                break
            value = getattr(value, part, None)

        if isinstance(value, Timestamp):
            if value.seconds > 0 or value.nanos > 0:
                epoch = value.seconds + value.nanos / 1e9
                value = int(epoch * 1000) if unit_ms else int(epoch)
            else:
                value = None
        elif reverse is not None:
            value = reverse.get(value, value)

        if value is not None:
            current = result
            for key in parent_keys:
                if key not in current:
                    current[key] = {}
                current = current[key]
            current[last_key] = value

    return op
