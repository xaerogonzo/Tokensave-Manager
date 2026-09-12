"""Guards on the post-read advisory hook.

The tests that matter most here are not the predicate cases -- those are cheap
and obvious. They are:

* **the permission invariant** (`test_never_emits_a_permission_key`), because a
  hook that only ever returns `allow` silently auto-approves every read it sees,
  and that failure would look exactly like success;
* **the concurrency claim** (`test_concurrent_processes_claim_once`), which the
  shared-JSON design this replaced could not have passed;
* **ownership** (`test_a_foreign_read_hook_is_never_claimed`), because the one
  thing worse than not installing our hook is repairing somebody else's.
"""

import json
import os
import subprocess
import sys
import threading

import pytest

from helpers import read_nudge as rn


# ── Helpers ───────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def nudge():
    """The predicate, imported from the rendered script -- not a second copy."""
    return rn.predicate_module()


def _payload(**over):
    base = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "C:/repo/src/mod.py"},
        "tool_response": {"file": {"content": "x" * 9000}},
        "cwd": "C:/repo",
        "session_id": "s-1",
    }
    base.update(over)
    return base


def _run_script(nudge_mod, payload_text, env=None, state_dir=None):
    """Run the generated script exactly as Claude Code would."""
    environ = dict(os.environ)
    environ.pop("TOKENSAVE_DISABLE_READ_NUDGE", None)
    if state_dir is not None:
        # Keep each test's markers out of the machine's real temp directory.
        for name in ("TMPDIR", "TEMP", "TMP"):
            environ[name] = str(state_dir)
    if env:
        environ.update(env)
    proc = subprocess.run(
        [sys.executable, nudge_mod.__file__],
        input=payload_text, capture_output=True, text=True, env=environ)
    return proc


def _make_project(tmp_path, name="repo", with_metadata=True):
    root = tmp_path / name
    (root / "src").mkdir(parents=True)
    if with_metadata:
        (root / ".tokensave").mkdir()
    source = root / "src" / "mod.py"
    source.write_text("print('x')\n", encoding="utf-8")
    return root, source


# ── The permission invariant ──────────────────────────────────────────────

_DECISION_KEYS = ("permissionDecision", "permissionDecisionReason",
                  "decision", "reason", "continue", "stopReason",
                  "updatedInput", "permission")


@pytest.mark.parametrize("payload", [
    _payload(),
    _payload(tool_name="Grep"),
    _payload(tool_input={}),
    _payload(tool_input={"file_path": "a.png"}),
    _payload(tool_input={"file_path": "a.ipynb"}),
    _payload(tool_input={"file_path": "a.py", "offset": 2, "limit": 9}),
    _payload(tool_response=None),
    _payload(tool_response="short"),
    _payload(cwd=""),
    _payload(session_id=""),
])
def test_never_emits_a_permission_key(nudge, tmp_path, monkeypatch, payload):
    """THE invariant: advisory means `additionalContext` and nothing else.

    Stronger than "never denies" on purpose. `allow` skips the permission
    prompt, so a hook that only ever allows would quietly approve every read it
    matched -- a permissions change nobody asked for, wearing the costume of a
    reminder.
    """
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))

    _verdict, advice = nudge.decide(payload)
    emitted = {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                      "additionalContext": advice}} if advice else {}
    flat = json.dumps(emitted)
    for key in _DECISION_KEYS:
        assert key not in flat, "hook emitted a permission-shaped key: %s" % key


