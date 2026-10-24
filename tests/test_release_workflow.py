"""`.github/workflows/release.yml` is draft-only, minimally privileged and pinned.

Each claim is the one a quiet edit would break while the workflow still ran green:

* nothing can PUBLISH (`--draft` on the only `gh release create`);
* write access exists on exactly one job, and only for a tag push;
* every third-party action is a full commit SHA, so a moved tag cannot change what runs;
* no `${{ }}` expression is spliced into a `run:` script (script injection);
* the build the workflow runs is the one the Manager ships, with the real tokensave pinned,
  because a smoke with no tokensave cannot see a missing tokensave path;
* a pull request that changes how a release is made runs the dry run.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PATH = ROOT / ".github" / "workflows" / "release.yml"
TEXT = PATH.read_text(encoding="utf-8")
WF = yaml.safe_load(TEXT)
ON = WF[True]            # YAML 1.1 reads the key `on` as the boolean True
JOBS = WF["jobs"]


def _steps(job):
    return JOBS[job]["steps"]


def _runs(job):
    return [(s.get("name", ""), s["run"]) for s in _steps(job) if "run" in s]


def test_the_workflow_has_no_permissions_by_default():
    assert WF["permissions"] == {}


def test_only_the_draft_job_can_write_contents_and_only_on_a_tag_push():
    writers = [n for n, j in JOBS.items() if j.get("permissions", {}).get("contents") == "write"]
    assert writers == ["draft"]
    assert "github.ref_type == 'tag'" in JOBS["draft"]["if"] and "push" in JOBS["draft"]["if"]
    assert JOBS["build"]["permissions"]["contents"] == "read"


def test_the_only_release_that_is_created_is_a_draft_of_a_verified_tag():
    creates = [r for _, r in _runs("draft") + _runs("build") if "gh release create" in r]
    assert len(creates) == 1
    assert "--draft" in creates[0] and "--verify-tag" in creates[0]
    assert "--latest" not in TEXT and "gh release edit" not in TEXT and "--prerelease=false" not in TEXT


def test_the_draft_job_refuses_an_existing_release_before_creating_one():
    names = [s.get("name", "") for s in _steps("draft")]
    refuse = next(i for i, n in enumerate(names) if n.startswith("Refuse to touch"))
    create = next(i for i, n in enumerate(names) if n.startswith("Create the draft"))
    assert refuse < create
    assert "gh release view" in dict(_runs("draft"))[names[refuse]]


def test_every_third_party_action_is_pinned_to_a_full_commit_sha():
    uses = [s["uses"] for j in JOBS.values() for s in j["steps"] if "uses" in s]
    assert uses
    for u in uses:
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", u), "not pinned to a SHA: " + u
    for line in TEXT.splitlines():
        if "uses:" in line:
            assert "#" in line, "a pin needs its tag in a comment: " + line.strip()


def test_no_expression_is_spliced_into_a_run_script():
    for job in JOBS:
        for name, script in _runs(job):
            assert "${{" not in script, "%s / %s interpolates an expression into a script" % (job, name)


def test_the_tag_and_changelog_are_checked_before_the_long_build():
    names = [s.get("name", "") for s in _steps("build")]
    build = next(i for i, n in enumerate(names) if n.startswith("Build, smoke-test"))
    for prefix in ("The tag must name this version", "The changelog must have this version"):
        assert next(i for i, n in enumerate(names) if n.startswith(prefix)) < build
    tag_step = next(s for s in _steps("build") if s.get("name", "").startswith("The tag must"))
    assert tag_step["if"] == "github.ref_type == 'tag'"
    assert tag_step["env"]["TAG"] == "${{ github.ref_name }}"


def test_it_runs_the_release_build_the_manager_ships_and_checks_its_checksums():
    scripts = dict(_runs("build"))
    assert "./build.ps1 -Release" in scripts["Build, smoke-test and package"]
    assert "sha256sum -c SHA256SUMS.txt" in scripts["Check the checksums of what was built"]
    assert "sha256sum -c SHA256SUMS.txt" in dict(_runs("draft"))["Check the checksums again"]


def test_the_smoke_has_a_real_tokensave_pinned_by_version_and_hash():
    scripts = dict(_runs("build"))
    install = scripts["Install tokensave 7.16.0"]
    assert re.search(r'\$expected = "[0-9a-f]{64}"', install)
    assert "Get-FileHash" in install and "throw" in install
    assert "download/v7.16.0/" in install and "TOKENSAVE_EXE=" in install


def test_the_python_build_tools_are_pinned_to_versions():
    install = dict(_runs("build"))["Install the build tools"]
    for tool in ("nuitka", "ordered-set", "zstandard", "pillow", "pystray"):
        assert re.search(r"\b%s==\d" % tool, install), tool + " is not pinned"


def test_attestation_is_for_a_tag_push_only():
    step = next(s for s in _steps("build") if "attest-build-provenance" in s.get("uses", ""))
    assert "tag" in step["if"] and "push" in step["if"]


def test_a_tag_push_and_a_release_path_pull_request_both_trigger_it():
    assert ON["push"]["tags"] == ["v[0-9]+.[0-9]+.[0-9]+"]
    paths = ON["pull_request"]["paths"]
    for needed in (".github/workflows/release.yml", "build.ps1", "src/constants.py",
                   "scripts/release_info.py", "scripts/make_release_zip.py", "scripts/frozen_smoke.py"):
        assert needed in paths, needed
    assert "workflow_dispatch" in ON


def test_every_path_the_pull_request_filter_names_exists():
    for pattern in ON["pull_request"]["paths"]:
        if "*" in pattern:
            assert list(ROOT.glob(pattern)), pattern
        else:
            assert (ROOT / pattern).exists(), pattern


def test_a_tag_build_is_never_cancelled_half_way():
    assert "pull_request" in WF["concurrency"]["cancel-in-progress"]
    assert "cancel-in-progress: true" not in TEXT


def test_the_workflow_builds_the_installer_and_ships_it_with_its_provenance():
    assert "./build.ps1 -Release -Installer" in dict(_runs("build"))["Build, smoke-test and package"]
    setup = "release/Tokensave-Manager-v*-windows-setup.exe"
    keep = next(s for s in _steps("build") if "upload-artifact" in s.get("uses", ""))
    assert setup in keep["with"]["path"]
    attest = next(s for s in _steps("build") if "attest-build-provenance" in s.get("uses", ""))
    assert setup in attest["with"]["subject-path"]
    assert setup in dict(_runs("draft"))["Create the draft release"]


def test_inno_setup_is_pinned_by_version_and_hash_and_found_by_path():
    install = dict(_runs("build"))["Install Inno Setup 6.7.3"]
    assert re.search(r'\$expected = "[0-9a-f]{64}"', install) and "Get-FileHash" in install and "throw" in install
    assert "ISCC=" in install and "6_7_3" in install


def test_a_change_to_the_installer_triggers_the_dry_run():
    paths = ON["pull_request"]["paths"]
    for needed in ("packaging/**", "scripts/make_installer.py", "src/helpers/installer.py", "src/helpers/installer_drive.py"):
        assert needed in paths, needed
