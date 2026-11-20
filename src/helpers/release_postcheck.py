"""release_postcheck -- what to verify AFTER the release workflow has drafted a release.

Two kinds of evidence, kept apart because they are different claims:

  * **Machine checks** (`verify_draft`): the workflow run for the tag, that the release is still
    a DRAFT, that its tag is the planned commit, that every expected asset is attached with a
    non-zero size, and that `SHA256SUMS.txt` names them (and, when GitHub reports a digest,
    that the hash agrees). Same three-valued statuses as `release_flow`: unreachable is
    `unknown`, never a tick.
  * **Manual checklist** (`load_checklist`, ticks in `load_ticks`/`save_ticks`): what a person
    must try on the downloaded installer. The pipeline runs the app; it does not draw a
    molecule. The list is the PROJECT's (`checklist` in release-flow.json); the built-in list
    is only the fallback for a project that has none.

The ticks are stored per tag under the repository's git common dir, so they are shared by every
worktree of one clone and git can never track them. Nothing here publishes a release.
"""

from __future__ import annotations

import json
import os
import re
import tempfile

from helpers.release_flow import (
    FAIL, INFO, OK, UNKNOWN, WARN, Check, Env, FlowConfig, Preflight, _json, _remote_tag, _tail,
)

#: Used only when the project names no checklist file.
DEFAULT_CHECKLIST = (
    "Downloaded the installer from the DRAFT release, not from a local build",
    "Install folder page is always shown, and Browse > Make New Folder works",
    "Installed per-user, and again for all users",
    "Documentation and the changelog are installed",
    "Start menu entries: the program, Uninstall, the documentation, the changelog",
    "Running Setup again asks repair / uninstall / cancel",
    "Uninstall asks about settings and data, and keeps saved project files",
    "Double-clicking a project file opens it in the app",
    "Save and Save As work",
    "Windows Defender Controlled Folder Access: if it is on, saving to Documents is blocked "
    "until the app is allowed; check the message tells the person what to do",
    "Core features work: the editor draws, the viewers render, Help opens and F1 works",
)

_ITEM = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s+(.+?)\s*$")


def expected_assets(cfg: FlowConfig, version: str, tag: str) -> "list[str]":
    return [a.format(version=version, tag=tag) for a in cfg.assets]


def load_checklist(path: str, cfg: FlowConfig) -> "tuple[list[str], str]":
    """(items, source). The project's checklist when it has one, else the built-in fallback."""
    if cfg.checklist:
        try:
            with open(os.path.join(path, *cfg.checklist.split("/")), encoding="utf-8") as handle:
                items = [m.group(1) for m in map(_ITEM.match, handle.read().splitlines()) if m]
        except OSError:
            items = []
        if items:
            return items, cfg.checklist
    return list(DEFAULT_CHECKLIST), "built-in checklist"


# ── ticks ────────────────────────────────────────────────────────────────

def _ticks_file(env: Env, path: str, tag: str) -> str:
    res = env.call(path, ["git", "rev-parse", "--git-common-dir"])
    if res.rc != 0 or not res.out.strip():
        return ""
    common = res.out.strip()
    if not os.path.isabs(common):
        common = os.path.join(path, common)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", tag)
    return os.path.join(os.path.normpath(common), "tokensave", f"release-{safe}.json")


def load_ticks(env: Env, path: str, tag: str) -> "set[str]":
    file = _ticks_file(env, path, tag)
    try:
        with open(file, encoding="utf-8") as handle:
            data = json.load(handle)
        return {t for t in data.get("ticked", []) if isinstance(t, str)}
    except (OSError, ValueError, AttributeError, TypeError):
        return set()


def save_ticks(env: Env, path: str, tag: str, ticked: "set[str]") -> str:
    """'' on success, else why not. Atomic: a crash never leaves half a file."""
    file = _ticks_file(env, path, tag)
    if not file:
        return "not a git repository, so the ticks cannot be kept"
    try:
        os.makedirs(os.path.dirname(file), exist_ok=True)
        tmp = file + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"tag": tag, "ticked": sorted(ticked)}, handle, indent=1)
        os.replace(tmp, file)
    except OSError as exc:
        return str(exc)
    return ""


# ── machine checks ───────────────────────────────────────────────────────