def test_the_script_itself_emits_only_additional_context(nudge, tmp_path):
    """Same invariant, asserted on the artifact rather than on the function."""
    root, source = _make_project(tmp_path)
    payload = _payload(tool_input={"file_path": str(source)}, cwd=str(root))
    proc = _run_script(nudge, json.dumps(payload), state_dir=tmp_path)

    assert proc.returncode == 0
    body = json.loads(proc.stdout)
    assert set(body) == {"hookSpecificOutput"}
    assert set(body["hookSpecificOutput"]) == {"hookEventName", "additionalContext"}
    assert body["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "tokensave_read" in body["hookSpecificOutput"]["additionalContext"]


def test_the_advice_claims_only_what_was_checked(nudge):
    """`.tokensave/` proves a project carries metadata, not that a file is served.

    Freshness, ignore rules and parser coverage all sit between those two facts,
    so the wording must not promise the file comes back.
    """
    assert "can serve indexed content" in nudge.ADVICE
    lowered = nudge.ADVICE.lower()
    for overclaim in ("will return", "should have used", "instead of read",
                      "you should"):
        assert overclaim not in lowered, "advice overclaims: %r" % overclaim


# ── Predicate ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tool,inp,size,in_project,expected", [
    ("Read", {"file_path": "a/b.py"}, 9000, True, "eligible"),
    ("Read", {"path": "a/b.py"}, 9000, True, "eligible"),
    ("Grep", {"file_path": "a/b.py"}, 9000, True, "not_read"),
    ("Read", {}, 9000, True, "no_path"),
    ("Read", {"file_path": "a/b.py", "offset": 1}, 9000, True, "sliced"),
    ("Read", {"file_path": "a/b.py", "limit": 5}, 9000, True, "sliced"),
    ("Read", {"file_path": "a/b.png"}, 999999, True, "excluded_extension"),
    ("Read", {"file_path": "a/b.ipynb"}, 999999, True, "excluded_extension"),
    ("Read", {"file_path": "a/b.exe"}, 999999, True, "excluded_extension"),
    ("Read", {"file_path": "a/b.py"}, 2047, True, "small"),
    ("Read", {"file_path": "a/b.py"}, 2048, True, "eligible"),
    ("Read", {"file_path": "a/b.py"}, 9000, False, "no_metadata_project"),
])
def test_classify_read(nudge, tool, inp, size, in_project, expected):
    assert nudge.classify_read(tool, inp, size, in_project) == expected


def test_a_notebook_is_excluded_for_its_stated_reason(nudge):
    """Read renders .ipynb as cells with outputs; tokensave_read does not."""
    assert ".ipynb" in nudge.EXCLUDED_SPECIAL
    assert ".ipynb" not in nudge.NUDGE_EXTENSIONS


def test_response_bytes_measures_the_payload_not_the_file(nudge):
    """`tool_response` arrives as a dict -- measured live, not assumed."""
    assert nudge.response_bytes(None) == 0
    assert nudge.response_bytes("abcd") == 4
    assert nudge.response_bytes({"a": "bb"}) == len(json.dumps({"a": "bb"}))
    assert nudge.response_bytes(object()) == 0        # unserialisable, not a crash


# ── The project boundary ──────────────────────────────────────────────────

def test_metadata_project_found_at_the_root(nudge, tmp_path):
    """(a) cwd=repo, file in repo/src, repo/.tokensave -> eligible."""
    root, source = _make_project(tmp_path)
    assert nudge.project_with_tokensave_metadata(str(source), str(root)) is True


def test_a_parent_projects_metadata_does_not_capture_a_child(nudge, tmp_path):
    """(b) The boundary case the bound exists for.

    Without stopping at `cwd`, one stray `D:/projects/.tokensave` would mark
    every unrelated repository beneath it as served.
    """
    (tmp_path / ".tokensave").mkdir()
    root, source = _make_project(tmp_path, with_metadata=False)
    assert nudge.project_with_tokensave_metadata(str(source), str(root)) is False


def test_a_file_outside_the_session_cwd_is_not_this_project(nudge, tmp_path):
    """(c) Tools can be called on absolute paths anywhere on the machine."""
    root, _source = _make_project(tmp_path, name="repo")
    elsewhere, other = _make_project(tmp_path, name="unrelated",
                                     with_metadata=False)
    assert elsewhere.exists()
    assert nudge.project_with_tokensave_metadata(str(other), str(root)) is False


def test_the_ancestor_walk_is_bounded(nudge, tmp_path):
    deep = tmp_path.joinpath(*["level%d" % i for i in range(30)])
    deep.mkdir(parents=True)
    (tmp_path / ".tokensave").mkdir()
    target = deep / "mod.py"
    target.write_text("x", encoding="utf-8")
    # cwd is unset, so only the ancestor bound stops the climb.
    assert nudge.project_with_tokensave_metadata(str(target), "") is False


# ── Dedupe state: atomic, and actually raced ──────────────────────────────

