"""cli_release_command.py - the `release-guide` command.

The Release Wizard's pre-flight, tag plan and post-build check, as one JSON envelope, so an
agent or a script can ask "is it safe to tag this tip, and what exactly would be run?" without
the GUI. It is READ-ONLY by construction: it reports the commands the plan would offer and runs
none of them (running a step is the Wizard's Run button, behind a typed confirmation). The only
thing it touches is `git fetch origin`, which refreshes remote-tracking refs.

    manager-cli release-guide --project P --stage preflight   # the checks and the verdict
    manager-cli release-guide --project P --stage plan        # + the exact commands
    manager-cli release-guide --project P --stage post        # + the drafted release's files

Exit codes follow the verdict, so a caller can branch on it without parsing: 0 ready, 1 blocked
(a check FAILED), 4 not confirmed (something is a warning or could not be read - which is not a
pass). `post` uses the same rule over its own checks.

Split out of `cli.py` like `cli_test_commands`: `_load_manager_config` is imported from `cli` at
call time because the suite patches it by module path.
"""
from __future__ import annotations

import dataclasses
import types

from cli_support import (
    EXIT_FAILED, EXIT_OK, EXIT_VERIFY_FAILED, Result, _Prerequisite, _resolve_project,
)

STAGES = ("preflight", "plan", "post")


def _env_for(config_path: str):
    from cli import _load_manager_config
    from helpers import release_flow as rfl
    try:
        raw = _load_manager_config(config_path) or {}
    except _Prerequisite:
        if config_path:
            raise
        raw = {}      # no Manager config beside this exe: git and gh are looked up on PATH
    return rfl.Env.from_config(types.SimpleNamespace(git_exe=raw.get("git_exe", ""), raw=raw))


def _code_for(statuses) -> int:
    from helpers import release_flow as rfl
    statuses = set(statuses)
    if rfl.FAIL in statuses:
        return EXIT_FAILED
    if statuses & {rfl.WARN, rfl.UNKNOWN}:
        return EXIT_VERIFY_FAILED
    return EXIT_OK


def _check(c) -> dict:
    return dataclasses.asdict(c)


def _plan(plan) -> dict:
    from helpers import release_flow as rfl
    return {
        "tag": plan.tag, "tip": plan.tip, "branch": plan.branch, "message": plan.message,
        "steps": [{"kind": s.kind, "title": s.title, "command": s.display, "argv": list(s.argv),
                   "note": s.note,
                   "needs_typed_confirmation": s.kind in rfl.CONFIRM_TYPED} for s in plan.steps],
    }


def _cmd_release_guide(args) -> Result:
    """Pre-flight / plan / post-build report for one project. Never runs a planned step."""
    from helpers import release_flow as rfl
    from helpers import release_postcheck as rpc
    from helpers import release_profile as rprof
    project = _resolve_project(args.project)
    env = _env_for(args.config)
    cfg, problems = rfl.load_config(project)
    pre = rfl.gather(env, project, cfg, args.tag or "")
    profile = rprof.detect(project)
    data = {
        "stage": args.stage,
        "profile": {"build_systems": list(profile.build_systems), "installers": list(profile.installers),
                    "release": profile.release, "label": profile.label(),
                    "evidence": [dataclasses.asdict(e) for e in profile.evidence]},
        "config": {"source": cfg.source, "problems": problems},
        "state": dataclasses.asdict(pre.state),
        "verdict": pre.verdict,
        "checks": [_check(c) for c in pre.checks],
    }
    statuses = [c.status for c in pre.checks]
    if args.stage in ("plan", "post"):
        data["plan"] = _plan(rfl.make_plan(pre, cfg))
    if args.stage == "post":
        if not pre.state.tip:
            raise _Prerequisite("cannot check the draft: the pre-flight could not read origin's tip")
        post = rpc.verify_draft(env, project, cfg, pre, pre.state.tip)
        data["post"] = {"checks": [_check(c) for c in post]}
        statuses = [c.status for c in post]
    code = _code_for(statuses)
    word = {EXIT_OK: "ready", EXIT_FAILED: "blocked", EXIT_VERIFY_FAILED: "not confirmed"}[code]
    return Result(code, data, warnings=list(problems),
                  human=f"release-guide {args.stage} for {pre.state.tag or '?'}: {word}")
