"""Condition language for MORPH policies, backed by CEL (Common Expression Language).

A policy's ``when`` may be:

* a CEL expression string: ``source.status == "live" && destination.latency_ms < 120``
* a list of CEL strings, which are ANDed together
* a list of structured clauses (``{"field": ..., "equals": ...}``), the original MORPH
  format, which is translated to CEL
* a mixed list of the two

Evaluation is deny-by-default: an expression that references a missing field, compares
incompatible types, or does not produce a boolean is treated as not matching.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

import celpy
from celpy import celtypes
from lark import Token, Tree

MACROS = {"all", "exists", "exists_one", "map", "filter"}
# Identifiers CEL treats as type names; they are not context paths.
TYPE_IDENTS = {
    "string", "int", "uint", "double", "bool", "bytes", "list", "map",
    "null_type", "type", "timestamp", "duration", "dyn",
}
CLAUSE_OPERATORS = {"equals": "==", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}

_ENV = celpy.Environment()


class ExpressionError(ValueError):
    """Raised when a condition cannot be translated or parsed."""


class EvaluationError(ValueError):
    """Raised when a value expression cannot be evaluated against a context."""


def cel_to_python(value: Any) -> Any:
    """Convert a CEL result into plain Python values."""
    if isinstance(value, Exception):
        # celpy can return a CELEvalError as a value instead of raising it.
        raise EvaluationError(str(value))
    if value is None or isinstance(value, celtypes.NullType):
        return None
    if isinstance(value, celtypes.BoolType):
        return bool(value)
    if isinstance(value, (celtypes.IntType, celtypes.UintType)):
        return int(value)
    if isinstance(value, celtypes.DoubleType):
        return float(value)
    if isinstance(value, celtypes.StringType):
        return str(value)
    if isinstance(value, celtypes.BytesType):
        return bytes(value)
    if isinstance(value, celtypes.ListType):
        return [cel_to_python(item) for item in value]
    if isinstance(value, celtypes.MapType):
        return {cel_to_python(key): cel_to_python(item) for key, item in value.items()}
    if isinstance(value, (celtypes.TimestampType, celtypes.DurationType)):
        return str(value)
    return value


def cel_literal(value: Any) -> str:
    """Render a Python value as a CEL literal."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple, set)):
        return "[" + ", ".join(cel_literal(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{cel_literal(str(key))}: {cel_literal(item)}" for key, item in value.items()) + "}"
    raise ExpressionError(f"Unsupported literal in condition: {value!r}")


def clause_to_cel(clause: dict[str, Any]) -> str:
    """Translate one structured clause into a CEL expression."""
    field = clause.get("field")
    if not isinstance(field, str) or not field:
        raise ExpressionError("clause.field is required and must be a non-empty string")

    parts: list[str] = []
    for operator, symbol in CLAUSE_OPERATORS.items():
        if operator in clause:
            parts.append(f"{field} {symbol} {cel_literal(clause[operator])}")
    if "contains" in clause:
        literal = cel_literal(clause["contains"])
        parts.append(f"(type({field}) == string ? {field}.contains({literal}) : {literal} in {field})")

    if not parts:
        raise ExpressionError(f"clause on '{field}' declares no operator")
    return " && ".join(parts)


def when_to_cel(when: Any) -> str:
    """Normalise any supported ``when`` form into a single CEL expression."""
    if when is None:
        return "true"
    if isinstance(when, str):
        return when.strip() or "true"
    if isinstance(when, list):
        parts: list[str] = []
        for item in when:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    parts.append(f"({text})")
            elif isinstance(item, dict):
                parts.append(f"({clause_to_cel(item)})")
            else:
                raise ExpressionError(f"Unsupported condition item: {item!r}")
        return " && ".join(parts) if parts else "true"
    raise ExpressionError("when must be a CEL string, a list of CEL strings, or a list of clause objects")


class Expression:
    """A compiled CEL expression: its source, the context paths it reads, and an evaluator."""

    __slots__ = ("source", "paths", "literals", "comparisons", "constraints", "_program")

    def __init__(self, source: str):
        try:
            ast = _ENV.compile(source)
        except Exception as exc:  # celpy raises CELParseError, a lark error subclass
            raise ExpressionError(f"Invalid expression {source!r}: {exc}") from exc

        self.source = source
        self.paths: frozenset[str] = frozenset(_collect_paths(ast))
        self.literals: tuple[Any, ...] = tuple(_collect_literals(ast))
        constraints = _collect_comparisons(ast)
        # path -> ((operator, literal), ...) with the path on the left-hand side
        self.constraints: dict[str, tuple[tuple[str, Any], ...]] = {path: tuple(pairs) for path, pairs in constraints.items()}
        # path -> (literal, ...) for callers that only care about the values
        self.comparisons: dict[str, tuple[Any, ...]] = {
            path: tuple(dict.fromkeys(literal for _, literal in pairs)) for path, pairs in constraints.items()
        }
        self._program = _ENV.program(ast)

    def value(self, context: dict[str, Any]) -> Any:
        """Evaluate to a Python value. Raises EvaluationError when the context cannot satisfy it."""
        if not isinstance(context, dict):
            raise EvaluationError("context must be an object")
        try:
            activation = celpy.json_to_cel(context)
            result = self._program.evaluate(activation)
        except EvaluationError:
            raise
        except Exception as exc:
            raise EvaluationError(f"{self.source!r}: {exc}") from exc
        return cel_to_python(result)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}({self.source!r})"