def test_claim_is_true_exactly_once_per_session_and_file(nudge, tmp_path,
                                                         monkeypatch):
    monkeypatch.setattr(nudge, "_temp_root", lambda: str(tmp_path))
    assert nudge.claim_first_sighting("s", "C:/repo/a.py") is True
    assert nudge.claim_first_sighting("s", "C:/repo/a.py") is False
    assert nudge.claim_first_sighting("s", "C:/repo/b.py") is True
    assert nudge.claim_first_sighting("other", "C:/repo/a.py") is True


def test_concurrent_threads_claim_once(nudge, tmp_path, monkeypatch):
    monkeypatch.setattr(nudge, "_temp_root", lambda: str(tmp_path))
    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(16)

    def worker():
        barrier.wait()
        got = nudge.claim_first_sighting("race", "C:/repo/same.py")
        with lock:
            results.append(got)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 1, results


def test_concurrent_processes_claim_once(nudge, tmp_path):
    """The real shape of the race: separate hook processes, started together.

    This is the test the shared-JSON state file could not have passed -- two
    readers both see "not yet nudged", both nudge, and one entry is lost.
    """
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import importlib.util, os, sys\n"
        "os.environ['TMPDIR'] = os.environ['TEMP'] = os.environ['TMP'] = sys.argv[2]\n"
        "spec = importlib.util.spec_from_file_location('n', sys.argv[1])\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        # TMPDIR/TEMP/TMP above is enough: `tempfile` is imported lazily, on
        # first use, so it reads the environment this driver just set.
        "print('CLAIMED' if mod.claim_first_sighting('proc-race', 'C:/r/x.py')"
        " else 'SILENT')\n",
        encoding="utf-8")

    state = tmp_path / "state"
    state.mkdir()
    procs = [subprocess.Popen(
        [sys.executable, str(driver), nudge.__file__, str(state)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(8)]
    outs = [proc.communicate()[0].strip() for proc in procs]

    assert all(proc.returncode == 0 for proc in procs), outs
    assert outs.count("CLAIMED") == 1, outs


def test_the_marker_directory_is_capped(nudge, tmp_path, monkeypatch):
    monkeypatch.setattr(nudge, "_temp_root", lambda: str(tmp_path))
    monkeypatch.setattr(nudge, "MAX_MARKERS", 5)
    for index in range(6):
        nudge.claim_first_sighting("cap", "C:/repo/f%d.py" % index)
    # Sentinel dropped; from here the hot path is one `exists` and silence.
    assert nudge.claim_first_sighting("cap", "C:/repo/brand-new.py") is False


# ── Fail open ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("stdin_text", ["", "not json at all", "[]", "null",
                                        '{"tool_input": "not a dict"}'])
def test_fail_open_on_malformed_stdin(nudge, stdin_text):
    proc = _run_script(nudge, stdin_text)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_the_env_override_silences_it(nudge, tmp_path):
    root, source = _make_project(tmp_path)
    payload = _payload(tool_input={"file_path": str(source)}, cwd=str(root))
    proc = _run_script(nudge, json.dumps(payload), state_dir=tmp_path,
                       env={"TOKENSAVE_DISABLE_READ_NUDGE": "1"})
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# ── Interpreter resolution ────────────────────────────────────────────────

def test_pythonw_resolves_to_its_console_sibling(tmp_path):
    """`pythonw.exe` has no stdout to write the advisory to."""
    (tmp_path / "pythonw.exe").write_text("", encoding="utf-8")
    (tmp_path / "python.exe").write_text("", encoding="utf-8")
    found, error = rn.resolve_interpreter(str(tmp_path / "pythonw.exe"))
    assert error == ""
    assert os.path.basename(found).lower() == "python.exe"


def test_a_configured_console_python_is_used_as_is(tmp_path):
    exe = tmp_path / "python.exe"
    exe.write_text("", encoding="utf-8")
    found, error = rn.resolve_interpreter(str(exe))
    assert (found, error) == (str(exe), "")


def test_a_configured_but_absent_python_falls_back(tmp_path):
    found, error = rn.resolve_interpreter(str(tmp_path / "nope" / "python.exe"))
    assert error == ""
    assert os.path.isfile(found)


