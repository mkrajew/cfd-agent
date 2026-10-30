"""Tools an agent can call: the registry, a safe calculator and run-directory file tools.

Everything here treats the model as hostile or merely confused. A tool call never raises for
anything the model caused: an unknown tool, arguments that are not JSON, arguments that fail
validation, an expression that is not arithmetic, a path outside the allowed scope all come back
as plain text so the model can correct itself. Only programmer errors (any exception other than
``AgentInputError`` raised by a handler) propagate.

File access goes through ``RunDir`` (no ``..``, no absolute path, no symbolic link, atomic writes)
and is further limited by a ``FileScope`` of glob patterns per stage.
"""

from __future__ import annotations

import ast
import math
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from cfdagent.errors import AgentInputError
from cfdagent.llm.client import UNPARSED_KEY, ToolCall
from cfdagent.rundir import RunDir, split_relative

MAX_TOOL_OUTPUT_BYTES = 8000
MAX_WRITE_BYTES = 200_000  # one write_file call
MAX_READ_BYTES = 200_000  # the readable part of one file (the rest is cut off with a marker)
READ_PAGE_BYTES = 7500  # one read_file page; with its marker it stays under the tool output cap
MAX_LIST_ENTRIES = 300

MAX_EXPRESSION_CHARS = 500
MAX_EXPONENT = 1000
MAX_EXPRESSION_DEPTH = 100
MAX_CALL_ARGS = 20

# Names an agent may never write: they belong to the orchestrator. Hidden names (any path
# component starting with a dot, for example the sandbox exchange directory) are neither
# readable nor writable through the file tools.
RESERVED_FILES = (
    "run_record.json",
    "summary.md",
    "llm_transcript.jsonl",
    "status.json",
    "run.log",
)


class NoArgs(BaseModel):
    """Arguments of a tool that takes none."""


@dataclass(frozen=True)
class Tool:
    """A function the model may call. ``args`` is the pydantic model of its arguments; the
    handler receives a validated instance and returns text."""

    name: str
    description: str
    args: type[BaseModel]
    handler: Callable[[Any], str]


def truncate_utf8(text: str, max_bytes: int, marker: str = "\n[output truncated]") -> str:
    """Cut ``text`` so that the result, marker included, is at most ``max_bytes`` of UTF-8.

    Text that already fits is returned unchanged. A multi-byte character is never split.
    """
    data = text.encode("utf-8", "replace")
    if len(data) <= max_bytes:
        return text
    room = max(0, max_bytes - len(marker.encode("utf-8")))
    return data[:room].decode("utf-8", "ignore") + marker


def _validation_text(tool: Tool, exc: ValidationError) -> str:
    problems = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        where = ".".join(str(part) for part in error["loc"]) or "(arguments)"
        problems.append(f"{where}: {error['msg']}")
    required = tool.args.model_json_schema().get("required", [])
    hint = f" Required arguments: {', '.join(required)}." if required else " It takes no arguments."
    return f"error: invalid arguments for {tool.name}: " + "; ".join(problems) + "." + hint