class Predicate(Expression):
    """A compiled condition. Evaluation never raises: anything but `true` is a non-match."""

    __slots__ = ()

    def evaluate(self, context: dict[str, Any]) -> bool:
        try:
            result = self.value(context)
        except EvaluationError:
            # Missing field, incomparable types, unsupported value: never a match.
            return False
        return result is True


@lru_cache(maxsize=2048)
def compile_expression(source: str) -> Predicate:
    return Predicate(source)


@lru_cache(maxsize=2048)
def compile_value(source: str) -> Expression:
    return Expression(source)


def compile_when(when: Any) -> Predicate:
    return compile_expression(when_to_cel(when))


# --- path extraction -----------------------------------------------------------------


def _dotted(node: Any) -> str | None:
    """Return the dotted path a node denotes, or None if it is not a plain path."""
    if isinstance(node, Token):
        return str(node) if node.type == "IDENT" else None
    if not isinstance(node, Tree):
        return None
    if node.data == "ident":
        return str(node.children[0])
    if node.data == "member_dot":
        left = _dotted(node.children[0])
        return f"{left}.{node.children[1]}" if left else None
    if len(node.children) == 1:
        return _dotted(node.children[0])
    return None


def _first_ident(exprlist: Any) -> str | None:
    if isinstance(exprlist, Tree) and exprlist.children:
        path = _dotted(exprlist.children[0])
        if path and "." not in path:
            return path
    return None


def _collect(node: Any, bound: frozenset[str], out: set[str]) -> None:
    if not isinstance(node, Tree):
        return

    if node.data == "member_dot":
        path = _dotted(node)
        if path is not None:
            if path.split(".")[0] not in bound:
                out.add(path)
            return

    if node.data == "ident":
        name = str(node.children[0])
        if name not in bound and name not in TYPE_IDENTS:
            out.add(name)
        return

    if node.data == "member_dot_arg":
        receiver = node.children[0]
        method = str(node.children[1])
        args = node.children[2] if len(node.children) > 2 else None
        path = _dotted(receiver)
        if path is not None:
            if path.split(".")[0] not in bound:
                out.add(path)
        else:
            _collect(receiver, bound, out)
        inner = bound
        if method in MACROS and args is not None:
            variable = _first_ident(args)
            if variable:
                inner = bound | {variable}
        _collect(args, inner, out)
        return

    for child in node.children:
        _collect(child, bound, out)


def _collect_paths(ast: Tree) -> set[str]:
    out: set[str] = set()
    _collect(ast, frozenset(), out)
    return out


