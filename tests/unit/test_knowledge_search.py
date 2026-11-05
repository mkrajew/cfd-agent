"""Unit tests for the tutorial search and reader (``knowledge.search``)."""

import os

import pytest

from cfdagent.errors import AgentInputError, ToolFailure
from cfdagent.knowledge.search import (
    MAX_FILE_BYTES,
    Hit,
    read_tutorial,
    search_tutorials,
)


def write(root, rel, text="", data=None):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if data is not None:
        path.write_bytes(data)
    else:
        path.write_bytes(text.encode("utf-8"))  # no newline translation on Windows
    return path


@pytest.fixture
def tutorials(tmp_path):
    root = tmp_path / "tutorials"
    root.mkdir()
    return root


def test_search_ranks_filename_and_content_hits(tutorials):
    write(tutorials, "incompressible/icoFoam/alpha/system/fvSchemes", "ddtSchemes\n{\n}\n")
    write(tutorials, "incompressible/icoFoam/alpha/system/controlDict", "// see fvSchemes\n")
    write(tutorials, "incompressible/icoFoam/beta/notes.txt", "uses the fvSchemes dictionary\n")
    write(tutorials, "incompressible/icoFoam/beta/unrelated", "nothing here\n")

    hits = search_tutorials("fvSchemes", tutorials)

    assert all(isinstance(h, Hit) for h in hits)
    paths = [h.path for h in hits]
    # File-name hit first; content-only hits after it, in path order; no hit for the other file.
    assert paths == [
        "incompressible/icoFoam/alpha/system/fvSchemes",
        "incompressible/icoFoam/alpha/system/controlDict",
        "incompressible/icoFoam/beta/notes.txt",
    ]
    assert hits[0].line == 0 and "name" in hits[0].text  # the match is in the file name only
    assert hits[1].line == 1 and hits[1].text == "// see fvSchemes"
    assert hits[2].line == 1


def test_search_needs_all_terms_case_insensitively_over_name_and_content(tutorials):
    write(tutorials, "a/pimpleFoam/RAS/system/fvSolution", "solvers\n{\n    PIMPLE\n}\n")
    write(tutorials, "a/pimpleFoam/RAS/system/fvSchemes", "divSchemes\n")
    write(tutorials, "b/simpleFoam/system/fvSolution", "SIMPLE\n")

    both = search_tutorials("PIMPLEFOAM fvsolution", tutorials)  # one term in the path, one too
    assert [h.path for h in both] == ["a/pimpleFoam/RAS/system/fvSolution"]
    mixed = search_tutorials("pimple solvers", tutorials)  # path term and content term
    assert [h.path for h in mixed] == ["a/pimpleFoam/RAS/system/fvSolution"]
    assert mixed[0].line == 1 and mixed[0].text == "solvers"
    assert search_tutorials("pimple nonexistentword", tutorials) == []


def test_search_is_deterministic_and_bounded(tutorials):
    for name in ("c", "a", "b", "e", "d"):
        write(tutorials, f"case/{name}/system/controlDict", "application icoFoam;\n")
    first = search_tutorials("icoFoam", tutorials, max_hits=3)
    again = search_tutorials("icoFoam", tutorials, max_hits=3)
    assert first == again
    assert len(first) == 3
    assert [h.path for h in first] == sorted(h.path for h in first)
    assert search_tutorials("icoFoam", tutorials, max_hits=1)[0].path == first[0].path


def test_search_skips_large_and_binary_files(tutorials):
    big = "x" * (MAX_FILE_BYTES + 1)
    write(tutorials, "big/dict", "needle\n" + big)
    write(tutorials, "exact/dict", "needle\n" + "x" * (MAX_FILE_BYTES - len("needle\n")))
    write(tutorials, "bin/data", data=b"\x00\x01needle\x00")
    write(tutorials, "ok/dict", "needle\n")
    paths = [h.path for h in search_tutorials("needle", tutorials)]
    assert paths == ["exact/dict", "ok/dict"]  # the file of exactly the limit is searched


