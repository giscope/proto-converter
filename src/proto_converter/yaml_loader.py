"""
YAML loader with !include directive support.

Allows splitting large mapping configurations into reusable snippets
(e.g., sharing common SecurityId mappings between positions and orders).

Usage:
    from proto_converter import load_yaml_with_includes

    config = load_yaml_with_includes("mappings/position.yaml")
"""

from pathlib import Path
from typing import Any, Dict, Union

import yaml


class IncludeLoader(yaml.SafeLoader):
    """
    Custom YAML loader that supports the `!include` directive.

    This allows splitting large mapping configurations into reusable snippets
    (e.g., sharing common `SecurityId` mappings between positions and orders).
    """
    pass


def _include_constructor(loader: IncludeLoader, node: yaml.Node) -> Any:
    """Constructor for the !include directive in YAML."""
    filepath = Path(loader.name).parent / loader.construct_scalar(node)
    with open(filepath) as f:
        return yaml.load(f, IncludeLoader)


IncludeLoader.add_constructor("!include", _include_constructor)


def load_yaml_with_includes(path: Union[str, Path]) -> Dict[str, Any]:
    """
    Load a YAML file and recursively resolve all `!include` directives.

    Args:
        path: Absolute or relative path to the root YAML file.

    Returns:
        Dict: Fully expanded configuration dictionary.
    """
    with open(path) as f:
        return yaml.load(f, IncludeLoader)
