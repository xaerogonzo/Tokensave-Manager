"""config_home -- where the Manager's settings live, and moving them there.

THE PROBLEM. `manager-config.json` used to sit beside the executable. That put the one file a
person owns inside the folder a release replaces: the zip shipped a CLEAN config, so unzipping
over an old folder overwrote the user's real settings, and a versioned folder per release (the
fix for two versions' files mixing) starts every upgrade with none. It also meant an installer
could not upgrade by clearing the program folder without deleting settings.

THE RULE. A packaged (frozen) Manager keeps its config and logs in a per-user folder,
`%LOCALAPPDATA%\\TokenSaveManager\\`. A source checkout is unchanged: its config stays in the
repository root, because that is where a developer, the launcher `.bat` and every existing
habit look. `TOKENSAVE_MANAGER_CONFIG` (one explicit file) and `TOKENSAVE_MANAGER_HOME` (a
different home folder, used to run a second instance in isolation and to test this) override
it, in that order.

THE MIGRATION. The first packaged run with no config in the home looks for the old one and
COPIES it: beside the executable first (the install being run), then in sibling folders named
like a release (`Tokensave-Manager-v2.6.0-windows`), newest version first. Properties that make
it safe to run unattended, each held by a test:

  * it never overwrites a config that is already there, and never deletes the old file;
  * it copies only a file that parses as a JSON object, and verifies the copy by hash;
  * the new file appears atomically (a temp file, then a rename that fails if the target exists),
    so a crash cannot leave a half-written config where the real one belongs;
  * a record, `config-migration.json`, says where it came from, so the choice can be audited;
  * a file it cannot use is REPORTED as unreadable, never treated as "no settings".

Read-only consumers (the CLI, the MCP wrapper) must not write into a user's data folder as a side
effect of being asked a question, so they use `read_path`, which reads the home config and,
only until the Manager has migrated, falls back to the old file in place.

Standard library only and no import of anything else in this project: `constants` imports it at
module scope and the wrapper imports it before it has read any config.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

HOME_ENV = "TOKENSAVE_MANAGER_HOME"
CONFIG_ENV = "TOKENSAVE_MANAGER_CONFIG"
CONFIG_NAME = "manager-config.json"
APP_DIR = "TokenSaveManager"
RECORD_NAME = "config-migration.json"

#: A release folder as `make_release_zip` names it.
_RELEASE_DIR = re.compile(r"^Tokensave-Manager-v(\d+(?:\.\d+)*)-windows$", re.IGNORECASE)

KIND_BESIDE = "beside the executable"
KIND_SIBLING = "a previous release folder"

STATE_EXISTS = "exists"          # the home already has a config; nothing to do
STATE_FRESH = "fresh"            # no config anywhere: first run
STATE_IMPORT = "import"          # a usable old config was found
STATE_UNREADABLE = "unreadable"  # old config(s) exist but none can be used
STATE_IMPORTED = "imported"      # done
STATE_FAILED = "failed"          # an import was attempted and did not complete


# ── where things live ─────────────────────────────────────────────────────

def user_data_dir(env) -> str:
    """`%LOCALAPPDATA%\\TokenSaveManager`, with the usual fallback when the variable is absent."""
    base = (env.get("LOCALAPPDATA") or "").strip()
    if not base:
        base = os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, APP_DIR)


def default_home(env, frozen: bool, base_dir: str) -> str:
    """The home this INSTALL has, whatever the process was told: the packaged data folder, or
    the repository root of a checkout."""
    return user_data_dir(env) if frozen else base_dir


def home_dir(env, frozen: bool, base_dir: str) -> str:
    override = (env.get(HOME_ENV) or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return default_home(env, frozen, base_dir)


def config_path(env, frozen: bool, base_dir: str) -> str:
    """The config file this process uses: one explicit file, else the home's."""
    explicit = (env.get(CONFIG_ENV) or "").strip()
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    return os.path.join(home_dir(env, frozen, base_dir), CONFIG_NAME)


def default_config_path(env, frozen: bool, base_dir: str) -> str:
    """The install's own config path: what an isolated run is told apart from."""
    return os.path.join(default_home(env, frozen, base_dir), CONFIG_NAME)


# ── what an older installation left ───────────────────────────────────────

@dataclass(frozen=True)
class Candidate:
    path: str
    kind: str
    #: The release version in the folder's name, newest sorts first; () for "beside".
    version: tuple = ()


