"""
Where this machine keeps its recordings and its builds, remembered once.

Every program in the chain opens a file dialog on the same folder, and until
now each guessed at it separately - so the Recording Fixer opened somewhere
the location GUI did not, and neither opened where the SD card was actually
copied. This is that folder, set once in the viewer's workflow list and read
by everything else.

Kept under %LOCALAPPDATA% rather than in the repo: it describes this machine,
not this project, and it must survive a checkout being cleared.

Builds are the same argument, one step further. The browser app and the
phone app are separate repositories that read the same surveys, so a build
sitting inside either checkout is reachable from only one of them. Both
folders are therefore neutral ground, set once and read by everything.
"""

from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# The places a recordings folder normally sits, tried in order when nothing
# has been set. RECORDINGS_DIR still wins over a guess, so an existing setup
# that already exported it keeps working.
GUESSES = [
    os.environ.get("RECORDINGS_DIR", ""),
    os.path.join(os.path.dirname(ROOT), "pingmapper", "recordings"),
    os.path.join(os.path.expanduser("~"), "Desktop", "pingmapper", "recordings"),
    os.path.join(ROOT, "recordings"),
]


# Where builds go when nothing has been chosen. A checkout that already
# holds surveys keeps them: pointing at a new empty folder would look
# exactly like the charts having been lost.
OUTPUT_GUESSES = [
    os.environ.get("ANCHORHOLD_OUTPUT", ""),
    os.path.join(ROOT, "output"),
]
OUTPUT_DEFAULT = os.path.join(os.path.expanduser("~"), "AnchorHold", "output")


def settings_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "AnchorHold", "workspace.json")


def _load() -> dict:
    try:
        with open(settings_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    path = settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
        fh.write("\n")


def saved_recordings_dir() -> str:
    """What was chosen, whether or not it still exists."""
    value = _load().get("recordingsDir") or ""
    return value if isinstance(value, str) else ""


def recordings_dir() -> str:
    """
    The folder to open recordings from, or empty if there is nowhere sensible.

    A folder that was set and has since gone is not silently replaced by a
    guess - that would move a program's dialog without telling anyone. It is
    only skipped once it is genuinely not a directory any more.
    """
    chosen = saved_recordings_dir()
    if chosen and os.path.isdir(chosen):
        return chosen
    for path in GUESSES:
        if path and os.path.isdir(path):
            return path
    return ""


def set_recordings_dir(folder: str) -> str:
    """
    Remember where recordings are kept. Returns the folder as stored.

    The path comes from a person typing or pasting it, so it is checked before
    it is kept: a setting that points nowhere is worse than none, because every
    dialog downstream would open on it.
    """
    folder = os.path.expandvars(os.path.expanduser((folder or "").strip().strip('"')))
    if not folder:
        raise ValueError("No folder given.")
    if not os.path.isdir(folder):
        raise ValueError(f"There is no folder at {folder}.")
    folder = os.path.abspath(folder)
    data = _load()
    data["recordingsDir"] = folder
    _save(data)
    return folder


def clear_recordings_dir() -> None:
    """Go back to guessing."""
    data = _load()
    data.pop("recordingsDir", None)
    _save(data)


def step_marks() -> dict:
    """Steps ticked or cleared by hand: id -> True/False."""
    marks = _load().get("stepMarks")
    if not isinstance(marks, dict):
        return {}
    return {k: bool(v) for k, v in marks.items() if isinstance(v, bool)}


def set_step_mark(step_id: str, done: bool) -> None:
    """
    Tick or clear one step by hand.

    Two states, and the mark stays where it was put. What the disk holds
    still decides a step nobody has touched, so the list is right the first
    time it is opened and yours after that.
    """
    data = _load()
    marks = data.get("stepMarks")
    if not isinstance(marks, dict):
        marks = {}
    marks[step_id] = bool(done)
    data["stepMarks"] = marks
    _save(data)


def panel_sections() -> dict:
    """Which of the Fixer's side-panel groups are folded open."""
    sections = _load().get("panelSections")
    if not isinstance(sections, dict):
        return {}
    return {k: bool(v) for k, v in sections.items() if isinstance(v, bool)}


def set_panel_section(name: str, is_open: bool) -> None:
    """Remember one group, so the panel opens the way it was left."""
    data = _load()
    sections = data.get("panelSections")
    if not isinstance(sections, dict):
        sections = {}
    sections[name] = bool(is_open)
    data["panelSections"] = sections
    _save(data)

def open_dir(preferred: str = "") -> str:
    """
    What a file dialog should open on, ready to pass as initialdir.

    Tk treats a missing directory as "use the last one", which is exactly the
    behaviour this is meant to remove, so an empty string is returned rather
    than a path that is not there.
    """
    if preferred and os.path.isdir(preferred):
        return preferred
    return recordings_dir()


if __name__ == "__main__":
    print("settings:", settings_path())
    print("saved:   ", saved_recordings_dir() or "(none)")
    print("in use:  ", recordings_dir() or "(nowhere found)")


# -- Where builds go --------------------------------------------------------

def saved_output_dir() -> str:
    """What was chosen for builds, whether or not it still exists."""
    value = _load().get("outputDir") or ""
    return value if isinstance(value, str) else ""


def output_dir() -> str:
    """
    The folder every build is written to and read from.

    A chosen folder wins, then ANCHORHOLD_OUTPUT, then a checkout's own
    output/ if it already holds surveys, and finally ~/AnchorHold/output.
    The folder is created, because every caller is about to use it.
    """
    chosen = saved_output_dir()
    if chosen:
        os.makedirs(chosen, exist_ok=True)
        return chosen
    for guess in OUTPUT_GUESSES:
        if guess and os.path.isdir(guess) and os.listdir(guess):
            return guess
    os.makedirs(OUTPUT_DEFAULT, exist_ok=True)
    return OUTPUT_DEFAULT


def set_output_dir(path: str) -> str:
    """Remember where builds go. Returns the folder, or raises."""
    path = os.path.abspath(os.path.expanduser((path or "").strip().strip('"')))
    if not path:
        raise ValueError("No folder given.")
    os.makedirs(path, exist_ok=True)
    data = _load()
    data["outputDir"] = path
    _save(data)
    return path


def clear_output_dir() -> None:
    """Forget the choice and fall back to the guesses."""
    data = _load()
    data.pop("outputDir", None)
    _save(data)
