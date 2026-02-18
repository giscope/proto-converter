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
from datetime import date, datetime
from typing import Any, Callable, Dict, Type, TypeVar

from google.protobuf.message import Message
from google.protobuf.timestamp_pb2 import Timestamp

from proto_converter.helpers import get_nested_value, set_nested_value
from proto_converter.transforms import TRANSFORMS

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=Message)


# =============================================================================
# Expression Context Registry
# =============================================================================

# Functions available inside YAML expression evaluations
_expression_functions: Dict[str, Callable] = {}

# Named constants/variables available inside YAML expression evaluations
_expression_names: Dict[str, Any] = {}


def register_expression_function(name: str, fn: Callable) -> None:
    """
    Register a function available in YAML `type: expression` evaluations.

    Example:
        register_expression_function("generate_uid", my_uid_generator)

        # Then in YAML:
        #   expression: "generate_uid(data)"
    """
    _expression_functions[name] = fn


def register_expression_name(name: str, value: Any) -> None:
    """
    Register a named constant or variable available in YAML expressions.

    Example:
        register_expression_name("SECURITY_TYPE_EQUITY", 1)

        # Then in YAML:
        #   expression: "SECURITY_TYPE_EQUITY if ticker else 0"
    """
    _expression_names[name] = value


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
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the converter with a mapping configuration.

        Args:
            config: Dictionary containing 'proto_class', 'type_maps', and 'fields'.
        """
        self.config = config
        self._proto_class = None
        self._proto_module = None
        self._additional_modules = []
        self._type_maps = None

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
        proto = self.proto_class()

        # 1. Support legacy 'fields' list format
        if "fields" in self.config:
            for field_config in self.config["fields"]:
                value = self._convert_field_to_proto(field_config, json_data, **extra_fields)
                if value is not None:
                    self._set_proto_field(proto, field_config["proto_field"], value)

        # 2. Support new 'mappings' dictionary format (includes recursion)
        if "mappings" in self.config:
            self._apply_mappings(proto, self.config["mappings"], json_data, extra_fields)

        # 3. Set explicit extra fields (override everything)
        for field_name, value in extra_fields.items():
            if hasattr(proto, field_name):
                self._set_proto_field(proto, field_name, value)

        return proto

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
        if config.isidentifier() and config in source:
            return source[config]

        # 2. Dotted path lookup (e.g. "product.type")
        if "." in config and " " not in config and not any(c in config for c in "()+-*/"):
            val = get_nested_value(source, config)
            if val is not None:
                return val

        # 3. Fallback to expression evaluation (the most powerful path)
        field_config = {"type": "expression", "expression": config}
        return self._convert_field_to_proto(field_config, source, **extra_fields)

    def to_json(self, proto: Message) -> Dict[str, Any]:
        """
        Reverse convert a Protobuf message back to a JSON dictionary.

        Args:
            proto: Source Protobuf message.

        Returns:
            Dict[str, Any]: JSON-compatible dictionary.
        """
        result = {}

        for field_config in self.config.get("fields", []):
            json_path = field_config.get("json_path")
            if not json_path:
                continue

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

        return result

    def _convert_field_to_proto(
        self,
        field_config: Dict[str, Any],
        source: Dict[str, Any],
        **extra_fields
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
            try:
                fmt = field_config.get("format", "epoch")
                if fmt == "iso8601":
                    # Parse ISO8601 date string (e.g., "2024-07-04T09:30:00Z")
                    import dateutil.parser
                    val_str = str(json_value)
                    # If it's a date-only string (YYYY-MM-DD), force it to UTC midnight
                    if len(val_str) == 10 and val_str.count("-") == 2 and "T" not in val_str:
                        val_str += "T00:00:00Z"
                    return dateutil.parser.parse(val_str)
                elif fmt == "date_yyyymmdd":
                    # Parse date string in YYYYMMDD format (e.g., "20260116")
                    date_str = str(json_value)
                    if len(date_str) >= 8:
                        return datetime.strptime(date_str[:8], "%Y%m%d")
                    return None
                else:
                    # Epoch timestamp (default)
                    unit = field_config.get("unit", "ms")
                    epoch = int(json_value)
                    if unit == "ms":
                        epoch = epoch / 1000
                    return datetime.fromtimestamp(epoch)
            except (ValueError, TypeError, OSError) as e:
                logger.debug(f"Timestamp parse error: {e}")
                return None

        elif field_type == "constant":
            # Direct constant value from YAML (useful for enums/fixed flags)
            return field_config.get("value")

        elif field_type in ("expression", "calculated"):
            expr = field_config.get("expression", "")
            try:
                # [SECURITY] Sandboxed Expression Evaluation
                # simpleeval parses expressions into an AST and only allows whitelisted
                # operations, blocking attribute chain attacks ().__class__ and imports.
                from simpleeval import EvalWithCompoundTypes
                import base64

                # Built-in safe functions
                safe_functions = {
                    "int": int, "str": str, "float": float, "bool": bool,
                    "abs": abs, "len": len, "min": min, "max": max, "round": round,
                    "get": lambda p, default=None: get_nested_value(source, p, default),
                    "datetime": datetime,
                }
                # Merge registered expression functions
                safe_functions.update(_expression_functions)

                # Helper to allow dot-access on dictionaries in expressions
                class DotDict(dict):
                    def __getattr__(self, name):
                        if name in self:
                            val = self[name]
                            if isinstance(val, dict):
                                return DotDict(val)
                            return val
                        return None

                # Context variables accessible in expressions
                names = {
                    "data": source,
                    "p": source,  # Compatibility
                    "base64": base64,
                    "list": list,
                    "dict": dict,
                    # Provide extra fields and source data directly to expressions
                    **{k: (DotDict(v) if isinstance(v, dict) else v) for k, v in source.items()},
                    **extra_fields,
                }
                # Merge registered expression names
                names.update(_expression_names)

                import ast
                evaluator = EvalWithCompoundTypes(names=names, functions=safe_functions)
                # Explicitly enable list/dict nodes even if the whitelisting is strict
                evaluator.nodes[ast.List] = evaluator._eval_list
                evaluator.nodes[ast.Dict] = evaluator._eval_dict
                return evaluator.eval(expr)
            except Exception as e:
                logger.warning(f"Expression eval failed for '{expr}': {e}")
                return default

        return default

    def __getattr__(self, name):
        """Allow accessing config values as attributes for convenience."""
        if name in self.config:
            return self.config[name]
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
            field_obj = getattr(target, field_name, None)
        else:
            target = proto
            field_obj = getattr(proto, field_name, None)

        # Handle Timestamp fields
        if isinstance(field_obj, Timestamp) and isinstance(value, datetime):
            field_obj.FromDatetime(value)
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
