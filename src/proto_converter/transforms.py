"""
Transform registry for ProtoConverter field conversions.

Built-in transforms handle common type coercions (str, int, float, etc.).
Domain-specific transforms can be registered at runtime via `register_transform`.

Usage:
    from proto_converter.transforms import register_transform

    register_transform("my_uid", lambda data: generate_uid(data))
"""

from typing import Any, Callable, Dict


# =============================================================================
# Built-in Transforms
# =============================================================================

TRANSFORMS: Dict[str, Callable[[Any], Any]] = {
    "str": str,
    "int": int,
    "float": lambda x: float(x) if x and str(x).lower() != "none" else 0.0,
    "abs_float": lambda x: abs(float(x)) if x and str(x).lower() != "none" else 0.0,
    "bool": bool,
    "empty_if_none": lambda x: x if x else "",
    "parse_date": lambda x: {
        "year": int(x.split("-")[0]),
        "month": int(x.split("-")[1]),
        "day": int(x.split("-")[2]),
    } if x and "-" in x else None,
}


def register_transform(name: str, fn: Callable[[Any], Any]) -> None:
    """
    Register a named transform function for use in YAML mapping configurations.

    Once registered, the transform can be referenced by name in field configs:
        fields:
          - json_path: data.value
            proto_field: my_field
            transform: my_transform_name

    Args:
        name: The name to reference in YAML configs.
        fn: A callable that accepts the raw JSON value and returns the transformed value.
    """
    TRANSFORMS[name] = fn
