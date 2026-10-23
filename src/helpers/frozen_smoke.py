"""frozen_smoke -- run the BUILT executables and judge what they did.

`smoke_runner.py` runs the pytest suite against source. That proves the code, not the
build: v2.6.0 shipped a headline command that did not work in the packaged exe, and only a
by-hand run found it. This module launches the real `.exe`s, in a scratch folder with a
scratch config, and returns one verdict per layer:

  * **version resource** -- each exe's Properties > Details version equals the app version;
  * **cli** -- `commands --json` and `status --json` from a foreign working directory
    return a well-formed envelope carrying the expected `cli_version`;
  * **gui** -- the exe, started on a drive script, writes the drive report
    (`helpers/drive_ledger`) and `judge_drive_report` accepts it.

Each layer is separate evidence: source tests passing, the frozen GUI starting, the frozen
CLI answering and an extracted zip passing are different claims, and a release needs all
of them.

**First-run versus configured.** A fresh install has no `tokensave_exe`, no `git_exe`, no
`template_dir` and no projects, and that is the state the first layers start the program in.
Every git and tokensave field in a `status` envelope is then null, so an envelope can be
well-formed and say nothing. The `(configured)` layers start the same executables with the real
`tokensave.exe`, `git`, the build's own `templates\\` and a scratch git repository that
`tokensave init` has really indexed, and require the program to SEE them: `tokensave.indexed`
true, a git branch, the project listed in the Projects tab. When tokensave cannot be found
that is a failed layer, not a quiet pass.

**Fail closed.** Every judge starts from "not ok" and every way a run can be wrong is its
own reason. A report that is missing, unreadable, stale, for another script, from a run
that did not quit, or that holds no passing expectation for a REQUIRED id is a failure;
unknown is never a pass.

**The scratch folder is the point.** A GUI smoke test used to pollute `dist\\` (`logs\\`
and a rewritten `manager-config.json`) and the zip shipped the residue. The app is started
isolated (`TOKENSAVE_MANAGER_CONFIG` names a config in a scratch folder, so the install's
own config, logs and single-instance lock are untouched), and the drive script is copied
there so its report lands there too.

Standard library plus `helpers.drive_ledger` (no Tk). Safe off Windows to import; the
version-resource reader says so when it cannot run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

from constants import CREATE_NO_WINDOW, show_no_activate_startupinfo
from helpers import drive_ledger

GUI_EXE = "tokensave-manager.exe"
CLI_EXE = "tokensave-manager-cli.exe"
WRAPPER_EXE = "tokensave-wrapper.exe"
ALL_EXES = (GUI_EXE, WRAPPER_EXE, CLI_EXE)

#: Expectation ids the shipped drive script must have recorded as passed. A script that
#: quietly lost one asserts less than it used to, and that must not pass.
# `help_corpus`: Help serves the text THIS build was made from (the compiled-in bundle, not the
# loose docs beside the exe). A build that left the bundle out opens Help fine and shows nothing.
GUI_REQUIRED_IDS = ("on_projects", "help_corpus", "expect_clean")

LAYER_VERSION = "version resource"
# "first run" is in the name so a report never shows a bare "gui" that reads as THE test: these
# start the program with nothing configured, which is what a new user has, and say little about
# tokensave or git. The "(configured)" layers are the ones that do.
LAYER_CLI = "cli (first run)"
LAYER_GUI = "gui (first run)"
LAYER_CLI_CONFIGURED = "cli (configured)"
LAYER_GUI_CONFIGURED = "gui (configured)"

#: Expectation ids the configured drive script must have recorded as passed.
GUI_CONFIGURED_REQUIRED_IDS = ("on_projects", "project_listed", "help_corpus", "expect_clean")

#: A config that is valid but points at nothing: what a first-time user has.
CLEAN_CONFIG = {
    "tokensave_exe": "", "template_dir": "", "python_exe": "", "editor_cmd": "code",
    "git_exe": "", "codegraph_exe": "", "search_roots": [], "project_categories": {},
    "user_snippets": [], "auto_commit_after_sync": False,
}

_LOG_TAIL_LINES = 40
_CLOCK_SLACK_S = 2.0


@dataclass
class Judgement:
    ok: bool
    failures: "list[str]" = field(default_factory=list)

    def text(self) -> str:
        return "PASS" if self.ok else "FAIL: " + "; ".join(self.failures)


@dataclass
class LayerResult:
    layer: str
    target: str
    judgement: Judgement
    work_dir: str = ""
    seconds: float = 0.0
    #: Set only by an EXPLICIT request to leave a layer out. `judgement.ok` stays False, so
    #: anything that ignores this flag still fails closed; `layer_ok` is the one place that
    #: reads it.
    skipped: bool = False

    def line(self) -> str:
        if self.skipped:
            return "%-17s %-26s SKIPPED (%s)" % (self.layer, self.target, "; ".join(self.judgement.failures))
        return "%-17s %-26s %s" % (self.layer, self.target, self.judgement.text())


def layer_ok(result: "LayerResult") -> bool:
    """A layer passed, or was explicitly skipped. Nothing else counts."""
    return result.judgement.ok or result.skipped


# ── the drive report ──────────────────────────────────────────────────────

def judge_drive_report(report_path: str, *, exit_code: "int | None", timed_out: bool,
                       launched_at: float, script: str,
                       required_ids: "tuple[str, ...]" = GUI_REQUIRED_IDS,
                       allow_startup_diagnostics: bool = False) -> Judgement:
    """Every way a driven run can be wrong, each its own reason. Nothing passes by default."""
    failures: "list[str]" = []
    if timed_out:
        failures.append("the run did not finish in time")
    if exit_code != 0:
        failures.append("exit status %r, expected 0" % (exit_code,))
    try:
        with open(report_path, encoding="utf-8") as fh:
            report = json.load(fh)
    except FileNotFoundError:
        return Judgement(False, failures + ["no report was written"])
    except (OSError, ValueError):
        return Judgement(False, failures + ["the report could not be read as JSON"])
    if not isinstance(report, dict):
        return Judgement(False, failures + ["the report is not an object"])
    failures += _report_identity(report, launched_at, script)
    failures += _report_findings(report, required_ids, allow_startup_diagnostics)
    return Judgement(not failures, failures)


def _report_identity(report: dict, launched_at: float, script: str) -> "list[str]":
    """Is this report about THIS launch of THIS script, and did it end the normal way?"""
    out: "list[str]" = []
    if report.get("report_schema") != drive_ledger.REPORT_SCHEMA:
        out.append("report schema is %r, expected %r"
                   % (report.get("report_schema"), drive_ledger.REPORT_SCHEMA))
    if report.get("finished_because") != "quit":
        out.append("the run ended because of %r, not by reaching quit" % (report.get("finished_because"),))
    if report.get("passed") is not True:
        out.append("the report says passed=%r" % (report.get("passed"),))
    started, finished = report.get("started_at"), report.get("finished_at")
    if not isinstance(started, (int, float)) or not isinstance(finished, (int, float)):
        out.append("the report does not say when the run started and finished")
    else:
        if started < launched_at - _CLOCK_SLACK_S:
            out.append("the report is stale: its run started before this launch")
        if finished < started:
            out.append("the report finished before it started")
    want = drive_ledger.script_hash(script)
    if want is None:
        out.append("the script could not be read to compare")
    elif report.get("script_hash") != want:
        out.append("the report is for a different version of the script")
    return out


def _first(value) -> str:
    if not isinstance(value, list):
        return "unreadable"
    return "%d, first %s" % (len(value), str(value[0])[:200])


def _report_findings(report: dict, required_ids: "tuple[str, ...]", allow_startup: bool) -> "list[str]":
    out: "list[str]" = []
    expectations = report.get("expectations")
    if not isinstance(expectations, list) or not expectations:
        out.append("the report holds no expectations")
        expectations = []
    for ident in required_ids:
        if not any(isinstance(e, dict) and e.get("id") == ident for e in expectations):
            out.append("required expectation %r never ran" % ident)
    for e in expectations:
        if isinstance(e, dict) and e.get("passed") is not True:
            out.append("expectation %r failed: %s" % (e.get("id"), str(e.get("failure_reason"))[:300]))
    for key, label in (("steps_failed", "failed steps"), ("drive_diagnostics", "diagnostics during the run")):
        if report.get(key) != []:
            out.append("%s: %s" % (label, _first(report.get(key))))
    if report.get("startup_diagnostics") != [] and not allow_startup:
        out.append("startup diagnostics: %s" % _first(report.get("startup_diagnostics")))
    return out


# ── the CLI envelope ──────────────────────────────────────────────────────

def judge_cli_envelope(stdout: str, exit_code: "int | None", *, command: str,
                       expected_version: str, require_ok: bool = True) -> Judgement:
    """A frozen CLI answered in the documented shape, and says it is the expected version."""
    failures: "list[str]" = []
    if exit_code is None:
        failures.append("the process did not finish")
    try:
        envelope = json.loads(stdout)
    except ValueError:
        return Judgement(False, failures + ["stdout is not JSON: %r" % stdout[:120]])
    if not isinstance(envelope, dict):
        return Judgement(False, failures + ["the envelope is not an object"])
    if envelope.get("command") != command:
        failures.append("command is %r, expected %r" % (envelope.get("command"), command))
    if envelope.get("cli_version") != expected_version:
        failures.append("cli_version is %r, expected %r" % (envelope.get("cli_version"), expected_version))
    if require_ok and envelope.get("ok") is not True:
        failures.append("ok is %r (error: %s)" % (envelope.get("ok"), str(envelope.get("error"))[:200]))
    if require_ok and exit_code != 0:
        failures.append("exit status %r, expected 0" % (exit_code,))
    return Judgement(not failures, failures)


# ── the Windows version resource ──────────────────────────────────────────

@dataclass(frozen=True)
class VersionInfo:
    file_version: "tuple[int, int, int, int]"
    product_version: "tuple[int, int, int, int]"
    product_name: str = ""


def read_version_info(path: str) -> "VersionInfo | None":
    """The fixed version fields of a Windows executable, or None when they cannot be read.

    None is "not known", never "matches": the judge treats it as a failure.
    """
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    version = ctypes.WinDLL("version", use_last_error=True)
    size = version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(path, 0, size, buffer):
        return None
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)) or length.value < 52:
        return None
    fixed = list((wintypes.DWORD * 13).from_address(pointer.value))
    file_version, product_version = parse_fixed_info(fixed)
    return VersionInfo(file_version, product_version, _product_name(version, buffer))


def parse_fixed_info(fixed: "list[int]") -> "tuple[tuple[int, int, int, int], tuple[int, int, int, int]]":
    """(file version, product version) from a VS_FIXEDFILEINFO's thirteen DWORDs. Pure.

    Layout: [0] signature, [1] structure version, [2:4] FILE version (MS, LS), [4:6] PRODUCT
    version (MS, LS), then the flag fields. Reading product from [6:8] returned the flags and
    only looked right on an executable whose two versions happen to be equal.
    """
    def quad(ms: int, ls: int) -> "tuple[int, int, int, int]":
        return (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)

    return quad(fixed[2], fixed[3]), quad(fixed[4], fixed[5])


def _product_name(version, buffer) -> str:
    import ctypes
    from ctypes import wintypes

    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if not version.VerQueryValueW(buffer, "\\VarFileInfo\\Translation", ctypes.byref(pointer), ctypes.byref(length)) or length.value < 4:
        return ""
    lang = ctypes.c_uint16.from_address(pointer.value).value
    page = ctypes.c_uint16.from_address(pointer.value + 2).value
    key = "\\StringFileInfo\\%04x%04x\\ProductName" % (lang, page)
    if not version.VerQueryValueW(buffer, key, ctypes.byref(pointer), ctypes.byref(length)) or not length.value:
        return ""
    # NUL-terminated read, not the reported length: a compiler may pad the value, and the
    # length then runs into the next key ("TokenSave Manager" + "FileDescription").
    return ctypes.wstring_at(pointer.value)


def _dotted(parts: "tuple[int, ...]") -> str:
    return ".".join(map(str, parts))


def judge_version_info(info: "VersionInfo | None", expected: str, *, expected_product: str = "") -> Judgement:
    """`expected` is the dotted app version ('2.6.0'); the resource's fourth part must be 0."""
    if info is None:
        return Judgement(False, ["the version resource could not be read (no resource, or not Windows)"])
    want = tuple(int(p) for p in expected.split(".")) + (0,)
    failures = []
    if info.file_version != want:
        failures.append("file version is %s, expected %s" % (_dotted(info.file_version), _dotted(want)))
    if info.product_version != want:
        failures.append("product version is %s, expected %s" % (_dotted(info.product_version), _dotted(want)))
    if expected_product and info.product_name != expected_product:
        failures.append("product name is %r, expected %r" % (info.product_name, expected_product))
    return Judgement(not failures, failures)


