"""
Generic Protocol Buffer Converter.

Provides bidirectional conversion between JSON dictionaries and Protocol Buffer
messages using declarative configuration.

Usage:
    # Create converter from config dict (typically loaded from YAML)
    converter = ProtoConverter(config)

    # Convert JSON to Proto
    proto = converter.to_proto(json_dict, account_id="extra_field")

    # Convert Proto to JSON
    json_dict = converter.to_json(proto)
"""

import importlib
import logging
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Type, TypeVar

from google.protobuf.message import Message
from google.protobuf.timestamp_pb2 import Timestamp

from proto_converter.compiler import compile_to_json, compile_to_proto
from proto_converter.expressions import (  # noqa: F401 - registries re-exported for existing imports
    CompiledExpression,
    _expression_functions,
    _expression_names,
    check_engine,
    get_default_expression_engine,
    has_name,
    is_plain_name,
    register_expression_function,
    register_expression_name,
    report_failure,
    resolve_name,
)
from proto_converter.helpers import get_nested_value, set_nested_value
from proto_converter.timestamps import parse_epoch, parse_iso8601, parse_yyyymmdd, set_timestamp
from proto_converter.transforms import TRANSFORMS

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=Message)


# =============================================================================
# Mapping path fallback
# =============================================================================

class _PathFallback:
    """
    Resolve a `mappings` value that names a source field (`account_id`, `product.type`)
    when the field is absent from the payload.

    Such a value has always fallen back to expression evaluation, so it can still read
    extra fields, registered names, `data.x` and attributes of source objects. When
    its first name resolves nowhere, the result is None without building an evaluator,
    and the failure is logged once per mapping value instead of once per message.
    """

    __slots__ = ("text", "_root", "_bare", "_expression", "_warned")

    def __init__(self, text: str, engine: str):
        self.text = text
        root = text.split(".", 1)[0]
        self._root = root if is_plain_name(root) else None
        self._bare = self._root is not None and "." not in text
        self._expression = None if self._bare else CompiledExpression(text, engine)
        self._warned = False

    def __call__(self, source: Dict[str, Any], extra: Dict[str, Any]) -> Any:
        try:
            if self._bare:
                return resolve_name(self.text, source, extra)
            if self._root is not None and not has_name(self._root, source, extra):
                raise NameError(f"name '{self._root}' is not defined")
            return self._expression.evaluate(source, extra)
        except Exception as e:
            if not self._warned:
                self._warned = True
                logger.warning(
                    f"Expression eval failed for '{self.text}': {e} "
                    "(not logged again for this mapping value)"
                )
            return None


# =============================================================================
# Proto Converter
# =============================================================================

