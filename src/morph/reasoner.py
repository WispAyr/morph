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


@dataclass(frozen=True)
class _ListField:
    """A declared list field. Its element sort is fixed by its first use, since schemas do not declare it."""

    path: str


class _PredicateTranslator:
    """Translate CEL predicates to z3, keeping list fields exact.

    A list is never a z3 sequence, whose theory the solver often cannot decide. Each ``t in L``
    is a fresh Boolean and ``size(L)`` a fresh integer, tied together by ``list_axioms()``: equal
    terms get the same answer, and the size is at least the number of distinct members. Any
    model then corresponds to a real list (the members found true, padded with values equal to
    no tested term, which exist because strings and numbers are unbounded), so a counterexample
    is always a real input and every proof also holds for real lists.
    """

    def __init__(self, types: dict[str, str]):
        self.types = types
        self._members: dict[str, list[tuple[z3.ExprRef, z3.BoolRef]]] = {}
        self._sizes: dict[str, z3.ArithRef] = {}

    def _member(self, field: _ListField, term: z3.ExprRef) -> z3.BoolRef:
        if term.sort() not in (z3.StringSort(), z3.IntSort(), z3.RealSort()):
            raise _UnsupportedExpression(f"membership in '{field.path}' needs string or number elements")
        members = self._members.setdefault(field.path, [])
        if members and members[0][0].sort() != term.sort():
            raise _UnsupportedExpression(f"'{field.path}' is used with elements of different types")
        for existing, flag in members:
            if existing.eq(term):
                return flag
        flag = z3.Bool(f"{field.path}__contains_{len(members)}")
        members.append((term, flag))
        return flag

    def _size(self, field: _ListField) -> z3.ArithRef:
        if field.path not in self._sizes:
            self._sizes[field.path] = z3.Int(f"{field.path}__size")
        return self._sizes[field.path]

    def list_axioms(self) -> z3.BoolRef:
        """Constraints that make every model of the translated lists a real list."""
        axioms = []
        for path in set(self._members) | set(self._sizes):
            members = self._members.get(path, [])
            size = self._size(_ListField(path))
            axioms.append(size >= 0)
            distinct = []
            for index, (term, flag) in enumerate(members):
                for other, other_flag in members[:index]:
                    axioms.append(z3.Implies(term == other, flag == other_flag))
                first = z3.And(flag, *(z3.Or(term != other, z3.Not(other_flag)) for other, other_flag in members[:index]))
                distinct.append(z3.If(first, 1, 0))
            if distinct:
                axioms.append(size >= z3.Sum(*distinct))
        return z3.And(*axioms) if axioms else z3.BoolVal(True)

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
            if type_name == "list":
                return _GuardedValue(_ListField(path), z3.Bool(f"{path}__present"))
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
        if name == "ident_arg" and len(children) == 2 and str(children[0]) == "size":
            arguments = self._expression(children[1])
            if len(arguments) != 1 or not isinstance(arguments[0], _GuardedValue):
                raise _UnsupportedExpression("size() takes one argument")
            (argument,) = arguments
            if isinstance(argument.value, _ListField):
                return _GuardedValue(self._size(argument.value), argument.defined)
            if isinstance(argument.value, z3.SeqRef):
                return _GuardedValue(z3.Length(argument.value), argument.defined)
            raise _UnsupportedExpression("size() requires a string or list")
        if name == "member_dot_arg":
            raise _UnsupportedExpression("external capability or method semantics are unavailable")
        raise _UnsupportedExpression(f"unsupported CEL construct '{name}'")

    def _compare(self, operator: str, left: _GuardedValue, right: Any) -> _GuardedValue:
        if isinstance(left.value, _ListField):
            raise _UnsupportedExpression("a list can only be tested for membership or size")
        if operator == "in" and isinstance(right, _GuardedValue) and isinstance(right.value, _ListField):
            return _GuardedValue(self._member(right.value, left.value), z3.And(left.defined, right.defined))
        if operator == "in":
            if not isinstance(right, list) or not all(isinstance(item, _GuardedValue) for item in right):
                raise _UnsupportedExpression("membership requires a literal list")
            return _GuardedValue(
                z3.Or(*(left.value == item.value for item in right)),
                z3.And(left.defined, *(item.defined for item in right)),
            )
        if not isinstance(right, _GuardedValue) or isinstance(right.value, _ListField):
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

    Supported fields are bool, int, double, string, and list. A list may be tested with
    ``x in list`` (for string or number elements) and ``size(list)``, and is modelled exactly. Unsupported CEL features or
    undeclared field types produce UNKNOWN rather than a heuristic conclusion.
    """

    def analyze(self, baseline: Any, candidate: Any, *, types: dict[str, str] | None = None) -> dict[str, str]:
        try:
            translator = _PredicateTranslator(types or {})
            left = translator.translate(when_to_cel(baseline))
            right = translator.translate(when_to_cel(candidate))
            axioms = translator.list_axioms()
            left_implies_right = self._check(z3.And(axioms, left, z3.Not(right)))
            right_implies_left = self._check(z3.And(axioms, right, z3.Not(left)))
            overlap = self._check(z3.And(axioms, left, right))
        except Exception as exc:
            return {
                "relationship": "unknown",
                "confidence": "unknown",
                "reason": str(exc),
            }

        return self._relationship(left_implies_right, right_implies_left, overlap)

    def analyze_decisions(
        self,
        baseline: list[tuple[Any, str, bool]],
        candidate: list[tuple[Any, str, bool]],
        *,
        types: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Relate two first-match decision functions.

        Each side is its policies in order, as ``(when, outcome, allows)``: the condition, a key
        naming the decision it produces, and whether that decision allows. A policy decides only
        when no earlier one matched, and nothing matching is its own outcome. The result is
        ``equivalent`` when every outcome is reached by exactly the same inputs on both sides.
        Otherwise it relates the sets of inputs each side allows, which is ``unknown`` when those
        sets are equal but other outcomes differ, for example a different deny reason.
        """
        try:
            translator = _PredicateTranslator(types or {})
            left_outcomes, left_allowed = self._decision_function(translator, baseline)
            right_outcomes, right_allowed = self._decision_function(translator, candidate)
            axioms = translator.list_axioms()
            identical = True
            for key in set(left_outcomes) | set(right_outcomes):
                left = left_outcomes.get(key, z3.BoolVal(False))
                right = right_outcomes.get(key, z3.BoolVal(False))
                checks = {self._check(z3.And(axioms, left, z3.Not(right))), self._check(z3.And(axioms, right, z3.Not(left)))}
                if "unknown" in checks:
                    return {"relationship": "unknown", "confidence": "unknown", "reason": "the solver could not decide the decision relationship"}
                identical = identical and checks == {"unsat"}
            if identical:
                return {"relationship": "equivalent", "confidence": "proven", "reason": "every input reaches the same outcome"}
            allowed = self._relationship(
                self._check(z3.And(axioms, left_allowed, z3.Not(right_allowed))),
                self._check(z3.And(axioms, right_allowed, z3.Not(left_allowed))),
                self._check(z3.And(axioms, left_allowed, right_allowed)),
            )
        except Exception as exc:
            return {"relationship": "unknown", "confidence": "unknown", "reason": str(exc)}
        if allowed["relationship"] == "equivalent":
            return {"relationship": "unknown", "confidence": "unknown", "reason": "the same inputs are allowed, but other outcomes differ"}
        return allowed

    @staticmethod
    def _decision_function(translator: _PredicateTranslator, policies: list[tuple[Any, str, bool]]) -> tuple[dict[str, z3.BoolRef], z3.BoolRef]:
        unmatched: z3.BoolRef = z3.BoolVal(True)
        outcomes: dict[str, list[z3.BoolRef]] = {}
        allowed: list[z3.BoolRef] = []
        for when, outcome, allows in policies:
            # translate() is true exactly when the runtime would match: a condition reading an
            # absent field does not match, so "no earlier policy matched" is its plain negation.
            match = translator.translate(when_to_cel(when))
            decides = z3.And(unmatched, match)
            outcomes.setdefault(outcome, []).append(decides)
            if allows:
                allowed.append(decides)
            unmatched = z3.And(unmatched, z3.Not(match))
        outcomes.setdefault("<no matching policy>", []).append(unmatched)
        return {key: z3.Or(*terms) for key, terms in outcomes.items()}, z3.Or(*allowed) if allowed else z3.BoolVal(False)

    @staticmethod
    def _relationship(left_implies_right: str, right_implies_left: str, overlap: str) -> dict[str, str]:
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