def test_no_interpreter_refuses_rather_than_writing_a_dead_hook(monkeypatch,
                                                               tmp_path):
    monkeypatch.setattr(rn.shutil, "which", lambda _name: None)
    monkeypatch.setattr(rn.sys, "executable", str(tmp_path / "pythonw.exe"))
    found, error = rn.resolve_interpreter("")
    assert found == ""
    assert "not installed" in error

    ok, install_error, actions = rn.install(
        python_exe="", settings_path=str(tmp_path / "settings.json"),
        script_path=str(tmp_path / "hook.py"))
    assert ok is False
    assert actions == []
    assert install_error == error
    assert not (tmp_path / "settings.json").exists()
    assert not (tmp_path / "hook.py").exists()


# ── Install, ownership and drift ──────────────────────────────────────────

@pytest.fixture
def paths(tmp_path):
    """A settings file and a script path, both under the test's own directory."""
    return (str(tmp_path / "settings.json"), str(tmp_path / "hooks" / "nudge.py"))


def _install(paths, **over):
    settings_path, script_path = paths
    kwargs = {"python_exe": sys.executable, "settings_path": settings_path,
              "script_path": script_path}
    kwargs.update(over)
    return rn.install(**kwargs)


def _load(settings_path):
    with open(settings_path, encoding="utf-8") as handle:
        return json.load(handle)


def test_install_writes_one_entry_and_reports_current(paths):
    settings_path, script_path = paths
    ok, error, actions = _install(paths)
    assert (ok, error) == (True, "")
    assert any("Added" in line for line in actions)

    entries = _load(settings_path)["hooks"]["PostToolUse"]
    assert len(entries) == 1
    assert entries[0]["matcher"] == "Read"
    assert entries[0]["hooks"][0]["args"] == [script_path]
    # `command` + `args`, never one quoted string -- no shell quoting involved.
    assert entries[0]["hooks"][0]["type"] == "command"
    assert rn.installed_state(settings_path, script_path) == (rn.CURRENT, "")


def test_installing_twice_leaves_exactly_one_entry(paths):
    settings_path, script_path = paths
    _install(paths)
    _install(paths)
    assert len(_load(settings_path)["hooks"]["PostToolUse"]) == 1
    assert rn.installed_state(settings_path, script_path)[0] == rn.CURRENT


def test_unrelated_settings_survive_semantically(paths):
    """Not byte-for-byte: serialisation may re-indent or reorder legitimately.

    Asserting bytes would encode the serialiser formatting as a product
    contract. What must survive is the meaning.
    """
    settings_path, _script_path = paths
    original = {
        "theme": "dark",
        "permissions": {"allow": ["mcp__tokensave__*"]},
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "tokensave.exe",
                                 "args": ["hook-stop"]}]}],
            "PreToolUse": [{"matcher": "Agent|Grep|Bash|Glob",
                            "hooks": [{"type": "command",
                                       "command": "tokensave.exe",
                                       "args": ["hook-pre-tool-use"]}]}],
        },
    }
    with open(settings_path, "w", encoding="utf-8") as handle:
        json.dump(original, handle, indent=2)

    ok, error, _actions = _install(paths)
    assert (ok, error) == (True, "")

    after = _load(settings_path)
    assert after["theme"] == original["theme"]
    assert after["permissions"] == original["permissions"]
    assert after["hooks"]["Stop"] == original["hooks"]["Stop"]
    assert after["hooks"]["PreToolUse"] == original["hooks"]["PreToolUse"]
    assert len(after["hooks"]["PostToolUse"]) == 1


def test_a_foreign_read_hook_is_never_claimed(paths):
    """Somebody else Read hook is not ours, even carrying our marker string.

    D1b "unknown is never false", applied to ownership: the one thing worse
    than failing to install our hook is repairing a hook we do not own.
    """
    settings_path, script_path = paths
    foreign = {"matcher": "Read",
               "hooks": [{"type": "command", "command": "their-python",
                          "args": ["C:/theirs/tokensave-read-nudge-lookalike.py"]}]}
    with open(settings_path, "w", encoding="utf-8") as handle:
        json.dump({"hooks": {"PostToolUse": [foreign]}}, handle, indent=2)

    assert rn.is_owned_entry(foreign, script_path) is False
    state, detail = rn.installed_state(settings_path, script_path)
    assert state == rn.ABSENT
    assert "no owned" in detail

    _install(paths)
    entries = _load(settings_path)["hooks"]["PostToolUse"]
    assert foreign in entries, "a foreign hook was modified or dropped"
    assert len(entries) == 2


