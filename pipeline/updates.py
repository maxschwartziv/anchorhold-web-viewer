#!/usr/bin/env python3
"""
Is there a newer AnchorHold, or newer sonar tools, than the ones on this PC?

AnchorHold is a git checkout of its GitHub repository, so "an update" is new
commits on the branch it tracks. Checking fetches them and says what they
are; updating is a fast-forward pull and nothing stronger - git refuses a
pull that would touch a file edited here, so local changes are never lost to
it, and a checkout that has commits of its own is told so rather than merged.

The sonar tools - PINGMapper, RockMapper, GhostVision - each live in a conda
environment of their own, with the PINGEcosystem packages they are built from
(PINGVerter, PINGTile, PINGSeg, ...). Each package is checked against PyPI in
the environment that uses it. Upgrading is offered, never assumed: it runs
pip in that environment, and a new PINGMapper can change the behaviour the
pipeline works around (see depth_csv_from_sonar.py), so it deserves a look.

    python pipeline/updates.py            # check, print what was found
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPO_PAGE = "https://github.com/maxschwartziv/anchorhold-web-viewer"
PYPI = "https://pypi.org/pypi/{name}/json"

# At most this many new commits are listed; the rest are counted.
LIST_COMMITS = 15

# The tools, and the PINGEcosystem packages each environment is built from.
# A package missing from an environment is simply not reported there.
TOOLS = [
    ("PINGMapper", "ping", ["pingmapper", "pingverter", "pingwizard"]),
    ("RockMapper", "rockmapper", ["rockmapper", "pingseg", "pingtile", "pingwizard"]),
    ("GhostVision", "ghostvision", ["ghostvision", "pingdetect", "pingtile",
                                    "pingmapper", "pingverter"]),
]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(*args, timeout=60):
    """Run git in the repository. Returns (exit code, stdout+stderr stripped)."""
    proc = subprocess.run(["git", "-C", ROOT, *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout,
                          creationflags=_NO_WINDOW)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def check_app(log=print) -> dict:
    """
    What GitHub has that this checkout does not.

    Returns {ok, reason, branch, upstream, behind, ahead, commits, blocked}:
    `behind` new commits are available, `ahead` are local ones GitHub lacks,
    and `blocked` names locally edited files the new commits also change - a
    pull would stop on those.
    """
    result = {"ok": False, "reason": "", "branch": "", "upstream": "",
              "behind": 0, "ahead": 0, "commits": [], "blocked": []}
    if not shutil.which("git"):
        result["reason"] = ("Git is not installed, so this copy cannot update "
                            f"itself. Newer versions are at {REPO_PAGE}")
        return result
    code, _ = _git("rev-parse", "--is-inside-work-tree")
    if code != 0:
        result["reason"] = ("This copy was not cloned with git (a zip download?), "
                            f"so it cannot update itself. Newer versions are at {REPO_PAGE}")
        return result

    _, result["branch"] = _git("rev-parse", "--abbrev-ref", "HEAD")
    code, upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if code != 0:
        result["reason"] = (f"Branch {result['branch']} does not track a GitHub "
                            "branch, so there is nothing to compare it with.")
        return result
    result["upstream"] = upstream

    log(f"Fetching {upstream} from GitHub ...")
    code, out = _git("fetch", "--quiet", timeout=120)
    if code != 0:
        result["reason"] = "Could not reach GitHub: " + (out.splitlines() or ["no answer"])[-1]
        return result

    _, counts = _git("rev-list", "--left-right", "--count", "HEAD...@{u}")
    ahead, behind = (int(n) for n in counts.split())
    result["ahead"], result["behind"] = ahead, behind
    if behind:
        _, lines = _git("log", "--format=%h  %ad  %s", "--date=short",
                        f"-{LIST_COMMITS}", "HEAD..@{u}")
        result["commits"] = lines.splitlines()
        # Files changed both here (uncommitted) and in what is coming: the
        # ones a pull would refuse to overwrite.
        _, incoming = _git("diff", "--name-only", "HEAD...@{u}")
        _, unstaged = _git("diff", "--name-only")
        _, staged = _git("diff", "--name-only", "--cached")
        edited = set(unstaged.splitlines()) | set(staged.splitlines())
        result["blocked"] = sorted(edited & set(incoming.splitlines()))
    result["ok"] = True
    return result


def update_app(log=print) -> str:
    """Fast-forward to what GitHub has. Raises with git's own words if it cannot."""
    log("git pull --ff-only")
    code, out = _git("pull", "--ff-only", timeout=300)
    for line in out.splitlines():
        log("  " + line)
    if code != 0:
        raise RuntimeError("The update did not go ahead - see the log. Nothing was changed.")
    return out


def _version(text):
    """A comparable version, or None for one that will not parse."""
    try:
        from packaging.version import Version
        return Version(text)
    except Exception:
        return None


def newer(latest: str, installed: str) -> bool:
    """Whether version `latest` is past `installed`; False if either is unknown."""
    a, b = _version(latest), _version(installed)
    if a is not None and b is not None:
        return a > b
    return False


def installed_versions(python_exe: str, packages: list) -> dict:
    """{package: version} for those installed in that interpreter's environment."""
    code = ("import importlib.metadata as m, json, sys\n"
            "out = {}\n"
            "for n in sys.argv[1:]:\n"
            "    try: out[n] = m.version(n)\n"
            "    except m.PackageNotFoundError: pass\n"
            "print(json.dumps(out))")
    proc = subprocess.run([python_exe, "-c", code, *packages], capture_output=True,
                          text=True, timeout=120, creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "no answer").strip().splitlines()[-1])
    return json.loads(proc.stdout.strip().splitlines()[-1])