# ── launching ─────────────────────────────────────────────────────────────

def _kill_tree(pid: int) -> None:
    """This process and its children, by pid. Never by image name: another copy of the
    app that somebody has open must not be touched."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, check=False,
                       creationflags=CREATE_NO_WINDOW)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _scratch(prefix: str, workdir: "str | None", extra: "dict | None" = None) -> "tuple[str, str]":
    """(folder, scratch config path): a valid first-run config in a folder of its own.

    `extra` is merged over the empty config to make the CONFIGURED variant.
    """
    folder = tempfile.mkdtemp(prefix=prefix, dir=workdir)
    config = os.path.join(folder, "manager-config.json")
    with open(config, "w", encoding="utf-8") as fh:
        json.dump({**CLEAN_CONFIG, **(extra or {})}, fh)
    return folder, config


def _run(argv: "list[str]", *, env: dict, cwd: str, timeout: float, out_dir: str,
         gui: bool) -> "tuple[int | None, bool, str]":
    """Run to completion or kill our own tree. Returns (exit code, timed out, stdout text)."""
    stdout_path = os.path.join(out_dir, "stdout.txt")
    with open(stdout_path, "wb") as out, open(os.path.join(out_dir, "stderr.txt"), "wb") as err:
        process = subprocess.Popen(
            argv, env=env, cwd=cwd, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
            startupinfo=show_no_activate_startupinfo() if gui else None,
            creationflags=CREATE_NO_WINDOW)
        timed_out = False
        try:
            code: "int | None" = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(process.pid)
            try:
                code = process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                code = None
    with open(stdout_path, encoding="utf-8", errors="replace") as fh:
        return code, timed_out, fh.read()


def run_gui(exe: "str | list[str]", script: str, *, timeout: float = 150.0, workdir: "str | None" = None,
            required_ids: "tuple[str, ...]" = GUI_REQUIRED_IDS, layer: str = LAYER_GUI,
            config: "dict | None" = None, script_vars: "dict | None" = None) -> LayerResult:
    """Start the GUI on `script` in isolation and judge its report.

    `exe` is the built executable, or an argv (`[python, "src/app.py"]`) to run the same
    judgement against source, which is how the harness itself is checked.
    """
    argv = [exe] if isinstance(exe, str) else list(exe)
    folder, config_path = _scratch("tsm-smoke-gui-", workdir, config)
    copy = os.path.join(folder, "drive.json")
    with open(script, encoding="utf-8") as fh:
        text = fh.read()
    for name, value in (script_vars or {}).items():
        text = text.replace("<<%s>>" % name, json.dumps(str(value))[1:-1])  # as the inside of a JSON string
    with open(copy, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)  # the report is written beside the script: keep it, and its variables, here
    env = dict(os.environ, TOKENSAVE_MANAGER_DRIVE=copy, TOKENSAVE_MANAGER_CONFIG=config_path)
    launched, began = time.time(), time.monotonic()
    code, timed_out, _ = _run(argv, env=env, cwd=folder, timeout=timeout, out_dir=folder, gui=True)
    judgement = judge_drive_report(os.path.join(folder, "drive.report.json"), exit_code=code,
                                   timed_out=timed_out, launched_at=launched, script=copy,
                                   required_ids=required_ids)
    return LayerResult(layer, os.path.basename(argv[-1]), judgement, folder, time.monotonic() - began)


def run_cli(exe: "str | list[str]", expected_version: str, *, timeout: float = 90.0,
            workdir: "str | None" = None) -> "list[LayerResult]":
    """`commands` and `status` from a working directory that is not the exe's own."""
    prefix = [exe] if isinstance(exe, str) else list(exe)
    folder, config = _scratch("tsm-smoke-cli-", workdir)
    project = os.path.join(folder, "project")
    os.mkdir(project)
    env = dict(os.environ, TOKENSAVE_MANAGER_CONFIG=config)
    results = []
    for label, args, require_ok in (("commands", ["commands", "--json"], True),
                                    ("status", ["status", "--project", project, "--json"], False)):
        began = time.monotonic()
        sub = os.path.join(folder, label)
        os.mkdir(sub)
        code, timed_out, stdout = _run([*prefix, *args], env=env, cwd=sub, timeout=timeout, out_dir=sub, gui=False)
        judgement = judge_cli_envelope(stdout, None if timed_out else code, command=label,
                                       expected_version=expected_version, require_ok=require_ok)
        results.append(LayerResult(LAYER_CLI, "%s %s" % (os.path.basename(prefix[-1]), label), judgement, sub,
                                   time.monotonic() - began))
    return results


