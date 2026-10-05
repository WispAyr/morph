from __future__ import annotations

from typing import Any

from .runtime import DENY_ACTION, MORPHRuntime

# Operators the SQL target can express as a WHERE clause.
_SQL_OPERATORS = {"equals": "=", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}


def _sql_literal(value: Any) -> str:
    """Render a Python value as a safe SQL literal."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value).replace("'", "''")
    return f"'{text}'"


def _sql_column(field: str) -> str:
    """Map a dotted context path to a column name, rejecting anything non-identifier."""
    column = field.replace(".", "_")
    if not column.replace("_", "").isalnum() or column[0].isdigit():
        raise ValueError(f"Field '{field}' cannot be expressed as a SQL column name.")
    return column


class Compiler:
    """Compile a MORPH definition into an executable target plan.

    The compiled plan keeps each policy's conditions and capability requirements, so a
    target can evaluate the plan against a context with exactly the runtime's semantics.
    """

    _TARGET_REGISTRY: dict[str, type[Any]] = {}

    @classmethod
    def register_target(cls, name: str, target_cls: type[Any]) -> None:
        cls._TARGET_REGISTRY[name] = target_cls

    @classmethod
    def list_targets(cls) -> list[str]:
        return sorted(cls._TARGET_REGISTRY)

    @classmethod
    def get_target(cls, name: str) -> type[Any]:
        try:
            return cls._TARGET_REGISTRY[name]
        except KeyError as exc:
            raise ValueError(f"Unknown target '{name}'. Registered targets: {sorted(cls._TARGET_REGISTRY)}") from exc

    @staticmethod
    def compile(ir: dict[str, Any], target: str = "python") -> dict[str, Any]:
        if target not in Compiler._TARGET_REGISTRY:
            raise ValueError(f"Unknown target '{target}'. Registered targets: {sorted(Compiler._TARGET_REGISTRY)}")

        # Constructing the runtime validates the policies and schema before anything is emitted.
        runtime = MORPHRuntime(
            name=ir.get("name", "morph"),
            version=ir.get("version", "0.1.0"),
            policies=ir.get("policies", []),
            capabilities=ir.get("capabilities", {}),
            entities=ir.get("entities"),
        )

        actions = []
        for policy, predicate in zip(runtime.policies, runtime._predicates):
            result = policy.get("result", {})
            actions.append({
                "name": policy.get("name", "unknown"),
                "action": result.get("action", DENY_ACTION),
                "status": result.get("status", "deny"),
                "requires": policy.get("requires", []),
                # The normalised CEL form is what targets evaluate; it is language-neutral.
                "when": predicate.source,
            })

        compiled: dict[str, Any] = {
            "target": target,
            "name": runtime.name,
            "version": runtime.version,
            "entities": runtime.schema.to_dict(),
            "capabilities": dict(runtime.capabilities),
            "plan": actions,
        }

        if target == "sql":
            statements = [Compiler._compile_sql_statement(policy) for policy in runtime.policies]
            compiled["statements"] = statements
            compiled["sql"] = "\n".join(statement["sql"] for statement in statements)

        return compiled

    @staticmethod
    def _compile_sql_statement(policy: dict[str, Any]) -> dict[str, Any]:
        """Render one policy as a parameterised SELECT plus a readable literal form."""
        result = policy.get("result", {})
        status = result.get("status", "deny")
        action = result.get("action", DENY_ACTION)

        when = policy.get("when", [])
        if not isinstance(when, list) or not all(isinstance(clause, dict) for clause in when):
            raise ValueError(
                f"Policy '{policy.get('name', 'unknown')}' uses a CEL expression; the SQL target "
                "can only translate structured clause lists."
            )

        clauses: list[str] = []
        literal_clauses: list[str] = []
        params: list[Any] = []
        for clause in when:
            column = _sql_column(clause["field"])
            for operator, symbol in _SQL_OPERATORS.items():
                if operator in clause:
                    clauses.append(f"{column} {symbol} ?")
                    literal_clauses.append(f"{column} {symbol} {_sql_literal(clause[operator])}")
                    params.append(clause[operator])
            if "contains" in clause:
                raise ValueError(
                    f"Policy '{policy.get('name', 'unknown')}' uses 'contains' on '{clause['field']}', "
                    "which the SQL target cannot express."
                )

        select = f"SELECT {_sql_literal(status)} AS status, {_sql_literal(action)} AS action"
        where = f" WHERE {' AND '.join(literal_clauses)}" if literal_clauses else ""
        where_params = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        return {
            "policy": policy.get("name", "unknown"),
            "sql": f"{select}{where};",
            "parameterized_sql": f"{select}{where_params};",
            "params": params,
        }


def _runtime_from_plan(compiled: dict[str, Any]) -> MORPHRuntime:
    return MORPHRuntime(
        name=compiled.get("name", "morph"),
        version=compiled.get("version", "0.1.0"),
        policies=[
            {
                "name": entry.get("name", "unknown"),
                "requires": entry.get("requires", []),
                "when": entry.get("when", []),
                "result": {"status": entry.get("status", "deny"), "action": entry.get("action", DENY_ACTION)},
            }
            for entry in compiled.get("plan", [])
        ],
        capabilities=compiled.get("capabilities", {}),
        entities=compiled.get("entities"),
    )


class _PlanTarget:
    """Shared executor: evaluates a compiled plan with the runtime's semantics."""

    def __init__(self, compiled: dict[str, Any]):
        self.compiled = compiled
        self.runtime = _runtime_from_plan(compiled)

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        decision = self.runtime.evaluate(context)
        return {
            "status": decision.get("status", "deny"),
            "action": decision.get("action", DENY_ACTION),
            "policy": decision.get("policy"),
            "name": self.compiled.get("name", "morph"),
            "version": self.compiled.get("version", "0.1.0"),
        }


class PythonTarget(_PlanTarget):
    """A Python execution target for compiled MORPH plans."""


class NodeTarget(_PlanTarget):
    """A JavaScript/Node-compatible plan target.

    The compiled plan is plain JSON that a Node runtime can evaluate with the same
    condition semantics; this class executes it in-process as the reference.
    """


class SQLTarget(_PlanTarget):
    """A SQL target for compiled MORPH plans.

    ``compiled["statements"]`` carries one parameterised SELECT per policy for an external
    database engine. ``execute`` runs the plan in-process as the reference evaluator.
    """

    def statements(self) -> list[dict[str, Any]]:
        return list(self.compiled.get("statements", []))


Compiler.register_target("python", PythonTarget)
Compiler.register_target("node", NodeTarget)
Compiler.register_target("sql", SQLTarget)
