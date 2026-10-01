"""
Expression evaluation for `type: expression` fields and `mappings` values.

An expression is parsed once, when its converter compiles the mapping, and then
evaluated per message by one of two engines:

- ``sandboxed`` (default): simpleeval walks the parsed AST under its whitelist
  and its runtime limits (MAX_POWER, MAX_STRING_LENGTH, comprehension length).
  Evaluators are pooled, so each concurrent evaluation gets its own: this is
  thread-safe and safe for registered functions that convert other messages.
- ``native`` (opt-in): the same AST is checked against the same node, operator,
  name and attribute rules, then compiled to a Python code object. It is about twice
  as fast per expression but drops simpleeval's runtime resource limits, so enable it
  only when every mapping YAML is written by trusted developers. Expressions it cannot
  express exactly (comprehensions, `**` unpacking, ...) stay sandboxed.

The engine is chosen in code (`ProtoConverter(..., expression_engine=...)` or
`set_default_expression_engine`), never from mapping YAML, so mapping data can
never switch the sandbox off.

Names an expression can read, highest precedence first: registered expression
names, extra fields passed to `to_proto`, top-level source keys (dict values are
wrapped so `person.name` works), then `data`/`p` (the whole source), `base64`,
`list` and `dict`, and finally the expression functions.
"""

import ast
import base64
import copy
import keyword
import logging
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from simpleeval import (
    DEFAULT_OPERATORS,
    DISALLOW_FUNCTIONS,
    DISALLOW_METHODS,
    DISALLOW_PREFIXES,
    AttributeDoesNotExist,
    EvalWithCompoundTypes,
    FeatureNotAvailable,
)

from proto_converter.helpers import get_nested_value

# Expression failures have always been logged under the converter's logger name.
logger = logging.getLogger("proto_converter.converter")

SANDBOXED = "sandboxed"
NATIVE = "native"
EXPRESSION_ENGINES = (SANDBOXED, NATIVE)

_default_engine = SANDBOXED


# =============================================================================
# Expression Context Registry
# =============================================================================

# Functions available inside YAML expression evaluations
_expression_functions: Dict[str, Callable] = {}

# Named constants/variables available inside YAML expression evaluations
_expression_names: Dict[str, Any] = {}


def register_expression_function(name: str, fn: Callable) -> None:
    """
    Register a function available in YAML `type: expression` evaluations.

    Example:
        register_expression_function("generate_uid", my_uid_generator)

        # Then in YAML:
        #   expression: "generate_uid(data)"
    """
    _expression_functions[name] = fn


def register_expression_name(name: str, value: Any) -> None:
    """
    Register a named constant or variable available in YAML expressions.

    Example:
        register_expression_name("SECURITY_TYPE_EQUITY", 1)

        # Then in YAML:
        #   expression: "SECURITY_TYPE_EQUITY if ticker else 0"
    """
    _expression_names[name] = value


def set_default_expression_engine(engine: str) -> None:
    """
    Choose the expression engine for converters that do not pick one explicitly.

    Applies to converters compiled afterwards (a converter compiles on its first
    conversion); call `clear_converter_cache()` to recompile cached ones.

    Args:
        engine: "sandboxed" (default) or "native". Only choose "native" when every
            mapping YAML is trusted: it drops simpleeval's runtime resource limits.
    """
    global _default_engine
    _default_engine = check_engine(engine)


def get_default_expression_engine() -> str:
    """Return the engine used by converters that do not pick one explicitly."""
    return _default_engine


def check_engine(engine: str) -> str:
    """Validate an engine name, returning it unchanged."""
    if engine not in EXPRESSION_ENGINES:
        raise ValueError(f"Unknown expression engine {engine!r}; expected one of {EXPRESSION_ENGINES}")
    return engine


# =============================================================================
# Name and function resolution
# =============================================================================

_BASE_NAMES = {"base64": base64, "list": list, "dict": dict}

