"""AST node whitelist for the Fox expression sandbox.

Only node types listed in ``ALLOWED_NODES`` survive validation. Everything else
(imports, lambdas, comprehensions, subscripts, dunders) is rejected.
"""

import ast

ALLOWED_NODES: set[type] = {
    # Top-level
    ast.Expression,
    # Values
    ast.Constant,
    ast.Name,
    ast.Load,
    # Operators
    ast.BinOp,
    ast.UnaryOp,
    ast.Compare,
    ast.BoolOp,
    ast.IfExp,
    # Binary operators
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    # Unary operators
    ast.UAdd,
    ast.USub,
    ast.Not,
    # Comparison operators
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    # Boolean operators
    ast.And,
    ast.Or,
    # Function calls
    ast.Call,
    # Tuple (for multi-arg functions like atan2)
    ast.Tuple,
}


def check_ast_safety(tree: ast.AST) -> list[str]:
    """Walk the AST and return a list of safety violations (empty = safe).

    Rejects any node not in ``ALLOWED_NODES``, plus dunder attribute access.
    """
    violations: list[str] = []
    for node in ast.walk(tree):
        node_type = type(node)
        if node_type == ast.Attribute:
            # Only allow attribute access on known safe objects (np, math)
            if isinstance(node.value, ast.Name) and node.value.id in ("np", "math"):
                continue
            violations.append(f"Attribute access not allowed: {ast.dump(node)}")
            continue
        if node_type not in ALLOWED_NODES:
            violations.append(f"Disallowed AST node: {node_type.__name__}")
    return violations
