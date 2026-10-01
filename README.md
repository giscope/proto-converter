# Proto Converter

Declarative YAML-driven JSON ↔ Protobuf converter with factory pattern support.

## Features

- **Declarative YAML mappings** for bidirectional JSON ↔ Proto conversion
- **Factory pattern** with caching for high-throughput scenarios
- **Pluggable registries** for transforms and expression functions
- **YAML `!include` directive** for reusable mapping fragments
- **Sandboxed expression evaluation** for computed fields, with an opt-in native engine
- **Compiled mappings**: each mapping becomes one generated Python function on first use
- **Text and binary serialization** support

## Installation

```bash
# From GitHub (pin a release tag)
pip install "proto-converter @ git+https://github.com/giscope/proto-converter.git@v0.1.1"

# From local path (development)
pip install -e /path/to/proto-converter
```

Upgrading from 0.1.0? Read [docs/upgrading-to-0.1.1.md](docs/upgrading-to-0.1.1.md).

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

## Performance

Each mapping is compiled into a generated Python function the first time it converts
(about 0.2 ms per mapping), and every per-message cost that used to be repeated —
path resolution, expression parsing, evaluator setup, date parsing — now happens once.
`bench/bench_converter.py` measures the current code against the pre-overhaul commit
`974baa1` in the same process and checks that outputs are identical:

| Workload | 974baa1 | now |
|---|---|---|
| Typical message via a factory (6 fields + expression + ISO timestamp) | 80 µs | 4.4 µs sandboxed / 3.6 µs native |
| `BaseProtoFactory._create`, one field | 19 µs | 0.33 µs |
| One expression field | 16 µs | 1.7 µs sandboxed / 0.9 µs native |

```bash
python bench/bench_converter.py --check   # exit 1 if a speedup gate is missed or an output differs
```

Things to know:

- **Config is compiled on first use.** Assigning a new `converter.config` recompiles. If you
  mutate the config dict in place, call `converter.recompile()` on the instance you hold.
  `clear_converter_cache()` only affects converters fetched from the cache afterwards.
- **Factory `mappings` are static.** A factory reads `mappings` and `proto_class` once per
  source and remembers the converter until `clear_converter_cache()`.
- **Batch conversion**: `_create_many` reuses the compiled function for every item.

## Expression engines

`type: expression` fields and expression-valued `mappings` run on one of two engines:

- **`sandboxed`** (default) — simpleeval with its whitelist and runtime limits.
- **`native`** (opt-in) — the expression is checked against the same node, operator, name and
  attribute rules and compiled to Python bytecode. About twice as fast per expression, but it
  drops simpleeval's runtime resource limits (huge powers, string length, comprehension size).
  Use it only when every mapping YAML is written by trusted developers. Expressions it cannot
  reproduce exactly (comprehensions, `**` unpacking, ...) stay on the sandboxed engine.

```python
from proto_converter import ProtoConverter, set_default_expression_engine

converter = ProtoConverter(config, expression_engine="native")  # one converter
set_default_expression_engine("native")                          # converters compiled from now on
```

The engine is chosen in code only — an `expression_engine` key in mapping YAML is ignored, so
mapping data can never switch the sandbox off. Both engines apply the installed simpleeval's rules:
from simpleeval 1.0.4 on, any value that is or contains a module or a forbidden function is refused
(so `base64.…` in an expression fails), and the native engine refuses exactly what the sandboxed one does.

Expressions read names in this order: registered names, extra fields passed to `to_proto`,
top-level payload keys (dicts support `person.name`), then `data`/`p` (the whole payload),
`base64`, `list`, `dict`, and the expression functions. Top-level payload names are a snapshot
taken when the evaluation starts; `data`, `p` and `get()` read the live payload.

## Behavior changes in the performance release

Compared with `974baa1`:

1. **Epoch timestamps are UTC on every host.** They used to be shifted by the host's UTC
   offset unless the process ran with `TZ=UTC`. Near the ends of Timestamp's range, values on
   0001-01-01 now convert (they were left unset on every host), and on non-UTC hosts instants
   just outside the range are no longer shifted back inside it.
2. **Epoch milliseconds are exact.** They were divided by 1000.0 and rounded to microseconds,
   which is exact only between 1833 and 2106; outside that window values could carry float error
   of up to tens of microseconds.
3. **Unresolvable `mappings` paths warn once** per mapping value instead of once per message.
4. **`to_proto(data, source=...)` works**; an extra field named `source` used to crash.
5. **Nested payload dicts in expressions** are copied when an expression first reads them, not
   when it starts, so a registered function that edits a nested payload dict in place before
   the expression reads it is now visible.

ISO-8601 values are unchanged: the fast parser is used only for the RFC 3339 shapes on which it
is proven to agree with dateutil, and dateutil handles everything else as before.

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
