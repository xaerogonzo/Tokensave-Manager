"""tests/test_isolated_run.py -- a driven run may sit beside the real Manager only
when it can be shown not to touch it.

The single-instance mutex stopped `TOKENSAVE_MANAGER_DRIVE` from running while
the user's window was open. Skipping the lock is easy and wrong on its own: two
processes would `save()` one `manager-config.json`, and both would answer the
same extension request. So the lock is skipped only when the run is PROVEN
isolated, and these tests hold the proof, the redirect it relies on, and the
places in `app.py` that must change with it.

No Tk display and no real window: `main()` is exercised with its collaborators
replaced, and the construction-time rules are read off the AST.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

import app as app_mod
import constants
from helpers import isolated_run as iso

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src")
REAL = os.path.join(REPO, "manager-config.json")


def _env(**kw):
    return {k: v for k, v in kw.items() if v is not None}


# -- the proof ------------------------------------------------------------------

@pytest.mark.parametrize("env, expected, why", [
    (_env(), False, "an ordinary launch"),
    (_env(TOKENSAVE_MANAGER_DRIVE="s.json"), False,
     "a drive on the real config must keep the lock"),
    (_env(TOKENSAVE_MANAGER_CONFIG="scratch.json"), False,
     "a redirected config without a drive changes nothing about the lock"),
    (_env(TOKENSAVE_MANAGER_DRIVE="s.json", TOKENSAVE_MANAGER_CONFIG="scratch.json"),
     True, "both, and the config is not the install's"),
    (_env(TOKENSAVE_MANAGER_DRIVE="  ", TOKENSAVE_MANAGER_CONFIG="scratch.json"),
     False, "a blank drive is not a drive"),
    (_env(TOKENSAVE_MANAGER_DRIVE="s.json", TOKENSAVE_MANAGER_CONFIG="  "),
     False, "a blank config is not a redirect"),
])
def test_isolation_needs_both_and_a_config_that_is_not_the_installs(env, expected, why):
    assert iso.is_isolated(env, default_config=REAL) is expected, why


def test_pointing_the_override_at_the_real_file_is_not_isolation(tmp_path):
    """The one spelling that would defeat the proof: the override naming the very
    file the real Manager writes. Case and a relative spelling both fold."""
    env = {"TOKENSAVE_MANAGER_DRIVE": "s.json", "TOKENSAVE_MANAGER_CONFIG": REAL}
    assert iso.is_isolated(env, default_config=REAL) is False
    swapped = REAL.swapcase() if os.name == "nt" else REAL
    env["TOKENSAVE_MANAGER_CONFIG"] = swapped
    assert iso.is_isolated(env, default_config=REAL) is False
    rel = os.path.relpath(REAL, os.getcwd()) if os.path.splitdrive(REAL)[0] == \
        os.path.splitdrive(os.getcwd())[0] else REAL
    env["TOKENSAVE_MANAGER_CONFIG"] = rel
    assert iso.is_isolated(env, default_config=REAL) is False


def test_the_drive_title_suffix_defeats_an_exact_title_match():
    """`manager_ipc` and `_bring_existing_to_front` find the Manager by exact
    title; the driven window must not be one of those matches."""
    from helpers import manager_ipc
    assert manager_ipc.WINDOW_TITLE + iso.DRIVE_TITLE_SUFFIX != manager_ipc.WINDOW_TITLE


# -- the redirect it relies on ----------------------------------------------------

def test_resolve_config_path_default_override_and_blank(tmp_path):
    base = str(tmp_path)
    assert constants._resolve_config_path({}, base) == os.path.join(base, "manager-config.json")
    assert constants._resolve_config_path({"TOKENSAVE_MANAGER_CONFIG": ""}, base) == \
        os.path.join(base, "manager-config.json")
    other = str(tmp_path / "x" / "c.json")
    assert constants._resolve_config_path({"TOKENSAVE_MANAGER_CONFIG": other}, base) == other


def _constants_in_subprocess(**env_extra):
    """What `constants` resolves to in a fresh interpreter. The module computes
    its paths at import, so only a fresh process can show the env taking effect."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("TOKENSAVE_MANAGER_CONFIG", "TOKENSAVE_MANAGER_DRIVE")}
    env.update(env_extra)
    out = subprocess.run(
        [sys.executable, "-c",
         "import constants as c; print(c._CONFIG_PATH); print(c.LOG_DIR); "
         "print(c.DEFAULT_CONFIG_PATH)"],
        cwd=SRC, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.splitlines()


def test_the_override_moves_the_config_and_the_logs_with_it(tmp_path):
    scratch = str(tmp_path / "manager-config.json")
    cfg, logs, default = _constants_in_subprocess(TOKENSAVE_MANAGER_CONFIG=scratch)
    assert cfg == scratch
    assert logs == str(tmp_path / "logs")
    assert default != cfg, "the install's own path must stay knowable"


def test_without_the_override_nothing_moves():
    cfg, logs, default = _constants_in_subprocess()
    assert cfg == default
    assert logs == os.path.join(os.path.dirname(default), "logs")


# -- main(): the lock is skipped only for an isolated run -----------------------

class _Run(SimpleNamespace):
    pass


@pytest.fixture
def main_run(monkeypatch):
    """`app.main` with its collaborators recorded instead of performed."""
    calls = _Run(lock=0, front=0, app=0, mainloop=0, exit=None)

    class FakeApp:
        def __init__(self):
            calls.app += 1

        def mainloop(self):
            calls.mainloop += 1

    def exit_(code=0):
        calls.exit = code
        raise SystemExit(code)

    monkeypatch.setattr(app_mod, "App", FakeApp)
    monkeypatch.setattr(app_mod, "_bring_existing_to_front",
                        lambda: setattr(calls, "front", calls.front + 1))
    monkeypatch.setattr(app_mod.sys, "exit", exit_)
    calls.patch = lambda isolated, lock: (
        monkeypatch.setattr(app_mod, "is_isolated", lambda *a, **k: isolated),
        monkeypatch.setattr(app_mod, "_acquire_instance_lock",
                            lambda: (setattr(calls, "lock", calls.lock + 1), lock)[1]))
    return calls


def test_a_second_ordinary_launch_still_yields_to_the_first(main_run):
    main_run.patch(isolated=False, lock=False)
    with pytest.raises(SystemExit):
        app_mod.main()
    assert (main_run.lock, main_run.front, main_run.app) == (1, 1, 0)


def test_the_first_ordinary_launch_runs(main_run):
    main_run.patch(isolated=False, lock=True)
    app_mod.main()
    assert (main_run.lock, main_run.front, main_run.app, main_run.mainloop) == (1, 0, 1, 1)


def test_an_isolated_run_never_touches_the_lock_and_never_yields(main_run):
    """It must not even TAKE the mutex: `manager_running()` describes the real
    Manager, and an isolated run holding it would make the real one look absent."""
    main_run.patch(isolated=True, lock=False)
    app_mod.main()
    assert (main_run.lock, main_run.front, main_run.app, main_run.mainloop) == (0, 0, 1, 1)


# -- construction: what an isolated App must leave out ----------------------------

def _app_init():
    tree = ast.parse(open(os.path.join(SRC, "app.py"), encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "App":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    return item
    raise AssertionError("App.__init__ not found")


def _guarded_by_not_isolated(init, wanted):
    """Every construction/call named in `wanted` must sit inside an `if` whose
    test is `not self._isolated`; returns the ones that do not."""
    inside, outside = set(), set()

    def names(node):
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                f = sub.func
                if isinstance(f, ast.Name) and f.id in wanted:
                    yield f.id
                if isinstance(f, ast.Attribute):
                    owner = f.value
                    label = "%s.%s" % (getattr(owner, "attr", getattr(owner, "id", "")), f.attr)
                    if label in wanted:
                        yield label

    def guarded(test):
        return (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
                and isinstance(test.operand, ast.Attribute)
                and test.operand.attr == "_isolated")

    def visit(node, under):
        if isinstance(node, ast.If):
            now = under or guarded(node.test)
            for child in node.body:
                visit(child, now)
            for child in node.orelse:
                visit(child, under)
            return
        for found in names(node) if not isinstance(node, (ast.If,)) else ():
            (inside if under else outside).add(found)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                visit(child, under)

    for stmt in init.body:
        visit(stmt, False)
    return outside & set(wanted), inside & set(wanted)


WANTED = {"RequestsController", "TrayManager", "_update_poller.start"}


def test_an_isolated_app_leaves_out_what_acts_on_shared_state():
    outside, inside = _guarded_by_not_isolated(_app_init(), WANTED)
    assert not outside, "built even when isolated: %s" % sorted(outside)
    assert inside == WANTED, "no longer constructed at all: %s" % sorted(WANTED - inside)


def test_the_isolated_title_carries_the_suffix():
    src = open(os.path.join(SRC, "app.py"), encoding="utf-8").read()
    assert "DRIVE_TITLE_SUFFIX" in src and "self._isolated = is_isolated()" in src


# -- the launcher ------------------------------------------------------------------

def _launcher():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "run_isolated", os.path.join(REPO, "scripts", "drive", "run_isolated.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_launcher_copies_the_config_and_never_writes_the_real_one(tmp_path):
    mod = _launcher()
    real = tmp_path / "real.json"
    real.write_text('{"tokensave_exe": "x"}', encoding="utf-8")
    script = tmp_path / "s.json"
    script.write_text("[]", encoding="utf-8")
    before = real.read_bytes()
    folder, env = mod.prepare(str(script), str(real), scratch_root=str(tmp_path))
    try:
        assert env["TOKENSAVE_MANAGER_CONFIG"] != str(real)
        assert os.path.dirname(env["TOKENSAVE_MANAGER_CONFIG"]) == folder
        assert open(env["TOKENSAVE_MANAGER_CONFIG"], "rb").read() == before
        assert iso.is_isolated(env, default_config=str(real)), \
            "what the launcher prepares must satisfy the proof the app checks"
    finally:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
    assert real.read_bytes() == before


def test_the_launcher_refuses_a_missing_config_rather_than_using_an_empty_one(tmp_path):
    mod = _launcher()
    script = tmp_path / "s.json"
    script.write_text("[]", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        mod.prepare(str(script), str(tmp_path / "absent.json"))


def test_the_launcher_removes_its_scratch_folder_even_when_the_run_fails(tmp_path):
    mod = _launcher()
    real = tmp_path / "real.json"
    real.write_text("{}", encoding="utf-8")
    script = tmp_path / "s.json"
    script.write_text("[]", encoding="utf-8")
    probe = ("import os, sys; p = os.environ['TOKENSAVE_MANAGER_CONFIG']; "
             "open(os.path.join(os.path.dirname(p), 'seen'), 'w').write(p); sys.exit(7)")
    seen = {}
    real_prepare = mod.prepare

    def spy(*a, **k):
        folder, env = real_prepare(*a, scratch_root=str(tmp_path), **{x: y for x, y in k.items()
                                                                       if x != "scratch_root"})
        seen["folder"] = folder
        return folder, env

    mod.prepare = spy
    code = mod.run(str(script), str(real), command=[sys.executable, "-c", probe])
    assert code == 7
    assert not os.path.exists(seen["folder"])
