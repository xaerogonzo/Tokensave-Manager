"""Doctor's index-scope rule: tracked vendor/ code the graph's exclude hides.

The matcher cases are the MEASURED behaviour of tokensave 7.12.1 (a three-file
probe run through `sync --force` once per pattern), not remembered globbing.
If tokensave changes what a pattern means, these are the tests that should be
re-measured, not edited to pass.
"""
import json
import os
import shutil
import subprocess

import pytest

from helpers import index_scope as sc
from helpers.doctor_rules import audit_index_scope

GIT = shutil.which("git") or ""
needs_git = pytest.mark.skipif(not GIT, reason="git not installed")

ROOT_V = "vendor/rv.py"
NESTED_V = "src/pkg/vendor/nv.py"
CORE = "src/pkg/core/c.py"


# -- matcher: measured on tokensave 7.12.1 ---------------------------------

@pytest.mark.parametrize("pattern, root, nested, core", [
    ("vendor/**",         True,  False, False),   # anchored: root only
    ("**/vendor/**",      True,  True,  False),   # any depth (today's default)
    ("src/pkg/vendor/**", False, True,  False),   # that path only
])
def test_matcher_agrees_with_what_tokensave_indexed(pattern, root, nested, core):
    assert sc.hides(pattern, ROOT_V) is root
    assert sc.hides(pattern, NESTED_V) is nested
    assert sc.hides(pattern, CORE) is core


@pytest.mark.parametrize("pattern", [
    "vendor",            # bare name: excluded SOMETHING when measured; which is unknown
    "vendor/",           # trailing slash: unmeasured
    "vendor/*",          # wildcard segment
    "**/vend*/**",
    "src/**/vendor/**",  # middle ** unmeasured
    "**/*.min.*",
    "/vendor/**",
])
def test_a_pattern_outside_the_measured_forms_is_not_interpreted(pattern):
    """None, never False: 'we cannot tell' must not read as 'does not hide'."""
    assert sc.hides(pattern, ROOT_V) is None
    assert sc.hides(pattern, NESTED_V) is None


def test_vendor_dir_of_names_the_first_vendor_segment():
    assert sc.vendor_dir_of("src/openchem/vendor/naming/x.py") == \
        "src/openchem/vendor"
    assert sc.vendor_dir_of("vendor/rv.py") == "vendor"
    assert sc.vendor_dir_of("src/core/c.py") == ""


# -- scan + audit against a real repository --------------------------------

def _repo(tmp_path, exclude, files=(ROOT_V, NESTED_V, CORE), tokensave=True,
          git=True):
    proj = str(tmp_path / "proj")
    for rel in files:
        path = os.path.join(proj, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")
    os.makedirs(proj, exist_ok=True)
    if tokensave:
        os.makedirs(os.path.join(proj, ".tokensave"), exist_ok=True)
        with open(os.path.join(proj, ".tokensave", "config.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"exclude": exclude, "include": []}, fh)
    if git:
        run = lambda *a: subprocess.run(   # noqa: E731
            [GIT, "-C", proj, "-c", "user.name=t", "-c", "user.email=t@t",
             *a], check=True, capture_output=True)
        run("init", "-q")
        run("add", "--", *sorted({rel.split("/")[0] for rel in files}))
        run("commit", "-qm", "x")
    return proj


@needs_git
def test_todays_default_hides_both_vendor_dirs(tmp_path):
    proj = _repo(tmp_path, ["**/vendor/**", "**/*.min.*"])
    rep = sc.scan(proj, GIT)
    assert rep.state == sc.HIDES
    assert sorted(rep.hidden["**/vendor/**"]) == sorted([ROOT_V, NESTED_V])
    assert rep.tracked_vendor_files == 2


@needs_git
def test_the_older_anchored_default_hides_only_the_root_vendor(tmp_path):
    """Why this repo's own `vendor/**` would not have hidden a nested engine."""
    rep = sc.scan(_repo(tmp_path, ["vendor/**"]), GIT)
    assert rep.state == sc.HIDES
    assert rep.hidden == {"vendor/**": [ROOT_V]}


@needs_git
def test_no_vendor_pattern_means_clear_and_silent(tmp_path):
    proj = _repo(tmp_path, ["node_modules/**"])
    assert sc.scan(proj, GIT).state == sc.CLEAR
    assert audit_index_scope(proj, GIT) == []


@needs_git
def test_untracked_vendor_is_not_reported(tmp_path):
    """The rule is about TRACKED code: an untracked vendor dir is git's call."""
    proj = _repo(tmp_path, ["**/vendor/**"], files=(CORE,))
    os.makedirs(os.path.join(proj, "vendor"))
    with open(os.path.join(proj, "vendor", "u.py"), "w") as fh:
        fh.write("y = 2\n")
    assert audit_index_scope(proj, GIT) == []


@needs_git
def test_an_uninterpreted_vendor_pattern_is_unknown_never_clean(tmp_path):
    proj = _repo(tmp_path, ["vendor"])
    rep = sc.scan(proj, GIT)
    assert rep.state == sc.UNKNOWN
    joined = " ".join(audit_index_scope(proj, GIT))
    assert "unknown, not clean" in joined


@needs_git
def test_include_is_not_credited_with_rescuing_a_file(tmp_path):
    """Measured: `include` did not override an exclude on 7.12.1."""
    proj = _repo(tmp_path, ["**/vendor/**"])
    cfg = os.path.join(proj, ".tokensave", "config.json")
    with open(cfg, "w", encoding="utf-8") as fh:
        json.dump({"exclude": ["**/vendor/**"],
                   "include": ["src/pkg/vendor/**"]}, fh)
    assert NESTED_V in sc.scan(proj, GIT).hidden["**/vendor/**"]


@needs_git
def test_audit_states_the_population_and_the_remedy(tmp_path):
    proj = _repo(tmp_path, ["**/vendor/**"])
    joined = " ".join(audit_index_scope(proj, GIT))
    assert "hides 2 tracked file(s)" in joined
    assert "src/pkg/vendor/ (1)" in joined
    assert "third-party" in joined                # never asserts it is wrong
    assert "sync --force" in joined
    assert "does not override" in joined


def test_not_a_tokensave_project_is_silent(tmp_path):
    proj = _repo(tmp_path, [], tokensave=False, git=False)
    assert sc.scan(proj, GIT).state == sc.NOT_APPLICABLE
    assert audit_index_scope(proj, GIT) == []


def test_no_git_directory_is_silent(tmp_path):
    proj = _repo(tmp_path, ["**/vendor/**"], git=False)
    assert audit_index_scope(proj, GIT) == []


def test_an_unreadable_config_is_unknown_not_silent(tmp_path):
    proj = _repo(tmp_path, [], git=False)
    with open(os.path.join(proj, ".tokensave", "config.json"), "w") as fh:
        fh.write("{not json")
    assert sc.scan(proj, GIT).state == sc.UNKNOWN


def test_missing_git_on_a_git_repo_is_unknown_not_clean(tmp_path):
    proj = _repo(tmp_path, ["**/vendor/**"], git=False)
    os.makedirs(os.path.join(proj, ".git"))
    rep = sc.scan(proj, "")
    assert rep.state == sc.UNKNOWN
    assert "git" in rep.reason


def test_empty_project_path_is_silent():
    assert audit_index_scope("") == []