def test_duplicate_owned_entries_are_reported_and_repaired_to_one(paths):
    settings_path, script_path = paths
    _install(paths)
    settings = _load(settings_path)
    settings["hooks"]["PostToolUse"].append(
        dict(settings["hooks"]["PostToolUse"][0]))
    with open(settings_path, "w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=2)

    state, detail = rn.installed_state(settings_path, script_path)
    assert state == rn.DUPLICATE
    assert "2 owned" in detail

    ok, _error, actions = _install(paths)
    assert ok is True
    assert any("duplicate" in line for line in actions)
    assert len(_load(settings_path)["hooks"]["PostToolUse"]) == 1


def test_a_hand_edited_script_names_the_script_not_the_entry(paths):
    """Which half is wrong is the diagnosis; stale alone is a wrong prompt."""
    settings_path, script_path = paths
    _install(paths)
    with open(script_path, "a", encoding="utf-8") as handle:
        handle.write("\n# somebody edited this\n")

    state, detail = rn.installed_state(settings_path, script_path)
    assert state == rn.STALE
    assert rn.HALF_SCRIPT in detail
    assert "%s is current" % rn.HALF_ENTRY in detail


def test_a_missing_script_is_stale_and_says_so(paths):
    settings_path, script_path = paths
    _install(paths)
    os.remove(script_path)
    state, detail = rn.installed_state(settings_path, script_path)
    assert state == rn.STALE
    assert "missing" in detail


def test_line_endings_alone_are_not_staleness(paths):
    """A checkout or editor may rewrite newlines without changing an instruction."""
    settings_path, script_path = paths
    _install(paths)
    with open(script_path, encoding="utf-8") as handle:
        text = handle.read()
    with open(script_path, "w", encoding="utf-8", newline="\r\n") as handle:
        handle.write(text)
    assert rn.installed_state(settings_path, script_path)[0] == rn.CURRENT


def test_an_older_generated_script_migrates_to_one_current_entry(paths):
    """A version bump is designed now, not at the first bump."""
    settings_path, script_path = paths
    _install(paths)
    with open(script_path, "w", encoding="utf-8") as handle:
        handle.write("# tokensave-read-nudge v0\nprint('older form')\n")
    assert rn.installed_state(settings_path, script_path)[0] == rn.STALE

    ok, error, _actions = _install(paths)
    assert (ok, error) == (True, "")
    assert len(_load(settings_path)["hooks"]["PostToolUse"]) == 1
    assert rn.installed_state(settings_path, script_path) == (rn.CURRENT, "")


def test_malformed_settings_are_unknown_and_refuse_repair(paths, tmp_path):
    """Conservative on purpose: this is an agent-control file.

    A lenient parser could often salvage enough of a broken settings.json to
    append to. Doing so rewrites a file we have just admitted we cannot read.
    """
    settings_path, script_path = paths
    with open(settings_path, "w", encoding="utf-8") as handle:
        handle.write('{"hooks": {"PostToolUse": [ ,,, ')
    with open(settings_path, "rb") as handle:
        before = handle.read()

    state, detail = rn.installed_state(settings_path, script_path)
    assert state == rn.UNKNOWN
    assert "malformed" in detail

    ok, error, actions = _install(paths)
    assert ok is False
    assert "Refusing to write" in error
    assert actions == []
    with open(settings_path, "rb") as handle:
        assert handle.read() == before
    backups = [p for p in os.listdir(str(tmp_path)) if ".backup." in p]
    assert backups == [], "a refused write left a backup behind"


def test_unreadable_settings_are_unknown_with_their_own_reason(paths):
    settings_path, script_path = paths
    os.mkdir(settings_path)          # a directory where a file is expected
    state, detail = rn.installed_state(settings_path, script_path)
    assert state == rn.UNKNOWN
    assert "unreadable" in detail

    ok, error, _actions = _install(paths)
    assert ok is False
    assert "Refusing to write" in error


def test_the_backup_holds_the_exact_pre_write_bytes(paths, tmp_path):
    settings_path, _script_path = paths
    original = b'{\n  "theme": "dark"\n}\n'
    with open(settings_path, "wb") as handle:
        handle.write(original)

    ok, _error, actions = _install(paths)
    assert ok is True
    backups = [p for p in os.listdir(str(tmp_path)) if ".backup." in p]
    assert len(backups) == 1
    with open(os.path.join(str(tmp_path), backups[0]), "rb") as handle:
        assert handle.read() == original
    assert any("Backed up" in line for line in actions)


def test_a_first_install_writes_no_backup(paths, tmp_path):
    """There is nothing to back up, and inventing one would be noise."""
    ok, _error, _actions = _install(paths)
    assert ok is True
    assert [p for p in os.listdir(str(tmp_path)) if ".backup." in p] == []


def test_default_paths_live_in_the_home_directory_not_the_install_dir(fake_home):
    """Rule D1f: an absolute pointer into the install dir is a relocation break."""
    script = rn.default_script_path()
    assert str(fake_home) in script
    assert os.path.basename(script) == rn.SCRIPT_BASENAME
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(rn.__file__))))
    assert not script.startswith(repo_root)


