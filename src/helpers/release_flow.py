"""release_flow -- pre-flight and tag plan for a project whose workflow builds and DRAFTS its release.

What this is for: a project (OpenChem Studio) whose `.github/workflows/release.yml` builds on a
clean runner when a `v<version>` tag is pushed, and creates a DRAFT release. The Release Wizard
used to push that tag itself with no look at the state around it; cutting v0.11.0 by hand then
took many round-trips (merge, delete the draft, delete the remote tag, re-tag the tip, push,
wait). This module answers, read-only, "is it safe to tag this tip, and what are the exact
commands?", and can run one planned step on an explicit click.

Four rules, each a defect avoided rather than a preference:

  * **Unknown is never pass.** Every check is `ok | warn | fail | unknown | info`, and the
    verdict is READY only when nothing is `fail`, `warn` or `unknown`. A source that could not be
    read (no `gh`, offline, script missing) is `unknown`, not a tick.
  * **The project's own scripts are the authority** on version / changelog / fragment rules
    (`release-flow.json` names them). This module re-implements none of them.
  * **A plan is a whitelist, not a filter.** Only four command shapes can ever be produced or
    run (create or move a LOCAL tag, push one tag, delete one remote tag, delete one DRAFT
    release). No branch push, no force, no publish, and `step_problems` rejects anything else
    even if a caller builds the step by hand.
  * **The commit is pinned.** Every step names the full SHA of `origin/<branch>` as it was when
    the plan was made, and `run_step` refuses (stale) if the tip moved since.

Nothing here publishes a release. Standard library plus `release_starters`/`gh_ci_status`; no UI.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from typing import Callable

from constants import CREATE_NO_WINDOW
from helpers import release_starters
from helpers.gh_ci_status import FAILED, RUNNING, SUCCESS, classify_run
from helpers.release_profile import RELEASE_WORKFLOW, workflow_drafts

CONFIG_FILE = "release-flow.json"

OK, WARN, FAIL, UNKNOWN, INFO = "ok", "warn", "fail", "unknown", "info"

READY, UNCONFIRMED, BLOCKED = "ready", "unconfirmed", "blocked"

REL_NONE, REL_DRAFT, REL_PUBLISHED, REL_UNKNOWN = "none", "draft", "published", "unknown"


# ── running things ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class RunResult:
    out: str
    err: str
    rc: int

    @property
    def text(self) -> str:
        return (self.out + ("\n" if self.out and self.err else "") + self.err).strip()


Runner = Callable[[list, str], RunResult]


def is_tag_only(path: str) -> bool:
    """Does this project use the tag-only flow: a drafting workflow AND a `release-flow.json`?

    The file is the project's opt-in. Without it the Wizard keeps its older workflow mode, which
    writes the changelog and commits release-prep itself.
    """
    return workflow_drafts(path) and os.path.isfile(os.path.join(path, CONFIG_FILE))


def default_run(argv: list, cwd: str, timeout: float = 90) -> RunResult:
    """Run `argv` with no console window and no credential prompt. Never raises."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=env,
                              creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return RunResult("", f"timed out after {timeout:.0f}s: {argv[0]}", 124)
    except OSError as exc:
        return RunResult("", f"cannot run {argv[0]}: {exc}", 127)
    return RunResult(proc.stdout or "", proc.stderr or "", proc.returncode)


@dataclass
class Env:
    git: str = "git"
    gh: str = "gh"
    python: str = ""
    run: Runner = default_run

    @classmethod
    def from_config(cls, cfg) -> "Env":
        raw = getattr(cfg, "raw", None) or {}
        return cls(git=getattr(cfg, "git_exe", "") or "git", gh=raw.get("gh_exe") or "gh",
                   python=shutil.which("python") or shutil.which("py") or "")

    def call(self, path: str, argv: list) -> RunResult:
        head = {"git": self.git, "gh": self.gh, "python": self.python}.get(argv[0], argv[0])
        if not head:
            return RunResult("", f"{argv[0]} was not found on this machine", 127)
        return self.run([head] + list(argv[1:]), path)


