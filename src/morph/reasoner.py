"""Deterministic semantic reasoning for a supported, typed CEL predicate fragment."""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from typing import Any

import z3
from lark import Token, Tree

from .expressions import when_to_cel


class _UnsupportedExpression(Exception):
    pass


@dataclass(frozen=True)
class _GuardedValue:
    value: Any
    defined: z3.BoolRef


class _PredicateTranslator:
    def __init__(self, types: dict[str, str]):
        self.types = types

    def translate(self, source: str) -> z3.BoolRef:
        tree = z3_parse(source)
        result = self._expression(tree)
        if not isinstance(result, _GuardedValue) or not isinstance(result.value, z3.BoolRef):
            raise _UnsupportedExpression("the predicate does not produce a Boolean result")
        return z3.And(result.defined, result.value)

    def _expression(self, node: Any) -> Any:
        if isinstance(node, Token):
            return _GuardedValue(self._literal(node), z3.BoolVal(True))
        if not isinstance(node, Tree):
            raise _UnsupportedExpression("unrecognized CEL syntax")

        name = str(node.data)
        children = node.children

        if name == "relation" and len(children) == 2 and isinstance(children[0], Tree):
            operator = str(children[0].data).removeprefix("relation_")
            if operator != str(children[0].data):
                left = self._expression(children[0].children[0])
                right = self._expression(children[1])
                return self._compare(operator, left, right)
        if name in {"expr", "paren_expr", "relation", "addition", "multiplication", "unary", "member", "primary"} and len(children) == 1:
            return self._expression(children[0])
        if name in {"conditionaland", "conditionalor"}:
            terms = [self._expression(child) for child in children]
            if len(terms) == 1:
                return terms[0]
            if not all(isinstance(term, _GuardedValue) and isinstance(term.value, z3.BoolRef) for term in terms):
                raise _UnsupportedExpression(f"{name} requires Boolean operands")
            result = terms[0]
            for term in terms[1:]:
                if name == "conditionaland":
                    defined = z3.Or(
                        z3.And(result.defined, term.defined),
                        z3.And(result.defined, z3.Not(result.value)),
                        z3.And(term.defined, z3.Not(term.value)),
                    )
                    result = _GuardedValue(z3.And(result.value, term.value), defined)
                else:
                    defined = z3.Or(
                        z3.And(result.defined, term.defined),
                        z3.And(result.defined, result.value),
                        z3.And(term.defined, term.value),
                    )
                    result = _GuardedValue(z3.Or(result.value, term.value), defined)
            return result
        if name == "unary_not":
            operand = self._expression(children[-1])
            if not isinstance(operand, _GuardedValue) or not isinstance(operand.value, z3.BoolRef):
                raise _UnsupportedExpression("logical negation requires a Boolean operand")
            return _GuardedValue(z3.Not(operand.value), operand.defined)
        if name == "ident" or name == "member_dot":
            path = self._path(node)
            type_name = self.types.get(path)
            sorts = {
                "bool": z3.Bool,
                "int": z3.Int,
                "double": z3.Real,
                "string": z3.String,
            }
            if type_name not in sorts:
                raise _UnsupportedExpression(f"no supported declared type for '{path}'")
            return _GuardedValue(sorts[type_name](path), z3.Bool(f"{path}__present"))
        if name == "unary" and len(children) == 2 and str(children[0].data) == "unary_not":
            operand = self._expression(children[1])
            if not isinstance(operand, _GuardedValue) or not isinstance(operand.value, z3.BoolRef):
                raise _UnsupportedExpression("logical negation requires a Boolean operand")
            return _GuardedValue(z3.Not(operand.value), operand.defined)
        if name == "literal":
            return _GuardedValue(self._literal(children[0]), z3.BoolVal(True))
        if name == "list_lit":
            values = next((child for child in children if isinstance(child, Tree) and str(child.data) == "exprlist"), None)
            return self._expression(values) if values is not None else []
        if name == "exprlist":
            return [self._expression(child) for child in children]
        if name == "member_dot_arg":
            raise _UnsupportedExpression("external capability or method semantics are unavailable")
        raise _UnsupportedExpression(f"unsupported CEL construct '{name}'")

    @staticmethod
    def _compare(operator: str, left: _GuardedValue, right: Any) -> _GuardedValue:
        if operator == "in":
            if not isinstance(right, list) or not all(isinstance(item, _GuardedValue) for item in right):
                raise _UnsupportedExpression("membership requires a literal list")
            return _GuardedValue(
                z3.Or(*(left.value == item.value for item in right)),
                z3.And(left.defined, *(item.defined for item in right)),
            )
        if not isinstance(right, _GuardedValue):
            raise _UnsupportedExpression("comparison requires scalar operands")
        operations = {
            "eq": lambda: left.value == right.value,
            "ne": lambda: left.value != right.value,
            "lt": lambda: left.value < right.value,
            "le": lambda: left.value <= right.value,
            "gt": lambda: left.value > right.value,
            "ge": lambda: left.value >= right.value,
        }
        if operator not in operations:
            raise _UnsupportedExpression(f"unsupported relation '{operator}'")
        return _GuardedValue(operations[operator](), z3.And(left.defined, right.defined))

    def _path(self, node: Any) -> str:
        if isinstance(node, Token):
            if node.type == "IDENT":
                return str(node)
            raise _UnsupportedExpression("expected a declared field path")
        if not isinstance(node, Tree):
            raise _UnsupportedExpression("expected a declared field path")
        name = str(node.data)
        if name == "ident":
            return self._path(node.children[0])
        if name == "member_dot":
            return f"{self._path(node.children[0])}.{self._path(node.children[1])}"
        if len(node.children) == 1:
            return self._path(node.children[0])
        raise _UnsupportedExpression("expected a declared field path")

    @staticmethod
    def _literal(token: Token) -> Any:
        value = str(token)
        if token.type == "STRING_LIT":
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = ast.literal_eval(value)
            return z3.StringVal(value)
        if token.type in {"INT_LIT", "UINT_LIT"}:
            return z3.IntVal(int(value.rstrip("uUlL")))
        if token.type == "DOUBLE_LIT":
            return z3.RealVal(value.rstrip("dD"))
        if token.type == "BOOL_LIT":
            return z3.BoolVal(value.lower() == "true")
        raise _UnsupportedExpression(f"unsupported literal type '{token.type}'")