# ── Measurement parity ────────────────────────────────────────────────────

def _measure_module():
    """Import the adherence script the way a caller would."""
    import importlib.util

    repo = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(rn.__file__))))          # src/helpers/x.py -> repo
    path = os.path.join(repo, "scripts", "measure_tokensave_adherence.py")
    spec = importlib.util.spec_from_file_location("measure_adherence", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, path


def test_the_measurement_imports_the_hook_predicate(nudge):
    """The denominator must come from the deployed logic, not a lookalike."""
    measure, _path = _measure_module()
    assert measure.NUDGE.__file__ == nudge.__file__
    assert measure.NUDGE.MIN_RESPONSE_BYTES == nudge.MIN_RESPONSE_BYTES
    assert measure.NUDGE.NUDGE_EXTENSIONS == nudge.NUDGE_EXTENSIONS


def test_the_measurement_defines_no_second_copy_of_the_predicate():
    """The drift guard, asserted on the source rather than on behaviour.

    Two implementations that agree today are still two implementations. This
    fails the moment somebody reimplements any part of eligibility in the
    script, which is how a ratio quietly starts measuring something else.
    """
    _measure, path = _measure_module()
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    for forbidden in ("def classify_read", "NUDGE_EXTENSIONS = ",
                      "EXCLUDED_SPECIAL = ", "MIN_RESPONSE_BYTES = ",
                      "def project_with_tokensave_metadata"):
        assert forbidden not in source, (
            "the measurement re-implements %r instead of importing it"
            % forbidden)


@pytest.mark.parametrize("verdict_name", [
    "ELIGIBLE", "SLICED", "SMALL", "EXCLUDED_EXTENSION",
    "NO_METADATA_PROJECT", "NOT_READ", "NO_PATH",
])
def test_every_verdict_the_measurement_reports_exists(nudge, verdict_name):
    """A typo in a verdict name would silently count zero of them forever."""
    measure, _path = _measure_module()
    assert hasattr(measure.NUDGE, verdict_name)
    assert getattr(measure.NUDGE, verdict_name) == getattr(nudge, verdict_name)


def test_the_verdicts_partition_every_whole_file_read(nudge):
    """excl + no_proj + small + eligible must equal the whole-file total.

    Measured live at 468 = 236 + 23 + 25 + 184 on 2026-09-11. If a new verdict
    is added without a column, the table stops accounting for its own
    population and a denominator can move unseen.
    """
    whole_file_verdicts = {nudge.EXCLUDED_EXTENSION, nudge.NO_METADATA_PROJECT,
                           nudge.SMALL, nudge.ELIGIBLE}
    reachable = set()
    for path in ("a/b.py", "a/b.png"):
        for size in (10, 99999):
            for in_project in (True, False):
                reachable.add(nudge.classify_read(
                    "Read", {"file_path": path}, size, in_project))
    assert reachable <= whole_file_verdicts, (
        "a whole-file read can reach a verdict with no column: %s"
        % (reachable - whole_file_verdicts))
