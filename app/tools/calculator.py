"""Calculator tool - exact arithmetic instead of the LLM guessing.

LLMs predict text; they often get multiplication wrong. This tool computes.

Security: Python's eval() would run ANY code ("__import__('os').remove(...)").
Instead we parse the expression into a syntax tree (ast) and evaluate only a
small whitelist: numbers, + - * / // % **, parentheses and a few math functions.
Anything else is rejected. Size limits stop "9**9**9"-style CPU/memory bombs.
"""

import ast
import math
import operator
import re
from typing import Any

from pydantic import BaseModel, Field

from app.tools.base import PermissionLevel, Tool, ToolContext, ToolError

MAX_EXPRESSION_CHARS = 200
MAX_RESULT_BITS = 10_000  # ~3000 decimal digits

_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCTIONS = {
    "sqrt": math.sqrt, "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil,
    "sin": math.sin, "cos": math.cos, "tan": math.tan, "log": math.log, "log10": math.log10,
    "exp": math.exp, "factorial": math.factorial, "min": min, "max": max,
}  # fmt: skip
_CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def normalize(expression: str) -> str:
    """Accept everyday notation: 482 × 29, 10 ÷ 4, 2^8, 482 x 29, 15% of 2480, 1,000."""
    text = expression.replace("×", "*").replace("÷", "/").replace("^", "**")
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)  # thousands separators: 2,480 -> 2480
    text = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\b", r"(\1/100)*", text, flags=re.I)
    return re.sub(r"(?<=[\d)])\s*[xX]\s*(?=[\d(])", "*", text)


def evaluate(expression: str) -> int | float:
    if len(expression) > MAX_EXPRESSION_CHARS:
        raise ToolError(f"Expression too long (max {MAX_EXPRESSION_CHARS} characters).")
    try:
        tree = ast.parse(normalize(expression), mode="eval")
    except SyntaxError as exc:
        raise ToolError(f"Not a valid math expression: {expression!r}") from exc
    try:
        result = _eval(tree.body)
    except ZeroDivisionError as exc:
        raise ToolError("Division by zero.") from exc
    except (OverflowError, ValueError, TypeError) as exc:
        raise ToolError(f"Math error: {exc}") from exc
    if isinstance(result, float):
        result = float(f"{result:.12g}")  # hide float noise: 0.1+0.2 -> 0.3
    return result


def _eval(node: ast.AST) -> Any:
    match node:
        case ast.Constant(value=value) if type(value) in (int, float):
            return value
        case ast.Name(id=name) if name in _CONSTANTS:
            return _CONSTANTS[name]
        case ast.UnaryOp(op=op, operand=operand) if type(op) in _UNARY:
            return _UNARY[type(op)](_eval(operand))
        case ast.BinOp(left=left, op=op, right=right) if type(op) in _BINARY:
            a, b = _eval(left), _eval(right)
            if isinstance(op, ast.Pow):
                _check_power(a, b)
            return _check_size(_BINARY[type(op)](a, b))
        case ast.Call(func=ast.Name(id=name), args=args, keywords=[]) if name in _FUNCTIONS:
            values = [_eval(a) for a in args]
            if name == "factorial" and (not values or values[0] > 1000):
                raise ToolError("factorial() is limited to numbers up to 1000.")
            return _check_size(_FUNCTIONS[name](*values))
    raise ToolError(
        "Unsupported expression. Use numbers, + - * / // % ** ( ) and "
        f"functions: {', '.join(sorted(_FUNCTIONS))}."
    )


def _check_power(base: Any, exponent: Any) -> None:
    if abs(exponent) > 10_000:
        raise ToolError("Exponent too large.")
    if isinstance(base, int) and base.bit_length() * abs(exponent) > MAX_RESULT_BITS:
        raise ToolError("Result would be too large.")


def _check_size(value: Any) -> Any:
    if isinstance(value, int) and value.bit_length() > MAX_RESULT_BITS:
        raise ToolError("Result would be too large.")
    return value


class CalculatorInput(BaseModel):
    expression: str = Field(
        min_length=1,
        max_length=MAX_EXPRESSION_CHARS,
        description="Math expression, e.g. '482 * 29', 'sqrt(2) * 10' or '15% of 2480'",
    )


class CalculatorTool(Tool[CalculatorInput]):
    name = "calculator"
    description = (
        "Evaluate a math expression exactly. Use this for ANY arithmetic instead of "
        "calculating in your head. Supports + - * / // % ** and sqrt, log, sin, round, etc."
    )
    input_model = CalculatorInput
    parallel_safe = True  # only reads, shares nothing: may run alongside other lookups
    permission_level = PermissionLevel.READ_ONLY
    timeout_seconds = 2.0

    async def run(self, args: CalculatorInput, context: ToolContext) -> dict:
        return {"expression": args.expression, "result": evaluate(args.expression)}

    def summarize(self, output: dict) -> str:
        return f"{output['expression']} = {output['result']}"
