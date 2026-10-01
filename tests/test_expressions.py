import logging
import threading
from datetime import datetime

import pytest

from proto_converter import (
    ProtoConverter,
    register_expression_function,
    register_expression_name,
    set_default_expression_engine,
)
from proto_converter import expressions
from proto_converter.expressions import CompiledExpression

SOURCE = {
    "first": "Ada",
    "last": "Lovelace",
    "qty": 3,
    "px": 2.5,
    "person": {"name": "Ada", "address": {"city": "London"}},
    "items": [1, 2, 3],
    "when": datetime(2024, 7, 4, 9, 30),
    "flag": None,
    "data": {"shadow": True},
}

# Expressions both engines must evaluate identically (value or failure)
CORPUS = [
    "first + ' ' + last",
    "qty * px - 1",
    "qty // 2 + qty % 2 + qty ** 2",
    "-qty if qty > 2 else +qty",
    "not flag and qty",
    "flag or first",
    "1 < qty <= 3 < 4",
    "qty in items and 9 not in items",
    "flag is None and qty is not None",
    "person.name + '@' + person.address.city",
    "person.missing",
    "person.missing.deeper",
    "person['name']",
    "person.get('name')",
    "data.shadow",
    "p.first",
    "items[1:] + [qty, *items]",
    "items[::-1][0]",
    "(qty, px)[1]",
    "{'a': qty, 'b': [px]}",
    "{qty, 1}",
    "f'{first} {last}'",
    "f'{px:.3f}|{qty:>4}|{first!r}'",
    "str(qty) + str(px)",
    "int('7') + float('1.5') + abs(-2) + len(items) + min(items) + max(items) + round(2.567, 1)",
    "get('person.address.city')",
    "get('person.nope', 'dflt')",
    "datetime(2024, 1, 2).year",
    "when.year + when.month",
    "base64.b64encode(b'ab')",
    "list(items) + list((9,))",
    "dict(a=1)['a']",
    "tuple(items)",
    "acct",
    "acct.upper()",
    "LIMIT * qty",
    "double(qty)",
    "undefined_name",
    "undefined_fn(1)",
    "first.__class__",
    "first.format(1)",
    "[x * 2 for x in items]",
    "{**person}",
    "qty / 0",
    "items[10]",
    "'abc' * 3",
    "1 if True else 2",
    "None",
    "x := 5",
    "lambda: 1",
    "qty = 4",
    "",
    "first +",
]


@pytest.fixture(autouse=True)
def _registrations():
    register_expression_function("double", lambda value: value * 2)
    register_expression_name("LIMIT", 10)


def _outcome(expression, source=SOURCE, extra=None):
    try:
        return ("ok", expression.evaluate(source, extra if extra is not None else {"acct": "u1"}))
    except Exception as e:  # noqa: BLE001 - failures must match too
        return ("raised", type(e).__name__ if not isinstance(e, (NameError, KeyError)) else "missing")


def _normalize(outcome):
    # A missing name is NameNotDefined/FunctionNotDefined in simpleeval and NameError/KeyError natively
    kind, value = outcome
    if kind == "raised" and value in ("NameNotDefined", "FunctionNotDefined"):
        return ("raised", "missing")
    return outcome


@pytest.mark.filterwarnings("ignore::simpleeval.AssignmentAttempted")
@pytest.mark.parametrize("text", CORPUS)
def test_native_engine_matches_sandboxed(text):
    sandboxed = CompiledExpression(text, "sandboxed")
    native = CompiledExpression(text, "native")
    left, right = _normalize(_outcome(sandboxed)), _normalize(_outcome(native))
    if left[0] == "raised":
        assert right[0] == "raised", (text, left, right)
    else:
        assert left == right, text


# An unsupported subtree in every position the transformer visits, after supported parts
FALLBACK_CASES = [
    "person.name if flag else [x for x in []]",
    "person.name if qty else [x for x in []]",
    "person.name + str([x for x in items])",
    "double(person.address.city) + str({**person})",
    "f'{person.name}' + str([x for x in items][0])",
    "{'a': person.name, 'b': [x for x in items]}",
    "person.name == [x for x in items]",
    "flag or person.name or [x for x in items]",
    "items[[x for x in items][0]]",
    "(person.name, first.__class__)",
    "person.get('name', [x for x in items])",
]


