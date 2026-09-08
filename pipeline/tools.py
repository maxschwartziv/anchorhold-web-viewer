"""
The desktop half of the workflow, offered to the browser as a checklist.

AnchorHold Web Viewer is the last stage of a pipeline whose earlier stages are
Windows programs: Survey Planner draws the lines, the Recording Fixer repairs
what came off the card, Add Survey Locations builds the charts. Each is a
separate launcher nobody remembers the name of, run in an order nobody writes
down, and the viewer is the one piece already open when you need the next one.

A browser cannot start a program. The server it is talking to can, and only
ever from this machine - the same rule that already guards adding and removing
charts. The client names a step from a fixed list; it never sends a path.

Each step also reports what is on disk, so the list says where the work has
actually got to rather than only what could be run.
"""

from __future__ import annotations

import os
import subprocess
import sys

import web_charts
import workspace                  # where recordings are kept

# Everything PINGVerter can hand to PINGMapper. Counting only .DAT read a
# folder of Lowrance recordings as empty, which is the checklist reporting
# on the format rather than on the work.
RECORDING_EXTS = (".dat", ".sl2", ".sl3", ".rsd", ".svlog", ".jsf", ".xtf")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Survey Planner is its own project and lives outside this repo, so it is
# looked for rather than assumed. An environment variable wins; after that,
# the places a checkout normally sits relative to this one.
# Survey Planner ships inside this repository. The three paths beside it are
# where the project used to live, and are kept so an older layout, or a copy
# someone keeps elsewhere, still resolves.
PLANNER_CANDIDATES = [
    os.environ.get("SURVEY_PLANNER", ""),
    os.path.join(ROOT, "SurveyPlanner", "run.bat"),
    os.path.join(os.path.dirname(ROOT), "SurveyPlanner", "run.bat"),
    os.path.join(os.path.dirname(ROOT), "survey-planner", "run.bat"),
    os.path.join(os.path.expanduser("~"), "Desktop", "SurveyPlanner", "run.bat"),
]

def _first_file(candidates) -> str:
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return ""


def _first_dir(candidates) -> str:
    for path in candidates:
        if path and os.path.isdir(path):
            return path
    return ""


def _in_repo(name: str) -> str:
    path = os.path.join(ROOT, name)
    return path if os.path.isfile(path) else ""


# id -> what it is and how to start it. The launcher is resolved when asked,
# not at import, so installing Survey Planner does not need a server restart.
STEPS = [
    {
        "id": "plan",
        "phase": 1,
        "name": "Plan the survey",
        "what": "Draw the lines in Survey Planner and export a mission for "
                "QGroundControl.",
        "launcher": lambda: _first_file(PLANNER_CANDIDATES),
        "missing": "Survey Planner was not found. Set SURVEY_PLANNER to its "
                   "run.bat, or put the project beside this one.",
    },
    {
        "id": "record",
        "phase": 2,
        "name": "Record and copy off the card",
        "what": "Run the survey, then copy the .DAT and its folder together "
                "onto this PC.",
        "launcher": lambda: "",          # nothing to launch; it happens on the water
    },
    {
        "id": "fix",
        "phase": 3,
        "name": "Check and repair the recording",
        "what": "Flag bad fixes, depth spikes and turns, and write a repaired "
                "copy. The files off the card are never touched.",
        "launcher": lambda: _in_repo("Fix_Recording.bat"),
    },
    {
        "id": "build",
        "phase": 4,
        "name": "Build the charts",
        "what": "Decode the recording and make the depth map, mosaic, "
                "substrate, rock and bottom objects.",
        "launcher": lambda: _in_repo("Add_Survey_Locations.bat"),
    },
    {
        "id": "library",
        "phase": 5,
        "name": "Add the survey to this viewer",
        "what": "Settings, then Charts on this computer. Charts are linked, "
                "not copied, so adding one costs no disk.",
        "launcher": lambda: "",          # done in the app itself
        "settings": True,
    },
]


def folder_for(step_id: str) -> str:
    """
    The folder a step works in, or empty.

    Three of the steps are really about a place on disk: where recordings
    are kept, where builds land, what the library holds. The other three
    are about a program, and have none.
    """
    if step_id == "record":
        return workspace.recordings_dir()
    if step_id == "build":
        path = workspace.output_dir()
        return path if os.path.isdir(path) else ""
    if step_id == "library":
        return web_charts.LIBRARY if os.path.isdir(web_charts.LIBRARY) else ""
    return ""


def open_folder(step_id: str) -> dict:
    """
    Show that folder in the file manager.

    Same rule as launching a program: the caller names a step, never a
    path, and only this machine may ask.
    """
    if not any(s["id"] == step_id for s in STEPS):
        raise ValueError(f"There is no step called {step_id}.")
    folder = folder_for(step_id)
    if not folder:
        raise ValueError("That step has no folder on this machine.")
    if os.name == "nt":
        os.startfile(folder)                      # noqa: S606 - a folder
    elif sys.platform == "darwin":
        subprocess.Popen(["open", folder], close_fds=True)
    else:
        subprocess.Popen(["xdg-open", folder], close_fds=True)
    return {"id": step_id, "folder": folder}


def _survey_dirs() -> list:
    """Every survey built under output/, whatever state it is in."""
    build_dir = workspace.output_dir()
    if not os.path.isdir(build_dir):
        return []
    out = []
    for name in sorted(os.listdir(build_dir)):
        path = os.path.join(build_dir, name)
        if os.path.isdir(path) and web_charts.looks_like_chart(path):
            out.append((name, path))
    return out