class ToolRegistry:
    """The tools of one stage attempt."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"a tool named {tool.name!r} is already registered")
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict]:
        """The tools in the OpenAI function-calling format (plain JSON schema, not strict)."""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.args.model_json_schema(),
                },
            }
            for tool in self._tools.values()
        ]

    def dispatch(self, call: ToolCall) -> str:
        """Run one tool call and return the text for the model (at most 8000 bytes).

        Model mistakes are returned as text; see the module docstring.
        """
        return truncate_utf8(self._run(call), MAX_TOOL_OUTPUT_BYTES, "\n[output truncated]")

    def _run(self, call: ToolCall) -> str:
        tool = self._tools.get(call.name)
        if tool is None:
            shown = str(call.name)[:80]
            return f"error: unknown tool {shown!r}; the available tools are: " + ", ".join(
                self._tools
            )
        arguments = call.arguments
        if isinstance(arguments, dict) and UNPARSED_KEY in arguments:
            raw = str(arguments[UNPARSED_KEY])[:300]
            return (
                f"error: the arguments of {tool.name} are not valid JSON for a single object "
                f"(received: {raw!r}); send a JSON object with the named arguments."
            )
        try:
            parsed = tool.args.model_validate(arguments)
        except ValidationError as exc:
            return _validation_text(tool, exc)
        try:
            result = tool.handler(parsed)
        except AgentInputError as exc:
            return f"error: {exc}"
        return result if isinstance(result, str) else str(result)


# ---------------------------------------------------------------------------- calculator


class CalculatorError(AgentInputError):
    """The expression is not plain arithmetic, or cannot be evaluated to a finite number."""


def _checked_pow(base: float, exponent: float) -> float:
    if abs(exponent) > MAX_EXPONENT:
        raise CalculatorError(
            f"exponent {exponent:g} is too large: the exponent must be between "
            f"-{MAX_EXPONENT} and {MAX_EXPONENT}"
        )
    return math.pow(base, exponent)  # a ValueError, never a complex number, for a negative base


_CONSTANTS: dict[str, float] = {"pi": math.pi, "e": math.e}
# name -> (function, minimum number of arguments, maximum number of arguments)
_FUNCTIONS: dict[str, tuple[Callable[..., float], int, int]] = {
    "sqrt": (math.sqrt, 1, 1),
    "sin": (math.sin, 1, 1),
    "cos": (math.cos, 1, 1),
    "tan": (math.tan, 1, 1),
    "asin": (math.asin, 1, 1),
    "acos": (math.acos, 1, 1),
    "atan": (math.atan, 1, 1),
    "atan2": (math.atan2, 2, 2),
    "log": (math.log, 1, 2),
    "log10": (math.log10, 1, 1),
    "exp": (math.exp, 1, 1),
    "pow": (_checked_pow, 2, 2),
    "abs": (abs, 1, 1),
    "min": (min, 1, MAX_CALL_ARGS),
    "max": (max, 1, MAX_CALL_ARGS),
}


def _finite(value: float) -> float:
    if not math.isfinite(value):
        raise CalculatorError("the result is not a finite number (overflow or undefined)")
    return value


def _evaluate(node: ast.AST, depth: int) -> float:
    if depth > MAX_EXPRESSION_DEPTH:
        raise CalculatorError("the expression is nested too deeply")
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, depth + 1)
    if isinstance(node, ast.Constant):
        if type(node.value) not in (int, float):  # also refuses bool, complex, str, None
            raise CalculatorError("only plain numbers are allowed in an expression")
        return _finite(float(node.value))
    if isinstance(node, ast.Name):
        if node.id not in _CONSTANTS:
            raise CalculatorError(
                f"unknown name {node.id!r}; the only names are pi and e, and the functions "
                + ", ".join(_FUNCTIONS)
            )
        return _CONSTANTS[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _evaluate(node.operand, depth + 1)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp):
        left = _evaluate(node.left, depth + 1)
        right = _evaluate(node.right, depth + 1)
        op = node.op
        if isinstance(op, ast.Add):
            return _finite(left + right)
        if isinstance(op, ast.Sub):
            return _finite(left - right)
        if isinstance(op, ast.Mult):
            return _finite(left * right)
        if isinstance(op, ast.Div):
            return _finite(left / right)
        if isinstance(op, ast.Mod):
            return _finite(left % right)
        if isinstance(op, ast.Pow):
            return _finite(_checked_pow(left, right))
        raise CalculatorError("only + - * / ** % and parentheses are allowed")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            raise CalculatorError("only these functions may be called: " + ", ".join(_FUNCTIONS))
        if node.keywords or any(isinstance(arg, ast.Starred) for arg in node.args):
            raise CalculatorError("functions take plain positional arguments only")
        function, fewest, most = _FUNCTIONS[node.func.id]
        if not fewest <= len(node.args) <= most:
            raise CalculatorError(
                f"{node.func.id} takes between {fewest} and {most} arguments, got {len(node.args)}"
            )
        values = [_evaluate(arg, depth + 1) for arg in node.args]
        return _finite(float(function(*values)))
    raise CalculatorError(
        f"unsupported syntax ({type(node).__name__}); use numbers, + - * / ** %, parentheses, "
        "pi, e and the allowed functions"
    )


def _format_number(value: float) -> str:
    if value == 0:
        return "0"
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def calculate(expression: str) -> str:
    """Evaluate an arithmetic expression and return the number as text.

    Allowed: numbers, ``+ - * / ** %``, parentheses, the names ``pi`` and ``e`` and the functions
    sqrt sin cos tan asin acos atan atan2 log log10 exp pow abs min max. Nothing else is
    evaluated. All arithmetic is in floating point; an exponent above 1000 in magnitude, a result
    that is not finite, an expression of more than 500 characters or a deeply nested one is
    refused with a ``CalculatorError`` (an ``AgentInputError``).
    """
    if len(expression) > MAX_EXPRESSION_CHARS:
        raise CalculatorError(f"the expression is longer than {MAX_EXPRESSION_CHARS} characters")
    text = expression.strip()
    if not text:
        raise CalculatorError("the expression is empty")
    try:
        tree = ast.parse(text, mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        detail = getattr(exc, "msg", None) or type(exc).__name__
        raise CalculatorError(f"not a valid arithmetic expression ({detail})") from None
    try:
        return _format_number(_evaluate(tree, 0))
    except RecursionError:
        raise CalculatorError("the expression is nested too deeply") from None
    except ZeroDivisionError:
        raise CalculatorError("division by zero") from None
    except OverflowError:
        raise CalculatorError("the result is not a finite number (overflow)") from None
    except ValueError as exc:
        raise CalculatorError(f"mathematical domain error ({exc})") from None


class CalculatorArgs(BaseModel):
    expression: str = Field(description="Arithmetic expression, for example '0.5 * 1.2 * 3 ** 2'")


def calculator_tool() -> Tool:
    return Tool(
        name="calculator",
        description=(
            "Evaluate an arithmetic expression exactly as written. Use it for every calculation; "
            "never compute numbers in your head. Supports numbers, + - * / ** %, parentheses, "
            "the constants pi and e, and the functions "
            + ", ".join(_FUNCTIONS)
            + ". Returns the number."
        ),
        args=CalculatorArgs,
        handler=lambda args: calculate(args.expression),
    )


# ---------------------------------------------------------------------------- file tools


@dataclass(frozen=True)
class FileScope:
    """Which run-directory paths a stage's file tools may read and write.

    Patterns are ``fnmatch`` globs matched case-sensitively against the normalised POSIX path
    relative to the run directory (``*`` also matches ``/``). Paths with a hidden component (a
    name starting with a dot) are never accessible, and the orchestrator's own files are never
    writable, whatever the patterns say. A malformed path raises ``UnsafePath``.
    """

    readable: list[str] = field(default_factory=list)
    writable: list[str] = field(default_factory=list)

    @staticmethod
    def normalise(path: str) -> str:
        """The path as ``a/b/c`` (``''`` for the run directory itself); may raise UnsafePath."""
        return "/".join(split_relative(path))

    @staticmethod
    def _hidden(norm: str) -> bool:
        return any(part.startswith(".") for part in norm.split("/"))

    @staticmethod
    def _matches(patterns: list[str], norm: str) -> bool:
        return any(fnmatchcase(norm, pattern) for pattern in patterns)

    def can_read(self, path: str) -> bool:
        norm = self.normalise(path)
        return bool(norm) and not self._hidden(norm) and self._matches(self.readable, norm)

    def can_write(self, path: str) -> bool:
        norm = self.normalise(path)
        return (
            bool(norm)
            and not self._hidden(norm)
            and norm not in RESERVED_FILES
            and self._matches(self.writable, norm)
        )

    def can_list(self, path: str) -> bool:
        """True if something readable could be inside the directory ``path``."""
        norm = self.normalise(path)
        if norm and self._hidden(norm):
            return False
        prefix = f"{norm}/" if norm else ""
        for pattern in self.readable:
            literal = pattern
            for index, char in enumerate(pattern):
                if char in "*?[":
                    literal = pattern[:index]
                    break
            if literal.startswith(prefix) or prefix.startswith(literal):
                return True
        return False


class ListDirArgs(BaseModel):
    path: str = Field(default=".", description="Directory relative to the run directory")


class ReadFileArgs(BaseModel):
    path: str = Field(description="Path relative to the run directory, with '/' separators")
    offset: int = Field(
        default=0, ge=0, description="Character offset to continue a long file; 0 for the start"
    )


class WriteFileArgs(BaseModel):
    path: str = Field(description="Path relative to the run directory, with '/' separators")
    content: str = Field(description="Complete new text content of the file (UTF-8)")


def _denied(path: str, verb: str, patterns: list[str]) -> AgentInputError:
    shown = ", ".join(patterns) if patterns else "nothing"
    return AgentInputError(f"{path!r} is not {verb} in this stage; allowed paths: {shown}")


def _read_page(run: RunDir, scope: FileScope, args: ReadFileArgs) -> str:
    norm = scope.normalise(args.path)
    if not scope.can_read(norm):
        raise _denied(args.path, "readable", scope.readable)
    try:
        text = run.read_text(norm)
    except FileNotFoundError as exc:
        raise AgentInputError(str(exc)) from None
    if "\x00" in text or "�" in text:
        raise AgentInputError(
            f"{args.path!r} is binary or not valid UTF-8 text; it cannot be shown"
        )
    data = text.encode("utf-8")
    cut_off = len(data) > MAX_READ_BYTES
    if cut_off:
        text = data[:MAX_READ_BYTES].decode("utf-8", "ignore")
    if not text:
        return "(empty file)"
    if args.offset >= len(text):
        raise AgentInputError(
            f"offset {args.offset} is at or beyond the end of the readable text "
            f"({len(text)} characters)"
        )
    window = text[args.offset : args.offset + READ_PAGE_BYTES]
    window_bytes = window.encode("utf-8")
    if len(window_bytes) > READ_PAGE_BYTES:
        window = window_bytes[:READ_PAGE_BYTES].decode("utf-8", "ignore")
    end = args.offset + len(window)
    if end < len(text):
        return window + f"\n[more text follows; call read_file again with offset={end} to continue]"
    if cut_off:
        return window + (
            f"\n[end of the readable part: read_file shows at most the first {MAX_READ_BYTES} "
            "bytes of a file (the cap); the rest is not available]"
        )
    return window


def _list_dir(run: RunDir, scope: FileScope, args: ListDirArgs) -> str:
    norm = scope.normalise(args.path)
    if not scope.can_list(norm or "."):
        raise _denied(args.path, "readable", scope.readable)
    directory = run.path(norm) if norm else run.root
    try:
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
    except (FileNotFoundError, NotADirectoryError):
        raise AgentInputError(f"{args.path!r} is not a directory that exists") from None
    lines = []
    for entry in entries:
        name = entry.name
        child = f"{norm}/{name}" if norm else name
        if name.startswith(".") or entry.is_symlink():
            continue
        junction = getattr(entry, "is_junction", None)
        if junction is not None and junction():
            continue
        if entry.is_dir(follow_symlinks=False):
            if scope.can_list(child):
                lines.append(_printable(name) + "/")
        elif entry.is_file(follow_symlinks=False) and scope.can_read(child):
            lines.append(_printable(name))
    if not lines:
        return "(no entries you can read)"
    if len(lines) > MAX_LIST_ENTRIES:
        extra = len(lines) - MAX_LIST_ENTRIES
        lines = [*lines[:MAX_LIST_ENTRIES], f"[{extra} more entries not shown]"]
    return "\n".join(lines)


def _printable(name: str) -> str:
    return "".join(char if char.isprintable() else "?" for char in name)


def _write_file(run: RunDir, scope: FileScope, args: WriteFileArgs) -> str:
    norm = scope.normalise(args.path)
    if not scope.can_write(norm):
        raise _denied(args.path, "writable", scope.writable)
    try:
        size = len(args.content.encode("utf-8"))
    except UnicodeEncodeError:
        raise AgentInputError("the content contains characters that cannot be encoded") from None
    target = run.path(norm)  # refuses a symbolic link in any component
    try:
        info = os.lstat(target)
    except OSError:
        info = None
    if info is not None and stat.S_ISDIR(info.st_mode):
        raise AgentInputError(f"{args.path!r} is a directory; give a file path")
    try:
        run.write_text(norm, args.content, max_bytes=MAX_WRITE_BYTES)
    except (FileExistsError, NotADirectoryError, IsADirectoryError):
        raise AgentInputError(
            f"cannot write {args.path!r}: a part of the path is a file where a directory is needed"
        ) from None
    return f"wrote {size} bytes to {norm}"


def file_tools(run: RunDir, scope: FileScope) -> list[Tool]:
    """``read_file``, ``list_dir`` and ``write_file`` bound to one run directory and scope."""
    return [
        Tool(
            "read_file",
            "Read a UTF-8 text file from the run directory. Long files are returned in pages of "
            "about 7500 characters; the reply says which offset to pass to continue.",
            ReadFileArgs,
            lambda args: _read_page(run, scope, args),
        ),
        Tool(
            "list_dir",
            "List the files and directories you may read in a directory of the run directory "
            "(directories end with '/').",
            ListDirArgs,
            lambda args: _list_dir(run, scope, args),
        ),
        Tool(
            "write_file",
            f"Write a complete text file in the run directory, replacing any existing file "
            f"(at most {MAX_WRITE_BYTES} bytes). Only some paths are writable in each stage.",
            WriteFileArgs,
            lambda args: _write_file(run, scope, args),
        ),
    ]


def builtin_tools(run: RunDir, scope: FileScope) -> list[Tool]:
    """The tools every stage has: the calculator and the scoped file tools."""
    return [calculator_tool(), *file_tools(run, scope)]