_BASE_FUNCTIONS = {
    "int": int, "str": str, "float": float, "bool": bool,
    "abs": abs, "len": len, "min": min, "max": max, "round": round,
    "datetime": datetime,
}

# Added by simpleeval's EvalWithCompoundTypes over every other function
_COMPOUND_FUNCTIONS = {"list": list, "tuple": tuple, "dict": dict, "set": set}


class _DotDict(dict):
    """Copy of a source dict whose missing attributes read as None, so `person.name` works."""

    def __getattr__(self, name):
        if name in self:
            val = self[name]
            if isinstance(val, dict):
                return _DotDict(val)
            return val
        return None


class _Functions:
    """The functions an expression can call, resolved on lookup so late registrations apply."""

    __slots__ = ("_source",)

    def __init__(self, source: Dict[str, Any]):
        self._source = source

    def _get_path(self, p, default=None):
        """`get('a.b', default)` inside expressions."""
        return get_nested_value(self._source, p, default)

    def __getitem__(self, key: str) -> Any:
        if key in _COMPOUND_FUNCTIONS:
            return _COMPOUND_FUNCTIONS[key]
        if key in _expression_functions:
            fn = _expression_functions[key]
            if fn in DISALLOW_FUNCTIONS:
                raise FeatureNotAvailable(f"This function {fn} is a really bad idea.")
            return fn
        if key == "get":
            return self._get_path
        return _BASE_FUNCTIONS[key]

    def __contains__(self, key: str) -> bool:
        return (
            key in _COMPOUND_FUNCTIONS
            or key in _expression_functions
            or key == "get"
            or key in _BASE_FUNCTIONS
        )


class _Names:
    """
    Lazy view of the names an expression can read (see module docstring for precedence).

    Top-level source names come from a snapshot taken when the evaluation starts, as
    when the previous implementation built a names dict per evaluation: a registered
    function that rebinds `source[k]` mid-expression does not change later reads of
    `k`. `data`, `p` and `get()` read the live source, as before. Unlike before, a
    nested dict is copied (into a `_DotDict`) at its first read in the evaluation
    rather than up front, so changes a function makes inside a nested source dict
    before the expression first reads it are visible.
    """

    __slots__ = ("_source", "_snapshot", "_extra", "_wrapped", "_functions")

    def __init__(self, source: Dict[str, Any], extra: Dict[str, Any], snapshot: bool = True):
        self._source = source
        self._snapshot = dict(source) if snapshot else source
        self._extra = extra
        self._wrapped: Optional[Dict[str, _DotDict]] = None
        self._functions: Optional[_Functions] = None

    @property
    def functions(self) -> _Functions:
        if self._functions is None:
            self._functions = _Functions(self._source)
        return self._functions

    def __getitem__(self, key: str) -> Any:
        if key in _expression_names:
            return _expression_names[key]
        extra = self._extra
        if key in extra:
            return extra[key]
        snapshot = self._snapshot
        if key in snapshot:
            value = snapshot[key]
            if isinstance(value, dict):
                # One wrapped copy per evaluation, as when every key was copied up front
                wrapped = self._wrapped
                if wrapped is None:
                    wrapped = self._wrapped = {}
                if key not in wrapped:
                    wrapped[key] = _DotDict(value)
                return wrapped[key]
            return value
        if key == "data" or key == "p":
            return self._source
        return _BASE_NAMES[key]


def _check_source(source: Any) -> None:
    """Fail like the previous implementation did for sources that are not mappings."""
    if type(source) is not dict:
        source.items  # noqa: B018 - AttributeError for non-mappings, as before


def has_name(name: str, source: Dict[str, Any], extra: Dict[str, Any]) -> bool:
    """Whether a bare `name` would resolve in an expression over this source."""
    _check_source(source)
    return (
        name in _expression_names
        or name in extra
        or name in source
        or name == "data"
        or name == "p"
        or name in _BASE_NAMES
        or name in _COMPOUND_FUNCTIONS
        or name in _expression_functions
        or name == "get"
        or name in _BASE_FUNCTIONS
    )


