"""Unit tests for the tutorial search and reader (``knowledge.search``)."""

import os
from importlib import resources

import pytest

from cfdagent.errors import AgentInputError, ToolFailure
from cfdagent.knowledge.search import (
    MAX_FILE_BYTES,
    NOTES_DIR,
    Hit,
    KnowledgeRoot,
    SourcedHit,
    read_knowledge,
    read_tutorial,
    search_knowledge,
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


# ------------------------------------------------------------------ curated notes and tutorials

NOTE_NAMES = (
    "README.md",
    "gmsh-api.md",
    "cadquery-api.md",
    "openfoam-function-objects.md",
    "steady-solver-setup.md",
    "transient-time-step.md",
    "forces-and-coefficients.md",
    "axisymmetric-wedge.md",
)


@pytest.fixture
def notes(tmp_path):
    root = tmp_path / "notes"
    root.mkdir()
    return root


@pytest.fixture
def roots(notes, tutorials):
    return [KnowledgeRoot("tutorial", tutorials), KnowledgeRoot("note", notes)]


def test_note_hits_rank_before_tutorial_hits(notes, tutorials, roots):
    # The tutorial files match better (the terms are in their path), the note only in its text.
    write(tutorials, "flow/relaxation/system/fvSolution", "relaxationFactors\n")
    write(tutorials, "flow/other/system/fvSolution", "relaxationFactors\n")
    write(notes, "steady-solver-setup.md", "# Steady\nSet relaxationFactors in fvSolution.\n")
    write(notes, "unrelated.md", "nothing here\n")

    hits = search_knowledge("relaxation fvSolution", roots)

    assert all(isinstance(h, SourcedHit) for h in hits)
    assert [(h.source, h.path) for h in hits] == [
        ("note", "note:steady-solver-setup.md"),
        ("tutorial", "flow/relaxation/system/fvSolution"),
        ("tutorial", "flow/other/system/fvSolution"),
    ]
    assert hits[0].line == 2 and "relaxationFactors" in hits[0].text
    # The same result whichever order the roots are given in.
    assert search_knowledge("relaxation fvSolution", list(reversed(roots))) == hits
    two = search_knowledge("relaxation fvSolution", roots, max_hits=2)
    assert [h.source for h in two] == ["note", "tutorial"]
    assert len(search_knowledge("relaxation fvSolution", roots, max_hits=1)) == 1


def test_search_knowledge_shows_only_plain_note_files(notes, roots):
    write(notes, "a.md", "needle\n")
    write(notes, "sub/b.md", "needle\n")  # not readable through note:<file>, so not offered
    write(notes, "c.txt", "needle\n")
    assert [h.path for h in search_knowledge("needle", roots)] == ["note:a.md"]


def test_a_missing_notes_root_is_the_images_fault_not_a_silent_skip(tutorials, tmp_path):
    write(tutorials, "a/dict", "needle\n")
    missing = KnowledgeRoot("note", tmp_path / "no-notes")
    with pytest.raises(ToolFailure):
        search_knowledge("needle", [missing, KnowledgeRoot("tutorial", tutorials)])
    with pytest.raises(ToolFailure):
        search_knowledge("needle", [missing])
    only = [KnowledgeRoot("tutorial", tutorials)]  # no notes root asked for: tutorials work
    hits = search_knowledge("needle", only)
    assert [(h.source, h.path) for h in hits] == [("tutorial", "a/dict")]


def test_a_missing_tutorial_root_fails_only_when_it_is_requested(notes, tmp_path):
    write(notes, "a.md", "needle\n")
    missing = KnowledgeRoot("tutorial", tmp_path / "no-tutorials")
    only_notes = [KnowledgeRoot("note", notes)]
    assert [h.path for h in search_knowledge("needle", only_notes)] == ["note:a.md"]
    with pytest.raises(ToolFailure):
        search_knowledge("needle", [*only_notes, missing])


def test_search_knowledge_rejects_bad_queries_and_limits(roots):
    for query in ("", "   ", "x" * 300):
        with pytest.raises(AgentInputError):
            search_knowledge(query, roots)
    with pytest.raises(ValueError):
        search_knowledge("x", roots, max_hits=0)


def test_knowledge_root_names_are_checked(tmp_path):
    with pytest.raises(ValueError):
        KnowledgeRoot("benchmark", tmp_path)  # type: ignore[arg-type]


def test_read_knowledge_note_prefix_and_refusals(notes, tutorials, roots, tmp_path):
    write(notes, "gmsh-api.md", "# Gmsh\nUse the OCC kernel.\n")
    write(notes, "long.md", "x" * 100)
    write(tutorials, "flow/alpha/system/fvSchemes", "ddtSchemes { default Euler; }")
    write(tmp_path, "secret.md", "secret")

    assert read_knowledge("note:gmsh-api.md", roots) == "# Gmsh\nUse the OCC kernel.\n"
    assert "[file truncated" in read_knowledge("note:long.md", roots, max_bytes=10)
    # Ordinary tutorial paths behave as read_tutorial does, directories included.
    assert read_knowledge("flow/alpha/system/fvSchemes", roots) == "ddtSchemes { default Euler; }"
    assert read_knowledge("flow/alpha", roots) == "system/"

    for refused in (
        "note:",
        "note:../secret.md",
        "note:..",
        "note:sub/x.md",
        "note:/gmsh-api.md",
        "note:" + chr(92) + "gmsh-api.md",
        "note:gmsh-api.txt",
        "note:gmsh-api",
        "note:missing.md",
        "note:gmsh-api.md" + chr(0),
        "note:" + "a" * 2000 + ".md",
        "Note:gmsh-api.md",
        "other:gmsh-api.md",
        "http://example.org/x",
        str(tmp_path / "secret.md"),
        "../secret.md",
    ):
        with pytest.raises(AgentInputError):
            read_knowledge(refused, roots)
    for not_a_text in ("", "  ", None, 5):
        with pytest.raises(AgentInputError):
            read_knowledge(not_a_text, roots)  # type: ignore[arg-type]


def test_read_knowledge_refuses_a_note_that_is_a_link(notes, roots, tmp_path):
    write(tmp_path, "outside.md", "secret")
    try:
        os.symlink(tmp_path / "outside.md", notes / "linked.md")
        os.symlink(tmp_path, notes / "dirlink", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    with pytest.raises(AgentInputError):
        read_knowledge("note:linked.md", roots)
    with pytest.raises(AgentInputError):
        read_knowledge("note:dirlink", roots)


def test_read_knowledge_without_the_requested_root_is_refused(notes, tutorials):
    write(notes, "a.md", "text")
    write(tutorials, "a/dict", "text")
    only_notes = [KnowledgeRoot("note", notes)]
    only_tutorials = [KnowledgeRoot("tutorial", tutorials)]
    assert read_knowledge("note:a.md", only_notes) == "text"
    assert read_knowledge("a/dict", only_tutorials) == "text"
    with pytest.raises(AgentInputError):
        read_knowledge("a/dict", only_notes)
    with pytest.raises(AgentInputError):
        read_knowledge("note:a.md", only_tutorials)


def test_read_knowledge_with_a_missing_notes_directory_is_the_images_fault(tmp_path):
    with pytest.raises(ToolFailure):
        read_knowledge("note:a.md", [KnowledgeRoot("note", tmp_path / "gone")])
    with pytest.raises(AgentInputError):  # no notes root requested at all
        read_knowledge("note:a.md", [])


def test_notes_are_packaged():
    package = resources.files("cfdagent.knowledge") / "notes"
    for name in NOTE_NAMES:
        entry = package / name
        assert entry.is_file(), f"{name} is not shipped inside the package"
        assert entry.read_text(encoding="utf-8").strip(), f"{name} is empty"
    assert NOTES_DIR.is_absolute() and NOTES_DIR.is_dir()
    assert {entry.name for entry in NOTES_DIR.iterdir()} >= set(NOTE_NAMES)