def _parse_string_literal(text: str) -> str:
    body = text
    if body[:1] in ("r", "R"):
        body = body[1:]
        quote = body[:3] if body[:3] in ('"""', "'''") else body[:1]
        return body[len(quote):-len(quote)]
    quote = body[:3] if body[:3] in ('"""', "'''") else body[:1]
    inner = body[len(quote):-len(quote)]
    try:
        return json.loads('"' + inner.replace('"', '\\"') + '"') if quote.startswith("'") else json.loads('"' + inner + '"')
    except ValueError:
        return inner


_NO_LITERAL = object()


def _literal_value(node: Tree) -> Any:
    """Parse a `literal` node, or return _NO_LITERAL."""
    if node.data != "literal" or not node.children:
        return _NO_LITERAL
    token = node.children[0]
    if not isinstance(token, Token):
        return _NO_LITERAL
    text = str(token)
    try:
        if token.type in ("INT_LIT", "UINT_LIT"):
            return int(text.rstrip("uU"), 0)
        if token.type == "FLOAT_LIT":
            return float(text)
        if token.type == "STRING_LIT":
            return _parse_string_literal(text)
        if token.type == "BOOL_LIT":
            return text == "true"
    except ValueError:
        return _NO_LITERAL
    return _NO_LITERAL


def _collect_literals(ast: Tree) -> list[Any]:
    """Return the literal values an expression mentions, in source order."""
    out: list[Any] = []
    for node in ast.iter_subtrees_topdown():
        value = _literal_value(node)
        if value is not _NO_LITERAL:
            out.append(value)
    return out


def _single_literal(node: Any) -> Any:
    """Descend single-child wrappers to a lone literal, else _NO_LITERAL."""
    while isinstance(node, Tree):
        if node.data == "literal":
            return _literal_value(node)
        if node.data == "unary" and len(node.children) == 2 and isinstance(node.children[0], Token) and str(node.children[0]) == "-":
            inner = _single_literal(node.children[1])
            return -inner if isinstance(inner, (int, float)) and not isinstance(inner, bool) else _NO_LITERAL
        if len(node.children) != 1:
            return _NO_LITERAL
        node = node.children[0]
    return _NO_LITERAL


_RELATION_OPS = {
    "relation_eq": "==", "relation_ne": "!=", "relation_lt": "<", "relation_le": "<=",
    "relation_gt": ">", "relation_ge": ">=", "relation_in": "in",
}
_FLIPPED_OPS = {"==": "==", "!=": "!=", "<": ">", "<=": ">=", ">": "<", ">=": "<=", "in": "contains_in"}


def _collect_comparisons(ast: Tree) -> dict[str, list[tuple[str, Any]]]:
    """Map each context path to (operator, literal) pairs it is compared with.

    The operator is normalised so the path is on the left: ``10 >= g`` records ``("<=", 10)``
    for ``g``. ``"x" in path`` records ``("contains_in", "x")``, i.e. the path must contain
    the literal; ``path in [...]`` is not recorded. Method calls record the method name.
    """
    out: dict[str, list[tuple[str, Any]]] = {}

    def add(path: str | None, operator: str, value: Any) -> None:
        if path is None or value is _NO_LITERAL:
            return
        bucket = out.setdefault(path, [])
        if (operator, value) not in bucket:
            bucket.append((operator, value))

    for node in ast.iter_subtrees_topdown():
        if node.data == "relation" and len(node.children) == 2:
            operator_node, right = node.children
            if isinstance(operator_node, Tree) and operator_node.data in _RELATION_OPS and operator_node.children:
                operator = _RELATION_OPS[operator_node.data]
                left = operator_node.children[0]
                if operator == "in":
                    add(_dotted(right), "contains_in", _single_literal(left))
                else:
                    add(_dotted(left), operator, _single_literal(right))
                    add(_dotted(right), _FLIPPED_OPS[operator], _single_literal(left))
        elif node.data == "member_dot_arg" and len(node.children) > 2:
            receiver, method, args = node.children[0], str(node.children[1]), node.children[2]
            if isinstance(args, Tree) and len(args.children) == 1:
                add(_dotted(receiver), method, _single_literal(args.children[0]))
    return out