def legacy_candidates(base_dir: str) -> "list[Candidate]":
    """Old configs, best first: beside the executable, then sibling release folders newest first."""
    found = []
    beside = os.path.join(base_dir, CONFIG_NAME)
    if os.path.isfile(beside):
        found.append(Candidate(beside, KIND_BESIDE))
    parent = os.path.dirname(os.path.abspath(base_dir))
    here = os.path.normcase(os.path.abspath(base_dir))
    siblings = []
    try:
        entries = sorted(os.listdir(parent))
    except OSError:
        entries = []
    for name in entries:
        match = _RELEASE_DIR.match(name)
        full = os.path.join(parent, name)
        if not match or os.path.normcase(os.path.abspath(full)) == here:
            continue
        config = os.path.join(full, CONFIG_NAME)
        if os.path.isfile(config):
            siblings.append(Candidate(config, KIND_SIBLING, tuple(int(p) for p in match.group(1).split("."))))
    siblings.sort(key=lambda c: c.version, reverse=True)
    return found + siblings


def _read_object(path: str) -> "tuple[bytes | None, str]":
    """`(bytes, "")` for a file that parses as a JSON object, else `(None, why)`."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
        parsed = json.loads(raw.decode("utf-8-sig"))
    except OSError as exc:
        return None, "cannot be read: %s" % exc
    except ValueError as exc:
        return None, "is not valid JSON: %s" % exc
    if not isinstance(parsed, dict):
        return None, "is not a JSON object"
    return raw, ""


# ── the plan, kept apart from the act ─────────────────────────────────────

@dataclass(frozen=True)
class Plan:
    state: str
    config_path: str
    source: "Candidate | None" = None
    #: Other old configs that were found and not used.
    others: tuple = ()
    #: Old configs that were skipped as unusable, as (path, why).
    skipped: tuple = ()
    reason: str = ""


def plan_migration(target: str, base_dir: str) -> Plan:
    """What would happen, without doing it. Pure apart from reading the filesystem."""
    if os.path.isfile(target):
        return Plan(STATE_EXISTS, target)
    skipped, usable = [], []
    for candidate in legacy_candidates(base_dir):
        raw, why = _read_object(candidate.path)
        (usable if raw is not None else skipped).append(candidate if raw is not None else (candidate.path, why))
    if usable:
        return Plan(STATE_IMPORT, target, usable[0], tuple(usable[1:]), tuple(skipped))
    if skipped:
        first = skipped[0]
        return Plan(STATE_UNREADABLE, target, None, (), tuple(skipped),
                    "%s %s" % (first[0], first[1]))
    return Plan(STATE_FRESH, target)


@dataclass(frozen=True)
class Result:
    state: str
    config_path: str
    source: str = ""
    sha256: str = ""
    reason: str = ""
    others: tuple = field(default_factory=tuple)
    skipped: tuple = field(default_factory=tuple)

    @property
    def imported(self) -> bool:
        return self.state == STATE_IMPORTED


def apply_plan(plan: Plan, now: "datetime | None" = None) -> Result:
    """Do an `import` plan. Anything else is returned as it stands, with nothing written.

    Copy to a temp file beside the target, verify it by hash against the source, then rename it
    into place. `os.rename` is used rather than `os.replace` on purpose: where the target exists
    it FAILS, which is the exclusive create wanted here (a config that appeared meanwhile wins).
    """
    base = Result(plan.state, plan.config_path, others=tuple(c.path for c in plan.others), skipped=plan.skipped,
                  reason=plan.reason)
    if plan.state != STATE_IMPORT or plan.source is None:
        return base
    raw, why = _read_object(plan.source.path)
    if raw is None:
        return Result(STATE_FAILED, plan.config_path, plan.source.path, reason="%s %s" % (plan.source.path, why),
                      others=base.others, skipped=base.skipped)
    digest = hashlib.sha256(raw).hexdigest()
    folder = os.path.dirname(plan.config_path)
    temporary = ""
    try:
        os.makedirs(folder, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=folder, prefix=CONFIG_NAME + ".", suffix=".tmp")
        with os.fdopen(handle, "wb") as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        with open(temporary, "rb") as check:
            if hashlib.sha256(check.read()).hexdigest() != digest:
                raise OSError("the copy does not match the original")
        if os.path.exists(plan.config_path):
            return Result(STATE_EXISTS, plan.config_path, others=base.others, skipped=base.skipped)
        os.rename(temporary, plan.config_path)
        temporary = ""
    except FileExistsError:
        return Result(STATE_EXISTS, plan.config_path, others=base.others, skipped=base.skipped)
    except OSError as exc:
        return Result(STATE_FAILED, plan.config_path, plan.source.path, digest, "could not copy: %s" % exc,
                      base.others, base.skipped)
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    result = Result(STATE_IMPORTED, plan.config_path, plan.source.path, digest, plan.source.kind,
                    base.others, base.skipped)
    _write_record(result, now)
    return result


def _write_record(result: Result, now: "datetime | None") -> None:
    """Best effort: the record explains the copy, it is not part of it."""
    record = {
        "imported_from": result.source,
        "sha256": result.sha256,
        "at": (now or datetime.now(timezone.utc)).isoformat(),
        "also_found_not_used": list(result.others),
        "skipped_unusable": [list(item) for item in result.skipped],
        "note": "The original was copied, not moved, and was left where it was.",
    }
    try:
        path = os.path.join(os.path.dirname(result.config_path), RECORD_NAME)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(record, handle, indent=2)
    except OSError:
        pass


def should_migrate(env, frozen: bool) -> bool:
    """Only a packaged run with no explicit config file. A checkout's config already is its home."""
    return frozen and not (env.get(CONFIG_ENV) or "").strip()