def latest_on_pypi(name: str, pre: bool) -> str:
    """
    The newest release of a package on PyPI.

    Pre-releases count only when `pre` is set - when what is installed is one
    itself, as RockMapper's alphas are - and withdrawn (yanked) releases never
    do, since pip would not install them either.
    """
    with urllib.request.urlopen(PYPI.format(name=name), timeout=20) as response:
        data = json.load(response)
    best = None
    for text, files in data.get("releases", {}).items():
        if not files or all(f.get("yanked") for f in files):
            continue
        version = _version(text)
        if version is None or (version.is_prerelease and not pre):
            continue
        if best is None or version > best[0]:
            best = (version, text)
    return best[1] if best else data["info"]["version"]


def check_tools(pythons: dict, log=print) -> list:
    """
    Every tool's packages against PyPI.

    `pythons` maps a tool name to its environment's python.exe ("" when the
    tool is not installed). Returns one entry per tool:
    {tool, env, python, reason, packages: [{name, installed, latest, newer}]}.
    """
    results = []
    wanted = {}
    for tool, env, packages in TOOLS:
        entry = {"tool": tool, "env": env, "python": pythons.get(tool, ""),
                 "reason": "", "packages": []}
        results.append(entry)
        if not entry["python"]:
            entry["reason"] = f"not installed (no '{env}' environment found)"
            continue
        log(f"Reading what the '{env}' environment has ...")
        try:
            have = installed_versions(entry["python"], packages)
        except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
            entry["reason"] = f"could not read its environment ({exc})"
            continue
        for name in packages:
            if name in have:
                entry["packages"].append({"name": name, "installed": have[name],
                                          "latest": "", "newer": False})
                version = _version(have[name])
                pre = bool(version is not None and version.is_prerelease)
                wanted[(name, pre)] = None

    log(f"Asking PyPI about {len({n for n, _ in wanted})} package(s) ...")

    def fetch(key):
        try:
            return key, latest_on_pypi(*key)
        except Exception:                          # offline, PyPI down, renamed
            return key, ""
    with ThreadPoolExecutor(max_workers=6) as pool:
        for key, latest in pool.map(fetch, list(wanted)):
            wanted[key] = latest

    for entry in results:
        for pkg in entry["packages"]:
            version = _version(pkg["installed"])
            pre = bool(version is not None and version.is_prerelease)
            pkg["latest"] = wanted.get((pkg["name"], pre)) or ""
            pkg["newer"] = newer(pkg["latest"], pkg["installed"])
    return results


def outdated(tools: list) -> list:
    """The tools with at least one package behind PyPI."""
    return [t for t in tools if any(p["newer"] for p in t["packages"])]


def upgrade_tool(tool: dict, log=print) -> None:
    """pip install -U the out-of-date packages, in that tool's own environment."""
    names = [p["name"] for p in tool["packages"] if p["newer"]]
    if not names:
        return
    cmd = [tool["python"], "-m", "pip", "install", "--upgrade", *names]
    log(f"{tool['tool']}: " + " ".join(cmd))
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PIP_NO_INPUT="1")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            env=env, creationflags=_NO_WINDOW)
    for line in proc.stdout:
        log("  " + line.rstrip())
    if proc.wait() != 0:
        raise RuntimeError(f"pip could not upgrade {tool['tool']} - see the log. "
                           "What was installed before is still there.")


def upgrade_command(tool: dict) -> str:
    """The same upgrade as a line to run by hand."""
    names = [p["name"] for p in tool["packages"] if p["newer"]]
    return f'"{tool["python"]}" -m pip install --upgrade ' + " ".join(names)


def summary(app: dict, tools: list) -> str:
    """Everything that was checked, as the lines a person reads."""
    lines = []
    if not app["ok"]:
        lines.append("AnchorHold: " + app["reason"])
    elif app["behind"]:
        lines.append(f"AnchorHold: {app['behind']} update(s) on {app['upstream']}:")
        lines += ["    " + c for c in app["commits"]]
        if app["behind"] > len(app["commits"]):
            lines.append(f"    ... and {app['behind'] - len(app['commits'])} more")
    else:
        lines.append(f"AnchorHold: up to date with {app['upstream']}.")
    if app.get("ahead"):
        lines.append(f"    ({app['ahead']} local commit(s) not on GitHub)")

    for tool in tools:
        if tool["reason"]:
            lines.append(f"{tool['tool']}: {tool['reason']}.")
            continue
        behind = [p for p in tool["packages"] if p["newer"]]
        state = f"{len(behind)} update(s)" if behind else "up to date"
        lines.append(f"{tool['tool']} ('{tool['env']}' environment): {state}")
        for p in tool["packages"]:
            if p["newer"]:
                note = f"{p['installed']} -> {p['latest']}"
            elif p["latest"]:
                note = f"{p['installed']}, latest"
            else:
                note = f"{p['installed']} (PyPI did not answer)"
            lines.append(f"    {p['name']:<12} {note}")
    return "\n".join(lines)


def find_pythons(candidates: dict = None) -> dict:
    """
    Each tool's environment python, found the way the build finds it.

    The build passes its own candidate lists; run from the command line, they
    are read from add_survey_locations.
    """
    if candidates is None:
        import sys
        sys.path.insert(0, HERE)
        import add_survey_locations as gui
        candidates = {"PINGMapper": gui.PINGMAPPER_PYTHON_CANDIDATES,
                      "RockMapper": gui.ROCKMAPPER_PYTHON_CANDIDATES,
                      "GhostVision": gui.GHOSTVISION_PYTHON_CANDIDATES}
    return {tool: next((p for p in paths if p and os.path.isfile(p)), "")
            for tool, paths in candidates.items()}


if __name__ == "__main__":
    print(summary(check_app(), check_tools(find_pythons())))