def resolve_name(name: str, source: Dict[str, Any], extra: Dict[str, Any]) -> Any:
    """
    Evaluate a bare name exactly as an expression would, without an evaluator.

    Raises:
        NameError: if the name does not resolve.
    """
    _check_source(source)
    names = _Names(source, extra, snapshot=False)  # one lookup, nothing can run in between
    try:
        return names[name]
    except (KeyError, TypeError):
        pass
    functions = names.functions
    if name in functions:
        return functions[name]
    raise NameError(f"name '{name}' is not defined")


# =============================================================================
# Sandboxed engine (simpleeval)
# =============================================================================

# Idle evaluators. Popping one per evaluation keeps evaluations independent across
# threads and across nested conversions started from registered functions.
_evaluator_pool: list = []


def _new_evaluator() -> EvalWithCompoundTypes:
    evaluator = EvalWithCompoundTypes(names={}, functions={})
    # Explicitly enable list/dict nodes even if the whitelisting is strict
    evaluator.nodes[ast.List] = evaluator._eval_list
    evaluator.nodes[ast.Dict] = evaluator._eval_dict
    return evaluator


def _eval_sandboxed(expr: str, parsed: ast.AST, names: _Names) -> Any:
    try:
        evaluator = _evaluator_pool.pop()
    except IndexError:
        evaluator = _new_evaluator()
    evaluator.names = names
    evaluator.functions = names.functions
    try:
        return evaluator.eval(expr, previously_parsed=parsed)
    finally:
        # Don't keep message data alive while the evaluator sits in the pool
        evaluator.names = evaluator.functions = None
        _evaluator_pool.append(evaluator)


# =============================================================================
# Native engine (opt-in)
# =============================================================================

# Helper names in the compiled code. "·" cannot appear in an identifier, so
# expression text can never refer to them.
_ATTR = "·attr"
_JOIN = "·join"
_FORMAT = "·fmt"
_CHECK = "·chk"

# simpleeval 1.0.4 and later refuse any intermediate result that is (or contains) a
# module or a forbidden function; earlier versions do not. The native engine applies
# whatever the installed simpleeval applies, by calling simpleeval's own check.
_disallowed_items_check = getattr(
    EvalWithCompoundTypes(names={}, functions={}), "_check_disallowed_items", None
)
_ALWAYS_ALLOWED = frozenset({int, float, str, bool, type(None), bytes, complex})


def _native_check(value: Any) -> Any:
    """Pass `value` through simpleeval's result check (a no-op on versions without one)."""
    if _disallowed_items_check is not None and type(value) not in _ALWAYS_ALLOWED:
        _disallowed_items_check(value)
    return value


def _native_getattr(obj: Any, attr: str) -> Any:
    """Attribute access as simpleeval does it: attribute first, then `obj[attr]`."""
    try:
        value = getattr(obj, attr)
    except (AttributeError, TypeError):
        try:
            value = obj[attr]
        except (KeyError, TypeError):
            raise AttributeDoesNotExist(attr, "") from None
    return _native_check(value)


def _native_join(*parts: Any) -> str:
    """f-string assembly as simpleeval does it (str() of each part, conversions ignored)."""
    return "".join([str(part) for part in parts])


def _native_format(value: Any, spec: str) -> str:
    return ("{:" + spec + "}").format(value)


# Globals of every compiled expression function. Expression names never reach them:
# each name in the expression is compiled to a lookup on the function's argument.
_NATIVE_GLOBALS: Dict[str, Any] = {
    "__builtins__": {},
    _ATTR: _native_getattr,
    _JOIN: _native_join,
    _FORMAT: _native_format,
    _CHECK: _native_check,
}

# The compiled function's one argument: the _NativeNames for the evaluation
_NAMES_ARG = "·n"


