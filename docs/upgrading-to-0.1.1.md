# Upgrading to 0.1.1

**Status:** CURRENT MIGRATION GUIDE — 0.1.1 performance release

0.1.1 is a performance release. A typical message converted through a factory takes **4.4 µs instead
of 75–80 µs** (about 17×), or **3.4 µs** with the opt-in native expression engine (about 22×). Under
UTC every output is identical to 0.1.0 (commit `974baa1`), except for the corrections listed below.

Pin `v0.1.1` and refresh your lockfile. For a uv project that takes proto-converter from git:

```toml
# pyproject.toml — pin the tag rather than following the branch
[tool.uv.sources]
proto-converter = { git = "https://github.com/giscope/proto-converter.git", tag = "v0.1.1" }
```

```bash
uv lock --upgrade-package proto-converter && uv sync
```

- There are **no protobuf changes** and **no removed or renamed public names**.
- Mapping YAML needs no changes.

## What you must check

Most consumers change nothing. Check each row that applies to you.

| If you… | Then… |
|---|---|
| Convert **epoch** `type: timestamp` fields (the default format) on a host whose time zone is **not UTC** | Values are now correct UTC. Before, they were shifted by the host's UTC offset, so data converted on such a host so far is off by that offset (8 hours late on a UTC+8 machine). Find any stored data or code that compensated for the shift. Hosts running in UTC, such as default Docker containers, are unaffected. |
| Edit `converter.config` **in place** after the converter's first conversion | Call `converter.recompile()` afterwards. The mapping is compiled on first use; assigning a new `converter.config` still recompiles automatically. `clear_converter_cache()` does not refresh converters you already hold. |
| Return **different paths over time** from a factory's `mappings` property | Mappings are now read once per source and the converter is remembered until `clear_converter_cache()`. Call that after changing them. |
| Alert on the warning logged for every message whose `mappings` value cannot be resolved (`Expression eval failed for '…'`) | It is now logged **once per mapping value** per converter. The field is still left unset. |
| Register an expression function that **mutates a nested dict inside the payload** while an expression runs | A nested dict is now copied when the expression first reads it, not when the expression starts, so a change made before that first read is visible. Top-level payload names are still a snapshot from the start, and `data`, `p` and `get()` still read the live payload. |
| Call the private `ProtoConverter._convert_field_to_proto(field_config, source, **extra)` | Its signature is now `(field_config, source, extra_fields)`, taking a dict. `to_proto` no longer calls it for most fields. |

## Corrected behaviour

- **Epoch timestamps are UTC on every host.** `datetime.fromtimestamp(epoch)` built local time, which
  protobuf then read as UTC. Two edge cases changed with it:
  - every epoch on 0001-01-01 (the first day a `Timestamp` can hold) used to leave the field unset on
    every host, UTC included; it now converts;
  - on a non-UTC host, an instant just outside the `Timestamp` range could be shifted back inside it and
    stored wrongly; it is now left unset.
- **Epoch milliseconds are exact.** They were divided by 1000.0 and rounded to microseconds, which is
  exact only between 1833 and 2106. Outside that window values could be off by up to tens of
  microseconds.
- **An extra field named `source` works.** `to_proto(data, source="x")` used to raise `TypeError`.

ISO-8601 parsing gives the same values as before. The fast parser handles only the RFC 3339 shapes on
which it is proven to agree with dateutil; everything else, such as `09:30.5` or ISO week dates, still
goes through dateutil.

## New, if you want it

- **Native expression engine** for trusted mappings: `ProtoConverter(config, expression_engine="native")`
  or `set_default_expression_engine("native")`. It is about twice as fast per expression, but it drops
  simpleeval's runtime resource limits (huge powers, string length, comprehension size). Use it only when
  every mapping YAML is written by trusted developers. Mapping YAML cannot switch it on. Expressions it
  cannot reproduce exactly stay on the sandboxed engine. It applies the installed simpleeval's rules:
  from simpleeval 1.0.4 on, a value that is or contains a module or a forbidden function is refused (for
  example `base64.b64encode(...)`), exactly as on the sandboxed engine.
- `get_default_expression_engine()` and `EXPRESSION_ENGINES`.
- `converter.recompile()` and `converter.expression_engine` (the engine a converter uses, fixed once it compiles).
- `bench/bench_converter.py --check` measures the current code against 0.1.0 (`974baa1`) in one
  process, and fails if a speedup gate is missed or an output differs.

## Repository

- `uv.lock` pins the development and CI environment (`uv run --frozen --extra test pytest`). It does not
  constrain consumers, who resolve proto-converter's dependencies in their own lockfile.
- CI (`.github/workflows/ci.yml`) runs the suite on Python 3.11, 3.12 and 3.13 from the lock, once on the
  lowest versions the manifest allows (protobuf 5.26, simpleeval 1.0.0), and the benchmark against
  `974baa1` (informational on shared runners).

## Performance

Medians on Apple Silicon with Python 3.13 and protobuf 7.34 (upb), one thread:

| Workload | 0.1.0 | 0.1.1 |
|---|---|---|
| Typical message through a factory (6 fields, 1 expression, 1 ISO timestamp) | 75–80 µs | 4.4 µs sandboxed / 3.4 µs native |
| `BaseProtoFactory._create`, one field | 19 µs | 0.33 µs |
| One `type: expression` field | 16 µs | 1.7 µs sandboxed / 0.9 µs native |
| `mappings` value missing from the payload | 14.5 µs | 0.9 µs |
| ISO-8601 timestamp field | 17 µs | 0.84 µs |
| Six plain fields | 5.5 µs | 1.7 µs |

The first conversion with each mapping now also compiles it, about 0.2 ms once per mapping.

Pin commit: the tagged `v0.1.1` commit on `master`.