def check_versions(dist: str, expected: str, *, expected_product: str = "") -> "list[LayerResult]":
    results = []
    for name in ALL_EXES:
        path = os.path.join(dist, name)
        if not os.path.isfile(path):
            results.append(LayerResult(LAYER_VERSION, name, Judgement(False, ["%s is not there" % path])))
            continue
        results.append(LayerResult(LAYER_VERSION, name, judge_version_info(read_version_info(path), expected,
                                                                            expected_product=expected_product)))
    return results


def smoke_dist(dist: str, expected_version: str, script: str, *, workdir: "str | None" = None,
               expected_product: str = "", gui_timeout: float = 150.0,
               configured_script: "str | None" = None, tokensave_exe: str = "",
               skip_tokensave: bool = False) -> "list[LayerResult]":
    """Every layer for one folder of built executables (dist\\ or an extracted release).

    The configured layers need `configured_script`; they run when tokensave and git are found,
    fail when they are not, and are skipped (visibly, as SKIPPED) only on `skip_tokensave`.
    """
    results = _first_run_layers(dist, expected_version, script, workdir, expected_product, gui_timeout)
    if configured_script:
        results += configured_layers(dist, expected_version, configured_script, workdir=workdir,
                                     gui_timeout=gui_timeout, tokensave_exe=tokensave_exe, skip=skip_tokensave)
    return results


