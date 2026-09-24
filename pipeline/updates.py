#!/usr/bin/env python3
"""
Is there a newer AnchorHold, or a newer PINGMapper, than the one on this PC?

AnchorHold is a git checkout of its GitHub repository, so "an update" is new
commits on the branch it tracks. Checking fetches them and says what they
are; updating is a fast-forward pull and nothing stronger - git refuses a
pull that would touch a file edited here, so local changes are never lost to
it, and a checkout that has commits of its own is told so rather than merged.

PINGMapper is only reported: installed against the latest on PyPI. Upgrading
it is left to the person, because a new release can change the behaviour the
pipeline works around (see depth_csv_from_sonar.py) and deserves a look first.

    python pipeline/updates.py            # check, print what was found
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPO_PAGE = "https://github.com/maxschwartziv/anchorhold-web-viewer"
PYPI_PINGMAPPER = "https://pypi.org/pypi/pingmapper/json"

# At most this many new commits are listed; the rest are counted.
LIST_COMMITS = 15


def _git(*args, timeout=60):
    """Run git in the repository. Returns (exit code, stdout+stderr stripped)."""
    proc = subprocess.run(["git", "-C", ROOT, *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
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


def check_pingmapper(python_exe: str, log=print) -> dict:
    """Installed PINGMapper against the newest release on PyPI."""
    result = {"installed": "", "latest": "", "reason": ""}
    if python_exe:
        try:
            proc = subprocess.run(
                [python_exe, "-c",
                 "import importlib.metadata as m; print(m.version('pingmapper'))"],
                capture_output=True, text=True, timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            result["installed"] = proc.stdout.strip() if proc.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            pass
    log("Asking PyPI for the latest PINGMapper ...")
    try:
        with urllib.request.urlopen(PYPI_PINGMAPPER, timeout=20) as response:
            result["latest"] = json.load(response)["info"]["version"]
    except Exception as exc:                       # offline, PyPI down, ...
        result["reason"] = f"Could not reach PyPI ({exc.__class__.__name__})."
    return result


def newer(latest: str, installed: str) -> bool:
    """Whether dotted version `latest` is past `installed`; False if either is unknown."""
    def parts(v):
        out = []
        for piece in v.split("."):
            digits = "".join(ch for ch in piece if ch.isdigit())
            out.append(int(digits) if digits else 0)
        return out
    return bool(latest and installed) and parts(latest) > parts(installed)


def summary(app: dict, ping: dict) -> str:
    """Both checks, as the lines a person reads."""
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

    if ping["installed"] and ping["latest"]:
        state = ("a newer version is out" if newer(ping["latest"], ping["installed"])
                 else "up to date")
        lines.append(f"PINGMapper: {ping['installed']} installed, "
                     f"{ping['latest']} latest - {state}.")
    elif ping["latest"]:
        lines.append(f"PINGMapper: latest is {ping['latest']}; "
                     "no installed copy was found to compare.")
    else:
        lines.append("PINGMapper: " + (ping["reason"] or "could not be checked."))
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, HERE)
    try:
        from add_survey_locations import find_pingmapper_python
        python = find_pingmapper_python()
    except Exception:
        python = ""
    print(summary(check_app(), check_pingmapper(python)))