@pytest.mark.parametrize("text", FALLBACK_CASES)
def test_native_fallback_evaluates_the_untouched_expression(text):
    native = CompiledExpression(text, "native")
    assert native.engine == "sandboxed"
    for source in (SOURCE, {**SOURCE, "flag": True}):
        assert _outcome(native, source=source) == _outcome(CompiledExpression(text, "sandboxed"), source=source), text
    flagged = {**SOURCE, "flag": True}
    if text.startswith("person.name if flag"):
        assert _outcome(native, source=flagged) == ("ok", "Ada")


def test_native_compile_never_mutates_the_parsed_tree():
    import ast
    for text in CORPUS + FALLBACK_CASES:
        expression = CompiledExpression(text, "sandboxed")
        if expression._parsed is None:
            continue
        before = ast.dump(expression._parsed)
        expressions._compile_native(text, expression._parsed)
        assert ast.dump(expression._parsed) == before, text


@pytest.mark.parametrize("engine", ["sandboxed", "native"])
def test_names_are_a_snapshot_taken_when_evaluation_starts(engine):
    source = {"value": 1, "person": {"name": "old"}}

    def rebind():
        source["value"] = 2

    def edit_nested():
        source["person"]["name"] = "new"

    register_expression_function("rebind", rebind)
    register_expression_function("edit_nested", edit_nested)

    # top-level names: unchanged from 974baa1
    assert CompiledExpression("(rebind(), value)[1]", engine).evaluate(source, {}) == 1
    source["value"] = 1
    # data/p and get() always read the live source
    assert CompiledExpression("(rebind(), data['value'])[1]", engine).evaluate(source, {}) == 2
    source["value"] = 1
    assert CompiledExpression("(rebind(), get('value'))[1]", engine).evaluate(source, {}) == 2

    # a nested dict read before the change keeps the value it had at that read
    assert CompiledExpression("(person.name, edit_nested(), person.name)", engine).evaluate(
        source, {}) == ("old", None, "old")
    # documented contract change: a nested dict is copied at its first read, not up front
    source["person"]["name"] = "old"
    assert CompiledExpression("(edit_nested(), person.name)[1]", engine).evaluate(source, {}) == "new"


def test_native_engine_leaves_unreproducible_expressions_sandboxed():
    for text in ("[x * 2 for x in items]", "{**person}", "first.__class__", "first.format(1)", "qty = 4"):
        assert CompiledExpression(text, "native").engine == "sandboxed", text
    for text in ("first + ' ' + last", "person.name", "f'{px:.1f}'", "double(qty)"):
        assert CompiledExpression(text, "native").engine == "native", text


def test_native_engine_cannot_reach_its_helpers_or_builtins():
    for text in ("__builtins__", "open", "getattr", "eval", "__import__('os')"):
        assert _outcome(CompiledExpression(text, "native"))[0] == "raised", text
    # a payload key spelled like a helper cannot replace it
    shadowing = {**SOURCE, "\u00b7attr": "payload", "\u00b7fn": "payload"}
    assert _outcome(CompiledExpression("person.name + str(qty)", "native"), source=shadowing) == ("ok", "Ada3")


def test_both_engines_apply_the_installed_simpleevals_result_checks():
    """simpleeval >= 1.0.4 refuses modules and forbidden functions in any result; older versions do not."""
    import base64 as module
    from types import SimpleNamespace

    strict = expressions._disallowed_items_check is not None
    register_expression_name("HOLDER", SimpleNamespace(ga=getattr, mod=module))
    register_expression_name("WRAPPED", [module])
    register_expression_function("give_module", lambda: module)
    for text in ("HOLDER.ga", "HOLDER.mod", "WRAPPED", "WRAPPED[0]", "give_module()", "base64.b64encode(b'ab')",
                 "[give_module()][0]", "(HOLDER.mod, 1)[1]"):
        sandboxed = _outcome(CompiledExpression(text, "sandboxed"))
        native = CompiledExpression(text, "native")
        assert native.engine == "native", text
        assert (sandboxed[0] == "raised") is strict, (text, sandboxed)
        assert _outcome(native)[0] == sandboxed[0], (text, sandboxed, _outcome(native))