class _NativeNames(_Names):
    """Names for compiled code: names, then functions, as simpleeval resolves them."""

    __slots__ = ()

    def __getitem__(self, key: str) -> Any:
        try:
            value = _Names.__getitem__(self, key)
        except (KeyError, TypeError):
            functions = self.functions
            if key not in functions:
                raise NameError(f"name '{key}' is not defined") from None
            value = functions[key]
        return _native_check(value)


class _Unsupported(Exception):
    """The expression uses something the native engine does not reproduce exactly."""


_NATIVE_PASSTHROUGH = (
    ast.Constant, ast.Load, ast.IfExp, ast.Slice,
    ast.List, ast.Tuple, ast.Set, ast.BoolOp, ast.And, ast.Or,
)


class _NativeCompiler(ast.NodeTransformer):
    """
    Rewrite a simpleeval-valid AST into Python that evaluates the same way.

    simpleeval checks the result of every node it evaluates. Names and attributes
    are checked by their helpers; subscripts, calls and operators are wrapped in
    the check here. Constants, literals, f-strings, `and`/`or` and `x if c else y`
    only produce primitives or values that were already checked.
    """

    def generic_visit(self, node):
        if not isinstance(node, _NATIVE_PASSTHROUGH):
            raise _Unsupported(type(node).__name__)
        return super().generic_visit(node)

    def visit_Subscript(self, node):
        if not isinstance(node.ctx, ast.Load):
            raise _Unsupported("assignment")
        subscript = ast.Subscript(value=self.visit(node.value), slice=self.visit(node.slice), ctx=ast.Load())
        return _checked(subscript)

    def visit_Name(self, node):
        if not isinstance(node.ctx, ast.Load):
            raise _Unsupported("assignment")
        # `n[name]` on the evaluation's _NativeNames, which applies simpleeval's lookup
        return ast.Subscript(value=_names_arg(), slice=ast.Constant(node.id), ctx=ast.Load())

    def visit_Attribute(self, node):
        # simpleeval refuses these when evaluated; leave them to it
        if node.attr.startswith(tuple(DISALLOW_PREFIXES)) or node.attr in DISALLOW_METHODS:
            raise _Unsupported(f"attribute {node.attr}")
        return _call(_ATTR, [self.visit(node.value), ast.Constant(node.attr)])

    def visit_Call(self, node):
        if any(isinstance(arg, ast.Starred) for arg in node.args):
            raise _Unsupported("starred argument")
        if any(kw.arg is None for kw in node.keywords):
            raise _Unsupported("** argument")
        if isinstance(node.func, ast.Name):
            # simpleeval calls through its function table only, never through names
            func = ast.Subscript(
                value=ast.Attribute(value=_names_arg(), attr="functions", ctx=ast.Load()),
                slice=ast.Constant(node.func.id),
                ctx=ast.Load(),
            )
        elif isinstance(node.func, ast.Attribute):
            func = self.visit(node.func)
        else:
            raise _Unsupported("call of an expression")
        return _checked(ast.Call(
            func=func,
            args=[self.visit(arg) for arg in node.args],
            keywords=[ast.keyword(arg=kw.arg, value=self.visit(kw.value)) for kw in node.keywords],
        ))

    def visit_BinOp(self, node):
        _check_operator(node.op)
        return _checked(ast.BinOp(left=self.visit(node.left), op=node.op, right=self.visit(node.right)))

    def visit_UnaryOp(self, node):
        _check_operator(node.op)
        return _checked(ast.UnaryOp(op=node.op, operand=self.visit(node.operand)))

    def visit_Compare(self, node):
        for op in node.ops:
            _check_operator(op)
        return _checked(ast.Compare(
            left=self.visit(node.left),
            ops=node.ops,
            comparators=[self.visit(c) for c in node.comparators],
        ))

    def visit_Dict(self, node):
        if any(key is None for key in node.keys):
            raise _Unsupported("** in dict literal")
        return ast.Dict(
            keys=[self.visit(key) for key in node.keys],
            values=[self.visit(value) for value in node.values],
        )

    def visit_JoinedStr(self, node):
        parts = []
        for value in node.values:
            if isinstance(value, ast.FormattedValue):
                part = self.visit(value.value)
                if value.format_spec is not None:
                    part = _call(_FORMAT, [part, self.visit(value.format_spec)])
                parts.append(part)
            else:
                parts.append(self.visit(value))
        return _call(_JOIN, parts)