def _first_run_layers(dist: str, expected_version: str, script: str, workdir: "str | None",
                      expected_product: str, gui_timeout: float) -> "list[LayerResult]":
    results = check_versions(dist, expected_version, expected_product=expected_product)
    cli = os.path.join(dist, CLI_EXE)
    gui = os.path.join(dist, GUI_EXE)
    # A missing exe is a failed layer, not a smaller run.
    if os.path.isfile(cli):
        results += run_cli(cli, expected_version, workdir=workdir)
    else:
        results.append(LayerResult(LAYER_CLI, CLI_EXE, Judgement(False, ["%s is not there" % cli])))
    if os.path.isfile(gui):
        results.append(run_gui(gui, script, workdir=workdir, timeout=gui_timeout))
    else:
        results.append(LayerResult(LAYER_GUI, GUI_EXE, Judgement(False, ["%s is not there" % gui])))
    return results


def evidence_tail(work_dir: str) -> str:
    """The last lines of what a failed run printed, so a failure is more than a verdict."""
    chunks = []
    for name in ("stderr.txt", "stdout.txt"):
        path = os.path.join(work_dir, name)
        if os.path.isfile(path) and os.path.getsize(path):
            with open(path, encoding="utf-8", errors="replace") as fh:
                tail = fh.read().splitlines()[-_LOG_TAIL_LINES:]
            chunks.append("--- %s (last %d lines)\n%s" % (name, len(tail), "\n".join(tail)))
    return "\n".join(chunks)


