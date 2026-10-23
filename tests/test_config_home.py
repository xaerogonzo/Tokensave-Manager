"""config_home: settings live in the user's data folder in a build, and move there safely.

Held here, none of them "it returns a path":

* where the config and the logs live for each kind of run, and which override wins;
* which old config is chosen (beside the exe, then sibling release folders newest first, numerically);
* the migration never overwrites, never deletes, copies only what parses, verifies the copy,
  appears atomically, and leaves a record;
* read-only consumers (the CLI, the wrapper) read without writing;
* a simulated packaged first run does all of it through the real modules.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import config_home as ch

ROOT = Path(__file__).resolve().parent.parent
BASE = os.path.join("C:\\", "tools", "Tokensave-Manager-v2.6.0-windows")


def _write(path: Path, data="{}", raw: bytes | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_bytes(raw)
    else:
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def _release(parent: Path, version: str, config=None) -> Path:
    folder = parent / ("Tokensave-Manager-v%s-windows" % version)
    folder.mkdir(parents=True, exist_ok=True)
    if config is not None:
        _write(folder / ch.CONFIG_NAME, config)
    return folder


# ── where things live ───────────────────────────────────────────────────

def test_the_data_folder_is_under_localappdata_with_a_fallback():
    assert ch.user_data_dir({"LOCALAPPDATA": "C:\\L"}) == os.path.join("C:\\L", ch.APP_DIR)
    fallback = ch.user_data_dir({})
    assert fallback.endswith(os.path.join("AppData", "Local", ch.APP_DIR)) and fallback == ch.user_data_dir({"LOCALAPPDATA": "  "})


def test_a_build_uses_the_data_folder_and_a_checkout_uses_the_repository_root():
    env = {"LOCALAPPDATA": "C:\\L"}
    assert ch.config_path(env, True, BASE) == os.path.join("C:\\L", ch.APP_DIR, ch.CONFIG_NAME)
    assert ch.config_path(env, False, BASE) == os.path.join(BASE, ch.CONFIG_NAME)


def test_one_explicit_file_beats_a_home_folder_which_beats_the_default(tmp_path):
    env = {"LOCALAPPDATA": "C:\\L", ch.HOME_ENV: str(tmp_path / "home"), ch.CONFIG_ENV: str(tmp_path / "one.json")}
    assert ch.config_path(env, True, BASE) == str(tmp_path / "one.json")
    del env[ch.CONFIG_ENV]
    assert ch.config_path(env, True, BASE) == str(tmp_path / "home" / ch.CONFIG_NAME)
    assert ch.config_path({ch.HOME_ENV: " ", ch.CONFIG_ENV: ""}, False, BASE) == os.path.join(BASE, ch.CONFIG_NAME)


def test_the_installs_own_config_ignores_every_override(tmp_path):
    env = {"LOCALAPPDATA": "C:\\L", ch.HOME_ENV: str(tmp_path), ch.CONFIG_ENV: str(tmp_path / "x.json")}
    assert ch.default_config_path(env, True, BASE) == os.path.join("C:\\L", ch.APP_DIR, ch.CONFIG_NAME)
    assert ch.default_config_path(env, False, BASE) == os.path.join(BASE, ch.CONFIG_NAME)


def test_constants_resolve_through_the_same_function():
    import constants
    assert constants._resolve_config_path({}, BASE) == os.path.join(BASE, ch.CONFIG_NAME)
    assert constants._resolve_config_path({"LOCALAPPDATA": "C:\\L"}, BASE, frozen=True).endswith(
        os.path.join(ch.APP_DIR, ch.CONFIG_NAME))


# ── which old config ────────────────────────────────────────────────────

def test_beside_the_executable_comes_first_then_siblings_newest_first_numerically(tmp_path):
    here = _release(tmp_path, "2.7.0", {"here": 1})
    _release(tmp_path, "2.9.0", {"s": 9})
    _release(tmp_path, "2.10.0", {"s": 10})
    _release(tmp_path, "2.6.0", {"s": 6})
    found = ch.legacy_candidates(str(here))
    assert [c.kind for c in found] == [ch.KIND_BESIDE] + [ch.KIND_SIBLING] * 3
    assert [os.path.basename(os.path.dirname(c.path)) for c in found[1:]] == [
        "Tokensave-Manager-v2.10.0-windows", "Tokensave-Manager-v2.9.0-windows", "Tokensave-Manager-v2.6.0-windows"]


def test_the_install_own_folder_other_names_and_folders_without_a_config_are_not_candidates(tmp_path):
    here = _release(tmp_path, "2.7.0")  # no config beside
    _release(tmp_path, "2.6.0")         # release folder, no config
    _write(tmp_path / "Tokensave-Manager-backup" / ch.CONFIG_NAME)   # not a release name
    _write(tmp_path / "Other-v2.5.0-windows" / ch.CONFIG_NAME)
    assert ch.legacy_candidates(str(here)) == []
    (here / ch.CONFIG_NAME).write_text("{}", encoding="utf-8")
    assert [c.kind for c in ch.legacy_candidates(str(here))] == [ch.KIND_BESIDE]  # never lists itself twice


def test_a_missing_parent_folder_is_no_candidates_not_an_error(tmp_path):
    assert ch.legacy_candidates(str(tmp_path / "gone" / "install")) == []


# ── the plan ────────────────────────────────────────────────────────────

def _plan(tmp_path, here):
    return ch.plan_migration(str(tmp_path / "home" / ch.CONFIG_NAME), str(here))


def test_an_existing_home_config_means_nothing_to_do_even_with_old_ones_around(tmp_path):
    here = _release(tmp_path, "2.7.0", {"old": 1})
    _write(tmp_path / "home" / ch.CONFIG_NAME, {"new": 1})
    assert _plan(tmp_path, here).state == ch.STATE_EXISTS


def test_no_config_anywhere_is_a_fresh_first_run(tmp_path):
    assert _plan(tmp_path, _release(tmp_path, "2.7.0")).state == ch.STATE_FRESH


def test_the_beside_config_is_chosen_over_a_newer_sibling_and_the_rest_are_recorded(tmp_path):
    here = _release(tmp_path, "2.7.0", {"here": 1})
    _release(tmp_path, "2.9.0", {"s": 9})
    plan = _plan(tmp_path, here)
    assert plan.state == ch.STATE_IMPORT and plan.source.kind == ch.KIND_BESIDE
    assert [o.kind for o in plan.others] == [ch.KIND_SIBLING]


@pytest.mark.parametrize("raw, why", [
    (b"{not json", "not valid JSON"), (b"[1, 2]", "not a JSON object"), (b"", "not valid JSON"), (b"\xff\xfe\x00", "not valid JSON")])
def test_an_unusable_old_config_is_skipped_and_the_next_is_used(tmp_path, raw, why):
    here = _release(tmp_path, "2.7.0")
    _write(here / ch.CONFIG_NAME, raw=raw)
    _release(tmp_path, "2.6.0", {"good": 1})
    plan = _plan(tmp_path, here)
    assert plan.state == ch.STATE_IMPORT and plan.source.kind == ch.KIND_SIBLING
    assert why in plan.skipped[0][1]


def test_when_every_old_config_is_unusable_that_is_reported_not_treated_as_no_settings(tmp_path):
    here = _release(tmp_path, "2.7.0")
    _write(here / ch.CONFIG_NAME, raw=b"{broken")
    plan = _plan(tmp_path, here)
    assert plan.state == ch.STATE_UNREADABLE and "not valid JSON" in plan.reason
    result = ch.apply_plan(plan)
    assert result.state == ch.STATE_UNREADABLE and not (tmp_path / "home").exists()


def test_a_bom_prefixed_config_is_usable(tmp_path):
    here = _release(tmp_path, "2.7.0")
    _write(here / ch.CONFIG_NAME, raw=b"\xef\xbb\xbf" + json.dumps({"a": 1}).encode())
    assert _plan(tmp_path, here).state == ch.STATE_IMPORT


# ── the act ─────────────────────────────────────────────────────────────

def _import(tmp_path, config=None):
    config = {"tokensave_exe": "C:/t/tokensave.exe", "editor_cmd": "marker"} if config is None else config
    here = _release(tmp_path, "2.7.0", config)
    plan = _plan(tmp_path, here)
    return here, plan, ch.apply_plan(plan)


def test_the_copy_is_byte_identical_the_original_is_untouched_and_the_home_is_created(tmp_path):
    here, _, result = _import(tmp_path)
    original = (here / ch.CONFIG_NAME).read_bytes()
    target = tmp_path / "home" / ch.CONFIG_NAME
    assert result.imported and target.read_bytes() == original
    assert (here / ch.CONFIG_NAME).read_bytes() == original  # copied, not moved
    assert not list((tmp_path / "home").glob("*.tmp"))


def test_a_record_says_where_it_came_from_and_what_else_was_found(tmp_path):
    here = _release(tmp_path, "2.7.0", {"a": 1})
    sib = _release(tmp_path, "2.6.0", {"b": 1})
    result = ch.apply_plan(_plan(tmp_path, here))
    record = json.loads((tmp_path / "home" / ch.RECORD_NAME).read_text(encoding="utf-8"))
    assert record["imported_from"] == str(here / ch.CONFIG_NAME) and record["sha256"] == result.sha256
    assert record["also_found_not_used"] == [str(sib / ch.CONFIG_NAME)] and "copied, not moved" in record["note"]


def test_it_never_overwrites_a_config_that_appears_between_the_plan_and_the_act(tmp_path):
    here = _release(tmp_path, "2.7.0", {"old": 1})
    plan = _plan(tmp_path, here)
    mine = _write(tmp_path / "home" / ch.CONFIG_NAME, {"mine": 1})
    result = ch.apply_plan(plan)
    assert result.state == ch.STATE_EXISTS and json.loads(mine.read_text(encoding="utf-8")) == {"mine": 1}
    assert not list((tmp_path / "home").glob("*.tmp"))


def test_a_second_run_changes_nothing(tmp_path):
    here, _, first = _import(tmp_path)
    target = tmp_path / "home" / ch.CONFIG_NAME
    before = target.read_bytes()
    second = ch.apply_plan(_plan(tmp_path, here))
    assert first.imported and second.state == ch.STATE_EXISTS and target.read_bytes() == before


def test_an_old_config_that_goes_bad_between_the_plan_and_the_act_writes_nothing(tmp_path):
    here = _release(tmp_path, "2.7.0", {"a": 1})
    plan = _plan(tmp_path, here)
    (here / ch.CONFIG_NAME).write_bytes(b"{broken")
    result = ch.apply_plan(plan)
    assert result.state == ch.STATE_FAILED and not (tmp_path / "home" / ch.CONFIG_NAME).exists()


@pytest.mark.parametrize("breaker", ["fsync", "rename"])
def test_an_io_failure_leaves_no_config_and_no_temp_file_and_says_so(tmp_path, monkeypatch, breaker):
    here = _release(tmp_path, "2.7.0", {"a": 1})
    plan = _plan(tmp_path, here)
    if breaker == "fsync":
        monkeypatch.setattr(ch.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk")))
    else:
        monkeypatch.setattr(ch.os, "rename", lambda a, b: (_ for _ in ()).throw(PermissionError("locked")))
    result = ch.apply_plan(plan)
    assert result.state == ch.STATE_FAILED and "could not copy" in result.reason
    assert not (tmp_path / "home" / ch.CONFIG_NAME).exists() and not list((tmp_path / "home").glob("*"))
    assert json.loads((here / ch.CONFIG_NAME).read_text(encoding="utf-8")) == {"a": 1}


def test_a_copy_that_does_not_match_its_source_is_never_put_in_place(tmp_path, monkeypatch):
    """The hash check is the only thing between a corrupted write and a config that looks right."""
    here = _release(tmp_path, "2.7.0", {"a": 1})
    plan = _plan(tmp_path, here)
    real_fdopen = ch.os.fdopen

    def corrupting(fd, mode="r", *args, **kwargs):
        handle = real_fdopen(fd, mode, *args, **kwargs)
        real_write = handle.write
        handle.write = lambda data: real_write(data[:-1] + b" ")
        return handle

    monkeypatch.setattr(ch.os, "fdopen", corrupting)
    result = ch.apply_plan(plan)
    assert result.state == ch.STATE_FAILED and "does not match" in result.reason
    assert not (tmp_path / "home" / ch.CONFIG_NAME).exists() and not list((tmp_path / "home").glob("*"))


def test_a_failed_record_does_not_fail_the_import(tmp_path, monkeypatch):
    here = _release(tmp_path, "2.7.0", {"a": 1})
    monkeypatch.setattr(ch.json, "dump", lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    result = ch.apply_plan(_plan(tmp_path, here))
    assert result.imported and (tmp_path / "home" / ch.CONFIG_NAME).is_file()


def test_applying_a_plan_that_is_not_an_import_writes_nothing(tmp_path):
    for plan in (ch.Plan(ch.STATE_FRESH, str(tmp_path / "h" / "c.json")), ch.Plan(ch.STATE_EXISTS, str(tmp_path / "h" / "c.json"))):
        assert ch.apply_plan(plan).state == plan.state
    assert not (tmp_path / "h").exists()


# ── read-only consumers ─────────────────────────────────────────────────

def _env(tmp_path):
    return {"LOCALAPPDATA": str(tmp_path / "local")}


def test_a_checkout_reads_its_own_config_and_nothing_else(tmp_path):
    base = _release(tmp_path, "2.7.0", {"a": 1})
    assert ch.read_path({}, False, str(base)) == str(base / ch.CONFIG_NAME)


def test_a_build_reads_the_home_config_when_there_is_one(tmp_path):
    base = _release(tmp_path, "2.7.0", {"old": 1})
    home = _write(tmp_path / "local" / ch.APP_DIR / ch.CONFIG_NAME, {"new": 1})
    assert ch.read_path(_env(tmp_path), True, str(base)) == str(home)


def test_a_build_that_has_not_migrated_yet_reads_the_old_file_in_place_and_writes_nothing(tmp_path):
    base = _release(tmp_path, "2.7.0", {"old": 1})
    before = sorted(p.name for p in tmp_path.rglob("*"))
    assert ch.read_path(_env(tmp_path), True, str(base)) == str(base / ch.CONFIG_NAME)
    assert sorted(p.name for p in tmp_path.rglob("*")) == before and not (tmp_path / "local").exists()


def test_an_explicit_config_file_is_never_second_guessed(tmp_path):
    base = _release(tmp_path, "2.7.0", {"old": 1})
    env = {**_env(tmp_path), ch.CONFIG_ENV: str(tmp_path / "not-there.json")}
    assert ch.read_path(env, True, str(base)) == str(tmp_path / "not-there.json")


def test_a_build_with_nothing_anywhere_answers_the_home_path(tmp_path):
    base = _release(tmp_path, "2.7.0")
    assert ch.read_path(_env(tmp_path), True, str(base)) == str(tmp_path / "local" / ch.APP_DIR / ch.CONFIG_NAME)


def test_only_a_build_without_an_explicit_file_migrates(tmp_path):
    base = _release(tmp_path, "2.7.0", {"a": 1})
    assert ch.migrate_at_startup(_env(tmp_path), False, str(base)) is None
    assert ch.migrate_at_startup({**_env(tmp_path), ch.CONFIG_ENV: str(tmp_path / "x.json")}, True, str(base)) is None
    assert not (tmp_path / "local").exists()
    assert ch.migrate_at_startup(_env(tmp_path), True, str(base)).imported


def test_a_home_override_is_migrated_into_that_home(tmp_path):
    base = _release(tmp_path, "2.7.0", {"a": 1})
    env = {**_env(tmp_path), ch.HOME_ENV: str(tmp_path / "scratch-home")}
    assert ch.migrate_at_startup(env, True, str(base)).imported
    assert (tmp_path / "scratch-home" / ch.CONFIG_NAME).is_file() and not (tmp_path / "local").exists()


# ── what is said ────────────────────────────────────────────────────────

def test_the_log_line_for_each_outcome(tmp_path):
    base = _release(tmp_path, "2.7.0", {"a": 1})
    _release(tmp_path, "2.6.0", {"b": 1})
    imported = ch.describe(ch.migrate_at_startup(_env(tmp_path), True, str(base)))
    assert "imported from" in imported and "copied, not moved" in imported and "1 other old settings" in imported
    assert ch.describe(None) == "" and ch.describe(ch.Result(ch.STATE_EXISTS, "x")) == "" and ch.describe(ch.Result(ch.STATE_FRESH, "x")) == ""
    assert "none can be used" in ch.describe(ch.Result(ch.STATE_UNREADABLE, "x", reason="bad"))
    assert "could not be imported" in ch.describe(ch.Result(ch.STATE_FAILED, "x", reason="disk"))


# ── the wrapper's own lines ─────────────────────────────────────────────

def _wrapper_snippet() -> str:
    text = (ROOT / "src" / "tokensave-wrapper.py").read_text(encoding="utf-8")
    start = text.index("import config_home")
    end = text.index("_CONFIG_PATH = config_home.read_path(os.environ, _FROZEN, _BASE_DIR)")
    return text[start:end + len("_CONFIG_PATH = config_home.read_path(os.environ, _FROZEN, _BASE_DIR)")]


def _run_wrapper_lines(monkeypatch, tmp_path, frozen: bool, argv0: str, cwd: Path):
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(sys, "argv", [argv0])
    if frozen:
        monkeypatch.setenv("NUITKA_ONEFILE_PARENT", "41108")  # a PID, as Nuitka really sets it
    else:
        monkeypatch.delenv("NUITKA_ONEFILE_PARENT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv(ch.CONFIG_ENV, raising=False)
    monkeypatch.delenv(ch.HOME_ENV, raising=False)
    namespace = {"os": os, "sys": sys, "__file__": str(ROOT / "src" / "tokensave-wrapper.py")}
    exec(compile(_wrapper_snippet(), "wrapper-snippet", "exec"), namespace)
    return namespace


def test_the_wrapper_finds_its_folder_from_argv0_not_from_the_current_directory(monkeypatch, tmp_path):
    """The old line took the VALUE of NUITKA_ONEFILE_PARENT (a PID) as a path: the cwd."""
    install = _release(tmp_path, "2.7.0", {"tokensave_exe": "legacy"})
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    ns = _run_wrapper_lines(monkeypatch, tmp_path, True, str(install / "tokensave-wrapper.exe"), elsewhere)
    assert ns["_BASE_DIR"] == str(install)
    assert ns["_CONFIG_PATH"] == str(install / ch.CONFIG_NAME)  # not migrated yet: read in place


def test_the_wrapper_prefers_the_home_config_once_the_manager_has_migrated(monkeypatch, tmp_path):
    install = _release(tmp_path, "2.7.0", {"tokensave_exe": "legacy"})
    home = _write(tmp_path / "local" / ch.APP_DIR / ch.CONFIG_NAME, {"tokensave_exe": "new"})
    ns = _run_wrapper_lines(monkeypatch, tmp_path, True, str(install / "tokensave-wrapper.exe"), tmp_path)
    assert ns["_CONFIG_PATH"] == str(home)


def test_the_wrapper_in_a_checkout_keeps_reading_the_repository_config(monkeypatch, tmp_path):
    ns = _run_wrapper_lines(monkeypatch, tmp_path, False, "ignored", tmp_path)
    assert ns["_CONFIG_PATH"] == str(ROOT / ch.CONFIG_NAME)


def test_the_wrapper_never_writes_while_looking(monkeypatch, tmp_path):
    install = _release(tmp_path, "2.7.0", {"x": 1})
    before = sorted(p.name for p in tmp_path.rglob("*"))
    _run_wrapper_lines(monkeypatch, tmp_path, True, str(install / "tokensave-wrapper.exe"), tmp_path)
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


# ── the whole path, through the real modules ────────────────────────────

PACKAGED_FIRST_RUN = r'''
import json, os, sys
sys.path.insert(0, os.environ["TSM_SRC"])
import constants
from state import ManagerConfig
cfg = ManagerConfig.load()
from helpers.config import last_migration
import config_home
print(json.dumps({
    "config_path": constants._CONFIG_PATH, "log_dir": constants.LOG_DIR,
    "base_dir": constants._BASE_DIR, "editor_cmd": cfg.raw.get("editor_cmd"),
    "install_dir": cfg.raw.get("install_dir"), "said": config_home.describe(last_migration()),
}))
'''


def test_a_simulated_packaged_first_run_imports_the_old_config_through_the_real_modules(tmp_path):
    install = _release(tmp_path, "2.7.0", {"editor_cmd": "migrated-marker", "search_roots": ["D:/x"]})
    old = (install / ch.CONFIG_NAME).read_bytes()
    script = install / "tokensave-manager.py"   # argv[0]: where a packaged exe would be
    script.write_text(PACKAGED_FIRST_RUN, encoding="utf-8")
    env = {**os.environ, "NUITKA_ONEFILE_PARENT": "41108", "LOCALAPPDATA": str(tmp_path / "local"),
           "TSM_SRC": str(ROOT / "src")}
    for name in (ch.CONFIG_ENV, ch.HOME_ENV):
        env.pop(name, None)
    done = subprocess.run([sys.executable, "-I", str(script)], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout.strip().splitlines()[-1])
    home = tmp_path / "local" / ch.APP_DIR
    assert out["config_path"] == str(home / ch.CONFIG_NAME) and out["log_dir"] == str(home / "logs")
    assert out["editor_cmd"] == "migrated-marker" and "imported from" in out["said"]
    assert (install / ch.CONFIG_NAME).read_bytes() == old, "the original must be left exactly as it was"
    saved = json.loads((home / ch.CONFIG_NAME).read_text(encoding="utf-8"))
    assert saved["editor_cmd"] == "migrated-marker" and saved["search_roots"] == ["D:/x"]
    # a second start finds the home config and says nothing
    again = subprocess.run([sys.executable, "-I", str(script)], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert json.loads(again.stdout.strip().splitlines()[-1])["said"] == ""


# ── verify_migration: what a driven run checks ─────────────────────────────

def _migrated(tmp_path):
    here = _release(tmp_path, "2.7.0", {"editor_cmd": "marker"})
    legacy = here / ch.CONFIG_NAME
    sha = __import__("hashlib").sha256(legacy.read_bytes()).hexdigest()
    result = ch.apply_plan(_plan(tmp_path, here))
    assert result.imported
    return str(tmp_path / "home"), str(legacy), sha, str(tmp_path / "home" / ch.CONFIG_NAME)


def _verify(home, legacy, sha, live, value="marker", live_value="marker"):
    return ch.verify_migration(live, home, legacy, sha, "editor_cmd", value, live_value)


def test_a_correct_migration_has_no_reasons_it_is_not(tmp_path):
    home, legacy, sha, live = _migrated(tmp_path)
    assert _verify(home, legacy, sha, live) == []


@pytest.mark.parametrize("what, expect", [
    ("wrong_live_path", "is using"),
    ("home_value", "home config has"),
    ("live_value", "running app holds"),
    ("no_record", "no readable"),
    ("wrong_source", "the record says"),
    ("original_changed", "was changed"),
    ("original_gone", "is gone"),
    ("home_gone", "the home config"),
])
def test_each_way_a_migration_can_be_wrong_is_its_own_reason(tmp_path, what, expect):
    home, legacy, sha, live = _migrated(tmp_path)
    value, live_value = "marker", "marker"
    if what == "wrong_live_path":
        live = legacy                       # copied, but the process is still on the old file
    elif what == "home_value":
        _write(Path(live), {"editor_cmd": "other"})
    elif what == "live_value":
        live_value = "stale"
    elif what == "no_record":
        os.unlink(os.path.join(home, ch.RECORD_NAME))
    elif what == "wrong_source":
        record = os.path.join(home, ch.RECORD_NAME)
        _write(Path(record), {"imported_from": "C:/somewhere/else.json"})
    elif what == "original_changed":
        Path(legacy).write_text('{"editor_cmd": "marker", "x": 1}', encoding="utf-8")
    elif what == "original_gone":
        os.unlink(legacy)
    elif what == "home_gone":
        os.unlink(live)
    reasons = _verify(home, legacy, sha, live, value, live_value)
    assert any(expect in r for r in reasons), reasons
