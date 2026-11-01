"""The limits on delegated tasks (docs/ORCHESTRATION.md section 9). Pure.

A limit refuses; it never queues. A launch that is over one says WHICH limit and
what to do, because "could not start" with no reason is the same defect as a red
row with none. Defaults are conservative on purpose and every one can be changed
with the optional `orchestration_limits` config dict.
"""
import dataclasses

GIB = 1024 ** 3


@dataclasses.dataclass(frozen=True)
class Limits:
    max_running_total: int = 2
    max_running_per_project: int = 1
    max_open_worktrees: int = 6
    min_free_bytes: int = 2 * GIB
    wall_clock_seconds: int = 30 * 60
    max_output_bytes: int = 1024 * 1024


KEYS = tuple(f.name for f in dataclasses.fields(Limits))


def from_config(raw: "dict | None") -> "tuple[Limits, list]":
    """`(limits, problems)`. A bad value is reported and its default kept.

    Never coerced silently: a typo that quietly became "unlimited" would remove
    the limit the person thought they had set.
    """
    base = Limits()
    given = raw if isinstance(raw, dict) else {}
    values, problems = {}, []
    if raw is not None and not isinstance(raw, dict):
        problems.append("orchestration_limits must be an object; defaults used")
    for key, value in given.items():
        if key not in KEYS:
            problems.append(f"orchestration_limits has an unknown key {key!r}")
        elif isinstance(value, bool) or not isinstance(value, int) or value < 1:
            problems.append(f"orchestration_limits.{key} must be a whole number "
                            f"of at least 1; the default was kept")
        else:
            values[key] = value
    return dataclasses.replace(base, **values), problems


def check_launch(limits: Limits, *, running_here: int, running_total: int,
                 open_worktrees: int, free_bytes: "int | None") -> list:
    """Reasons a launch is refused. Empty means go.

    `free_bytes` None is "could not measure", which is NOT enough room: a launch
    that cannot tell whether the disk has space is refused, not assumed fine.
    """
    why = []
    if running_here >= limits.max_running_per_project:
        why.append(f"this project already has {running_here} running task(s) "
                   f"(limit {limits.max_running_per_project}); wait for it or cancel it")
    if running_total >= limits.max_running_total:
        why.append(f"{running_total} task(s) are running across all projects "
                   f"(limit {limits.max_running_total})")
    if open_worktrees >= limits.max_open_worktrees:
        why.append(f"{open_worktrees} task worktrees are still open "
                   f"(limit {limits.max_open_worktrees}); accept, reject or "
                   f"discard one first")
    if free_bytes is None:
        why.append("could not measure free disk space on the worktree drive")
    elif free_bytes < limits.min_free_bytes:
        why.append(f"only {free_bytes / GIB:.1f} GB free on the worktree drive "
                   f"(need {limits.min_free_bytes / GIB:.1f} GB)")
    return why