def _recording_counts():
    """(recordings, repaired) in the recordings folder, or None if none found."""
    folder = workspace.recordings_dir()
    if not folder:
        return None
    total = repaired = 0
    try:
        for name in os.listdir(folder):
            lowered = name.lower()
            if not lowered.endswith(RECORDING_EXTS):
                continue
            total += 1
            # _fixed is this pipeline's own naming, so it only ever appears
            # on a Humminbird recording the Fixer has been through.
            if "_fixed" in lowered:
                repaired += 1
    except OSError:
        return None
    return {"folder": folder, "recordings": total, "repaired": repaired}


def status() -> dict:
    """
    Where the work has actually got to, read off the disk.

    Every figure here is a count of something real; nothing is remembered
    between runs, so a folder emptied by hand is reflected immediately.
    """
    surveys = _survey_dirs()
    with_objects = [name for name, path in surveys
                    if os.path.isfile(os.path.join(path, "detections.geojson"))]
    catalog = web_charts.load()
    installed = [entry.get("id") for entry in catalog.get("locations", [])]
    return {
        "recordings": _recording_counts(),
        "surveys": [name for name, _ in surveys],
        "surveysWithObjects": with_objects,
        "charts": [i for i in installed if i],
        "defaultChart": catalog.get("defaultLocationId"),
    }


def listing() -> dict:
    """The steps, each with its launcher resolved and the disk state alongside."""
    state = status()
    marks = workspace.step_marks()
    steps = []
    for step in STEPS:
        launcher = step["launcher"]()
        derived = _done(step["id"], state)
        marked = marks.get(step["id"])
        steps.append({
            "id": step["id"],
            "phase": step["phase"],
            "name": step["name"],
            "what": step["what"],
            "canLaunch": bool(launcher),
            "launcher": os.path.basename(launcher) if launcher else "",
            "missing": "" if launcher else step.get("missing", ""),
            "settings": bool(step.get("settings")),
            "folder": folder_for(step["id"]),
            "folderIsSet": (step["id"] == "record"
                            and bool(workspace.saved_recordings_dir())),
            "canSetFolder": step["id"] == "record",
            "done": derived if marked is None else marked,
            "doneFromDisk": derived,
            "marked": marked,
            "detail": _detail(step["id"], state),
        })
    return {"steps": steps, "status": state}


def _done(step_id: str, state: dict) -> bool:
    """
    Whether a step has visibly produced something.

    Deliberately conservative: this reports evidence on disk, not completion.
    Planning leaves nothing here to find, so it never claims to be done.
    """
    if step_id == "record":
        rec = state["recordings"]
        return bool(rec and rec["recordings"])
    if step_id == "fix":
        rec = state["recordings"]
        return bool(rec and rec["repaired"])
    if step_id == "build":
        return bool(state["surveys"])
    if step_id == "library":
        return bool(state["charts"])
    return False


def mark(step_id: str, done) -> dict:
    """Tick or clear a step by hand, against what the disk says."""
    if not any(s["id"] == step_id for s in STEPS):
        raise ValueError(f"There is no step called {step_id}.")
    derived = _done(step_id, status())
    workspace.set_step_mark(step_id, bool(done))
    return {"id": step_id, "done": bool(done), "doneFromDisk": derived}


def _detail(step_id: str, state: dict) -> str:
    """One line of what was found, in the terms of that step."""
    def plural(n, word):
        return f"{n} {word}" + ("" if n == 1 else "s")

    if step_id == "record":
        rec = state["recordings"]
        if not rec:
            return "No recordings folder yet - set the one you copy cards into."
        if not rec["recordings"]:
            return "No sonar recordings in that folder yet."
        return plural(rec["recordings"], "recording") + " on this PC."
    if step_id == "fix":
        rec = state["recordings"]
        if not rec:
            return ""
        if not rec["repaired"]:
            return "Nothing repaired yet."
        return plural(rec["repaired"], "repaired recording") + " ready to build."
    if step_id == "build":
        surveys = state["surveys"]
        if not surveys:
            return "Nothing built yet."
        objects = len(state["surveysWithObjects"])
        line = plural(len(surveys), "survey") + " built"
        return line + (f", {objects} with bottom objects." if objects else ".")
    if step_id == "library":
        charts = state["charts"]
        if not charts:
            return "No charts in the library."
        return plural(len(charts), "chart") + " in the library."
    return ""


def launch(step_id: str) -> dict:
    """
    Start one step's program, and do not wait for it.

    The id is looked up in the table above; nothing the caller sends becomes
    part of a path. A GUI that ran as a child of this server would die with it
    and hold the port open, so it is detached where Windows allows it.
    """
    step = next((s for s in STEPS if s["id"] == step_id), None)
    if step is None:
        raise ValueError(f"There is no step called {step_id}.")
    launcher = step["launcher"]()
    if not launcher:
        raise ValueError(step.get("missing")
                         or f"{step['name']} has nothing to launch.")

    if os.name == "nt":
        # Exactly what double-clicking it does, and that matters: these are
        # .bat launchers that install missing packages on a first run, print
        # what they are doing, and pause on failure. Started detached they get
        # no console, so all of that happens where nobody can see it - the
        # server reports a successful start and no window ever appears.
        os.startfile(launcher)                    # noqa: S606 - from a fixed table
    else:
        subprocess.Popen([launcher], cwd=os.path.dirname(launcher) or ROOT,
                         close_fds=True, start_new_session=True)
    return {"id": step_id, "name": step["name"],
            "launcher": os.path.basename(launcher)}


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(listing(), indent=2))