def migrate_at_startup(env, frozen: bool, base_dir: str) -> "Result | None":
    """Plan and apply, for the Manager's own startup. None when this is not a run that migrates."""
    if not should_migrate(env, frozen):
        return None
    target = config_path(env, frozen, base_dir)
    return apply_plan(plan_migration(target, base_dir))


def read_path(env, frozen: bool, base_dir: str) -> str:
    """The config a READ-ONLY consumer should use, without writing anything.

    The home's config when there is one. Only in a packaged run that has not migrated yet, the old
    file in place (so the CLI and the wrapper keep working until the Manager has run once).
    """
    target = config_path(env, frozen, base_dir)
    if os.path.isfile(target) or not should_migrate(env, frozen):
        return target
    for candidate in legacy_candidates(base_dir):
        raw, _ = _read_object(candidate.path)
        if raw is not None:
            return candidate.path
    return target


def verify_migration(live_config: str, home: str, legacy: str, legacy_sha256: str, key: str,
                     value, live_value) -> "list[str]":
    """Everything that should be true after a migration, as reasons it is not. Empty means it is.

    For a driven run of a packaged build: `live_config` is the path the PROCESS is using and
    `live_value` what it holds in memory for `key`, so a config that was copied but not the one
    in use cannot pass.
    """
    out = []
    want = os.path.join(home, CONFIG_NAME)
    if os.path.normcase(os.path.abspath(live_config)) != os.path.normcase(os.path.abspath(want)):
        out.append("the process is using %s, expected %s" % (live_config, want))
    raw, why = _read_object(want)
    if raw is None:
        out.append("the home config %s" % why)
    else:
        saved = json.loads(raw.decode("utf-8-sig")).get(key)
        if saved != value:
            out.append("the home config has %s=%r, expected %r" % (key, saved, value))
    if live_value != value:
        out.append("the running app holds %s=%r, expected %r" % (key, live_value, value))
    try:
        with open(os.path.join(home, RECORD_NAME), encoding="utf-8") as handle:
            record = json.load(handle)
        if os.path.normcase(os.path.abspath(str(record.get("imported_from", "")))) != os.path.normcase(os.path.abspath(legacy)):
            out.append("the record says it came from %r, expected %r" % (record.get("imported_from"), legacy))
    except (OSError, ValueError):
        out.append("no readable %s in the home" % RECORD_NAME)
    try:
        with open(legacy, "rb") as handle:
            if hashlib.sha256(handle.read()).hexdigest() != legacy_sha256:
                out.append("the original %s was changed" % legacy)
    except OSError:
        out.append("the original %s is gone" % legacy)
    return out


def describe(result: "Result | None") -> str:
    """One honest line for a log, or '' when there is nothing to say."""
    if result is None or result.state in (STATE_EXISTS, STATE_FRESH):
        return ""
    if result.imported:
        extra = (" (%d other old settings file(s) were found and not used)" % len(result.others)) if result.others else ""
        return ("Settings imported from %s (%s) into %s. The original was copied, not moved, and was left in "
                "place%s." % (result.source, result.reason, result.config_path, extra))
    if result.state == STATE_UNREADABLE:
        return ("Old settings exist but none can be used (%s); starting with defaults. Nothing was changed."
                % result.reason)
    return "Settings could not be imported (%s); starting with defaults. The original was not touched." % result.reason
