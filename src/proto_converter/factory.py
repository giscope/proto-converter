"""
Proto Factory Base Module.

Provides the abstract base class for type-safe proto factories with
cached ProtoConverter instances for high-performance conversions.

Usage:
    from proto_converter import BaseProtoFactory, get_cached_converter

    class MyFactory(BaseProtoFactory[MyProto]):
        @property
        def proto_class(self):
            return MyProto

        @property
        def mappings(self):
            return {"source_a": Path("mappings/source_a.yaml")}
"""

import logging
import yaml
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Generic, List, Optional, Type, TypeVar

from google.protobuf.message import Message

from proto_converter.converter import ProtoConverter
from proto_converter.yaml_loader import load_yaml_with_includes

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=Message)


# =============================================================================
# Global Converter Cache
# =============================================================================

# Class-level cache for ProtoConverter instances, keyed by mapping file path
_converter_cache: Dict[str, ProtoConverter] = {}


def get_cached_converter(mapping_path: Path | str) -> ProtoConverter:
    """
    Retrieve or initialize a cached `ProtoConverter` for the given mapping YAML.

    This optimization ensures that YAML parsing and internal configuration setup
    happens only once per mapping file, which is critical for high-throughput
    streaming data (hundreds of positions or transactions per second).

    Args:
        mapping_path: The absolute Path or string path to the YAML mapping configuration.

    Returns:
        ProtoConverter: A reusable, thread-safe (stateless) converter instance.
    """
    if isinstance(mapping_path, str):
        mapping_path = Path(mapping_path)
        
    key = str(mapping_path.resolve())
    if key not in _converter_cache:
        config = load_yaml_with_includes(mapping_path)
        _converter_cache[key] = ProtoConverter(config)
    return _converter_cache[key]


def clear_converter_cache() -> None:
    """
    Flush all cached `ProtoConverter` instances.

    Use this during testing or if YAML mappings are dynamically updated without
    restarting the server process.
    """
    _converter_cache.clear()
    _mapping_id_registry.clear()
    global _registry_initialized
    _registry_initialized = False


# =============================================================================
# Mapping ID Registry - Dynamic lookup by mapping_id
# =============================================================================

_mapping_id_registry: Dict[str, Path] = {}
_registry_initialized = False
_mapping_dirs: List[Path] = []


def register_mapping_dir(directory: Path) -> None:
    """
    Register a directory to scan for YAML mapping files.

    Call this at application startup to register directories containing
    your mapping YAML files. The registry is built lazily on first lookup.

    Args:
        directory: Path to a directory containing .yaml mapping files.
    """
    global _registry_initialized
    if directory not in _mapping_dirs:
        _mapping_dirs.append(directory)
        _registry_initialized = False  # Force re-scan on next lookup


def _initialize_mapping_registry() -> None:
    """
    Scan registered mapping directories and build a mapping_id -> path registry.
    Called lazily on first lookup.
    """
    global _registry_initialized
    if _registry_initialized:
        return

    for mapping_dir in _mapping_dirs:
        if not mapping_dir.exists():
            continue
        for yaml_file in mapping_dir.glob("*.yaml"):
            try:
                with open(yaml_file) as f:
                    config = yaml.safe_load(f)
                if config and "mapping_id" in config:
                    _mapping_id_registry[config["mapping_id"]] = yaml_file
            except Exception:
                pass  # Skip malformed files

    _registry_initialized = True


def get_converter_by_mapping_id(mapping_id: str) -> Optional[ProtoConverter]:
    """
    Get a ProtoConverter by its mapping_id.

    Scans mapping YAML files on first call and caches the registry.

    Args:
        mapping_id: The unique identifier from the YAML's mapping_id field

    Returns:
        ProtoConverter if found, None otherwise
    """
    _initialize_mapping_registry()

    if mapping_id not in _mapping_id_registry:
        return None

    return get_cached_converter(_mapping_id_registry[mapping_id])


# =============================================================================
# Base Proto Factory
# =============================================================================

class BaseProtoFactory(ABC, Generic[T]):
    """
    Abstract base class for type-safe Protobuf factories.

    Factories act as the high-level API for data conversion, shielding the rest
    of the system from the underlying `ProtoConverter` and mapping file details.

    Design Patterns:
    - Factory Method: Subclasses define source-specific conversion methods.
    - Singleton/Registry: Factories are often used via global getter functions.

    Usage Example:
        ```python
        factory = get_account_factory()
        snapshot = factory.from_ibkr(raw_json, account_id="U12345")
        ```
    """

    @property
    @abstractmethod
    def proto_class(self) -> Type[T]:
        """The specific Protobuf message class (type) this factory is designed to produce."""
        ...

    @property
    @abstractmethod
    def mappings(self) -> Dict[str, Path]:
        """
        Registry of source names to their corresponding YAML mapping file paths.

        Example:
            {"etrade": Path("mappings/etrade_pos.yaml"), "ibkr": Path("mappings/ibkr_pos.yaml")}
        """
        ...

    def _get_converter(self, source: str) -> ProtoConverter:
        """
        Retrieve the cached converter for the source, validating type compatibility.

        Args:
            source: The source identifier (e.g., 'etrade').

        Returns:
            ProtoConverter: Ready-to-use converter.

        Raises:
            ValueError: If the source is not registered in `mappings`.
            TypeError: If the converter's target Proto class doesn't match `self.proto_class`.
        """
        if source not in self.mappings:
            available = list(self.mappings.keys())
            raise ValueError(f"Unknown source '{source}'. Available: {available}")

        mapping_path = self.mappings[source]
        converter = get_cached_converter(mapping_path)

        # Validate proto_class matches (first time only, cached after)
        self._validate_proto_class(converter, source)

        return converter

    def _validate_proto_class(self, converter: ProtoConverter, source: str) -> None:
        """
        Ensure the mapping's target Protobuf class matches the factory's expected type.
        """
        expected_name = self.proto_class.DESCRIPTOR.full_name
        actual_name = converter.proto_class.DESCRIPTOR.full_name

        if expected_name != actual_name:
            raise TypeError(
                f"Factory expects {expected_name} but {source} mapping "
                f"produces {actual_name}"
            )

    def _create(self, source: str, json_data: Dict[str, Any], **extra) -> T:
        """
        Core internal method for single-object conversion.

        Args:
            source: Source mapping to use.
            json_data: Raw JSON dictionary from the provider.
            **extra: Additional metadata to inject into the Proto message.

        Returns:
            T: Populated Protobuf message.
        """
        converter = self._get_converter(source)
        return converter.to_proto(json_data, **extra)

    def _create_many(
        self,
        source: str,
        json_list: List[Dict[str, Any]],
        **extra
    ) -> List[T]:
        """
        Batch conversion method for lists of objects (e.g., positions).

        Efficiently reuses the same converter across all items in the list.
        """
        converter = self._get_converter(source)
        return [converter.to_proto(item, **extra) for item in json_list]

    def _to_json(self, source: str, proto: T) -> Dict[str, Any]:
        """
        Convert a Protobuf message back to its source-specific JSON format.

        Useful for generating mock data or submitting updates back to broker APIs.
        """
        converter = self._get_converter(source)
        return converter.to_json(proto)