def test_disallowed_registered_function_cannot_be_called():
    register_expression_function("ga", getattr)
    for engine in ("sandboxed", "native"):
        assert _outcome(CompiledExpression("ga(first, 'upper')", engine))[0] == "raised"
        assert _outcome(CompiledExpression("qty + 1", engine)) == ("ok", 4)


def test_name_precedence_registered_then_extra_then_source_then_aliases():
    source = {"x": "source", "data": "source-data"}
    for engine in ("sandboxed", "native"):
        expression = CompiledExpression("x", engine)
        assert expression.evaluate(source, {}) == "source"
        assert expression.evaluate(source, {"x": "extra"}) == "extra"
        register_expression_name("x", "registered")
        assert expression.evaluate(source, {"x": "extra"}) == "registered"
        expressions._expression_names.pop("x")
        # a source key named "data" shadows the whole-source alias, as before
        assert CompiledExpression("data", engine).evaluate(source, {}) == "source-data"
        assert CompiledExpression("p", engine).evaluate(source, {}) is source


def test_non_mapping_source_fails_like_before():
    for engine in ("sandboxed", "native"):
        assert _outcome(CompiledExpression("1 + 1", engine), source=["not", "a", "dict"])[0] == "raised"


def test_parse_errors_warn_on_every_evaluation(caplog, proto_module):
    converter = ProtoConverter({"proto_class": f"{proto_module.__name__}.Sample", "fields": [
        {"proto_field": "name", "type": "expression", "expression": "first +", "default": "dflt"}]})
    with caplog.at_level(logging.WARNING, logger="proto_converter.converter"):
        assert converter.to_proto({}).name == "dflt"
        assert converter.to_proto({}).name == "dflt"
    assert sum("Expression eval failed for 'first +'" in r.message for r in caplog.records) == 2


def test_expression_engine_comes_from_code_not_yaml(proto_module):
    config = {"proto_class": f"{proto_module.__name__}.Sample", "expression_engine": "native",
              "fields": [{"proto_field": "name", "type": "expression", "expression": "first"}]}
    assert ProtoConverter(config).expression_engine == "sandboxed"
    assert ProtoConverter(config, expression_engine="native").expression_engine == "native"
    with pytest.raises(ValueError):
        ProtoConverter(config, expression_engine="fast")


def test_default_engine_applies_to_converters_compiled_afterwards(proto_module):
    config = {"proto_class": f"{proto_module.__name__}.Sample",
              "fields": [{"proto_field": "name", "type": "expression", "expression": "first + last"}]}
    try:
        set_default_expression_engine("native")
        converter = ProtoConverter(config)
        assert converter.to_proto({"first": "a", "last": "b"}).name == "ab"
        assert converter.expression_engine == "native"
        with pytest.raises(ValueError):
            set_default_expression_engine("unsafe")
    finally:
        set_default_expression_engine("sandboxed")
    assert ProtoConverter(config).expression_engine == "sandboxed"


@pytest.mark.parametrize("engine", ["sandboxed", "native"])
def test_nested_conversion_inside_an_expression(proto_module, engine):
    """A registered function may convert another message mid-expression."""
    sample = f"{proto_module.__name__}.Sample"
    inner = ProtoConverter({"proto_class": sample, "fields": [
        {"proto_field": "name", "type": "expression", "expression": "first + '!'"}]}, expression_engine=engine)
    register_expression_function("inner_name", lambda value: inner.to_proto({"first": value}).name)
    outer = ProtoConverter({"proto_class": sample, "fields": [
        {"proto_field": "name", "type": "expression", "expression": "first + inner_name(last) + first"}]},
        expression_engine=engine)
    assert outer.to_proto({"first": "a", "last": "b"}).name == "ab!a"


@pytest.mark.parametrize("engine", ["sandboxed", "native"])
def test_concurrent_conversions_keep_their_own_names(proto_module, engine):
    converter = ProtoConverter({"proto_class": f"{proto_module.__name__}.Sample", "fields": [
        {"proto_field": "name", "type": "expression", "expression": "str(first) + '-' + str(acct)"}]},
        expression_engine=engine)
    errors = []

    def work(worker):
        for i in range(500):
            got = converter.to_proto({"first": worker}, acct=i).name
            if got != f"{worker}-{i}":
                errors.append((worker, i, got))

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