def _sums(text: str) -> "dict[str, str]":
    """`sha256sum` output: '<hash> <*| >name' per line -> {name: hash}."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
        if m:
            out[m.group(2)] = m.group(1).lower()
    return out


def verify_draft(env: Env, path: str, cfg: FlowConfig, pre: Preflight, expected_sha: str) -> "list[Check]":
    """Judge the drafted release for `pre.state.tag`. Read-only (downloads SHA256SUMS to a temp dir)."""
    st = pre.state
    tag = st.tag
    checks: "list[Check]" = []

    wf = cfg.workflow.rsplit("/", 1)[-1]
    res = env.call(path, ["gh", "run", "list", "--workflow", wf, "--commit", expected_sha, "--limit", "5",
                          "--json", "status,conclusion,url,event"])
    runs = _json(res)
    if res.rc != 0 or not isinstance(runs, list):
        checks.append(Check("run", "Release workflow run", UNKNOWN, _tail(res.text) or "could not list runs"))
    else:
        tag_runs = [r for r in runs if isinstance(r, dict) and r.get("event") == "push"] or \
                   [r for r in runs if isinstance(r, dict)]
        if not tag_runs:
            checks.append(Check("run", "Release workflow run", WARN, "no run for this commit yet; the tag push may not have started one"))
        else:
            run = tag_runs[0]
            if run.get("status") != "completed":
                checks.append(Check("run", "Release workflow run", WARN, "still building; check again later", str(run.get("url") or "")))
            elif run.get("conclusion") == "success":
                checks.append(Check("run", "Release workflow run", OK, "build and tests passed", str(run.get("url") or "")))
            else:
                checks.append(Check("run", "Release workflow run", FAIL, f"finished {run.get('conclusion')}", str(run.get("url") or "")))

    view = env.call(path, ["gh", "release", "view", tag, "--json", "isDraft,url,assets,body"])
    data = _json(view)
    if view.rc != 0 or not isinstance(data, dict):
        gone = "not found" in view.text.lower()
        checks.append(Check("draft", "Draft release", WARN if gone else UNKNOWN,
                            "no release for this tag yet" if gone else (_tail(view.text) or "could not read it")))
        return checks
    if data.get("isDraft") is True:
        checks.append(Check("draft", "Draft release", OK, "exists and is still a draft", str(data.get("url") or "")))
    else:
        checks.append(Check("draft", "Draft release", INFO, "already published", str(data.get("url") or "")))
    checks.append(Check("notes", "Release notes", OK if str(data.get("body") or "").strip() else FAIL,
                        "present" if str(data.get("body") or "").strip() else "the notes are empty"))

    remote, problem = _remote_sha(env, path, tag)
    if problem:
        checks.append(Check("tag_sha", "Tag points at the planned commit", UNKNOWN, problem))
    else:
        checks.append(Check("tag_sha", "Tag points at the planned commit",
                            OK if remote == expected_sha else FAIL,
                            f"{tag} is at {remote[:10]}" + ("" if remote == expected_sha else f", planned {expected_sha[:10]}")))

    checks += _asset_checks(env, path, cfg, st.version, tag, data)
    return checks


def _remote_sha(env: Env, path: str, tag: str) -> "tuple[str, str]":
    sha, problem = _remote_tag(env, path, tag)
    return (sha, problem) if sha or problem else ("", f"{tag} is not on origin")


def _asset_checks(env: Env, path: str, cfg: FlowConfig, version: str, tag: str, data: dict) -> "list[Check]":
    wanted = expected_assets(cfg, version, tag)
    if not wanted:
        return [Check("assets", "Release files", WARN, f"no assets are listed in {cfg.source}, so none were checked")]
    have = {str(a.get("name")): a for a in data.get("assets") or [] if isinstance(a, dict)}
    checks: "list[Check]" = []
    missing = [n for n in wanted if n not in have]
    empty = [n for n in wanted if n in have and not have[n].get("size")]
    if missing or empty:
        detail = (f"missing: {', '.join(missing)}. " if missing else "") + (f"empty: {', '.join(empty)}" if empty else "")
        checks.append(Check("assets", "Release files", FAIL, detail.strip()))
    else:
        checks.append(Check("assets", "Release files", OK, f"{len(wanted)} attached: " + ", ".join(wanted)))
    sums_name = next((n for n in wanted if n.lower().startswith("sha256sums")), "")
    if sums_name and sums_name in have:
        checks.append(_sums_check(env, path, tag, sums_name, [n for n in wanted if n != sums_name], have))
    return checks


def _sums_check(env: Env, path: str, tag: str, sums_name: str, others: "list[str]", have: dict) -> Check:
    with tempfile.TemporaryDirectory(prefix="release-sums-") as tmp:
        res = env.call(path, ["gh", "release", "download", tag, "--pattern", sums_name, "--dir", tmp, "--clobber"])
        try:
            with open(os.path.join(tmp, sums_name), encoding="utf-8") as handle:
                listed = _sums(handle.read())
        except OSError:
            return Check("sums", sums_name, UNKNOWN, _tail(res.text) or "could not download it")
    absent = [n for n in others if n not in listed]
    if absent:
        return Check("sums", sums_name, FAIL, f"does not list: {', '.join(absent)}")
    wrong = []
    compared = 0
    for name in others:
        digest = str(have.get(name, {}).get("digest") or "")
        if digest.startswith("sha256:"):
            compared += 1
            if digest[7:].lower() != listed[name]:
                wrong.append(name)
    if wrong:
        return Check("sums", sums_name, FAIL, f"hash differs from what GitHub holds for: {', '.join(wrong)}")
    note = f"lists every file; hashes agree with GitHub's digest for {compared}" if compared else \
           "lists every file (GitHub gave no digest, so the hashes themselves were not compared)"
    return Check("sums", sums_name, OK if compared else WARN, note)

