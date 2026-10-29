"""The opt-in release starters render to files that behave, and adoption never overwrites.

Three kinds of claim are held here, and none of them is "the template text looks right":

* the **inventory** is explicit (a template nobody listed, or a token nobody defined, fails);
* **rendering fails closed**, including the one injection a token could cause (a value spliced
  into a workflow `run:` script);
* the rendered Python is **executed**: the tag/version check, the archive builder and the
  smoke judge each run against real files, because a starter that compiles but judges wrong is
  worse than none.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from helpers import release_starters as rs

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = str(ROOT / "templates")
SUPPLIED = {"PRODUCT_NAME": "Demo App", "ARTIFACT_PREFIX": "DemoApp", "REPO": "me/demo-app"}
FIXED_GUIDS = iter(str(uuid.UUID(int=i)).upper() for i in range(1, 100))


def _guid():
    return next(FIXED_GUIDS)


def _rendered(name, **extra):
    values, problems = rs.resolve_values({**SUPPLIED, **extra}, _guid)
    assert not problems, problems
    starter = rs.by_name(name)
    text, missing = rs.render(rs._read(TEMPLATES, starter.source_rel), values)
    assert not missing, missing
    return starter, text


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ── the inventory is explicit ───────────────────────────────────────────

def test_every_template_file_is_a_listed_starter_and_the_reverse():
    on_disk = {"release/" + n for n in os.listdir(os.path.join(TEMPLATES, "release")) if n.endswith(".tmpl")}
    assert on_disk == {s.source_rel for s in rs.STARTERS}


def test_destinations_are_unique_fixed_and_inside_the_project():
    dests = [s.dest_rel for s in rs.STARTERS]
    assert len(set(dests)) == len(dests) and len({s.name for s in rs.STARTERS}) == len(dests)
    assert all(".." not in d and not os.path.isabs(d) and "\\" not in d for d in dests)


def test_every_token_a_template_uses_is_defined_and_every_defined_token_is_used():
    used = set()
    for s in rs.STARTERS:
        used |= rs.tokens_in(rs._read(TEMPLATES, s.source_rel))
    assert used - set(rs.TOKENS) == set(), "template tokens with no definition"
    assert set(rs.TOKENS) - used == set(), "defined tokens no template uses"


@pytest.mark.parametrize("starter", rs.STARTERS, ids=lambda s: s.name)
def test_a_starter_renders_with_defaults_and_passes_its_own_check(starter):
    _, text = _rendered(starter.name)
    assert rs.tokens_in(text) == set()  # no unresolved <<TOKEN>> (a bare "1 << 20" is code, not a token)
    assert rs.check_rendered(starter, text) == []
    assert "adapted from OpenChem Studio" in text  # provenance travels with the file


# ── rendering fails closed ──────────────────────────────────────────────

def test_a_required_token_with_no_value_is_reported():
    _, problems = rs.resolve_values({"PRODUCT_NAME": "X"}, _guid)
    assert any("ARTIFACT_PREFIX is required" in p for p in problems)
    assert any("REPO is required" in p for p in problems)


def test_an_unknown_token_and_a_token_inside_a_value_are_refused():
    _, problems = rs.resolve_values({**SUPPLIED, "NOPE": "1", "COPYRIGHT": "a <<REPO>> b"}, _guid)
    assert any("unknown token 'NOPE'" in p for p in problems)
    assert any("may not contain '<<'" in p for p in problems)


def test_generated_ids_differ_and_a_malformed_one_is_refused():
    values, problems = rs.resolve_values(SUPPLIED, _guid)
    assert not problems and values["APP_ID_GUID"] != values["TEST_APP_ID_GUID"]
    _, bad = rs.resolve_values({**SUPPLIED, "APP_ID_GUID": "not-a-guid"}, _guid)
    assert any("APP_ID_GUID must be" in p for p in bad)
    same = str(uuid.UUID(int=7)).upper()
    _, dup = rs.resolve_values({**SUPPLIED, "APP_ID_GUID": same, "TEST_APP_ID_GUID": same}, _guid)
    assert any("must differ" in p for p in dup)


def test_a_workflow_value_that_splices_an_expression_into_a_script_is_refused(tmp_path):
    out = rs.adopt(str(tmp_path), TEMPLATES, ["release-workflow"],
                   {**SUPPLIED, "BUILD_COMMAND": "./build.ps1 ${{ github.event.head_commit.message }}"})
    assert out[0].outcome == rs.OUT_INVALID
    assert any("spliced into a run:" in r for r in out[0].reasons)
    assert not (tmp_path / ".github").exists()  # nothing written


@pytest.mark.parametrize("mutation, expect", [
    (lambda t: t.replace("permissions: {}", "permissions: write-all", 1), "permissions: {}"),
    (lambda t: t.replace("--draft", "--latest"), "--draft"),
    (lambda t: t.replace("actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09", "actions/checkout@v5", 1), "pinned"),
    (lambda t: t + "\n      - run: gh release publish v1\n", "publishes"),
    (lambda t: t + "\n      - name: x\n        shell: bash\n        run: |\n          echo ${{ github.ref_name }}\n", "spliced into a run:"),
    (lambda t: t + "\n      - name: x\n        run: echo ${{ github.ref_name }}\n", "spliced into a run:"),
])
def test_each_workflow_rule_has_a_case_that_breaks_it(mutation, expect):
    _, text = _rendered("release-workflow")
    problems = rs.workflow_problems(mutation(text))
    assert any(expect in p for p in problems), problems


def test_the_installer_check_catches_a_ticked_removal_box_and_a_missing_section():
    _, text = _rendered("installer")
    assert rs.inno_problems(text) == []
    assert any("ticked" in p for p in rs.inno_problems(text + "\n  SettingsBox.Checked := True;\n"))
    assert any("[Code]" in p for p in rs.inno_problems(text.replace("[Code]", "[Cod]")))
    assert any("per-user" in p for p in rs.inno_problems(text.replace("PrivilegesRequired=lowest", "")))


# ── adoption never overwrites, and each starter is its own judgment ─────

def test_adopt_writes_every_starter_to_its_fixed_destination(tmp_path):
    out = rs.adopt(str(tmp_path), TEMPLATES, [s.name for s in rs.STARTERS], SUPPLIED)
    assert [o.outcome for o in out] == [rs.OUT_WRITTEN] * len(rs.STARTERS), out
    for s in rs.STARTERS:
        assert (tmp_path / s.dest_rel).is_file()


def test_a_second_adopt_changes_nothing(tmp_path):
    names = [s.name for s in rs.STARTERS]
    rs.adopt(str(tmp_path), TEMPLATES, names, SUPPLIED)
    before = {s.dest_rel: (tmp_path / s.dest_rel).read_bytes() for s in rs.STARTERS}
    again = rs.adopt(str(tmp_path), TEMPLATES, names, SUPPLIED)
    assert {o.outcome for o in again} == {rs.OUT_EXISTS}
    assert before == {s.dest_rel: (tmp_path / s.dest_rel).read_bytes() for s in rs.STARTERS}


def test_a_person_edited_file_is_left_alone_and_the_others_still_arrive(tmp_path):
    mine = tmp_path / "tools" / "release_info.py"
    mine.parent.mkdir()
    mine.write_text("# my own\n", encoding="utf-8")
    out = {o.name: o.outcome for o in rs.adopt(str(tmp_path), TEMPLATES, [s.name for s in rs.STARTERS], SUPPLIED)}
    assert out["release-info"] == rs.OUT_EXISTS and mine.read_text(encoding="utf-8") == "# my own\n"
    assert all(v == rs.OUT_WRITTEN for k, v in out.items() if k != "release-info")


def test_a_missing_required_value_writes_nothing(tmp_path):
    out = rs.adopt(str(tmp_path), TEMPLATES, ["release-zip"], {"PRODUCT_NAME": "X"})
    assert out[0].outcome == rs.OUT_INVALID and list(tmp_path.iterdir()) == []


def test_an_unknown_starter_name_is_reported_not_ignored(tmp_path):
    assert rs.adopt(str(tmp_path), TEMPLATES, ["nope"], SUPPLIED)[0].outcome == rs.OUT_INVALID


def test_dry_run_reports_without_writing(tmp_path):
    out = rs.adopt(str(tmp_path), TEMPLATES, ["release-info"], SUPPLIED, dry_run=True)
    assert out[0].outcome == rs.OUT_WOULD_WRITE and list(tmp_path.iterdir()) == []


# ── the rendered scripts are executed ───────────────────────────────────

def _adopted(tmp_path, **extra):
    rs.adopt(str(tmp_path), TEMPLATES, [s.name for s in rs.STARTERS], {**SUPPLIED, **extra})
    return tmp_path


def _project_with_version(tmp_path, version="1.2.3", changelog=None):
    _adopted(tmp_path)
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{version}"\n', encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        changelog if changelog is not None else
        "# Changelog\n\n## [Unreleased]\n\n## [1.2.3] — 2026-10-23\n\n### Added\n- a thing\n\n## [1.2.2]\n- old\n",
        encoding="utf-8")
    return tmp_path


def _run_info(project, *args):
    done = subprocess.run([sys.executable, "-I", str(project / "tools" / "release_info.py"), *args],
                          cwd=project, capture_output=True, text=True)
    return done.returncode, done.stdout, done.stderr


def test_release_info_checks_the_tag_against_the_version_file(tmp_path):
    p = _project_with_version(tmp_path)
    assert _run_info(p, "version")[:2] == (0, "1.2.3\n")
    assert _run_info(p, "check-tag", "v1.2.3")[0] == 0
    code, _, err = _run_info(p, "check-tag", "v1.2.4")
    assert code == 1 and "pyproject.toml says 1.2.3" in err
    assert _run_info(p, "check-tag", "1.2.3")[0] == 1  # no v prefix is not a release tag


def test_release_info_notes_are_the_versions_section_and_refuse_a_missing_or_empty_one(tmp_path):
    p = _project_with_version(tmp_path)
    code, out, _ = _run_info(p, "notes", "v1.2.3")
    assert code == 0 and out == "### Added\n- a thing\n"
    (p / "CHANGELOG.md").write_text("# C\n\n## [Unreleased]\n- x\n", encoding="utf-8")
    assert _run_info(p, "notes", "v1.2.3")[0] == 1
    (p / "CHANGELOG.md").write_text("# C\n\n## [1.2.3] — d\n\n## [1.2.2]\n- old\n", encoding="utf-8")
    code, _, err = _run_info(p, "notes", "v1.2.3")
    assert code == 1 and "empty" in err


def test_release_info_notes_survive_a_console_that_cannot_encode_them(tmp_path):
    """A Windows console is cp1252; a changelog with an arrow in it must not crash the print."""
    arrow, dash, nl = chr(0x2192), chr(0x2014), chr(10)
    text = nl.join(["# C", "", "## [1.2.3] " + dash + " d", "", "### Added", "- a " + arrow + " b", ""])
    p = _project_with_version(tmp_path, changelog=text)
    done = subprocess.run([sys.executable, "-I", str(p / "tools" / "release_info.py"), "notes", "v1.2.3"],
                          cwd=p, capture_output=True, env={**os.environ, "PYTHONIOENCODING": "cp1252"})
    assert done.returncode == 0, done.stderr
    assert ("a " + arrow + " b").encode("utf-8") in done.stdout


def test_release_info_reads_a_version_that_is_not_in_pyproject(tmp_path):
    rs.adopt(str(tmp_path), TEMPLATES, ["release-info"],
             {**SUPPLIED, "VERSION_FILE": "src/constants.py", "VERSION_REGEX": r'(?m)^APP_VERSION = "(\d+\.\d+\.\d+)"'})
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "constants.py").write_text('APP_VERSION = "4.5.6"\n', encoding="utf-8")
    assert _run_info(tmp_path, "version")[:2] == (0, "4.5.6\n")


def _zip_module(tmp_path):
    _adopted(tmp_path)
    return _load(tmp_path / "tools" / "make_release_zip.py", "starter_zip_" + tmp_path.name)


def _dist(tmp_path, *, drop=(), extra=None):
    dist = tmp_path / "dist" / "DemoApp"
    for rel in ("DemoApp.exe", "README.md", "LICENSE", "lib/a.dll"):
        if rel not in drop:
            (dist / rel).parent.mkdir(parents=True, exist_ok=True)
            (dist / rel).write_bytes(b"x" + rel.encode())
    for rel in (extra or ()):
        (dist / rel).parent.mkdir(parents=True, exist_ok=True)
        (dist / rel).write_bytes(b"y")
    return dist


def test_the_zip_has_one_versioned_folder_checksums_the_final_bytes_and_leaves_no_partial(tmp_path):
    z = _zip_module(tmp_path)
    out = tmp_path / "out"
    final = z.build_release(_dist(tmp_path), "1.2.3", out, smoke=lambda exe: exe.is_file() and " " in str(exe))
    assert final.name == "DemoApp-v1.2.3-windows-x64.zip"
    with zipfile.ZipFile(final) as archive:
        assert {n.split("/")[0] for n in archive.namelist()} == {"DemoApp-v1.2.3"}
    line = (out / "SHA256SUMS.txt").read_bytes().decode("utf-8")
    assert line == f"{hashlib.sha256(final.read_bytes()).hexdigest()} *{final.name}\n"
    assert not list(out.glob("*.partial")) and not line.startswith("\ufeff") and "\r" not in line


@pytest.mark.parametrize("case", ["missing", "smoke", "forbidden"])
def test_a_bad_archive_leaves_nothing_that_looks_final(tmp_path, case):
    z = _zip_module(tmp_path)
    out = tmp_path / "out"
    dist = _dist(tmp_path, drop=("LICENSE",) if case == "missing" else (),
                 extra=("__pycache__/x.pyc",) if case == "forbidden" else ())
    smoke = (lambda exe: False) if case == "smoke" else (lambda exe: True)
    if case == "forbidden":
        z.FORBIDDEN_PATHS = ("lib/",)
    with pytest.raises(z.ReleaseError):
        z.build_release(dist, "1.2.3", out, smoke=smoke)
    assert not list(out.glob("*.zip")) and not (out / "SHA256SUMS.txt").exists() and not list(out.glob("*.partial"))


def test_the_zip_never_overwrites_without_force_and_a_failure_keeps_the_previous_release(tmp_path):
    z = _zip_module(tmp_path)
    out = tmp_path / "out"
    first = z.build_release(_dist(tmp_path), "1.2.3", out, smoke=lambda e: True)
    bytes_before = first.read_bytes()
    with pytest.raises(z.ReleaseError, match="--force"):
        z.build_release(_dist(tmp_path), "1.2.3", out, smoke=lambda e: True)
    broken = _dist(tmp_path)
    (broken / "README.md").unlink()
    with pytest.raises(z.ReleaseError, match="missing from the archive"):
        z.build_release(broken, "1.2.3", out, force=True, smoke=lambda e: True)
    assert first.read_bytes() == bytes_before


@pytest.mark.parametrize("version", ["", "unknown", "1.0 0", "1/2"])
def test_an_unusable_version_is_refused(tmp_path, version):
    z = _zip_module(tmp_path)
    with pytest.raises(z.ReleaseError):
        z.build_release(_dist(tmp_path), version, tmp_path / "out", smoke=lambda e: True)


def _smoke_module(tmp_path):
    _adopted(tmp_path)
    return _load(tmp_path / "tools" / "frozen_smoke.py", "starter_smoke_" + tmp_path.name)


def _report(script: Path, **over):
    report = {
        "verdict": "PASS", "exit_code": 0,
        "run": {"started": datetime.now(timezone.utc).isoformat(),
                "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest()},
        "steps": {"run": 3, "in_script": 3},
        "assertions": [{"step": "expect_clean", "ok": True, "detail": ""}],
        "ledger": {"dropped": 0, "driver_failures": []},
    }
    report.update(over)
    return report


def _judge(m, tmp_path, report, *, exit_code=0, timed_out=False, launched=None):
    script = tmp_path / "s.json"
    script.write_text("{}", encoding="utf-8")
    path = tmp_path / "report.json"
    if report is not None:
        path.write_text(json.dumps(report(script) if callable(report) else report), encoding="utf-8")
    return m.judge(path, exit_code=exit_code, timed_out=timed_out,
                   launched_at=launched or datetime.now(timezone.utc) - timedelta(seconds=1), script=script)


def test_the_smoke_judge_passes_only_a_complete_fresh_report(tmp_path):
    m = _smoke_module(tmp_path)
    assert _judge(m, tmp_path, lambda s: _report(s)).ok


@pytest.mark.parametrize("label, report, kwargs, expect", [
    ("no report", None, {}, "no report"),
    ("exit status", lambda s: _report(s), {"exit_code": 3}, "exit status"),
    ("timeout", lambda s: _report(s), {"timed_out": True}, "in time"),
    ("verdict", lambda s: _report(s, verdict="FAIL"), {}, "verdict"),
    ("stale", lambda s: _report(s), {"launched": "five minutes from when the test runs"}, "stale"),
    ("wrong script", lambda s: _report(s, run={"started": datetime.now(timezone.utc).isoformat(), "script_sha256": "0"}), {}, "different version"),
    ("cut short", lambda s: _report(s, steps={"run": 1, "in_script": 3}), {}, "did not run to the end"),
    ("required assertion gone", lambda s: _report(s, assertions=[{"step": "other", "ok": True}]), {}, "never ran"),
    ("failed assertion", lambda s: _report(s, assertions=[{"step": "expect_clean", "ok": False, "detail": "boom"}]), {}, "boom"),
    ("dropped ledger", lambda s: _report(s, ledger={"dropped": 2, "driver_failures": []}), {}, "dropped"),
    ("driver failure", lambda s: _report(s, ledger={"dropped": 0, "driver_failures": ["x"]}), {}, "driver failures"),
    ("ledger missing", lambda s: {**_report(s), "ledger": None}, {}, "dropped"),
])
def test_every_way_the_smoke_run_can_be_wrong_is_its_own_failure(tmp_path, label, report, kwargs, expect):
    m = _smoke_module(tmp_path)
    if isinstance(kwargs.get("launched"), str):
        # Resolved NOW, not at collection: a parametrize value computed at import goes stale
        # during a long suite, which made this case fail only in the full run.
        kwargs = {**kwargs, "launched": datetime.now(timezone.utc) + timedelta(minutes=5)}
    judgement = _judge(m, tmp_path, report, **kwargs)
    assert not judgement.ok and any(expect in f for f in judgement.failures), (label, judgement.failures)


STAND_IN = r'''
import hashlib, json, os, sys, time
from datetime import datetime, timezone
mode = os.environ.get("STAND_IN_MODE", "pass")
if mode == "hang":
    time.sleep(60)
script = os.environ["DEMOAPP_DRIVE"]
data = os.environ["DEMOAPP_DATA_ROOT"]
assert os.path.isdir(data)
if mode == "silent":
    sys.exit(0)
report = {"verdict": "PASS", "exit_code": 0,
          "run": {"started": datetime.now(timezone.utc).isoformat(),
                  "script_sha256": hashlib.sha256(open(script, "rb").read()).hexdigest()},
          "steps": {"run": 1, "in_script": 1},
          "assertions": [{"step": "expect_clean", "ok": True, "detail": ""}],
          "ledger": {"dropped": 0, "driver_failures": []}}
open(os.environ["DEMOAPP_DRIVE_REPORT"], "w").write(json.dumps(report))
'''


@pytest.mark.parametrize("mode, ok", [("pass", True), ("silent", False), ("hang", False)])
def test_the_launcher_runs_a_stand_in_app_through_the_env_contract(tmp_path, monkeypatch, mode, ok):
    m = _smoke_module(tmp_path)
    app = tmp_path / "app.py"
    app.write_text(STAND_IN, encoding="utf-8")
    script = tmp_path / "drive.json"
    script.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("STAND_IN_MODE", mode)
    judgement, work = m.run_smoke([sys.executable, str(app)], script, timeout=3 if mode == "hang" else 60, workdir=tmp_path)
    assert judgement.ok is ok, judgement.failures
    shutil.rmtree(work, ignore_errors=True)


# ── the installer compiles, when a compiler is here ─────────────────────

def _iscc():
    found = os.environ.get("ISCC") or shutil.which("ISCC")
    for guess in (os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"),
                  r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"):
        found = found or (guess if os.path.isfile(guess) else None)
    return found


@pytest.mark.skipif(_iscc() is None, reason="Inno Setup (ISCC) is not installed here; the compile is not claimed")
def test_the_rendered_installer_script_compiles_with_the_real_compiler(tmp_path):
    rs.adopt(str(tmp_path), TEMPLATES, ["installer"], SUPPLIED)
    src = tmp_path / "src"
    src.mkdir()
    (src / "LICENSE").write_text("MIT\n", encoding="utf-8")
    (src / "DemoApp.exe").write_bytes(b"MZ")
    out = tmp_path / "out"
    done = subprocess.run([_iscc(), "/Qp", f"/DAppVersion=1.0.0", f"/DSourceDir={src}", f"/DOutputDir={out}",
                           str(tmp_path / "packaging" / "installer.iss")], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    assert (out / "DemoApp-v1.0.0-windows-x64-setup.exe").is_file()


# ── a release body GitHub would refuse is caught in seconds ───────────────

def test_notes_over_githubs_cap_are_refused_before_a_tag_exists_to_regret(tmp_path):
    p = _project_with_version(tmp_path)
    limit = 125_000
    fits = "# C\n\n## [1.2.3] — d\n\n" + "x" * (limit - 1) + "\n\n## [1.2.2]\n- old\n"
    assert _run_info(p, "notes", "v1.2.3")[0] == 0
    (p / "CHANGELOG.md").write_text(fits, encoding="utf-8")
    assert _run_info(p, "notes", "v1.2.3")[0] == 0                       # exactly at the cap still fits
    (p / "CHANGELOG.md").write_text(fits.replace("x" * (limit - 1), "x" * limit), encoding="utf-8")
    code, _, err = _run_info(p, "notes", "v1.2.3")
    assert code == 1 and "125,000" in err and "own heading" in err
