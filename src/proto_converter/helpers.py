"""
Utility functions for nested dictionary access.

These are the building blocks used by ProtoConverter for reading/writing
values from deeply nested JSON structures using dot-notation paths.
"""

from typing import Any, Dict


def get_nested_value(data: Dict[str, Any], path: str, default: Any = None) -> Any:
    """
    Retrieve a value from a nested dictionary using dot notation.

    Args:
        data: The source dictionary.
        path: Dot-separated path (e.g., 'profile.address.city').
        default: Value to return if the path is not found.

    Returns:
        The value at the path, or `default` if not found.
    """
    keys = path.split(".")
    current = data
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return default
    return current


def set_nested_value(data: Dict[str, Any], path: str, value: Any) -> None:
    """
    Set a value in a nested dictionary using dot notation, creating sub-dicts as needed.

    Args:
        data: The dictionary to modify.
        path: Dot-separated path (e.g., 'meta.id').
        value: The value to set.

    Side Effects:
        - Mutates the `data` dictionary by adding keys/sub-dictionaries.
    """
    keys = path.split(".")
    current = data
    for key in keys[:-1]:
        if key not in current:
            current[key] = {}
        current = current[key]
    current[keys[-1]] = value
