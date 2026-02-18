# Proto Converter

Declarative YAML-driven JSON ↔ Protobuf converter with factory pattern support.

## Features

- **Declarative YAML mappings** for bidirectional JSON ↔ Proto conversion
- **Factory pattern** with caching for high-throughput scenarios
- **Pluggable registries** for transforms and expression functions
- **YAML `!include` directive** for reusable mapping fragments
- **Sandboxed expression evaluation** for computed fields
- **Text and binary serialization** support

## Installation

```bash
# From GitHub
pip install git+https://github.com/giscope/proto-converter.git

# From local path (development)
pip install -e /path/to/proto-converter
```

## Quick Start

```python
from proto_converter import ProtoConverter, load_yaml_with_includes

# Load a YAML mapping
config = load_yaml_with_includes("mappings/my_mapping.yaml")
converter = ProtoConverter(config)

# Convert JSON → Proto
proto = converter.to_proto(json_data)

# Convert Proto → JSON
json_back = converter.to_json(proto)
```

## Extending

```python
from proto_converter import register_transform, register_expression_function

# Register custom transforms for YAML field configs
register_transform("my_uid", lambda data: generate_uid(data))

# Register functions available in YAML expressions
register_expression_function("my_func", my_custom_function)
```

## YAML Mapping Format

```yaml
mapping_id: "my_mapping"
proto_class: mypackage.my_pb2.MyMessage

type_maps:
  status_map:
    active: STATUS_ACTIVE
    inactive: STATUS_INACTIVE
    _default: STATUS_UNSPECIFIED

fields:
  - json_path: name
    proto_field: name
  - json_path: count
    proto_field: count
    transform: int
  - json_path: status
    proto_field: status
    type: enum
    type_map: status_map
  - proto_field: full_name
    type: expression
    expression: "f\"{get('first_name')} {get('last_name')}\""
```

## Requirements

- Python ≥ 3.11
- protobuf ≥ 5.0.0
- pyyaml ≥ 6.0
- simpleeval ≥ 1.0.0
