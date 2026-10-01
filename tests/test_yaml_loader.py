from proto_converter.yaml_loader import load_yaml_with_includes


def test_load_yaml_with_includes_resolves_relative_fragments(tmp_path):
    (tmp_path / "fields.yaml").write_text(
        "- json_path: person.name\n"
        "  proto_field: name\n",
        encoding="utf-8",
    )
    (tmp_path / "root.yaml").write_text(
        "mapping_id: sample\n"
        "fields: !include fields.yaml\n",
        encoding="utf-8",
    )

    loaded = load_yaml_with_includes(tmp_path / "root.yaml")
    assert loaded["mapping_id"] == "sample"
    assert loaded["fields"][0]["json_path"] == "person.name"
    assert loaded["fields"][0]["proto_field"] == "name"


def test_load_yaml_with_includes_supports_nested_includes(tmp_path):
    fragments = tmp_path / "fragments"
    fragments.mkdir()
    (fragments / "child.yaml").write_text("value: 123\n", encoding="utf-8")
    (fragments / "parent.yaml").write_text(
        "nested: !include child.yaml\n",
        encoding="utf-8",
    )
    (tmp_path / "root.yaml").write_text(
        "root: !include fragments/parent.yaml\n",
        encoding="utf-8",
    )

    loaded = load_yaml_with_includes(tmp_path / "root.yaml")
    assert loaded["root"]["nested"]["value"] == 123
