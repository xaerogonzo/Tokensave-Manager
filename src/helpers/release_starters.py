"""release_starters -- opt-in release files a project can adopt, rendered from templates/release/.

These are NOT lessons. `lessons_delivery` puts the same read-this-first text into every
wired project; a starter is code or configuration (a release workflow, a zip builder, an
installer script) that only a project which actually ships a Windows build wants. So:

  * nothing here runs during wiring, sync or Repair; a person (or an agent they asked)
    adopts a named starter into one project;
  * the inventory (`STARTERS`) is explicit, with a fixed destination for each, no glob;
  * **adoption never overwrites**: an existing destination is refused untouched, and each
    starter is its own judgment (one refusal does not stop the others);
  * the output is yours. There is no managed-copy header and no later sync, because a
    release script is edited to fit the project the day it is adopted.

Templates use `<<NAME>>` tokens, not `{{NAME}}`: GitHub Actions expressions (`${{ }}`) and
Inno Setup's escaped brace (`{{`) both contain braces. Rendering FAILS CLOSED: a token with
no value, a value for a token no template uses, a value that contains `<<`, or a result that
does not pass its kind's check (Python compiles; a workflow is draft-only, SHA-pinned and
splices no expression into a shell script) is refused with the reason, and nothing is written.

Imports nothing from the UI. Standard library only.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Callable

TEMPLATE_SUBDIR = "release"

TOKEN = re.compile(r"<<([A-Z][A-Z0-9_]*)>>")

KIND_PYTHON = "python"
KIND_WORKFLOW = "workflow"
KIND_INNO = "inno"


@dataclass(frozen=True)
class Starter:
    name: str
    #: Relative to `template_dir`, POSIX-style.
    source_rel: str
    #: Relative to the project root, POSIX-style. Fixed, never user input.
    dest_rel: str
    kind: str
    summary: str


STARTERS: "tuple[Starter, ...]" = (
    Starter("release-workflow", "release/release.yml.tmpl", ".github/workflows/release.yml",
            KIND_WORKFLOW, "tag-triggered, draft-only release build with provenance attestation"),
    Starter("release-info", "release/release_info.py.tmpl", "tools/release_info.py",
            KIND_PYTHON, "tag must equal the version file; notes are the version's CHANGELOG section"),
    Starter("release-zip", "release/make_release_zip.py.tmpl", "tools/make_release_zip.py",
            KIND_PYTHON, "versioned-folder zip, inventory check, extracted-copy smoke test, SHA256SUMS"),
    Starter("frozen-smoke", "release/frozen_smoke.py.tmpl", "tools/frozen_smoke.py",
            KIND_PYTHON, "launch the built app on a drive script and judge its report, fail closed"),
    Starter("installer", "release/installer.iss.tmpl", "packaging/installer.iss",
            KIND_INNO, "Inno Setup: per-user default, upgrade wipes the runtime, uninstall keeps data"),
)


@dataclass(frozen=True)
class TokenSpec:
    help: str
    #: None = the caller must supply it. Otherwise computed from the values given so far.
    default: "Callable[[dict], str] | None" = None
    #: Filled with a fresh GUID when absent; supplying one is allowed.
    generated: bool = False


def _py_tuple(items: "tuple[str, ...]") -> str:
    return repr(tuple(items))


TOKENS: "dict[str, TokenSpec]" = {
    "PRODUCT_NAME": TokenSpec("display name, e.g. 'OpenChem Studio'"),
    "ARTIFACT_PREFIX": TokenSpec("file prefix with no spaces, e.g. 'OpenChemStudio'"),
    "REPO": TokenSpec("GitHub owner/name"),
    "APP_FOLDER": TokenSpec("folder under dist/ holding the build", lambda v: v["ARTIFACT_PREFIX"]),
    "EXE_NAME": TokenSpec("the main executable", lambda v: v["ARTIFACT_PREFIX"] + ".exe"),
    "PLATFORM_TAG": TokenSpec("suffix in artifact names", lambda v: "windows-x64"),
    "PUBLISHER": TokenSpec("installer publisher", lambda v: v["PRODUCT_NAME"]),
    "PROJECT_URL": TokenSpec("project home", lambda v: "https://github.com/" + v["REPO"]),
    "COPYRIGHT": TokenSpec("installer copyright line", lambda v: "Copyright (C) " + v["PUBLISHER"]),
    "TEMP_PREFIX": TokenSpec("prefix of temp folders the smoke test makes", lambda v: v["ARTIFACT_PREFIX"].lower()),
    "ENV_PREFIX": TokenSpec("prefix of the app's DRIVE/DATA_ROOT variables", lambda v: v["ARTIFACT_PREFIX"].upper()),
    "DATA_FOLDER_NAME": TokenSpec("the app's per-user data folder name", lambda v: v["ARTIFACT_PREFIX"]),
    "RUNTIME_DIR": TokenSpec("program subfolder wiped on upgrade", lambda v: "_internal"),
    "VERSION_FILE": TokenSpec("file holding the one version number", lambda v: "pyproject.toml"),
    "VERSION_REGEX": TokenSpec("regex with ONE group capturing it",
                               lambda v: r'(?m)^version\s*=\s*"(\d+\.\d+\.\d+)"'),
    "SMOKE_SCRIPT": TokenSpec("drive script the smoke test runs", lambda v: "tools/frozen_smoke.json"),
    "REQUIRED_ASSERTIONS": TokenSpec("assertion names the report must contain (Python tuple)",
                                     lambda v: _py_tuple(("expect_clean",))),
    "REQUIRED_FILES": TokenSpec("files the archive must hold (Python tuple)",
                                lambda v: _py_tuple((v["EXE_NAME"], "README.md", "LICENSE"))),
    "FORBIDDEN_PATHS": TokenSpec("loose paths the archive must not hold (Python tuple)", lambda v: "()"),
    "BUILD_COMMAND": TokenSpec("what builds, smoke-tests and packages", lambda v: "./build.ps1 -Release -Installer"),
    "SETUP_COMMAND": TokenSpec("what installs dependencies on the runner", lambda v: "uv sync --frozen"),
    "APP_ID_GUID": TokenSpec("installer identity; never reuse another app's", generated=True),
    "TEST_APP_ID_GUID": TokenSpec("the test build's identity, so it cannot clobber a real install", generated=True),
}

_GUID = re.compile(r"^[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}$")


def tokens_in(text: str) -> "set[str]":
    return set(TOKEN.findall(text))


def resolve_values(supplied: "dict[str, str]",
                   new_guid: "Callable[[], str]" = lambda: str(uuid.uuid4()).upper()):
    """(complete values, problems). A token nobody supplied and nothing can default is a problem."""
    problems = [f"unknown token {k!r}" for k in supplied if k not in TOKENS]
    problems += [f"{k}: a value may not contain '<<'" for k, v in supplied.items() if "<<" in str(v)]
    values = {k: str(v) for k, v in supplied.items() if k in TOKENS}
    for name, spec in TOKENS.items():
        if name in values:
            continue
        if spec.generated:
            values[name] = new_guid()
        elif spec.default is not None:
            try:
                values[name] = spec.default(values)
            except KeyError as missing:
                problems.append(f"{name}: needs {missing.args[0]}")
        else:
            problems.append(f"{name} is required ({spec.help})")
    for guid in ("APP_ID_GUID", "TEST_APP_ID_GUID"):
        if guid in values and not _GUID.match(values[guid]):
            problems.append(f"{guid} must be an upper-case GUID like 00000000-0000-0000-0000-000000000000")
    if values.get("APP_ID_GUID") and values.get("APP_ID_GUID") == values.get("TEST_APP_ID_GUID"):
        problems.append("APP_ID_GUID and TEST_APP_ID_GUID must differ")
    return values, problems


def render(text: str, values: "dict[str, str]") -> "tuple[str, list[str]]":
    """Substitute every token. Tokens with no value are reported and left in place."""
    missing = sorted(t for t in tokens_in(text) if t not in values)
    out = TOKEN.sub(lambda m: values.get(m.group(1), m.group(0)), text)
    return out, [f"template uses {t!r} but no value was resolved" for t in missing]


# ── what a rendered file must satisfy ────────────────────────────────────

_USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)")
_FULL_SHA = re.compile(r"@[0-9a-f]{40}(\s|$)")


def workflow_problems(text: str) -> "list[str]":
    """The release workflow's non-negotiables, read from its text (stdlib only: no YAML parser).

    Draft only, no default permissions, every action pinned to a full commit SHA, and no
    `${{ }}` expression inside a `run:` block (a value spliced into a script is injection).
    """
    problems: list[str] = []
    lines = text.splitlines()
    if not re.search(r"(?m)^permissions:\s*\{\}\s*$", text):
        problems.append("top-level 'permissions: {}' is missing")
    if "--draft" not in text:
        problems.append("the release is not created with --draft")
    if re.search(r"(?m)^\s*(?:run:\s*)?.*gh release (?:publish|edit)\b", text):
        problems.append("the workflow publishes a release; it must only draft")
    for number, line in enumerate(lines, 1):
        used = _USES.match(line)
        if used and not used.group(1).startswith("./") and not _FULL_SHA.search(line):
            problems.append(f"line {number}: action {used.group(1)!r} is not pinned to a full commit SHA")
    problems += _expressions_in_run_blocks(lines)
    return problems


def _expressions_in_run_blocks(lines: "list[str]") -> "list[str]":
    problems: list[str] = []
    block_indent: "int | None" = None
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if block_indent is not None:
            if stripped and indent <= block_indent:
                block_indent = None
            elif "${{" in line:
                problems.append(f"line {number}: an expression is spliced into a run: script; pass it through env:")
                continue
        run = re.match(r"^(\s*)(?:-\s+)?run:\s*(.*)$", line)
        if run:
            inline = run.group(2)
            if inline and inline[0] not in "|>":
                if "${{" in inline:
                    problems.append(f"line {number}: an expression is spliced into a run: command; pass it through env:")
            else:
                block_indent = len(run.group(1)) + (2 if line.lstrip().startswith("-") else 0)
    return problems


def inno_problems(text: str) -> "list[str]":
    problems = [f"missing section {s}" for s in ("[Setup]", "[Files]", "[Code]") if s not in text]
    if "PrivilegesRequired=lowest" not in text:
        problems.append("the install must default to per-user (PrivilegesRequired=lowest)")
    if re.search(r"(?m)^\s*(Settings|Data)Box\.Checked\s*:=\s*True", text):
        problems.append("an uninstall data-removal box defaults to ticked; the default must be keep")
    return problems


def python_problems(name: str, text: str) -> "list[str]":
    try:
        compile(text, name, "exec")
    except SyntaxError as exc:
        return [f"{name} does not compile: {exc.msg} (line {exc.lineno})"]
    return []


def check_rendered(starter: Starter, text: str) -> "list[str]":
    if starter.kind == KIND_PYTHON:
        return python_problems(starter.dest_rel, text)
    if starter.kind == KIND_WORKFLOW:
        return workflow_problems(text)
    return inno_problems(text)


# ── adoption ─────────────────────────────────────────────────────────────

OUT_WRITTEN = "written"
OUT_WOULD_WRITE = "would_write"
OUT_EXISTS = "refused_exists"
OUT_INVALID = "refused_invalid"
OUT_FAILED = "failed"


@dataclass(frozen=True)
class StarterOutcome:
    name: str
    dest_rel: str
    outcome: str
    reasons: "tuple[str, ...]" = field(default_factory=tuple)


def by_name(name: str) -> "Starter | None":
    return next((s for s in STARTERS if s.name == name), None)


def _read(template_dir: str, rel: str) -> str:
    with open(os.path.join(template_dir, *rel.split("/")), encoding="utf-8", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


def _inside(root: str, path: str) -> bool:
    return os.path.commonpath([os.path.realpath(root), os.path.realpath(path)]) == os.path.realpath(root)


def adopt(project_root: str, template_dir: str, names: "list[str]", supplied: "dict[str, str]",
          *, dry_run: bool = False,
          new_guid: "Callable[[], str]" = lambda: str(uuid.uuid4()).upper()) -> "tuple[StarterOutcome, ...]":
    """Render and write the named starters. Per artifact, never a transaction; never overwrites."""
    values, value_problems = resolve_values(supplied, new_guid)
    out = []
    for name in names:
        starter = by_name(name)
        if starter is None:
            out.append(StarterOutcome(name, "", OUT_INVALID, (f"no starter named {name!r}",)))
            continue
        out.append(_adopt_one(project_root, template_dir, starter, values, value_problems, dry_run))
    return tuple(out)


def _adopt_one(root: str, template_dir: str, starter: Starter, values: dict,
               value_problems: "list[str]", dry_run: bool) -> StarterOutcome:
    try:
        source = _read(template_dir, starter.source_rel)
    except OSError as exc:
        return StarterOutcome(starter.name, starter.dest_rel, OUT_FAILED, (f"cannot read the template: {exc}",))
    needed = tokens_in(source)
    problems = [p for p in value_problems if not _problem_irrelevant(p, needed)]
    text, missing = render(source, values)
    problems += missing + check_rendered(starter, text)
    if problems:
        return StarterOutcome(starter.name, starter.dest_rel, OUT_INVALID, tuple(problems))
    dest = os.path.join(root, *starter.dest_rel.split("/"))
    if os.path.lexists(dest):
        return StarterOutcome(starter.name, starter.dest_rel, OUT_EXISTS, (f"{starter.dest_rel} already exists; left untouched",))
    if not _inside(root, os.path.dirname(dest)):
        return StarterOutcome(starter.name, starter.dest_rel, OUT_INVALID, ("the destination resolves outside the project",))
    if dry_run:
        return StarterOutcome(starter.name, starter.dest_rel, OUT_WOULD_WRITE)
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except FileExistsError:
        return StarterOutcome(starter.name, starter.dest_rel, OUT_EXISTS, (f"{starter.dest_rel} appeared meanwhile; left untouched",))
    except OSError as exc:
        return StarterOutcome(starter.name, starter.dest_rel, OUT_FAILED, (str(exc),))
    return StarterOutcome(starter.name, starter.dest_rel, OUT_WRITTEN)


def _problem_irrelevant(problem: str, needed: "set[str]") -> bool:
    """A 'required' complaint about a token this template never uses should not block it."""
    token = problem.split(" ", 1)[0].rstrip(":")
    return token in TOKENS and token not in needed