class ProtoConverter:
    """
    Bidirectional JSON ↔ Proto converter using declarative YAML configurations.

    The `ProtoConverter` avoids hardcoded mapping logic by reading a configuration
    dictionary (usually loaded from YAML) that describes how JSON paths map to
    Protobuf fields. It supports enums, timestamps, data transforms, and complex
    Python expressions.

    Features:
    - Declarative: New data sources can be added by creating a YAML file without changing code.
    - Type Safety: Validates that the generated Protobuf matches the expected class.
    - Consistency: Standardizes the mapping logic across different data sources.
    - Extensible: Custom transforms and expression functions can be registered at runtime.

    Performance:
    - The configuration is compiled into a generated Python function on the first
      conversion (see `compiler.py`). Assigning a new `config` recompiles; after
      mutating the config dict in place, call `recompile()` on this instance.
    - Expressions are parsed once. `expression_engine="native"` compiles them to
      Python code; see `expressions.py` for what that trades away.
    """

    def __init__(self, config: Dict[str, Any], *, expression_engine: Optional[str] = None):
        """
        Initialize the converter with a mapping configuration.

        Args:
            config: Dictionary containing 'proto_class', 'type_maps', and 'fields'.
            expression_engine: "sandboxed" or "native" for this converter's expressions.
                None uses the process default (`set_default_expression_engine`), which
                is "sandboxed" unless changed. Mapping YAML cannot choose the engine.
        """
        self._requested_engine = None if expression_engine is None else check_engine(expression_engine)
        self.config = config

    @property
    def config(self) -> Dict[str, Any]:
        return self._config

    @config.setter
    def config(self, config: Dict[str, Any]) -> None:
        """Replace the configuration, dropping everything compiled from the old one."""
        self._config = config
        self._proto_class = None
        self._proto_module = None
        self._additional_modules = []
        self._type_maps = None
        self._engine: Optional[str] = None
        self._to_proto_fn: Optional[Callable[[Any, Dict[str, Any]], Message]] = None
        self._to_proto_source: Optional[str] = None
        self._to_json_fn: Optional[Callable[[Message], Dict[str, Any]]] = None
        self._extra_setters: Dict[str, Optional[Callable[[Message, Any], None]]] = {}
        self._expressions: Dict[Any, CompiledExpression] = {}
        self._path_fallbacks: Dict[str, _PathFallback] = {}

    def recompile(self) -> None:
        """
        Drop everything derived from the config, so the next conversion compiles it afresh.

        Needed only after mutating `config` in place (assigning a new config already does
        this). Resets the resolved message class, enum maps, expression engine choice,
        compiled functions, parsed expressions and extra-field setters.
        `clear_converter_cache()` does not reach converters you already hold.
        """
        self.config = self._config

    @property
    def expression_engine(self) -> str:
        """The engine this converter's expressions run on, fixed on first use."""
        if self._engine is None:
            self._engine = self._requested_engine or get_default_expression_engine()
        return self._engine

    @property
    def proto_class(self) -> Type[Message]:
        """
        Lazily resolve the Protobuf message class from the string path in config.

        Returns:
            Type[Message]: The resolved Protobuf message class.
        """
        if self._proto_class is None:
            class_path = self.config["proto_class"]
            module_path, class_name = class_path.rsplit(".", 1)
            self._proto_module = importlib.import_module(module_path)
            self._proto_class = getattr(self._proto_module, class_name)
            # Load additional modules for enum resolution (configured in YAML or registered)
            for extra_module in self.config.get("additional_modules", []):
                try:
                    self._additional_modules.append(importlib.import_module(extra_module))
                except ImportError:
                    pass
        return self._proto_class

    @property
    def type_maps(self) -> Dict[str, Dict[str, Any]]:
        """
        Lazily resolve YAML enum maps into bi-directional mapping structures.

        Returns:
            Dict[str, Dict]: Map name -> {
                "forward": {json_val: int_val},
                "reverse": {int_val: json_val},
                "default": int_val
            }
        """
        if self._type_maps is None:
            self._type_maps = {}
            _ = self.proto_class  # Ensure modules are loaded

            for map_name, value_map in self.config.get("type_maps", {}).items():
                forward = {}
                reverse = {}
                default_val = 0

                for json_val, enum_name in value_map.items():
                    resolved_int = self._resolve_enum(enum_name)
                    if json_val == "_default":
                        default_val = resolved_int
                    else:
                        forward[json_val] = resolved_int
                        # Store first mapping as canonical for reverse
                        if resolved_int not in reverse:
                            reverse[resolved_int] = json_val

                self._type_maps[map_name] = {
                    "forward": forward,
                    "reverse": reverse,
                    "default": default_val
                }

        return self._type_maps

    def _resolve_enum(self, enum_name: str) -> int:
        """Internal helper to convert a string enum name to its Protobuf integer value."""
        if hasattr(self._proto_module, enum_name):
            return getattr(self._proto_module, enum_name)
        # Check additional modules
        for mod in self._additional_modules:
            if hasattr(mod, enum_name):
                return getattr(mod, enum_name)
        # Check modules imported by the proto module (e.g. security_pb2 enums
        # used by option_universe_pb2)
        import types
        for attr_name in dir(self._proto_module):
            attr = getattr(self._proto_module, attr_name, None)
            if isinstance(attr, types.ModuleType) and hasattr(attr, enum_name):
                return getattr(attr, enum_name)
        logger.warning(f"Unknown enum: {enum_name}")
        return 0

    def to_proto(self, json_data: Dict[str, Any], **extra_fields) -> T:
        """
        Create a Protobuf message from a JSON dictionary.
        """
        to_proto = self._to_proto_fn
        if to_proto is None:
            to_proto = self._compiled_to_proto()
        return to_proto(json_data, extra_fields)

    def _compiled_to_proto(self) -> Callable[[Any, Dict[str, Any]], Message]:
        """The generated `to_proto(source, extra_fields)` function, compiling it on first use."""
        if self._to_proto_fn is None:
            self._to_proto_fn = compile_to_proto(self)
        return self._to_proto_fn

    def to_json(self, proto: Message) -> Dict[str, Any]:
        """
        Reverse convert a Protobuf message back to a JSON dictionary.

        Args:
            proto: Source Protobuf message.

        Returns:
            Dict[str, Any]: JSON-compatible dictionary.
        """
        to_json = self._to_json_fn
        if to_json is None:
            to_json = self._to_json_fn = compile_to_json(self)
        return to_json(proto)

    def _expression(self, expr: Any) -> CompiledExpression:
        """The compiled form of an expression string, parsed once per converter."""
        try:
            compiled = self._expressions.get(expr)
        except TypeError:  # unhashable YAML value; it fails on evaluation, as before
            return CompiledExpression(expr, self.expression_engine)
        if compiled is None:
            compiled = self._expressions[expr] = CompiledExpression(expr, self.expression_engine)
        return compiled

    def _path_fallback(self, text: str) -> _PathFallback:
        """The fallback for a `mappings` value naming a source field, shared by both paths."""
        fallback = self._path_fallbacks.get(text)
        if fallback is None:
            fallback = self._path_fallbacks[text] = _PathFallback(text, self.expression_engine)
        return fallback

    # =========================================================================
    # Reference implementation
    #
    # The compiler emits fast code for the field shapes it can prove equivalent to
    # these methods and calls them for everything else; the test suite runs both
    # against each other, so behavior never depends on which path a field took.
    # =========================================================================

    def _reference_to_proto(self, json_data: Dict[str, Any], extra_fields: Dict[str, Any]) -> T:
        """`to_proto` without compilation: the behavior the compiled function must match."""
        proto = self.proto_class()

        # 1. Support legacy 'fields' list format
        if "fields" in self.config:
            self._reference_fields(proto, self.config["fields"], json_data, extra_fields)

        # 2. Support new 'mappings' dictionary format (includes recursion)
        if "mappings" in self.config:
            self._apply_mappings(proto, self.config["mappings"], json_data, extra_fields)

        # 3. Set explicit extra fields (override everything)
        for field_name, value in extra_fields.items():
            if hasattr(proto, field_name):
                self._set_proto_field(proto, field_name, value)

        return proto

    def _reference_fields(self, proto: Message, fields: Any, source: Dict[str, Any], extra_fields: Dict[str, Any]) -> None:
        for field_config in fields:
            self._reference_field(proto, field_config, source, extra_fields)

    def _reference_field(self, proto: Message, field_config: Dict[str, Any], source: Dict[str, Any], extra_fields: Dict[str, Any]) -> None:
        value = self._convert_field_to_proto(field_config, source, extra_fields)
        if value is not None:
            self._set_proto_field(proto, field_config["proto_field"], value)

    def _apply_mappings(self, target: Message, mappings: Dict[str, Any], source: Dict[str, Any], extra_fields: Dict[str, Any]) -> None:
        """Recursive helper to apply dictionary-based mappings to a proto message."""
        for field_name, config in mappings.items():
            if isinstance(config, dict) and "proto_class" in config:
                # Nested object mapping
                nested_proto = getattr(target, field_name)
                self._apply_mappings(nested_proto, config["mappings"], source, extra_fields)
            elif isinstance(config, dict):
                # Potential sub-fields for a message (without explicit proto_class)
                nested_proto = getattr(target, field_name)
                for sub_field, sub_config in config.items():
                    val = self._eval_config_value(sub_config, source, extra_fields)
                    if val is not None:
                        self._set_proto_field(nested_proto, sub_field, val)
            else:
                # Direct value mapping (string expression or simple field)
                val = self._eval_config_value(config, source, extra_fields)
                if val is not None:
                    self._set_proto_field(target, field_name, val)

    def _eval_config_value(self, config: Any, source: Dict[str, Any], extra_fields: Dict[str, Any]) -> Any:
        """Evaluate a mapping value which can be a simple string (expression) or complex config."""
        if not isinstance(config, str):
            return config

        # 1. Simple direct field lookup (no operators, just a field name)
        if config.isidentifier():
            if config in source:
                return source[config]
            return self._path_fallback(config)(source, extra_fields)

        # 2. Dotted path lookup (e.g. "product.type")
        if "." in config and " " not in config and not any(c in config for c in "()+-*/"):
            val = get_nested_value(source, config)
            if val is not None:
                return val
            return self._path_fallback(config)(source, extra_fields)

        # 3. Fallback to expression evaluation (the most powerful path)
        try:
            return self._expression(config).evaluate(source, extra_fields)
        except Exception as e:
            return report_failure(config, e, None)

    def _reference_to_json(self, proto: Message) -> Dict[str, Any]:
        """`to_json` without the precompiled plan: the behavior the plan must match."""
        result = {}
        for field_config in self.config.get("fields", []):
            self._reference_json_field(proto, field_config, result)
        return result

    def _reference_json_field(self, proto: Message, field_config: Dict[str, Any], result: Dict[str, Any]) -> None:
        json_path = field_config.get("json_path")
        if not json_path:
            return

        proto_field = field_config["proto_field"]

        # Handle nested proto fields (e.g., "security.id")
        if "." in proto_field:
            parts = proto_field.split(".")
            value = proto
            for part in parts:
                if value is None:
                    break
                value = getattr(value, part, None)
        else:
            value = getattr(proto, proto_field, None)

        # Handle specialized types for JSON output
        if isinstance(value, Timestamp):
            if value.seconds > 0 or value.nanos > 0:
                unit = field_config.get("unit", "ms")
                epoch = value.seconds + value.nanos / 1e9
                value = int(epoch * 1000) if unit == "ms" else int(epoch)
            else:
                value = None
        elif field_config.get("type") == "enum":
            type_map_name = field_config.get("type_map")
            if type_map_name and type_map_name in self.type_maps:
                tmap = self.type_maps[type_map_name]
                value = tmap["reverse"].get(value, value)

        if value is not None:
            set_nested_value(result, json_path, value)

    def _convert_field_to_proto(
        self,
        field_config: Dict[str, Any],
        source: Dict[str, Any],
        extra_fields: Dict[str, Any],
    ) -> Any:
        """
        Execute the specific conversion logic for a single field.

        Supports:
        - `simple`: Direct copy with optional `transform`.
        - `enum`: Lookup via configured `type_maps`.
        - `timestamp`: Parsing of epoch/ISO dates into Protobuf Timestamps.
        - `expression`: Secure execution of Python snippets for computed fields.
        """
        field_type = field_config.get("type", "simple")
        json_path = field_config.get("json_path", "")
        json_value = get_nested_value(source, json_path) if json_path else None
        default = field_config.get("default")

        if field_type == "simple" or field_type is None:
            if json_value is None:
                return default
            transform_name = field_config.get("transform")
            if transform_name and transform_name in TRANSFORMS:
                return TRANSFORMS[transform_name](json_value)
            return json_value

        elif field_type == "enum":
            type_map_name = field_config["type_map"]
            tmap = self.type_maps.get(type_map_name)
            if tmap:
                return tmap["forward"].get(json_value, tmap["default"])
            return 0

        elif field_type == "timestamp":
            if json_value is None:
                return None
            fmt = field_config.get("format", "epoch")
            if fmt == "iso8601":
                # ISO8601 date string (e.g., "2024-07-04T09:30:00Z"); YYYY-MM-DD is UTC midnight
                return parse_iso8601(json_value)
            elif fmt == "date_yyyymmdd":
                # Date string in YYYYMMDD format (e.g., "20260116")
                return parse_yyyymmdd(json_value)
            else:
                # Epoch timestamp (default), in UTC
                return parse_epoch(json_value, field_config.get("unit", "ms") == "ms")

        elif field_type == "constant":
            # Direct constant value from YAML (useful for enums/fixed flags)
            return field_config.get("value")

        elif field_type in ("expression", "calculated"):
            # [SECURITY] Sandboxed Expression Evaluation (see expressions.py)
            # simpleeval parses expressions into an AST and only allows whitelisted
            # operations, blocking attribute chain attacks ().__class__ and imports.
            expr = field_config.get("expression", "")
            try:
                return self._expression(expr).evaluate(source, extra_fields)
            except Exception as e:
                return report_failure(expr, e, default)

        return default

    def __getattr__(self, name):
        """Allow accessing config values as attributes for convenience."""
        config = self.__dict__.get("_config")
        if config is not None and name in config:
            return config[name]
        raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")

    def _set_proto_field(self, proto: Message, field_name: str, value: Any) -> None:
        """
        Safely set a Protobuf field value, handling nested messages and repeated fields automatically.
        """
        # Handle nested field paths (e.g., "inner.field_a")
        if "." in field_name:
            parts = field_name.split(".")
            target = proto
            for part in parts[:-1]:
                target = getattr(target, part)
            field_name = parts[-1]
        else:
            target = proto
        self._assign_field(target, field_name, getattr(target, field_name, None), value)

    def _assign_field(self, target: Message, field_name: str, field_obj: Any, value: Any) -> None:
        """Set `target.<field_name>` (currently `field_obj`) to `value`, adapting the value to the field's type."""
        # Handle Timestamp fields
        if isinstance(field_obj, Timestamp) and isinstance(value, datetime):
            set_timestamp(field_obj, value)
            return

        # Handle google.type.Date fields
        # Check if field_obj is a Date proto (has year, month, day fields)
        if hasattr(field_obj, 'year') and hasattr(field_obj, 'month') and hasattr(field_obj, 'day'):
            if isinstance(value, str) and '-' in value:
                # Parse "YYYY-MM-DD" format
                try:
                    parts = value.split('-')
                    field_obj.year = int(parts[0])
                    field_obj.month = int(parts[1])
                    field_obj.day = int(parts[2])
                    return
                except (ValueError, IndexError) as e:
                    logger.warning(f"Failed to parse date string '{value}': {e}")
            elif isinstance(value, dict) and 'year' in value:
                # Dict format: {"year": 1999, "month": 1, "day": 22}
                field_obj.year = int(value.get('year', 0))
                field_obj.month = int(value.get('month', 0))
                field_obj.day = int(value.get('day', 0))
                return
            elif isinstance(value, datetime):
                field_obj.year = value.year
                field_obj.month = value.month
                field_obj.day = value.day
                return

        # Handle repeated fields (list/tuple)
        if isinstance(value, (list, tuple)):
            # Check if it's a repeated field container
            if hasattr(field_obj, "extend"):
                # Clear and extend
                del field_obj[:]
                field_obj.extend(value)
                return

        # Handle simple assignment
        try:
            setattr(target, field_name, value)
        except (TypeError, AttributeError) as e:
            # Fallback for composite message fields that can't be assigned directly
            # e.g. "Assignment not allowed to composite field"
            if hasattr(field_obj, "CopyFrom") and isinstance(value, Message):
                field_obj.CopyFrom(value)
            elif field_obj is not None and isinstance(value, dict):
                # Attempt to set fields on nested message from dict
                for k, v in value.items():
                    self._set_proto_field(field_obj, k, v)
            else:
                logger.warning(f"Failed to set {field_name}={value}: {e}")
