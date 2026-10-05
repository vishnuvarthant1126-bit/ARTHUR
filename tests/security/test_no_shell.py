"""The rule "the LLM never executes arbitrary shell commands", checked in the code itself.

ARTHUR starts exactly one kind of program: an allowed desktop app (Notepad, Calculator,
Explorer) from a fixed path, without a shell. This test fails if anyone adds another way to
run commands anywhere in app/.
"""

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"
ALLOWED_LAUNCHER = APP / "computer" / "desktop.py"  # Popen([fixed exe path, ...]), no shell
FORBIDDEN_CALLS = {"system", "popen", "spawnl", "spawnv", "execv", "execl", "startfile"}


def python_files():
    return sorted(APP.rglob("*.py"))


def test_no_shell_true_anywhere():
    for path in python_files():
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if isinstance(node, ast.keyword) and node.arg == "shell":
                pytest.fail(f"{path.relative_to(APP)}: shell= is not allowed")


def test_no_os_command_functions():
    for path in python_files():
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in FORBIDDEN_CALLS
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
            ):
                pytest.fail(f"{path.relative_to(APP)}: os.{node.attr} is not allowed")


def test_subprocess_only_in_the_app_launcher():
    def imports_subprocess(path: Path) -> bool:
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if isinstance(node, ast.Import) and any(a.name == "subprocess" for a in node.names):
                return True
            if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                return True
        return False

    users = [
        path.relative_to(APP).as_posix() for path in python_files() if imports_subprocess(path)
    ]
    assert users == [ALLOWED_LAUNCHER.relative_to(APP).as_posix()]


def test_the_launcher_starts_only_fixed_programs():
    tree = ast.parse(ALLOWED_LAUNCHER.read_text("utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"Popen", "run", "call", "check_output"}
    ]
    assert calls  # the launcher exists
    for call in calls:
        program = call.args[0]
        # Always a list whose first item comes from _system_exe(...) - never a string command.
        assert isinstance(program, ast.List), ast.unparse(call)
        assert ast.unparse(program.elts[0]).startswith("_system_exe("), ast.unparse(call)
