"""
Proto Converter - Declarative YAML-driven JSON ↔ Protobuf converter.

A standalone library for bidirectional conversion between JSON dictionaries
and Protocol Buffer messages using declarative YAML mapping configurations.

Features:
- Declarative YAML mappings for JSON ↔ Proto conversion
- Factory pattern with caching for high-throughput scenarios
- Pluggable transform and expression registries
- YAML !include directive for reusable mapping fragments
- Sandboxed expression evaluation for computed fields (opt-in native engine)
- Mappings compiled to generated Python on first use
- Text and binary serialization support

Quick Start:
    from proto_converter import ProtoConverter, load_yaml_with_includes

    config = load_yaml_with_includes("mappings/my_mapping.yaml")
    converter = ProtoConverter(config)

    proto = converter.to_proto(json_data)
    json_back = converter.to_json(proto)
"""

# Core converter
from proto_converter.converter import (
    ProtoConverter,
    register_expression_function,
    register_expression_name,
)

# Expression engines
from proto_converter.expressions import (
    EXPRESSION_ENGINES,
    get_default_expression_engine,
    set_default_expression_engine,
)

# Factory pattern
from proto_converter.factory import (
    BaseProtoFactory,
    get_cached_converter,
    clear_converter_cache,
    get_converter_by_mapping_id,
    register_mapping_dir,
)

# Serializer
from proto_converter.serializer import ProtoSerializer

# YAML utilities
from proto_converter.yaml_loader import load_yaml_with_includes

# Transform registry
from proto_converter.transforms import register_transform, TRANSFORMS

# Dict helpers
from proto_converter.helpers import get_nested_value, set_nested_value

__all__ = [
    # Core
    "ProtoConverter",
    # Factory
    "BaseProtoFactory",
    "get_cached_converter",
    "clear_converter_cache",
    "get_converter_by_mapping_id",
    "register_mapping_dir",
    # Serializer
    "ProtoSerializer",
    # YAML
    "load_yaml_with_includes",
    # Transforms & Expressions
    "register_transform",
    "register_expression_function",
    "register_expression_name",
    "set_default_expression_engine",
    "get_default_expression_engine",
    "EXPRESSION_ENGINES",
    "TRANSFORMS",
    # Helpers
    "get_nested_value",
    "set_nested_value",
]
