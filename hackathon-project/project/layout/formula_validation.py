"""Bounded symbolic checks for diagnostic use, independently of exact source fidelity."""
import ast
import re


def polynomial_equivalence(left, right):
    # Accept only single-letter variable polynomials of low total degree. No evaluation of input strings.
    import sympy

    def polynomial(text):
        compact = "".join(text.split())
        if len(compact) > 96 or compact.count("=") != 1 or not re.fullmatch(r"[A-Za-z0-9=()+*^\-]+", compact):
            raise ValueError
        steps = 0

        def visit(node):
            nonlocal steps
            steps += 1
            if steps > 64:
                raise ValueError
            if isinstance(node, ast.Name) and len(node.id) == 1:
                return sympy.Symbol(node.id), 1
            if isinstance(node, ast.Constant) and type(node.value) is int and abs(node.value) <= 100000:
                return sympy.Integer(node.value), 0
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                value, degree = visit(node.operand)
                return (-value if isinstance(node.op, ast.USub) else value), degree
            if isinstance(node, ast.BinOp):
                value, a = visit(node.left)
                other, b = visit(node.right)
                if isinstance(node.op, ast.Add):
                    return value + other, max(a, b)
                if isinstance(node.op, ast.Sub):
                    return value - other, max(a, b)
                if isinstance(node.op, ast.Mult) and a + b <= 4:
                    return value * other, a + b
                if (isinstance(node.op, ast.Pow) and isinstance(node.right, ast.Constant)
                        and other in (1, 2, 3, 4) and a * other <= 4 and value.free_symbols):
                    return value ** other, a * other
            raise ValueError

        expressions = [visit(ast.parse(side.replace("^", "**"), mode="eval").body)[0] for side in compact.split("=")]
        return expressions[0] - expressions[1]

    try:
        left, right = polynomial(left), polynomial(right)
    except (ValueError, SyntaxError, RecursionError):
        return None
    return sympy.expand(left - right) == 0