def _call(helper: str, args: list) -> ast.Call:
    return ast.Call(func=ast.Name(id=helper, ctx=ast.Load()), args=args, keywords=[])


def _checked(node: ast.AST) -> ast.AST:
    """Wrap a node in simpleeval's result check, when the installed simpleeval has one."""
    return node if _disallowed_items_check is None else _call(_CHECK, [node])


def _names_arg() -> ast.Name:
    return ast.Name(id=_NAMES_ARG, ctx=ast.Load())


def _check_operator(op: ast.AST) -> None:
    if type(op) not in DEFAULT_OPERATORS:
        raise _Unsupported(type(op).__name__)


def _compile_native(expr: str, parsed: ast.AST) -> Optional[Callable[["_NativeNames"], Any]]:
    """
    Compile a parsed expression into `f(names)` for the native engine, or None to stay sandboxed.

    A function body (rather than `eval` with a names mapping) lets the helpers be
    plain globals, which Python looks up at C speed.
    """
    if not isinstance(parsed, ast.Expr):
        return None  # assignments etc.: simpleeval's own handling applies
    try:
        # NodeTransformer rewrites nodes in place; work on a copy so that falling back
        # part-way through leaves the sandboxed engine's tree untouched.
        body = _NativeCompiler().visit(copy.deepcopy(parsed.value))
        module = ast.parse("def expression(names):\n    return None")
        function = module.body[0]
        function.args.args[0].arg = _NAMES_ARG
        function.body[0].value = body
        ast.fix_missing_locations(module)
        namespace = dict(_NATIVE_GLOBALS)
        exec(compile(module, f"<expression {expr!r}>", "exec"), namespace)
        return namespace["expression"]
    except (_Unsupported, SyntaxError, ValueError, TypeError) as e:
        logger.debug(f"Expression '{expr}' stays on the sandboxed engine: {e}")
        return None


# =============================================================================
# Compiled expressions
# =============================================================================

class CompiledExpression:
    """
    An expression parsed once and evaluated per message.

    `evaluate` raises on failure; callers decide how to report it (fields log a
    warning per failure and fall back to their default).
    """

    __slots__ = ("expr", "engine", "_parsed", "_code", "_error")

    def __init__(self, expr: Any, engine: str = SANDBOXED):
        self.expr = expr
        self._parsed = None
        self._code = None
        self._error: Optional[Exception] = None
        try:
            self._parsed = EvalWithCompoundTypes.parse(expr)
        except Exception as e:  # reported on every evaluation, as before
            self._error = e
        if self._parsed is not None and engine == NATIVE:
            self._code = _compile_native(expr, self._parsed)
        self.engine = NATIVE if self._code is not None else SANDBOXED

    def evaluate(self, source: Dict[str, Any], extra: Dict[str, Any]) -> Any:
        if self._error is not None:
            raise self._error.with_traceback(None)
        _check_source(source)
        if self._code is not None:
            return self._code(_NativeNames(source, extra))
        return _eval_sandboxed(self.expr, self._parsed, _Names(source, extra))


def report_failure(expr: Any, error: Exception, default: Any) -> Any:
    """Log a failed evaluation the way the converter always has, and return the default."""
    logger.warning(f"Expression eval failed for '{expr}': {error}")
    return default


def is_plain_name(text: str) -> bool:
    """An identifier that parses as a Name (not a keyword such as True or None)."""
    return text.isidentifier() and not keyword.iskeyword(text)
