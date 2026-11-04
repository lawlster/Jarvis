
import ast
import operator


# Only allow safe mathematical operations.
OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def calculate(expression: str):
    """
    Safely evaluate basic mathematical expressions.
    Does not use eval() or execute arbitrary Python code.
    """

    try:
        if len(expression) > 200:
            return "Error: Expression is too long."

        tree = ast.parse(expression, mode="eval")

        def evaluate(node):

            if isinstance(node, ast.Constant):
                if type(node.value) not in (int, float):
                    raise ValueError("Only numbers are allowed.")
                return node.value

            if isinstance(node, ast.BinOp):
                operation = OPERATORS.get(type(node.op))

                if operation is None:
                    raise ValueError("Unsupported operation.")

                left = evaluate(node.left)
                right = evaluate(node.right)

                if isinstance(node.op, ast.Pow):
                    if abs(right) > 10:
                        raise ValueError("Exponent is too large.")

                result = operation(left, right)

                if abs(result) > 1e100:
                    raise ValueError("Result is too large.")

                return result

            if isinstance(node, ast.UnaryOp):
                operation = OPERATORS.get(type(node.op))

                if operation is None:
                    raise ValueError("Unsupported operation.")

                return operation(evaluate(node.operand))

            raise ValueError("Invalid mathematical expression.")

        result = evaluate(tree.body)

        return str(result)

    except ZeroDivisionError:
        return "Error: Cannot divide by zero."

    except Exception as error:
        return f"Calculation error: {error}"
