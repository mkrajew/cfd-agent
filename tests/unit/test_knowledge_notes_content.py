"""Guards on the content of the shipped knowledge notes (README rules for a note)."""

from __future__ import annotations

import re

from cfdagent.knowledge.search import NOTES_DIR

MAX_NOTE_LINES = 150  # README.md, "Rules for the content of a note", rule 4
# A residualControl block is a flat dictionary: no nested braces inside it.
RESIDUAL_CONTROL_BLOCK = re.compile(r"residualControl\s*\{([^{}]*)\}")
COMPONENT_KEY = re.compile(r"(?<![A-Za-z])U[xyz]\s+[0-9.]+(?:[eE][+-]?[0-9]+)?\s*;")


def note_files():
    files = sorted(NOTES_DIR.glob("*.md"))
    assert files, "no notes found: the checks below would pass vacuously"
    return files


def test_every_note_is_under_the_line_limit():
    too_long = {
        p.name: n
        for p in note_files()
        if (n := len(p.read_text(encoding="utf-8").splitlines())) >= MAX_NOTE_LINES
    }
    assert too_long == {}, f"notes at or over {MAX_NOTE_LINES} lines: {too_long}"


def test_no_note_recommends_component_keys_in_residual_control():
    # residualControl keys are field names; a component key (Ux) is silently ignored, so a note
    # that writes one as the way to stop a run teaches a control that does nothing.
    offenders = []
    for path in note_files():
        text = path.read_text(encoding="utf-8")
        for block in RESIDUAL_CONTROL_BLOCK.findall(text):
            if COMPONENT_KEY.search(block):
                offenders.append(f"{path.name}: residualControl {{{block.strip()}}}")
    assert offenders == [], "component keys in residualControl:\n" + "\n".join(offenders)


def test_the_component_key_check_can_fail():
    block = "residualControl { p 1e-5; Ux 1e-5; Uy 1e-5; }"
    (found,) = RESIDUAL_CONTROL_BLOCK.findall(block)
    assert COMPONENT_KEY.search(found)
    (clean,) = RESIDUAL_CONTROL_BLOCK.findall("residualControl { p 1e-8; U 1e-5; }")
    assert not COMPONENT_KEY.search(clean)


def test_cadquery_note_says_shape_faces_and_solids_are_lists():
    text = (NOTES_DIR / "cadquery-api.md").read_text(encoding="utf-8")
    assert "len(solid.val().Faces())" in text
    assert "len(solid.val().Solids())" in text
    assert "lists" in text and "`.size()`" in text and "Workplane method" in text


def test_cadquery_note_says_to_rotate_a_wedge_after_revolving():
    text = " ".join((NOTES_DIR / "cadquery-api.md").read_text(encoding="utf-8").split())
    assert "rotate the revolved solid" in text
    assert "after `.revolve`" in text
    assert "rotating the sketch before `.revolve`" in text
    assert "no effect" in text
    assert "original pending wire, not the rotated copy" in text
    assert "about 2 R sin(angle/2)" in text