def test_search_does_not_follow_symbolic_links(tutorials, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("needle\n", encoding="utf-8")
    write(tutorials, "ok/dict", "needle\n")
    try:
        os.symlink(outside, tutorials / "linked_dir", target_is_directory=True)
        os.symlink(outside / "secret", tutorials / "linked_file")
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    assert [h.path for h in search_tutorials("needle", tutorials)] == ["ok/dict"]


def test_search_hit_text_is_one_bounded_line(tutorials):
    write(tutorials, "a/dict", "short\n" + "needle " + "y" * 1000 + "\nneedle again\n")
    (hit,) = search_tutorials("needle", tutorials)
    assert hit.line == 2
    assert len(hit.text) <= 200 and "\n" not in hit.text


def test_search_rejects_bad_queries_and_a_missing_root(tutorials, tmp_path):
    for query in ("", "   ", "x" * 500):
        with pytest.raises(AgentInputError):
            search_tutorials(query, tutorials)
    with pytest.raises(AgentInputError):
        search_tutorials(" ".join(f"t{i}" for i in range(30)), tutorials)
    with pytest.raises(ToolFailure, match="tutorial"):
        search_tutorials("x", tmp_path / "missing")
    with pytest.raises(ValueError):
        search_tutorials("x", tutorials, max_hits=0)


def test_read_tutorial_returns_the_text_and_marks_truncation(tutorials):
    write(tutorials, "a/system/controlDict", "application icoFoam;\n")
    assert read_tutorial("a/system/controlDict", tutorials) == "application icoFoam;\n"
    write(tutorials, "a/long", "z" * 50)
    text = read_tutorial("a/long", tutorials, max_bytes=20)
    assert text.startswith("z" * 20)
    assert "truncated" in text and len(text.encode()) < 200


def test_read_tutorial_refuses_escape_and_symlink(tutorials, tmp_path):
    write(tutorials, "ok/dict", "fine\n")
    (tmp_path / "secret").write_text("hidden\n", encoding="utf-8")
    for bad in (
        "../secret",
        "ok/../../secret",
        "/etc/passwd",
        "ok/\x00dict",
        "",
        "..",
        "a" + chr(92) + "b",
    ):
        with pytest.raises(AgentInputError):
            read_tutorial(bad, tutorials)
    absolute = str(tmp_path / "secret")
    with pytest.raises(AgentInputError):
        read_tutorial(absolute, tutorials)
    try:
        os.symlink(tmp_path / "secret", tutorials / "ok" / "link")
        os.symlink(tmp_path, tutorials / "dirlink", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    with pytest.raises(AgentInputError, match="symbolic link"):
        read_tutorial("ok/link", tutorials)
    with pytest.raises(AgentInputError, match="symbolic link"):
        read_tutorial("dirlink/secret", tutorials)


def test_read_tutorial_accepts_an_absolute_path_inside_the_root(tutorials):
    write(tutorials, "ok/dict", "fine\n")
    assert read_tutorial(str(tutorials / "ok" / "dict"), tutorials) == "fine\n"


def test_read_tutorial_refuses_binary_and_missing_and_lists_directories(tutorials):
    write(tutorials, "bin/data", data=b"\x00\x01")
    write(tutorials, "dir/b", "b")
    write(tutorials, "dir/a/inner", "a")
    with pytest.raises(AgentInputError, match="binary"):
        read_tutorial("bin/data", tutorials)
    with pytest.raises(AgentInputError, match="does not exist"):
        read_tutorial("dir/missing", tutorials)
    assert read_tutorial("dir", tutorials).splitlines() == ["a/", "b"]
    with pytest.raises(ValueError):
        read_tutorial("dir/b", tutorials, max_bytes=0)


# ------------------------------------------------------------------ fix round 1


def test_read_tutorial_refuses_dot_dot_after_an_absolute_prefix(tutorials, tmp_path):
    write(tutorials, "ok/dict", "fine\n")
    (tmp_path / "secret").write_bytes(b"hidden\n")
    base = str(tutorials)
    for bad in (
        base + "/../secret",
        base + "/ok/../../secret",
        base + "/../../../../../../../../etc/hostname",
        base + "/..",
        "/ok/../../secret",
    ):
        with pytest.raises(AgentInputError):
            read_tutorial(bad, tutorials)
    # A dot-dot that stays inside is refused too: the rule is "no `..` at all".
    with pytest.raises(AgentInputError):
        read_tutorial(base + "/ok/../ok/dict", tutorials)


def test_read_tutorial_checks_the_real_path_as_defence_in_depth(tutorials, tmp_path, monkeypatch):
    import cfdagent.knowledge.search as module

    write(tutorials, "ok/dict", "fine\n")
    monkeypatch.setattr(module, "_parts_below", lambda path, root: ["..", "secret"])
    (tmp_path / "secret").write_bytes(b"hidden\n")
    with pytest.raises(AgentInputError, match="outside"):
        read_tutorial("anything", tutorials)


def test_validation_directories_and_plot_files_are_not_searched_or_readable(tutorials):
    write(tutorials, "case/system/controlDict", "needle\n")
    write(tutorials, "case/validation/Data/profile.dat", "needle\n")
    write(tutorials, "case/Validation/x", "needle\n")
    write(tutorials, "case/plotResults", "needle\n")
    write(tutorials, "case/Plot.gp", "needle\n")
    write(tutorials, "case/plain/notplot", "needle\n")
    paths = [h.path for h in search_tutorials("needle", tutorials)]
    assert paths == ["case/plain/notplot", "case/system/controlDict"]
    for hidden in ("case/validation/Data/profile.dat", "case/plotResults", "case/Plot.gp"):
        with pytest.raises(AgentInputError, match="not available"):
            read_tutorial(hidden, tutorials)
    assert read_tutorial("case", tutorials).splitlines() == ["plain/", "system/"]
    with pytest.raises(AgentInputError):
        read_tutorial("case/validation", tutorials)


def test_experimental_data_directories_are_skipped_but_validation_set_ups_stay(tutorials):
    write(tutorials, "case/system/controlDict", "needle\n")
    write(tutorials, "case/experimentalData/profile.dat", "needle\n")
    write(tutorials, "verificationAndValidation/setup/system/controlDict", "needle\n")
    paths = [h.path for h in search_tutorials("needle", tutorials)]
    assert paths == [
        "case/system/controlDict",
        "verificationAndValidation/setup/system/controlDict",
    ]
    with pytest.raises(AgentInputError, match="not available"):
        read_tutorial("case/experimentalData/profile.dat", tutorials)
    assert read_tutorial("case", tutorials).splitlines() == ["system/"]


def test_a_real_path_on_another_drive_counts_as_outside_the_root(monkeypatch):
    import types

    import cfdagent.knowledge.search as module

    def different_drives(paths):
        raise ValueError("Paths don't have the same drive")

    fake_os = types.SimpleNamespace(path=types.SimpleNamespace(commonpath=different_drives))
    monkeypatch.setattr(module, "os", fake_os)
    assert module._is_inside("/root", "/root") is True
    assert module._is_inside("/root", "elsewhere") is False