# ── the CONFIGURED layers ─────────────────────────────────────────────────

@dataclass(frozen=True)
class Tools:
    tokensave_exe: str = ""
    git_exe: str = ""

    def missing(self) -> "list[str]":
        return [name for name, path in (("tokensave", self.tokensave_exe), ("git", self.git_exe)) if not path]


class SetupError(RuntimeError):
    """The scratch project could not be made, so the configured layers cannot run."""


def find_tools(tokensave_exe: str = "") -> Tools:
    """The real tokensave and git, from an explicit path, `TOKENSAVE_EXE`, or PATH."""
    ts = tokensave_exe or os.environ.get("TOKENSAVE_EXE", "") or shutil.which("tokensave") or ""
    return Tools(ts if ts and os.path.isfile(ts) else "", shutil.which("git") or "")


def _tool(argv: "list[str]", cwd: str, timeout: float = 180.0) -> None:
    done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                          creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        raise SetupError("%s failed (exit %d): %s" % (" ".join(argv[:3]), done.returncode,
                                                      (done.stderr or done.stdout).strip()[-300:]))


def make_project(parent: str, tools: Tools) -> str:
    """A git repository with one commit that `tokensave init` has really indexed."""
    project = os.path.join(parent, "project")
    os.makedirs(project)
    with open(os.path.join(project, "hello.py"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("def hello():\n    return 'hi'\n")
    ident = ["-c", "user.name=smoke", "-c", "user.email=smoke@example.invalid"]
    _tool([tools.git_exe, *ident, "init", "-q", "-b", "main"], project)
    _tool([tools.git_exe, *ident, "add", "-A"], project)
    _tool([tools.git_exe, *ident, "commit", "-q", "-m", "smoke"], project)
    _tool([tools.tokensave_exe, "init", project], parent)
    if not os.path.isdir(os.path.join(project, ".tokensave")):
        raise SetupError("tokensave init produced no .tokensave in %s" % project)
    return project


def judge_configured_status(stdout: str, exit_code: "int | None", *, expected_version: str) -> Judgement:
    """`status` on an indexed git project must SHOW tokensave and git, not just be well-formed.

    A first-run envelope is well-formed and says nothing (every field null). Here that is a
    failure: the program was given a real tokensave and a real repository and must have used them.
    """
    base = judge_cli_envelope(stdout, exit_code, command="status", expected_version=expected_version)
    if not base.ok:
        return base
    data = json.loads(stdout).get("data") or {}
    failures = []
    if (data.get("tokensave") or {}).get("indexed") is not True:
        failures.append("tokensave.indexed is %r: the program did not see the index" % ((data.get("tokensave") or {}).get("indexed"),))
    git = data.get("git") or {}
    if not git.get("branch"):
        failures.append("git.branch is %r: the program did not read the repository" % (git.get("branch"),))
    return Judgement(not failures, failures)


def _unavailable(layer: str, target: str, reason: str, skip: bool) -> "list[LayerResult]":
    return [LayerResult(layer, target, Judgement(False, [reason]), skipped=skip)]


def configured_layers(dist: str, expected_version: str, script: str, *, workdir: "str | None" = None,
                      gui_timeout: float = 150.0, tokensave_exe: str = "", skip: bool = False) -> "list[LayerResult]":
    """CLI `status` and the GUI on a project that really is indexed, with real tools configured."""
    tools = find_tools(tokensave_exe)
    cli, gui = os.path.join(dist, CLI_EXE), os.path.join(dist, GUI_EXE)
    if tools.missing():
        why = "%s not found (set TOKENSAVE_EXE or put it on PATH); the program cannot be shown tokensave or git" % " and ".join(tools.missing())
        return (_unavailable(LAYER_CLI_CONFIGURED, CLI_EXE, why, skip)
                + _unavailable(LAYER_GUI_CONFIGURED, GUI_EXE, why, skip))
    folder = tempfile.mkdtemp(prefix="tsm-smoke-proj-", dir=workdir)
    try:
        project = make_project(os.path.join(folder, "roots"), tools)
    except (SetupError, OSError, subprocess.SubprocessError) as exc:
        why = "could not make the indexed scratch project: %s" % exc
        return (_unavailable(LAYER_CLI_CONFIGURED, CLI_EXE, why, False)
                + _unavailable(LAYER_GUI_CONFIGURED, GUI_EXE, why, False))
    config = {"tokensave_exe": tools.tokensave_exe.replace("\\", "/"), "git_exe": tools.git_exe.replace("\\", "/"),
              "template_dir": os.path.join(dist, "templates").replace("\\", "/"),
              "search_roots": [os.path.dirname(project).replace("\\", "/")]}
    results = []
    results += [_configured_cli(cli, expected_version, project, config, workdir)] if os.path.isfile(cli) else \
        _unavailable(LAYER_CLI_CONFIGURED, CLI_EXE, "%s is not there" % cli, False)
    if os.path.isfile(gui):
        results.append(run_gui(gui, script, workdir=workdir, timeout=gui_timeout, layer=LAYER_GUI_CONFIGURED,
                               required_ids=GUI_CONFIGURED_REQUIRED_IDS, config=config,
                               script_vars={"PROJECT": project.replace("\\", "/")}))
    else:
        results += _unavailable(LAYER_GUI_CONFIGURED, GUI_EXE, "%s is not there" % gui, False)
    return results


def _configured_cli(exe: "str | list[str]", expected_version: str, project: str, config: dict,
                    workdir: "str | None") -> LayerResult:
    prefix = [exe] if isinstance(exe, str) else list(exe)
    folder, config_path = _scratch("tsm-smoke-cli-cfg-", workdir, config)
    env = dict(os.environ, TOKENSAVE_MANAGER_CONFIG=config_path)
    began = time.monotonic()
    code, timed_out, stdout = _run([*prefix, "status", "--project", project, "--json"], env=env, cwd=folder,
                                   timeout=90.0, out_dir=folder, gui=False)
    judgement = judge_configured_status(stdout, None if timed_out else code, expected_version=expected_version)
    return LayerResult(LAYER_CLI_CONFIGURED, "%s status" % os.path.basename(prefix[-1]), judgement, folder,
                       time.monotonic() - began)
