import logging
from datetime import datetime

import pytest

from proto_converter import ProtoConverter, register_expression_name


@pytest.fixture
def make(proto_module):
    def make(mappings, **kwargs):
        return ProtoConverter({"proto_class": f"{proto_module.__name__}.Sample", "mappings": mappings}, **kwargs)
    return make


def test_missing_identifier_resolves_like_an_expression(make):
    register_expression_name("DEFAULT_NAME", "registered")
    assert make({"name": "account_id"}).to_proto({}, account_id="from-extra").name == "from-extra"
    assert make({"name": "DEFAULT_NAME"}).to_proto({}).name == "registered"
    # present in the payload: the payload wins, even over extra fields (unchanged)
    assert make({"name": "account_id"}).to_proto({"account_id": "payload"}, account_id="extra").name == "payload"


def test_missing_dotted_path_still_reaches_aliases_and_attributes(make):
    assert make({"name": "data.person.name"}).to_proto({"person": {"name": "via-data"}}).name == "via-data"
    assert make({"count": "when.year"}).to_proto({"when": datetime(2024, 7, 4)}).count == 2024
    assert make({"name": "acct.id"}).to_proto({}, acct={"id": "x1"}).name == "x1"


@pytest.mark.parametrize("engine", ["sandboxed", "native"])
def test_unresolvable_mapping_values_warn_once(make, caplog, engine):
    converter = make({"name": "nickname", "count": "profile.age", "tags": "missing.deeper.path"}, expression_engine=engine)
    with caplog.at_level(logging.WARNING, logger="proto_converter.converter"):
        for _ in range(50):
            message = converter.to_proto({"profile": {}})
            assert (message.name, message.count, list(message.tags)) == ("", 0, [])
    warnings = [r.message for r in caplog.records if "Expression eval failed" in r.message]
    assert len(warnings) == 2  # 'nickname' and 'missing.deeper.path'; profile.age is None via DotDict
    assert all("not logged again" in w for w in warnings)


def test_keyword_mapping_values_keep_expression_meaning(make):
    converter = make({"name": "None"})
    assert converter.to_proto({}).name == ""
    assert converter._eval_config_value("True", {}, {}) is True
    assert converter._eval_config_value("None", {"None": "payload key"}, {}) == "payload key"