# ── config ───────────────────────────────────────────────────────────────

_KEYS = {"version_file", "version_regex", "default_branch", "tag_pattern", "workflow", "workflow_name",
         "tag_check", "notes_check", "fragments_check", "assets", "checklist", "tag_message"}


@dataclass(frozen=True)
class FlowConfig:
    version_file: str = "pyproject.toml"
    version_regex: str = ""
    default_branch: str = ""
    tag_pattern: str = r"^v(\d+\.\d+\.\d+)$"
    workflow: str = RELEASE_WORKFLOW.replace(os.sep, "/")
    #: The workflow's `name:`; its own runs are not "CI on the tip".
    workflow_name: str = "release"
    tag_check: "tuple[str, ...]" = ()
    notes_check: "tuple[str, ...]" = ()
    fragments_check: "tuple[str, ...]" = ()
    assets: "tuple[str, ...]" = ()
    checklist: str = ""
    tag_message: str = "Release {version}"
    source: str = "defaults"


def _guess(path: str, script: str, *args: str) -> "tuple[str, ...]":
    for folder in ("tools", "scripts"):
        if os.path.isfile(os.path.join(path, folder, script)):
            return ("python", f"{folder}/{script}") + args
    return ()


def load_config(path: str) -> "tuple[FlowConfig, list[str]]":
    """(config, problems). A missing file means defaults, with what exists on disk guessed."""
    defaults = {
        "tag_check": _guess(path, "release_info.py", "check-tag", "{tag}"),
        "notes_check": _guess(path, "release_info.py", "notes", "{tag}"),
        "fragments_check": _guess(path, "changelog.py", "check", "--no-pending"),
    }
    file = os.path.join(path, CONFIG_FILE)
    if not os.path.isfile(file):
        return FlowConfig(**defaults), []
    try:
        with open(file, encoding="utf-8-sig") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        return FlowConfig(**defaults), [f"{CONFIG_FILE} could not be read: {exc}; using defaults"]
    if not isinstance(raw, dict):
        return FlowConfig(**defaults), [f"{CONFIG_FILE} must hold a JSON object; using defaults"]
    problems = [f"{CONFIG_FILE}: unknown key {k!r}" for k in raw if k not in _KEYS]
    values = dict(defaults)
    for key in _KEYS & raw.keys():
        value = raw[key]
        if key in ("tag_check", "notes_check", "fragments_check", "assets"):
            if not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
                problems.append(f"{CONFIG_FILE}: {key} must be a list of strings")
                continue
            values[key] = tuple(value)
        elif isinstance(value, str):
            values[key] = value
        else:
            problems.append(f"{CONFIG_FILE}: {key} must be a string")
    try:
        re.compile(values.get("tag_pattern", FlowConfig.tag_pattern))
    except re.error as exc:
        problems.append(f"{CONFIG_FILE}: tag_pattern is not a valid regex ({exc}); using the default")
        values.pop("tag_pattern", None)
    return FlowConfig(**values, source=CONFIG_FILE), problems