def z3_parse(source: str) -> Tree:
    from celpy import Environment

    try:
        return Environment().compile(source)
    except Exception as exc:
        raise _UnsupportedExpression(f"invalid or unsupported CEL expression: {exc}") from exc


class SemanticReasoner:
    """Prove relationships between supported CEL predicates using typed SMT constraints.

    Supported fields are bool, int, double, and string. Unsupported CEL features or
    undeclared field types produce UNKNOWN rather than a heuristic conclusion.
    """

    def analyze(self, baseline: Any, candidate: Any, *, types: dict[str, str] | None = None) -> dict[str, str]:
        try:
            translator = _PredicateTranslator(types or {})
            left = translator.translate(when_to_cel(baseline))
            right = translator.translate(when_to_cel(candidate))
            left_implies_right = self._check(z3.And(left, z3.Not(right)))
            right_implies_left = self._check(z3.And(right, z3.Not(left)))
            overlap = self._check(z3.And(left, right))
        except Exception as exc:
            return {
                "relationship": "unknown",
                "confidence": "unknown",
                "reason": str(exc),
            }

        if "unknown" in {left_implies_right, right_implies_left, overlap}:
            return {
                "relationship": "unknown",
                "confidence": "unknown",
                "reason": "the solver could not decide the predicate relationship",
            }
        if left_implies_right == "unsat" and right_implies_left == "unsat":
            return {"relationship": "equivalent", "confidence": "proven", "reason": "both predicates imply each other"}
        if left_implies_right == "unsat":
            return {"relationship": "broader", "confidence": "proven", "reason": "the candidate admits every baseline case and additional cases"}
        if right_implies_left == "unsat":
            return {"relationship": "narrower", "confidence": "proven", "reason": "the candidate admits only cases allowed by the baseline"}
        if overlap == "unsat":
            return {"relationship": "conflicting", "confidence": "proven", "reason": "no typed input satisfies both predicates"}
        return {
            "relationship": "unknown",
            "confidence": "unknown",
            "reason": "the predicates overlap but neither implies the other",
        }

    @staticmethod
    def _check(predicate: z3.BoolRef) -> str:
        solver = z3.Solver()
        solver.add(predicate)
        result = solver.check()
        if result == z3.unsat:
            return "unsat"
        if result == z3.sat:
            return "sat"
        return "unknown"