"""Unit tests for the agent tool registry, the calculator and the scoped file tools."""

import errno
import os
import sys

import pytest
from pydantic import BaseModel

from cfdagent.agents import tools as tools_module
from cfdagent.agents.tools import (
    MAX_TOOL_OUTPUT_BYTES,
    MAX_WRITE_BYTES,
    RESERVED_FILES,
    FileScope,
    NoArgs,
    Tool,
    ToolRegistry,
    builtin_tools,
    calculate,
    truncate_utf8,
)
from cfdagent.errors import AgentInputError
from cfdagent.llm.client import UNPARSED_KEY, ToolCall
from cfdagent.prompts import PROMPTS_DIR, load_prompt
from cfdagent.record import RECORD_FILE, SUMMARY_FILE
from cfdagent.rundir import RunDir


def call(name: str, **arguments) -> ToolCall:
    return ToolCall(id="c1", name=name, arguments=arguments)


@pytest.fixture
def run(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    return RunDir(root)


def registry_for(run, readable=("work/*",), writable=("work/*",)) -> ToolRegistry:
    registry = ToolRegistry()
    scope = FileScope(readable=list(readable), writable=list(writable))
    for tool in builtin_tools(run, scope):
        registry.add(tool)
    return registry


# ------------------------------------------------------------------ calculator


def test_calculator_evaluates_and_rejects_names_and_calls():
    assert calculate("2 + 3 * 4") == "14"
    assert calculate("(1 + 2) ** 2 / 4") == "2.25"
    assert calculate("7 % 3") == "1"
    assert calculate("-2 ** 2") == "-4"
    assert calculate("sqrt(16) + 0 * pi") == "4"
    assert float(calculate("sin(pi / 2)")) == pytest.approx(1.0)
    assert float(calculate("log(e)")) == pytest.approx(1.0)
    assert float(calculate("atan2(1, 1) * 4 / pi")) == pytest.approx(1.0)
    assert calculate("max(1, 5, 3) + min(4, 2) + abs(-1) + pow(2, 3)") == "16"
    assert calculate("1e-3 * 2") == "0.002"
    for hostile in (
        "x + 1",
        "__import__('os').system('echo hi')",
        "open('f')",
        "().__class__",
        "math.sqrt(4)",
        "(lambda: 1)()",
        "[i for i in range(3)]",
        "{1: 2}",
        "'abc'",
        "True + 1",
        "1j",
        "sqrt(x=4)",
        "sqrt(*[4])",
        "1 if 1 else 2",
        "1 < 2",
        "not 1",
        "foo(1)",
        "sqrt",
        "sqrt(1, 2)",
        "atan2(1)",
        "min()",
        "1 +",
        "",
        "1; 2",
        "x = 1",
        "1 // 2",
    ):
        with pytest.raises(AgentInputError):
            calculate(hostile)


def test_calculator_rejects_huge_exponent():
    for expression in ("2 ** 1001", "2 ** -1001", "9 ** 9 ** 9", "pow(2, 5000)", "10 ** 400 ** 2"):
        with pytest.raises(AgentInputError, match="exponent"):
            calculate(expression)
    assert calculate("2 ** 1000") != ""  # exactly at the limit is allowed (a float, no error)


def test_calculator_rejects_long_and_deep_expressions_and_non_finite_results():
    with pytest.raises(AgentInputError, match="500"):
        calculate("1+" * 300 + "1")
    with pytest.raises(AgentInputError):
        calculate("-(" * 150 + "1" + ")" * 150)
    with pytest.raises(AgentInputError):
        calculate("-" * 400 + "1")
    with pytest.raises(AgentInputError):
        calculate("+".join(["1"] * 200))  # nesting depth of a long left-leaning sum
    for expression in ("1 / 0", "5 % 0", "sqrt(-1)", "log(0)", "(-8) ** 0.5", "exp(1000)"):
        with pytest.raises(AgentInputError):
            calculate(expression)
    with pytest.raises(AgentInputError, match="finite"):
        calculate("1e308 * 10")
    with pytest.raises(AgentInputError):
        calculate("9" * 400)  # an integer too large for a float


def test_calculator_tool_through_the_registry(run):
    registry = registry_for(run)
    assert registry.dispatch(call("calculator", expression="2*3")) == "6"
    out = registry.dispatch(call("calculator", expression="__import__('os')"))
    assert out.startswith("error:")
    assert registry.dispatch(call("calculator", expression="1/0")).startswith("error:")


# ------------------------------------------------------------------ registry


class Echo(BaseModel):
    text: str
    times: int = 1


def echo_tool(handler=None) -> Tool:
    return Tool("echo", "Repeat text.", Echo, handler or (lambda a: a.text * a.times))


def test_registry_schemas_are_openai_function_format():
    registry = ToolRegistry()
    registry.add(echo_tool())
    (schema,) = registry.schemas()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "echo"
    assert schema["function"]["description"] == "Repeat text."
    assert schema["function"]["parameters"]["properties"]["text"]["type"] == "string"
    assert schema["function"]["parameters"]["required"] == ["text"]
    with pytest.raises(ValueError, match="echo"):
        registry.add(echo_tool())


def test_dispatch_returns_validation_error_text():
    registry = ToolRegistry()
    registry.add(echo_tool())
    assert registry.dispatch(call("echo", text="ab", times=2)) == "abab"
    missing = registry.dispatch(call("echo"))
    assert "text" in missing and "required" in missing.lower()
    wrong = registry.dispatch(call("echo", text="a", times="many"))
    assert "times" in wrong
    assert "http" not in wrong.lower()  # no documentation URLs from pydantic
    unparsed = registry.dispatch(ToolCall("c", "echo", {UNPARSED_KEY: '{"text": "a'}))
    assert "not valid JSON" in unparsed and '{"text": "a' in unparsed


def test_dispatch_unknown_tool_lists_the_available_ones():
    registry = ToolRegistry()
    registry.add(echo_tool())
    out = registry.dispatch(call("rm_rf", path="/"))
    assert "unknown tool" in out and "rm_rf" in out and "echo" in out


def test_dispatch_converts_agent_input_errors_and_propagates_bugs():
    def bad_input(_args):
        raise AgentInputError("the script is wrong")

    def bug(_args):
        raise KeyError("programmer error")

    registry = ToolRegistry()
    registry.add(Tool("bad", "x", Echo, bad_input))
    registry.add(Tool("bug", "x", Echo, bug))
    assert registry.dispatch(call("bad", text="a")) == "error: the script is wrong"
    with pytest.raises(KeyError):
        registry.dispatch(call("bug", text="a"))


def test_dispatch_caps_output_and_stays_utf8_valid():
    registry = ToolRegistry()
    registry.add(echo_tool())
    out = registry.dispatch(call("echo", text="é" * 20000))  # 2 bytes per character
    data = out.encode("utf-8")
    assert len(data) <= MAX_TOOL_OUTPUT_BYTES
    assert "truncated" in out
    assert "\ufffd" not in out
    short = registry.dispatch(call("echo", text="x" * 7000))
    assert short == "x" * 7000  # under the cap: untouched
    assert MAX_TOOL_OUTPUT_BYTES == 8000


def test_dispatch_coerces_non_text_handler_results():
    registry = ToolRegistry()
    registry.add(Tool("n", "x", Echo, lambda a: 42))  # type: ignore[arg-type,return-value]
    assert registry.dispatch(call("n", text="a")) == "42"


# ------------------------------------------------------------------ file tools


def test_write_file_and_read_back(run):
    registry = registry_for(run)
    out = registry.dispatch(call("write_file", path="work/a.txt", content="hello\n"))
    assert "work/a.txt" in out and "6 bytes" in out
    assert run.read_text("work/a.txt") == "hello\n"
    assert registry.dispatch(call("read_file", path="work/a.txt")) == "hello\n"
    listing = registry.dispatch(call("list_dir", path="work"))
    assert listing.splitlines() == ["a.txt"]


def test_write_file_rejects_escape_and_oversize(run, tmp_path):
    registry = registry_for(run, readable=("*",), writable=("*",))
    for path in ("../evil.txt", "/etc/passwd", "a/../../b", "~/x", "C:/x", "a\\b", "", "a//b"):
        out = registry.dispatch(call("write_file", path=path, content="x"))
        assert out.startswith("error:"), path
    assert not (tmp_path / "evil.txt").exists()
    big = registry.dispatch(call("write_file", path="big.txt", content="x" * (MAX_WRITE_BYTES + 1)))
    assert big.startswith("error:") and "limit" in big
    assert not (run.root / "big.txt").exists()
    edge = registry.dispatch(call("write_file", path="ok.txt", content="x" * MAX_WRITE_BYTES))
    assert edge.startswith("wrote")
    # multi-byte content counts in bytes, not characters
    wide = registry.dispatch(call("write_file", path="w.txt", content="é" * MAX_WRITE_BYTES))
    assert wide.startswith("error:")


def test_write_file_respects_scope(run):
    registry = registry_for(run, readable=("work/*", "notes.md"), writable=("work/*.py",))
    run.write_text("notes.md", "read only\n")
    assert registry.dispatch(call("write_file", path="work/s.py", content="x=1")).startswith(
        "wrote"
    )
    for path in ("work/s.txt", "notes.md", "other/s.py", "work"):
        out = registry.dispatch(call("write_file", path=path, content="x"))
        assert out.startswith("error:") and "not writable" in out, path
    assert not (run.root / "other").exists()
    assert registry.dispatch(call("read_file", path="notes.md")) == "read only\n"
    out = registry.dispatch(call("read_file", path="run_record.json"))
    assert out.startswith("error:") and "not readable" in out


def test_scope_protects_hidden_and_reserved_names(run):
    registry = registry_for(run, readable=("*",), writable=("*",))
    run.write_text(".sandbox/req.json", "{}")
    for path in (".sandbox/req.json", "a/.hidden", "run_record.json", "llm_transcript.jsonl"):
        out = registry.dispatch(call("write_file", path=path, content="x"))
        assert out.startswith("error:"), path
    assert registry.dispatch(call("read_file", path=".sandbox/req.json")).startswith("error:")
    run.write_text("visible.txt", "v")
    assert ".sandbox" not in registry.dispatch(call("list_dir", path="."))


def test_write_file_refuses_symlinks_and_directories(run, tmp_path):
    registry = registry_for(run, readable=("*",), writable=("*",))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    try:
        os.symlink(outside, run.root / "link", target_is_directory=True)
        os.symlink(outside / "secret.txt", run.root / "flink")
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    for tool, args in (
        ("write_file", {"path": "link/new.txt", "content": "x"}),
        ("write_file", {"path": "flink", "content": "x"}),
        ("read_file", {"path": "flink"}),
        ("read_file", {"path": "link/secret.txt"}),
        ("list_dir", {"path": "link"}),
    ):
        out = registry.dispatch(call(tool, **args))
        assert out.startswith("error:") and "symbolic link" in out, (tool, args)
    assert not (outside / "new.txt").exists()
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "secret"
    assert "link" not in registry.dispatch(call("list_dir", path="."))  # links are not listed
    (run.root / "d").mkdir()
    out = registry.dispatch(call("write_file", path="d", content="x"))
    assert out.startswith("error:")
    (run.root / "afile").write_text("x", encoding="utf-8")
    out = registry.dispatch(call("write_file", path="afile/sub.txt", content="x"))
    assert out.startswith("error:")


def test_read_file_pages_through_a_long_file_and_reports_binary(run):
    registry = registry_for(run, readable=("*",), writable=())
    text = "".join(f"line {i}\n" for i in range(5000))  # about 44 KB
    run.write_text("long.txt", text)
    first = registry.dispatch(call("read_file", path="long.txt"))
    assert "offset=" in first and len(first.encode()) <= MAX_TOOL_OUTPUT_BYTES
    assert first.startswith("line 0\n")
    marker_offset = int(first.rsplit("offset=", 1)[1].split()[0].strip("`.,)]"))
    second = registry.dispatch(call("read_file", path="long.txt", offset=marker_offset))
    body = first.split("\n[")[0] + second.split("\n[")[0]
    assert text.startswith(body)
    assert registry.dispatch(call("read_file", path="long.txt", offset=10**9)).startswith("error:")
    (run.root / "bin.dat").write_bytes(b"\x00\x01\xff\xfe binary")
    out = registry.dispatch(call("read_file", path="bin.dat"))
    assert out.startswith("error:") and "binary" in out
    (run.root / "latin.txt").write_bytes("caf\xe9".encode("latin-1"))
    assert "UTF-8" in registry.dispatch(call("read_file", path="latin.txt"))
    missing = registry.dispatch(call("read_file", path="nope.txt"))
    assert missing.startswith("error:") and "does not exist" in missing
    (run.root / "dir").mkdir()
    assert registry.dispatch(call("read_file", path="dir")).startswith("error:")


def test_read_file_caps_at_200_kb_with_a_marker(run):
    registry = registry_for(run, readable=("*",), writable=())
    (run.root / "huge.txt").write_text("a" * 300_000, encoding="utf-8")
    out = registry.dispatch(call("read_file", path="huge.txt", offset=199_000))
    assert "200000" in out and "cap" in out
    out = registry.dispatch(call("read_file", path="huge.txt", offset=200_000))
    assert out.startswith("error:")
    (run.root / "toobig.txt").write_text("a" * 1_100_000, encoding="utf-8")
    out = registry.dispatch(call("read_file", path="toobig.txt"))
    assert out.startswith("error:") and "limit" in out


def test_list_dir_follows_the_scope(run):
    registry = registry_for(run, readable=("work/*.py", "docs/*"), writable=())
    run.write_text("work/a.py", "1")
    run.write_text("work/b.txt", "2")
    run.write_text("work/sub/c.py", "3")
    run.write_text("secret/s.txt", "4")
    run.write_text("docs/d.md", "5")
    top = registry.dispatch(call("list_dir", path="."))
    assert top.splitlines() == ["docs/", "work/"]  # secret/ is outside every readable pattern
    assert registry.dispatch(call("list_dir")).splitlines() == ["docs/", "work/"]
    assert registry.dispatch(call("list_dir", path="work")).splitlines() == ["a.py", "sub/"]
    out = registry.dispatch(call("list_dir", path="secret"))
    assert out.startswith("error:") and "not readable" in out
    assert registry.dispatch(call("list_dir", path="nope")).startswith("error:")
    assert registry.dispatch(call("list_dir", path="work/a.py")).startswith("error:")


def test_file_scope_uses_case_sensitive_fnmatch_on_normalised_paths():
    scope = FileScope(readable=["attempts/geo-*/*.py"], writable=["out/*"])
    assert scope.can_read("attempts/geo-1/s.py")
    assert scope.can_read("./attempts/geo-2/./s.py")
    assert not scope.can_read("attempts/geo-1/s.txt")
    assert not scope.can_read("ATTEMPTS/geo-1/s.py")
    assert scope.can_write("out/x/y.txt")
    assert not scope.can_write("attempts/geo-1/s.py")
    with pytest.raises(AgentInputError):
        scope.can_read("../x")


# ------------------------------------------------------------------ prompts


def test_load_prompt_version_is_a_hash_of_the_file(tmp_path):
    (tmp_path / "demo.md").write_bytes(b"You are an agent.\n")
    prompt = load_prompt("demo", prompts_dir=tmp_path)
    assert prompt.text == "You are an agent.\n"
    import hashlib

    assert prompt.version == hashlib.sha256(b"You are an agent.\n").hexdigest()[:12]
    (tmp_path / "demo.md").write_bytes(b"You are an agent!\n")
    assert load_prompt("demo", prompts_dir=tmp_path).version != prompt.version
    for bad in ("../demo", "a/b", "", "Demo", "demo.md", "x" * 100):
        with pytest.raises(ValueError):
            load_prompt(bad, prompts_dir=tmp_path)
    with pytest.raises(FileNotFoundError, match="missing"):
        load_prompt("missing", prompts_dir=tmp_path)
    assert PROMPTS_DIR.name == "prompts"


# ------------------------------------------------------------------ fix round 1

SURROGATE = chr(0xD800)


def test_calculator_min_max_need_two_arguments():
    for expression in ("min(1)", "max(5)", "min(pi)", "max(e)", "min(-1)", "min()", "max()"):
        with pytest.raises(AgentInputError, match="arguments"):
            calculate(expression)
    assert calculate("min(3, 1)") == "1"
    assert calculate("max(3, 1, 2)") == "3"


def test_calculator_turns_unexpected_evaluation_errors_into_text(monkeypatch):
    def boom(*_values):
        raise TypeError("surprise")

    monkeypatch.setitem(tools_module._FUNCTIONS, "sqrt", (boom, 1, 1))
    with pytest.raises(AgentInputError):
        calculate("sqrt(4)")
    for expression in ("abs(" * 120 + "1" + ")" * 120, "(" * 190 + "1" + ")" * 190):
        try:
            calculate(expression)  # deep nesting: an answer or text for the model, never a crash
        except AgentInputError:
            pass


def test_no_argument_tools_accept_empty_and_null_arguments():
    registry = ToolRegistry()
    registry.add(Tool("ping", "x", NoArgs, lambda args: "pong"))
    assert registry.dispatch(call("ping")) == "pong"
    for raw in ("", "null", "{}", "  null "):
        assert registry.dispatch(ToolCall("c", "ping", {UNPARSED_KEY: raw})) == "pong", raw
    assert "not valid JSON" in registry.dispatch(ToolCall("c", "ping", {UNPARSED_KEY: "[1"}))
    registry.add(echo_tool())
    out = registry.dispatch(ToolCall("c", "echo", {UNPARSED_KEY: "null"}))
    assert out.startswith("error:")  # a tool with required arguments still needs them


def test_paths_that_cannot_be_encoded_or_are_too_long_come_back_as_text(run):
    registry = registry_for(run, readable=("*",), writable=("*",))
    bad_paths = [
        "a" + SURROGATE + "b.txt",
        "x" * 201,
        "d/" + "y" * 201,
        chr(0xE9) * 101,  # 202 bytes in one component
        "/".join([chr(0xE9) * 100] * 6),  # 1205 bytes in total, 605 characters
    ]
    for path in bad_paths:
        for name, args in (
            ("write_file", {"path": path, "content": "x"}),
            ("read_file", {"path": path}),
            ("list_dir", {"path": path}),
        ):
            out = registry.dispatch(call(name, **args))
            assert out.startswith("error:"), (name, ascii(path)[:30])
    assert list(run.root.iterdir()) == []
    assert FileScope.normalise("y" * 200) == "y" * 200  # exactly at the limit is fine


def test_operating_system_path_errors_become_text_but_other_errors_propagate(run, monkeypatch):
    registry = registry_for(run, readable=("*",), writable=("*",))

    def too_long(*_args, **_kwargs):
        raise OSError(errno.ENAMETOOLONG, "File name too long")

    monkeypatch.setattr(run, "write_text", too_long)
    out = registry.dispatch(call("write_file", path="a.txt", content="x"))
    assert out.startswith("error:") and "cannot write" in out

    def disk_full(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(run, "write_text", disk_full)
    with pytest.raises(OSError):
        registry.dispatch(call("write_file", path="a.txt", content="x"))


@pytest.mark.skipif(sys.platform == "win32", reason="needs a file system that stores raw bytes")
def test_list_dir_skips_entries_whose_names_cannot_be_used(run):
    (run.root / "good.txt").write_text("g", encoding="utf-8")
    (run.root / os.fsdecode(b"bad" + bytes([0xFF]) + b".txt")).write_text("b", encoding="utf-8")
    (run.root / ("n" * 250)).write_text("n", encoding="utf-8")
    registry = registry_for(run, readable=("*",), writable=())
    assert registry.dispatch(call("list_dir", path=".")).splitlines() == ["good.txt"]


def test_truncate_utf8_always_returns_valid_text_within_the_limit():
    for text in ("a" + SURROGATE + "b", chr(0xE9) * 10, "x" * 10, ""):
        for limit in (1, 3, 5, 100):
            out = truncate_utf8(text, limit)
            out.encode("utf-8")  # strict: raises on a lone surrogate
            assert len(out.encode("utf-8")) <= limit, (ascii(text), limit)
    assert truncate_utf8("a" + SURROGATE + "b", 100) == "a?b"


def test_a_single_trailing_slash_is_accepted(run):
    registry = registry_for(run, readable=("*",), writable=("*",))
    run.write_text("attempts/toy-1/a.txt", "a")
    assert registry.dispatch(call("list_dir", path="attempts/toy-1/")).splitlines() == ["a.txt"]
    assert registry.dispatch(call("list_dir", path="attempts/")).splitlines() == ["toy-1/"]
    assert registry.dispatch(call("list_dir", path="./")).splitlines() == ["attempts/"]
    assert registry.dispatch(call("read_file", path="attempts/toy-1/a.txt/")).startswith("a")
    for path in ("a//", "/", "//", "attempts///"):
        assert registry.dispatch(call("list_dir", path=path)).startswith("error:"), path


def test_scope_fields_are_tuples_and_reserved_names_include_the_record_files():
    scope = FileScope(readable=["a/*"], writable=["b/*"])
    assert scope.readable == ("a/*",) and scope.writable == ("b/*",)
    assert FileScope().readable == ()
    assert {RECORD_FILE, SUMMARY_FILE, "llm_transcript.jsonl", "comparison.json"} <= set(
        RESERVED_FILES
    )
    assert not FileScope(writable=["*"]).can_write("comparison.json")
