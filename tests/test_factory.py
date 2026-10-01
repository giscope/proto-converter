from pathlib import Path

import pytest
import yaml
from google.protobuf.timestamp_pb2 import Timestamp

from proto_converter.factory import BaseProtoFactory
from proto_converter.factory import clear_converter_cache
from proto_converter.factory import get_cached_converter
from proto_converter.factory import get_converter_by_mapping_id
from proto_converter.factory import register_mapping_dir


def _write_mapping(path: Path, config: dict) -> Path:
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


class SampleFactory(BaseProtoFactory):
    def __init__(self, proto_class, mappings):
        self._proto_class = proto_class
        self._mappings = mappings

    @property
    def proto_class(self):
        return self._proto_class

    @property
    def mappings(self):
        return self._mappings


def test_get_cached_converter_reuses_same_instance(tmp_path, proto_module):
    mapping_path = _write_mapping(
        tmp_path / "sample.yaml",
        {
            "proto_class": f"{proto_module.__name__}.Sample",
            "fields": [{"json_path": "name", "proto_field": "name"}],
        },
    )

    converter_a = get_cached_converter(mapping_path)
    converter_b = get_cached_converter(str(mapping_path))

    assert converter_a is converter_b


def test_clear_converter_cache_drops_cached_instances(tmp_path, proto_module):
    mapping_path = _write_mapping(
        tmp_path / "sample.yaml",
        {
            "proto_class": f"{proto_module.__name__}.Sample",
            "fields": [{"json_path": "name", "proto_field": "name"}],
        },
    )

    first = get_cached_converter(mapping_path)
    clear_converter_cache()
    second = get_cached_converter(mapping_path)

    assert first is not second


def test_get_converter_by_mapping_id_from_registered_directory(tmp_path, proto_module):
    mapping_dir = tmp_path / "mappings"
    mapping_dir.mkdir()
    _write_mapping(
        mapping_dir / "sample.yaml",
        {
            "mapping_id": "sample_map",
            "proto_class": f"{proto_module.__name__}.Sample",
            "fields": [{"json_path": "name", "proto_field": "name"}],
        },
    )
    _write_mapping(mapping_dir / "invalid.yaml", {"fields": "not-valid-for-converter"})

    register_mapping_dir(mapping_dir)
    converter = get_converter_by_mapping_id("sample_map")

    assert converter is not None
    assert converter.proto_class is proto_module.Sample
    assert get_converter_by_mapping_id("missing") is None


def test_base_factory_create_many_and_to_json(tmp_path, proto_module):
    mapping_path = _write_mapping(
        tmp_path / "sample.yaml",
        {
            "proto_class": f"{proto_module.__name__}.Sample",
            "fields": [{"json_path": "name", "proto_field": "name"}],
        },
    )
    factory = SampleFactory(proto_module.Sample, {"alpha": mapping_path})

    one = factory._create("alpha", {"name": "A"})
    many = factory._create_many("alpha", [{"name": "A"}, {"name": "B"}])
    back = factory._to_json("alpha", one)

    assert one.name == "A"
    assert [item.name for item in many] == ["A", "B"]
    assert back == {"name": "A"}


def test_base_factory_raises_for_unknown_source(tmp_path, proto_module):
    mapping_path = _write_mapping(
        tmp_path / "sample.yaml",
        {
            "proto_class": f"{proto_module.__name__}.Sample",
            "fields": [{"json_path": "name", "proto_field": "name"}],
        },
    )
    factory = SampleFactory(proto_module.Sample, {"alpha": mapping_path})

    with pytest.raises(ValueError, match="Unknown source"):
        factory._create("missing", {"name": "x"})


def test_base_factory_raises_for_proto_type_mismatch(tmp_path, proto_module):
    bad_mapping_path = _write_mapping(
        tmp_path / "bad.yaml",
        {
            "proto_class": "google.protobuf.timestamp_pb2.Timestamp",
            "fields": [],
        },
    )
    factory = SampleFactory(proto_module.Sample, {"bad": bad_mapping_path})

    with pytest.raises(TypeError, match="Factory expects"):
        factory._create("bad", {})

    assert Timestamp.DESCRIPTOR.full_name == "google.protobuf.Timestamp"


def test_cached_converter_skips_path_resolution_after_first_lookup(tmp_path, proto_module, monkeypatch):
    mapping_path = _write_mapping(
        tmp_path / "sample.yaml",
        {"proto_class": f"{proto_module.__name__}.Sample", "fields": [{"json_path": "name", "proto_field": "name"}]},
    )
    first = get_cached_converter(mapping_path)

    calls = []
    original = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: calls.append(self) or original(self, *a, **k))
    assert get_cached_converter(mapping_path) is first
    assert get_cached_converter(str(mapping_path)) is first
    assert calls == [] or len(calls) == 1  # a new spelling resolves once, then it is cached too
    calls.clear()
    assert get_cached_converter(str(mapping_path)) is first
    assert calls == []


def test_relative_paths_are_cached_per_working_directory(tmp_path, proto_module, monkeypatch):
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        _write_mapping(
            tmp_path / name / "m.yaml",
            {"proto_class": f"{proto_module.__name__}.Sample", "mapping_id": name},
        )
    monkeypatch.chdir(tmp_path / "a")
    in_a = get_cached_converter("m.yaml")
    monkeypatch.chdir(tmp_path / "b")
    in_b = get_cached_converter("m.yaml")
    assert (in_a.mapping_id, in_b.mapping_id) == ("a", "b")


def test_factory_reads_mappings_once_per_source_until_cache_cleared(tmp_path, proto_module):
    mapping_path = _write_mapping(
        tmp_path / "sample.yaml",
        {"proto_class": f"{proto_module.__name__}.Sample", "fields": [{"json_path": "name", "proto_field": "name"}]},
    )

    class CountingFactory(SampleFactory):
        reads = 0

        @property
        def mappings(self):
            CountingFactory.reads += 1
            return self._mappings

    factory = CountingFactory(proto_module.Sample, {"alpha": mapping_path})
    for _ in range(5):
        assert factory._create("alpha", {"name": "A"}).name == "A"
    assert CountingFactory.reads == 1

    before = factory._get_converter("alpha")
    clear_converter_cache()
    assert factory._get_converter("alpha") is not before
    assert CountingFactory.reads == 2

    with pytest.raises(ValueError, match="Unknown source"):
        factory._create("missing", {})


def test_create_many_passes_extra_fields_to_every_item(tmp_path, proto_module):
    mapping_path = _write_mapping(
        tmp_path / "sample.yaml",
        {"proto_class": f"{proto_module.__name__}.Sample", "fields": [{"json_path": "name", "proto_field": "name"}]},
    )
    factory = SampleFactory(proto_module.Sample, {"alpha": mapping_path})
    many = factory._create_many("alpha", [{"name": "A"}, {"name": "B"}], count=4)
    assert [(m.name, m.count) for m in many] == [("A", 4), ("B", 4)]