def read_version(path: str, cfg: FlowConfig) -> "tuple[str, str]":
    """(version, problem). Exactly one is non-empty."""
    file = os.path.join(path, *cfg.version_file.split("/"))
    try:
        with open(file, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        return "", f"cannot read {cfg.version_file}: {exc}"
    if cfg.version_regex:
        match = re.search(cfg.version_regex, text)
        return (match.group(1), "") if match and match.groups() else ("", "version_regex found no version")
    try:
        version = tomllib.loads(text)["project"]["version"]
    except (KeyError, ValueError, TypeError):
        return "", f"{cfg.version_file} has no [project] version (set version_regex in {CONFIG_FILE})"
    return (version, "") if isinstance(version, str) and version else ("", "the version is not a string")


# ── pre-flight ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Check:
    id: str
    label: str
    status: str
    detail: str
    fix: str = ""


@dataclass
class State:
    version: str = ""
    tag: str = ""
    branch: str = ""
    default_branch: str = ""
    head: str = ""
    tip: str = ""
    dirty: bool = False
    ahead: int = 0
    behind: int = 0
    local_tag_sha: str = ""
    remote_tag_sha: str = ""
    release: str = REL_UNKNOWN
    release_url: str = ""


@dataclass
class Preflight:
    checks: "list[Check]" = field(default_factory=list)
    state: State = field(default_factory=State)
    problems: "list[str]" = field(default_factory=list)

    @property
    def verdict(self) -> str:
        statuses = {c.status for c in self.checks}
        if FAIL in statuses:
            return BLOCKED
        if statuses & {WARN, UNKNOWN} or not self.checks:
            return UNCONFIRMED
        return READY

    def get(self, check_id: str) -> "Check | None":
        return next((c for c in self.checks if c.id == check_id), None)


def _tail(text: str, n: int = 300) -> str:
    return text.strip()[-n:]


def _brief(text: str, lines: int = 3, width: int = 110) -> str:
    """The first few non-blank lines of a script's output on one line, cut per line, not mid-word."""
    parts = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    shown = [p if len(p) <= width else p[:width - 1] + "…" for p in parts[:lines]]
    more = f" (+{len(parts) - lines} more line(s))" if len(parts) > lines else ""
    return " | ".join(shown) + more


def _json(res: RunResult):
    try:
        return json.loads(res.out or "null")
    except ValueError:
        return None


def _fill(argv: "tuple[str, ...]", **values: str) -> list:
    return [a.format(**values) for a in argv]


def _script_check(env: Env, path: str, cid: str, label: str, argv: "tuple[str, ...]",
                  trusted: bool, **values: str) -> Check:
    """Run one of the PROJECT's own check scripts. Its verdict is the project's, not ours."""
    if not argv:
        return Check(cid, label, UNKNOWN, f"not configured: add it to {CONFIG_FILE}",
                     f"name the command in {CONFIG_FILE}")
    script = next((a for a in argv[1:] if a.endswith(".py")), "")
    if script and not os.path.isfile(os.path.join(path, *script.split("/"))):
        return Check(cid, label, UNKNOWN, f"{script} does not exist, so this was not checked")
    if not trusted:
        return Check(cid, label, UNKNOWN,
                     "your working tree is not the tip being tagged, so a script run here would "
                     "judge different files", "update your checkout to the tip, then refresh")
    res = env.call(path, _fill(argv, **values))
    if res.rc == 0:
        return Check(cid, label, OK, "passed")
    return Check(cid, label, FAIL, _brief(res.text) or f"exit {res.rc}")


def _default_branch(env: Env, path: str, cfg: FlowConfig) -> str:
    if cfg.default_branch:
        return cfg.default_branch
    head = env.call(path, ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if head.rc == 0 and head.out.strip().startswith("origin/"):
        return head.out.strip()[len("origin/"):]
    for name in ("master", "main"):
        if env.call(path, ["git", "rev-parse", "--verify", "-q", f"refs/remotes/origin/{name}"]).rc == 0:
            return name
    return "master"


def _remote_tag(env: Env, path: str, tag: str) -> "tuple[str, str]":
    """(commit sha or '', problem). An annotated tag answers twice; the peeled line is the commit."""
    res = env.call(path, ["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"])
    if res.rc != 0:
        return "", _tail(res.text) or f"git ls-remote exit {res.rc}"
    plain = peeled = ""
    for line in res.out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == f"refs/tags/{tag}^{{}}":
            peeled = sha.strip()
        elif ref.strip() == f"refs/tags/{tag}":
            plain = sha.strip()
    return peeled or plain, ""


def gather(env: Env, path: str, cfg: FlowConfig, tag: str = "") -> Preflight:
    """Read the state around a release and judge it. Read-only apart from `git fetch origin`."""
    pre = Preflight()
    st = pre.state
    add = pre.checks.append

    version, problem = read_version(path, cfg)
    if problem:
        add(Check("version", "Version", FAIL, problem))
        return pre
    st.version = version
    st.tag = tag or f"v{version}"
    if not re.search(cfg.tag_pattern, st.tag):
        add(Check("version", "Version", FAIL, f"{st.tag} does not match the tag pattern {cfg.tag_pattern}"))
        return pre
    add(Check("version", "Version", OK, f"{cfg.version_file} says {version}; tag {st.tag}"))

    st.default_branch = _default_branch(env, path, cfg)
    fetch = env.call(path, ["git", "fetch", "origin"])
    tip = env.call(path, ["git", "rev-parse", "--verify", "-q", f"refs/remotes/origin/{st.default_branch}^{{commit}}"])
    if fetch.rc != 0 or tip.rc != 0:
        add(Check("sync", "Up to date with origin", UNKNOWN,
                  _tail(fetch.text or tip.text) or "could not read origin", "check your network and git login"))
        return pre
    st.tip = tip.out.strip()
    head = env.call(path, ["git", "rev-parse", "HEAD"])
    st.head = head.out.strip() if head.rc == 0 else ""
    branch = env.call(path, ["git", "rev-parse", "--abbrev-ref", "HEAD"])
    st.branch = branch.out.strip() if branch.rc == 0 else ""
    status = env.call(path, ["git", "status", "--porcelain", "--untracked-files=no"])
    st.dirty = status.rc == 0 and bool(status.out.strip())
    counts = env.call(path, ["git", "rev-list", "--left-right", "--count", f"HEAD...refs/remotes/origin/{st.default_branch}"])
    if counts.rc == 0 and len(counts.out.split()) == 2:
        st.ahead, st.behind = (int(n) for n in counts.out.split())

    _git_checks(pre, cfg)
    at_tip = st.head == st.tip and not st.dirty

    add(_script_check(env, path, "tag_rule", "Tag matches the project's version", cfg.tag_check,
                      at_tip, tag=st.tag, version=version))
    add(_script_check(env, path, "notes", "Changelog section for this version", cfg.notes_check,
                      at_tip, tag=st.tag, version=version))
    add(_script_check(env, path, "fragments", "No unfolded changelog fragments", cfg.fragments_check,
                      at_tip, tag=st.tag, version=version))

    _tag_checks(env, path, pre)
    _ci_check(env, path, pre, cfg)
    _workflow_check(path, pre, cfg)
    _pr_info(env, path, pre)
    return pre


def _git_checks(pre: Preflight, cfg: FlowConfig) -> None:
    st, add = pre.state, pre.checks.append
    on = st.branch == st.default_branch
    add(Check("branch", "On the default branch", OK if on else WARN,
              f"on {st.branch or '?'}" + ("" if on else f"; the tag goes on origin/{st.default_branch}, not on this branch"),
              "" if on else f"git switch {st.default_branch}"))
    add(Check("clean", "No uncommitted tracked changes", WARN if st.dirty else OK,
              "uncommitted changes (they are not in the tag)" if st.dirty else "clean"))
    if st.ahead:
        add(Check("sync", "Up to date with origin", FAIL,
                  f"{st.ahead} local commit(s) are not on origin/{st.default_branch}; the tag would leave them out",
                  "push or drop them first"))
    elif st.behind:
        add(Check("sync", "Up to date with origin", WARN,
                  f"your checkout is {st.behind} commit(s) behind; the tag is planned on origin's tip {st.tip[:10]}",
                  f"git pull --ff-only origin {st.default_branch}"))
    else:
        add(Check("sync", "Up to date with origin", OK, f"tip {st.tip[:10]}"))


def _tag_checks(env: Env, path: str, pre: Preflight) -> None:
    st, add = pre.state, pre.checks.append
    local = env.call(path, ["git", "rev-parse", "--verify", "-q", f"refs/tags/{st.tag}^{{commit}}"])
    st.local_tag_sha = local.out.strip() if local.rc == 0 else ""
    st.remote_tag_sha, problem = _remote_tag(env, path, st.tag)
    if problem:
        add(Check("tag_state", "Tag state", UNKNOWN, f"could not ask origin about {st.tag}: {problem}"))
    else:
        add(_tag_state_check(st))

    res = env.call(path, ["gh", "release", "view", st.tag, "--json", "isDraft,url"])
    data = _json(res)
    if res.rc == 0 and isinstance(data, dict):
        st.release = REL_DRAFT if data.get("isDraft") else REL_PUBLISHED
        st.release_url = str(data.get("url") or "")
    elif "not found" in res.text.lower():
        st.release = REL_NONE
    else:
        st.release = REL_UNKNOWN
    if st.release == REL_PUBLISHED:
        add(Check("release_state", "GitHub release", FAIL,
                  f"{st.tag} is already PUBLISHED; this tool will not touch a published release",
                  "decide by hand; users may already have it"))
    elif st.release == REL_DRAFT:
        add(Check("release_state", "GitHub release", WARN,
                  f"a DRAFT for {st.tag} exists; re-tagging means deleting it first", ""))
    elif st.release == REL_NONE:
        add(Check("release_state", "GitHub release", OK, f"no release for {st.tag}"))
    else:
        add(Check("release_state", "GitHub release", UNKNOWN,
                  _tail(res.text) or "gh gave no answer", "run `gh auth status`"))


def _tag_state_check(st: State) -> Check:
    if not st.remote_tag_sha and not st.local_tag_sha:
        return Check("tag_state", "Tag state", OK, f"{st.tag} does not exist yet")
    parts = []
    if st.remote_tag_sha:
        where = "the tip" if st.remote_tag_sha == st.tip else f"{st.remote_tag_sha[:10]}, NOT the tip"
        parts.append(f"on origin at {where}")
    if st.local_tag_sha:
        where = "the tip" if st.local_tag_sha == st.tip else f"{st.local_tag_sha[:10]}, NOT the tip"
        parts.append(f"locally at {where}")
    wrong = ((st.remote_tag_sha and st.remote_tag_sha != st.tip)
             or (st.local_tag_sha and st.local_tag_sha != st.tip))
    return Check("tag_state", "Tag state", WARN if wrong else INFO, f"{st.tag} " + "; ".join(parts))


def _ci_check(env: Env, path: str, pre: Preflight, cfg: FlowConfig) -> None:
    st, add = pre.state, pre.checks.append
    res = env.call(path, ["gh", "run", "list", "--commit", st.tip, "--limit", "50",
                          "--json", "workflowName,status,conclusion,url"])
    runs = _json(res)
    if res.rc != 0 or not isinstance(runs, list):
        add(Check("ci", f"CI on {st.tip[:10]}", UNKNOWN, _tail(res.text) or "could not list runs",
                  "run `gh auth status`"))
        return
    runs = [r for r in runs if isinstance(r, dict) and r.get("workflowName") != cfg.workflow_name]
    states = [classify_run(r) for r in runs]
    if FAILED in states:
        bad = next(r for r, s in zip(runs, states) if s == FAILED)
        add(Check("ci", f"CI on {st.tip[:10]}", FAIL,
                  f"{bad.get('workflowName') or 'a workflow'} failed on the tip", str(bad.get("url") or "")))
    elif RUNNING in states:
        add(Check("ci", f"CI on {st.tip[:10]}", WARN, "still running; wait for it to finish"))
    elif SUCCESS in states:
        add(Check("ci", f"CI on {st.tip[:10]}", OK, f"{states.count(SUCCESS)} run(s) green"))
    else:
        add(Check("ci", f"CI on {st.tip[:10]}", WARN, "no completed CI run found for this commit"))


def _workflow_check(path: str, pre: Preflight, cfg: FlowConfig) -> None:
    file = os.path.join(path, *cfg.workflow.split("/"))
    try:
        with open(file, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        pre.checks.append(Check("workflow", "Release workflow drafts only", UNKNOWN, f"{cfg.workflow} not readable"))
        return
    problems = release_starters.workflow_problems(text)
    pre.checks.append(Check("workflow", "Release workflow drafts only", WARN if problems else OK,
                            "; ".join(problems[:3]) if problems else "draft-only, pinned, no spliced expressions"))


def _pr_info(env: Env, path: str, pre: Preflight) -> None:
    res = env.call(path, ["gh", "pr", "list", "--base", pre.state.default_branch, "--state", "open",
                          "--limit", "20", "--json", "number,title"])
    prs = _json(res)
    if res.rc != 0 or not isinstance(prs, list):
        pre.checks.append(Check("prs", "Open pull requests", UNKNOWN, _tail(res.text) or "could not list"))
        return
    shown = "; ".join(f"#{p.get('number')} {str(p.get('title'))[:50]}" for p in prs[:5] if isinstance(p, dict))
    pre.checks.append(Check("prs", "Open pull requests", INFO,
                            f"{len(prs)} open into {pre.state.default_branch}" + (f": {shown}" if prs else "")))


# ── the plan ─────────────────────────────────────────────────────────────

KIND_DELETE_RELEASE = "delete_release"
KIND_DELETE_REMOTE_TAG = "delete_remote_tag"
KIND_TAG = "tag"
KIND_PUSH_TAG = "push_tag"

#: Steps whose effect leaves this machine or removes something: Run needs the tag typed.
CONFIRM_TYPED = frozenset({KIND_DELETE_RELEASE, KIND_DELETE_REMOTE_TAG, KIND_PUSH_TAG})


@dataclass(frozen=True)
class Step:
    kind: str
    title: str
    argv: "tuple[str, ...]"
    note: str = ""

    @property
    def display(self) -> str:
        return subprocess.list2cmdline(list(self.argv))


@dataclass(frozen=True)
class Plan:
    tag: str
    tip: str
    branch: str
    steps: "tuple[Step, ...]" = ()
    #: Why there are no steps (published release, nothing to do, blocked). Empty when there are.
    message: str = ""


def step_problems(step: Step, tag: str, tip: str) -> "list[str]":
    """Why `step` may not run. Only the four whitelisted shapes pass, whoever built the step."""
    a = list(step.argv)
    ok = {
        KIND_TAG: _tag_shape(a, tag, tip),
        KIND_PUSH_TAG: a == ["git", "push", "origin", f"refs/tags/{tag}"],
        KIND_DELETE_REMOTE_TAG: a == ["git", "push", "origin", f":refs/tags/{tag}"],
        KIND_DELETE_RELEASE: a == ["gh", "release", "delete", tag, "--yes"],
    }.get(step.kind)
    return [] if ok else [f"{step.kind}: {step.display!r} is not one of the commands this tool is allowed to run"]


def _tag_shape(a: list, tag: str, tip: str) -> bool:
    force = a[2:3] == ["-f"]
    rest = a[3:] if force else a[2:]
    return (a[:2] == ["git", "tag"] and len(rest) == 5 and rest[0] == "-a" and rest[1] == tag
            and rest[2] == tip and rest[3] == "-m" and bool(rest[4]))


def make_plan(pre: Preflight, cfg: FlowConfig) -> Plan:
    """The exact commands for the tip, or the reason there are none."""
    st = pre.state

    def plan(steps=(), message=""):
        return Plan(st.tag, st.tip, st.default_branch, tuple(steps), message)

    if not st.tip or not st.tag:
        return plan(message="Not enough is known to plan: the pre-flight could not read the version or origin.")
    if st.release == REL_PUBLISHED:
        return plan(message=f"{st.tag} is already published. Nothing here will modify a published release.")
    if pre.verdict == BLOCKED:
        failed = ", ".join(c.label for c in pre.checks if c.status == FAIL)
        return plan(message=f"Blocked by: {failed}. Fix that and refresh; no commands are offered meanwhile.")
    if st.release == REL_UNKNOWN:
        return plan(message="Could not tell whether a release exists for this tag (is `gh` signed in?). "
                            "Planning without that could delete the wrong thing.")
    message = cfg.tag_message.format(version=st.version, tag=st.tag)
    steps: "list[Step]" = []
    if st.release == REL_DRAFT:
        steps.append(Step(KIND_DELETE_RELEASE, "Delete the DRAFT release",
                          ("gh", "release", "delete", st.tag, "--yes"),
                          "A draft only. The tag stays until the next step."))
    if st.remote_tag_sha:
        steps.append(Step(KIND_DELETE_REMOTE_TAG, "Delete the tag on origin",
                          ("git", "push", "origin", f":refs/tags/{st.tag}"),
                          "Removes only this tag from the remote; no branch is touched."))
    if st.local_tag_sha != st.tip or st.remote_tag_sha:
        steps.append(Step(KIND_TAG, "Tag the tip locally",
                          ("git", "tag") + (("-f",) if st.local_tag_sha else ())
                          + ("-a", st.tag, st.tip, "-m", message),
                          "Local only." + (" -f moves the existing local tag." if st.local_tag_sha else "")))
    steps.append(Step(KIND_PUSH_TAG, "Push the tag (starts the release build)",
                      ("git", "push", "origin", f"refs/tags/{st.tag}"),
                      "The workflow builds on a clean runner and creates a DRAFT. Nothing is published."))
    return plan(steps)


# ── running one step ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class StepResult:
    ok: bool
    text: str
    #: Set when the step was REFUSED before running anything.
    refused: str = ""


def run_step(env: Env, path: str, plan: Plan, step: Step, typed: str = "") -> StepResult:
    """Run one planned step. Refuses (and runs nothing) if the shape is not allowed, the tip moved
    since the plan was made, the tag was not typed for a step that needs it, or the release being
    deleted has since been published."""
    if step not in plan.steps:
        return StepResult(False, "", "that step is not part of the current plan")
    problems = step_problems(step, plan.tag, plan.tip)
    if problems:
        return StepResult(False, "", problems[0])
    if step.kind in CONFIRM_TYPED and typed.strip() != plan.tag:
        return StepResult(False, "", f"type {plan.tag} to confirm")
    fetch = env.call(path, ["git", "fetch", "origin"])
    tip = env.call(path, ["git", "rev-parse", "--verify", "-q", f"refs/remotes/origin/{plan.branch}^{{commit}}"])
    if fetch.rc != 0 or tip.rc != 0:
        return StepResult(False, "", "could not re-read origin to confirm the tip; nothing was run")
    if tip.out.strip() != plan.tip:
        return StepResult(False, "", f"origin/{plan.branch} moved to {tip.out.strip()[:10]} since this plan "
                                     f"({plan.tip[:10]}); refresh to plan again")
    if step.kind == KIND_DELETE_RELEASE:
        view = env.call(path, ["gh", "release", "view", plan.tag, "--json", "isDraft"])
        data = _json(view)
        if view.rc != 0 or not isinstance(data, dict) or data.get("isDraft") is not True:
            return StepResult(False, "", f"{plan.tag} is not confirmed to be a draft any more; refusing to delete it")
    res = env.call(path, list(step.argv))
    return StepResult(res.rc == 0, _tail(res.text, 600) or ("done" if res.rc == 0 else f"exit {res.rc}"))
