"""tests/test_test_gen_llm.py — AI test-content generation guards.

Regression coverage for the bug where "✨ AI generate selected" wrote 8 files
containing Claude's conversational reply ("May I write this file?" / "I need
permission to write the test file.") instead of pytest code. The generator must
now NEVER return un-parseable text to its callers (which write it to disk).

Hermetic: the `_dispatch_*` backends are monkeypatched — no real CLI / LLM.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import threading

import helpers.test_gen_llm as tg
from helpers.test_gen_llm import (
    _extract_code,
    _looks_like_python,
    generate_ai_test_content,
    generate_verified_test,
)

_VALID = '"""t."""\nimport pytest\n\n\ndef test_ok():\n    assert True\n'
_PROSE = ("I've prepared a comprehensive pytest test file for src/theme.py.\n"
          "May I write this file to tests/test_theme.py?")


# ── _extract_code ───────────────────────────────────────────────────────────

def test_extract_unwraps_fenced_block():
    raw = "Here you go:\n```python\nimport pytest\n\n\ndef test_x():\n    pass\n```\nDone!"
    out = _extract_code(raw)
    assert out.startswith("import pytest")
    assert "```" not in out
    assert "Here you go" not in out


def test_extract_strips_prose_preamble_before_code():
    raw = "Sure, here is the test file:\n\n" + _VALID
    out = _extract_code(raw)
    assert out.startswith('"""t."""')
    assert "Sure, here is" not in out


def test_extract_passthrough_clean_code():
    assert _extract_code(_VALID).strip() == _VALID.strip()


def test_extract_pure_prose_stays_prose():
    # No code-ish line → returned as-is (validation will then reject it).
    assert "May I write" in _extract_code(_PROSE)


# ── _looks_like_python ──────────────────────────────────────────────────────

def test_looks_like_python_accepts_valid():
    assert _looks_like_python(_VALID) is None


def test_looks_like_python_rejects_prose():
    err = _looks_like_python(_PROSE)
    assert err is not None
    assert "line" in err


# ── generate_ai_test_content ────────────────────────────────────────────────

def _cfg():
    return SimpleNamespace(
        claude_cli_exe="",          # force the "llm" backend
        claude_cli_model="",
        raw={"commit_message_llm": {"provider": "ollama", "model": "m"}},
    )


def _patch_dispatch(monkeypatch, replies):
    """Make _dispatch_llm return successive *replies* as (content, error) tuples;
    record call count. A reply may be a bare string (→ (str, None)) or a tuple."""
    calls = {"n": 0}

    def fake(cfg, system_prompt, user_prompt, on_token=None):
        i = calls["n"]
        calls["n"] += 1
        reply = replies[min(i, len(replies) - 1)]
        return reply if isinstance(reply, tuple) else (reply, None)

    monkeypatch.setattr(tg, "_dispatch_llm", fake)
    # also stub source read + example lookup so it's hermetic of the filesystem
    monkeypatch.setattr(tg, "_build_prompts", lambda *a, **k: ("sys", "user"))
    return calls


def test_generate_valid_first_try(monkeypatch):
    _patch_dispatch(monkeypatch, [_VALID])
    content, err = generate_ai_test_content("src/x.py", ".", "llm", _cfg())
    assert err is None
    assert content.strip() == _VALID.strip()


def test_generate_prose_twice_returns_error_not_prose(monkeypatch):
    calls = _patch_dispatch(monkeypatch, [_PROSE, _PROSE])
    content, err = generate_ai_test_content("src/x.py", ".", "llm", _cfg())
    assert content is None                       # NEVER hand prose back
    assert err and "valid Python" in err
    assert calls["n"] == 2                        # one repair attempt was made


def test_generate_repairs_on_second_try(monkeypatch):
    calls = _patch_dispatch(monkeypatch, [_PROSE, _VALID])
    content, err = generate_ai_test_content("src/x.py", ".", "llm", _cfg())
    assert err is None
    assert content.strip() == _VALID.strip()
    assert calls["n"] == 2


def test_generate_empty_response_is_error(monkeypatch):
    _patch_dispatch(monkeypatch, ["   "])
    content, err = generate_ai_test_content("src/x.py", ".", "llm", _cfg())
    assert content is None
    assert err


# ── neutral cwd for the Claude CLI backend ──────────────────────────────────

def test_claude_cli_dispatch_uses_neutral_cwd(monkeypatch):
    captured = {}

    def fake_print(**kwargs):
        captured.update(kwargs)
        return _VALID

    monkeypatch.setattr("helpers.claude_cli.call_claude_cli_print", fake_print)
    cfg = SimpleNamespace(claude_cli_exe="/usr/bin/claude", claude_cli_model="")
    out, err = tg._dispatch_claude_cli(cfg, "sys", "user", "/some/project/root")
    assert out == _VALID
    assert err is None
    # Must NOT run inside the repo (that triggers agentic Write-tool behaviour).
    assert captured["cwd"] == os.path.expanduser("~")
    assert captured["cwd"] != "/some/project/root"


def test_claude_cli_dispatch_folds_system_into_stdin(monkeypatch):
    """Regression guard: the system prompt goes via stdin (combined prompt), NOT as
    --append-system-prompt argv — that argv blew the Windows command-line limit."""
    captured = {}
    monkeypatch.setattr("helpers.claude_cli.call_claude_cli_print",
                        lambda **k: captured.update(k) or _VALID)
    cfg = SimpleNamespace(claude_cli_exe="/usr/bin/claude", claude_cli_model="")
    tg._dispatch_claude_cli(cfg, "SYS-MARKER conventions", "USER-MARKER source",
                            "/root")
    assert captured["system_prompt"] == ""              # no --append-system-prompt argv
    assert "SYS-MARKER conventions" in captured["prompt"]   # folded into stdin
    assert "USER-MARKER source" in captured["prompt"]


def test_claude_cli_dispatch_surfaces_specific_error(monkeypatch):
    """On None content, the SPECIFIC get_last_cli_error cause is bubbled up."""
    monkeypatch.setattr("helpers.claude_cli.call_claude_cli_print",
                        lambda **k: None)
    monkeypatch.setattr("helpers.claude_cli.get_last_cli_error",
                        lambda: "exited 1: not logged in")
    cfg = SimpleNamespace(claude_cli_exe="/usr/bin/claude", claude_cli_model="")
    out, err = tg._dispatch_claude_cli(cfg, "sys", "user", "/root")
    assert out is None
    assert err and "not logged in" in err


def test_claude_cli_dispatch_falls_back_when_no_cause(monkeypatch):
    monkeypatch.setattr("helpers.claude_cli.call_claude_cli_print",
                        lambda **k: None)
    monkeypatch.setattr("helpers.claude_cli.get_last_cli_error", lambda: None)
    cfg = SimpleNamespace(claude_cli_exe="/usr/bin/claude", claude_cli_model="")
    out, err = tg._dispatch_claude_cli(cfg, "sys", "user", "/root")
    assert out is None
    assert err and "no output" in err.lower()


# ── _dispatch_llm: streaming, timeout, dynamic num_ctx, no shared mutation ──

def _capture_call_llm(monkeypatch, *, ret="ok", last_error=None):
    """Stub helpers.llm._call_llm + get_last_llm_error; capture the call kwargs."""
    seen = {}

    def fake_call_llm(**kwargs):
        seen.update(kwargs)
        seen["n"] = seen.get("n", 0) + 1
        return ret

    monkeypatch.setattr("helpers.llm._call_llm", fake_call_llm)
    monkeypatch.setattr("helpers.llm.get_last_llm_error", lambda: last_error)
    return seen


def _llm_cfg(**extra):
    base = {"provider": "ollama", "model": "m", "enabled": True}
    base.update(extra)
    return SimpleNamespace(raw={"commit_message_llm": base})


def test_dispatch_llm_local_streams_long_timeout_and_num_ctx(monkeypatch):
    seen = _capture_call_llm(monkeypatch)
    sentinel = object()
    content, err = tg._dispatch_llm(_llm_cfg(), "sys", "user", on_token=sentinel)
    assert (content, err) == ("ok", None)
    assert seen["on_token"] is sentinel                 # streaming wired through
    assert seen["timeout"] == tg._LOCAL_TIMEOUT == 300   # long local timeout
    nc = seen["cfg"]["num_ctx"]
    assert tg._LOCAL_CTX_CEILING >= nc >= 4096
    assert nc % 1024 == 0                                # rounded to a clean alloc


def test_dispatch_llm_does_not_mutate_shared_config(monkeypatch):
    """Finding A: num_ctx is injected on a COPY, never the live config dict."""
    _capture_call_llm(monkeypatch)
    cfg = _llm_cfg()
    tg._dispatch_llm(cfg, "sys", "user")
    assert "num_ctx" not in cfg.raw["commit_message_llm"]


def test_dispatch_llm_preserves_explicit_num_ctx(monkeypatch):
    seen = _capture_call_llm(monkeypatch)
    tg._dispatch_llm(_llm_cfg(num_ctx=8192), "sys", "user")
    assert seen["cfg"]["num_ctx"] == 8192               # user value untouched


def test_dispatch_llm_cloud_no_num_ctx_short_timeout(monkeypatch):
    seen = _capture_call_llm(monkeypatch)
    cfg = SimpleNamespace(raw={"commit_message_llm": {
        "provider": "anthropic", "model": "claude", "enabled": True}})
    tg._dispatch_llm(cfg, "sys", "user")
    assert "num_ctx" not in seen["cfg"]
    assert seen["timeout"] == tg._CLOUD_TIMEOUT == 120


def test_dispatch_llm_too_large_early_exits_without_calling(monkeypatch):
    """Finding/R3 #1: an oversized prompt bails BEFORE burning a prefill."""
    seen = _capture_call_llm(monkeypatch)
    huge = "x" * (tg._LOCAL_CTX_CEILING * 3)             # forces needed > ceiling
    content, err = tg._dispatch_llm(_llm_cfg(), huge, huge)
    assert content is None
    assert err and "too large" in err and "Claude CLI" in err
    assert seen.get("n", 0) == 0                         # _call_llm never invoked


def test_dispatch_llm_surfaces_real_error(monkeypatch):
    """On None content, the dynamic get_last_llm_error string is returned verbatim."""
    _capture_call_llm(monkeypatch, ret=None, last_error="Timed out after 300s — slow")
    content, err = tg._dispatch_llm(_llm_cfg(), "sys", "user")
    assert content is None
    assert err == "Timed out after 300s — slow"         # no hardcoded literal


def test_max_gen_tokens_is_4000():
    """Raised from 2500 to curb mid-file truncation (smoke_runner case)."""
    assert tg._MAX_GEN_TOKENS == 4000


# ── deterministic import auto-repair ────────────────────────────────────────

def test_undefined_names_flags_used_but_unimported():
    code = '"""d."""\npytestmark = pytest.mark.tk\ndef test_x():\n    subprocess.run([])\n'
    und = tg._undefined_names(code)
    assert "pytest" in und and "subprocess" in und


def test_undefined_names_empty_for_clean_code():
    assert tg._undefined_names(_VALID) == []


def test_undefined_names_empty_on_syntax_error():
    assert tg._undefined_names('"""d."""\ndef oops(\n') == []


def test_module_dotted_src_layout(tmp_path):
    src = tmp_path / "src" / "controllers"
    src.mkdir(parents=True)
    f = src / "projects_tab.py"
    f.write_text("x = 1\n", encoding="utf-8")
    assert tg._module_dotted(str(f), str(tmp_path)) == "controllers.projects_tab"


def test_module_dotted_no_src_layout(tmp_path):
    f = tmp_path / "helpers" / "git.py"
    f.parent.mkdir(parents=True)
    f.write_text("x = 1\n", encoding="utf-8")
    assert tg._module_dotted(str(f), str(tmp_path)) == "helpers.git"


def _autofix(code, tmp_path, *, rel="src/helpers/foo.py", source=""):
    """Write a source file at *rel* with *source* body; run _autofix on *code*."""
    sp = tmp_path / rel
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(source or "def f():\n    return 1\n", encoding="utf-8")
    return tg._autofix_imports(code, str(sp), str(tmp_path))


def test_autofix_injects_pytest_multiline_docstring(tmp_path):
    code = ('"""Tests for app.\nSecond line.\nThird.\n"""\n'
            'pytestmark = pytest.mark.tk\ndef test_x():\n    assert True\n')
    out = _autofix(code, tmp_path)
    assert "import pytest" in out
    assert tg._undefined_names(out) == []                 # fully resolved
    # import lands AFTER the full 4-line docstring (closing """ on line 4),
    # before pytestmark
    lines = out.splitlines()
    assert lines[3] == '"""'                              # docstring closes here
    assert lines[4] == "import pytest"
    assert "pytestmark" in lines[5]


def test_autofix_injects_local_class(tmp_path):
    code = '"""d."""\ndef test_c():\n    assert ProjectsTabController\n'
    out = _autofix(code, tmp_path, rel="src/controllers/projects_tab.py",
                   source="class ProjectsTabController:\n    pass\n")
    assert "from controllers.projects_tab import ProjectsTabController" in out
    assert tg._undefined_names(out) == []


def test_autofix_injects_subprocess(tmp_path):
    code = '"""d."""\ndef test_s():\n    subprocess.run(["x"])\n'
    out = _autofix(code, tmp_path)
    assert "import subprocess" in out


def test_autofix_clusters_typing_single_line(tmp_path):
    code = '"""d."""\ndef test_t():\n    x: Optional[Dict] = None\n    assert x is None\n'
    out = _autofix(code, tmp_path)
    assert "from typing import Dict, Optional" in out      # ONE clustered line
    assert tg._undefined_names(out) == []


def test_autofix_module_ref_root(tmp_path):
    code = ('"""d."""\ndef test_m():\n'
            '    controllers.projects_tab.ProjectsTabController()\n')
    out = _autofix(code, tmp_path, rel="src/controllers/projects_tab.py",
                   source="class ProjectsTabController:\n    pass\n")
    assert "import controllers.projects_tab" in out


def test_autofix_module_ref_stem_binds_correctly(tmp_path):
    # bare stem `projects_tab` flagged → `from controllers import projects_tab`
    code = '"""d."""\ndef test_m():\n    projects_tab.helper()\n'
    out = _autofix(code, tmp_path, rel="src/controllers/projects_tab.py",
                   source="def helper():\n    return 1\n")
    assert "from controllers import projects_tab" in out
    assert "import controllers.projects_tab\n" not in out  # NOT the root form


def test_autofix_local_first_shadowing(tmp_path):
    """A source that defines its own Path wins over the stdlib map."""
    code = '"""d."""\ndef test_p():\n    p = Path()\n    assert p\n'
    out = _autofix(code, tmp_path, rel="src/helpers/weird.py",
                   source="class Path:\n    pass\n")
    assert "from helpers.weird import Path" in out
    assert "from pathlib import Path" not in out


def test_autofix_wraps_wide_imports(tmp_path):
    code = ('"""d."""\ndef test_w():\n'
            '    assert (A, B, C, D)\n')
    out = _autofix(code, tmp_path, rel="src/helpers/big.py",
                   source="class A:\n    pass\nclass B:\n    pass\n"
                          "class C:\n    pass\nclass D:\n    pass\n")
    assert "from helpers.big import (" in out             # parenthesised
    assert out.count("    D,") == 1                        # trailing comma on last
    assert tg._undefined_names(out) == []


def test_autofix_preserves_crlf(tmp_path):
    code = '"""d."""\r\npytestmark = pytest.mark.tk\r\ndef test_x():\r\n    assert True\r\n'
    out = _autofix(code, tmp_path)
    assert "import pytest" in out
    assert "\r\n" in out
    assert "\n" not in out.replace("\r\n", "")            # pure CRLF, no stray LF


def test_autofix_idempotent(tmp_path):
    code = '"""d."""\npytestmark = pytest.mark.tk\ndef test_x():\n    assert True\n'
    once = _autofix(code, tmp_path)
    twice = tg._autofix_imports(once, str(tmp_path / "src/helpers/foo.py"), str(tmp_path))
    assert once == twice


def test_autofix_empty_input_no_raise(tmp_path):
    assert _autofix("", tmp_path) == ""
    assert _autofix("   \n", tmp_path) == "   \n"


def test_autofix_syntax_error_returns_unchanged(tmp_path):
    bad = '"""d."""\ndef oops(\n'
    assert _autofix(bad, tmp_path) == bad


def test_autofix_skips_already_imported(tmp_path):
    out = _autofix(_VALID, tmp_path)                       # already has import pytest
    assert out.count("import pytest") == 1                 # no duplicate


def test_verified_test_writes_import_complete_file(tmp_path, monkeypatch):
    """Integration: an import-less candidate from the backend is written WITH the
    injected imports (autofix runs inside the real generate_ai_test_content)."""
    root, src = _verify_repo(tmp_path)                     # src = <tmp>/src/foo.py
    importless = '"""t."""\ndef test_f():\n    assert f() == 1\n'   # uses f, no import
    monkeypatch.setattr(tg, "_dispatch", lambda *a, **k: (importless, None))
    monkeypatch.setattr("helpers.smoke_runner.run_single_test_file",
                        lambda *a, **k: (True, "1 passed"))
    monkeypatch.setattr("helpers.test_scaffold._test_filename_for",
                        lambda *a, **k: "test_foo.py")
    res = generate_verified_test(src, root, "llm", _cfg())
    assert res.status == "pass"
    written = (tmp_path / "tests" / "test_foo.py").read_text(encoding="utf-8")
    assert "from foo import f" in written                  # symbol-under-test injected
    assert tg._undefined_names(written) == []


# ── generate_verified_test (generate → run → repair → keep-if-passing) ───────

def _verify_repo(tmp_path):
    """A minimal project root with a tests/ dir and a trivial source file."""
    (tmp_path / "tests").mkdir()
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    return str(tmp_path), str(src / "foo.py")


def _patch_verify(monkeypatch, *, gen=(_VALID, None), run_results=None,
                  repair_reply=_VALID, final_name="test_foo.py"):
    """Wire the verify pipeline to deterministic stand-ins."""
    monkeypatch.setattr(tg, "generate_ai_test_content", lambda *a, **k: gen)
    monkeypatch.setattr(tg, "_build_prompts", lambda *a, **k: ("sys", "user"))
    monkeypatch.setattr(tg, "_dispatch", lambda *a, **k: (repair_reply, None))
    monkeypatch.setattr("helpers.test_scaffold._test_filename_for",
                        lambda *a, **k: final_name)
    results = list(run_results or [(True, "ok")])
    calls = {"n": 0}

    def fake_run(project_root, test_relpath, timeout=20):
        i = calls["n"]
        calls["n"] += 1
        return results[min(i, len(results) - 1)]

    monkeypatch.setattr("helpers.smoke_runner.run_single_test_file", fake_run)
    return calls


def test_verify_pass_first_run_writes_file(tmp_path, monkeypatch):
    root, src = _verify_repo(tmp_path)
    _patch_verify(monkeypatch, run_results=[(True, "1 passed")])
    res = generate_verified_test(src, root, "llm", _cfg())
    assert res.status == "pass"
    final = tmp_path / "tests" / "test_foo.py"
    assert final.exists() and final.read_text(encoding="utf-8").strip() == _VALID.strip()
    # no temp debris
    assert not list((tmp_path / "tests").glob("test__aigen_*"))


def test_verify_fail_then_repair_pass(tmp_path, monkeypatch):
    root, src = _verify_repo(tmp_path)
    calls = _patch_verify(monkeypatch, run_results=[(False, "boom"), (True, "ok")])
    res = generate_verified_test(src, root, "llm", _cfg())
    assert res.status == "pass"
    assert calls["n"] == 2                       # ran, failed, repaired, ran again
    assert (tmp_path / "tests" / "test_foo.py").exists()
    assert not list((tmp_path / "tests").glob("test__aigen_*"))


def test_verify_fail_twice_discards(tmp_path, monkeypatch):
    root, src = _verify_repo(tmp_path)
    calls = _patch_verify(monkeypatch, run_results=[(False, "boom1"), (False, "boom2")])
    res = generate_verified_test(src, root, "llm", _cfg())
    assert res.status == "fail"
    assert "boom2" in res.report
    assert calls["n"] == 2                       # one repair attempt, then give up
    assert not (tmp_path / "tests" / "test_foo.py").exists()   # nothing written
    assert not list((tmp_path / "tests").glob("test__aigen_*"))  # temp cleaned


def test_verify_timeout_is_discarded(tmp_path, monkeypatch):
    root, src = _verify_repo(tmp_path)
    _patch_verify(monkeypatch,
                  run_results=[(False, "TIMEOUT after 20s"), (False, "TIMEOUT after 20s")])
    res = generate_verified_test(src, root, "llm", _cfg())
    assert res.status == "fail"
    assert not (tmp_path / "tests" / "test_foo.py").exists()


def test_verify_generator_error(tmp_path, monkeypatch):
    root, src = _verify_repo(tmp_path)
    _patch_verify(monkeypatch, gen=(None, "AI did not return valid Python"))
    res = generate_verified_test(src, root, "llm", _cfg())
    assert res.status == "error"
    assert not list((tmp_path / "tests").glob("*.py"))   # nothing written or temp'd


def test_verify_cancelled_before_start(tmp_path, monkeypatch):
    root, src = _verify_repo(tmp_path)
    _patch_verify(monkeypatch)
    ev = threading.Event()
    ev.set()
    res = generate_verified_test(src, root, "llm", _cfg(), cancel_event=ev)
    assert res.status == "cancelled"


def test_verify_run_false_skips_pytest(tmp_path, monkeypatch):
    """run=False short-circuits — returns content without touching pytest/disk."""
    root, src = _verify_repo(tmp_path)
    calls = _patch_verify(monkeypatch)
    res = generate_verified_test(src, root, "llm", _cfg(), run=False)
    assert res.status == "pass"
    assert calls["n"] == 0
    assert not (tmp_path / "tests" / "test_foo.py").exists()


# ── _test_function_ids (class-aware retention) ──────────────────────────────

def test_function_ids_collects_module_and_class_methods():
    src = (
        "def test_a():\n    pass\n"
        "class TestX:\n"
        "    def test_b(self):\n        pass\n"
        "    def helper(self):\n        pass\n"
        "def not_a_test():\n    pass\n"
    )
    assert tg._test_function_ids(src) == {"test_a", "TestX.test_b"}


def test_function_ids_qualifies_so_dupes_dont_mask():
    src = ("class TestA:\n    def test_x(self): pass\n"
           "class TestB:\n    def test_x(self): pass\n")
    assert tg._test_function_ids(src) == {"TestA.test_x", "TestB.test_x"}


# ── _find_example_test template matching ────────────────────────────────────

def _mk_tests(tmp_path, files):
    (tmp_path / "tests").mkdir(exist_ok=True)
    for name, body in files.items():
        (tmp_path / "tests" / name).write_text(body, encoding="utf-8")


def test_find_example_matches_subprocess(tmp_path):
    _mk_tests(tmp_path, {
        "test_pure.py": '"""p."""\ndef test_x():\n    assert 1\n',
        "test_sub.py": 'def test_y(mocker):\n    mocker.patch("m.subprocess.run")\n    assert 1\n',
    })
    ex = tg._find_example_test(str(tmp_path), "subprocess_helper")
    assert "subprocess" in ex and "test_y" in ex


def test_find_example_matches_tk(tmp_path):
    _mk_tests(tmp_path, {
        "test_pure.py": 'def test_x():\n    assert 1\n',
        "test_dlg.py": 'import pytest\npytestmark = pytest.mark.tk\ndef test_z(tk_root):\n    assert 1\n',
    })
    ex = tg._find_example_test(str(tmp_path), "dialog_tk")
    assert "pytest.mark.tk" in ex


def test_find_example_tk_fallback_not_pure(tmp_path):
    """No on-disk tk example → use the TK fallback constant, NOT the pure file."""
    _mk_tests(tmp_path, {"test_pure.py": 'def test_x():\n    assert 1\n'})
    ex = tg._find_example_test(str(tmp_path), "dialog_tk")
    assert "pytest.mark.tk" in ex
    assert "test_x" not in ex          # did not copy the pure example


# ── regenerate: retention gate + overwrite + lock-safety ────────────────────

def _existing_test(tmp_path, body):
    (tmp_path / "tests").mkdir(exist_ok=True)
    f = tmp_path / "tests" / "test_foo.py"
    f.write_text(body, encoding="utf-8")
    return str(f)


def test_regenerate_retains_all_overwrites(tmp_path, monkeypatch):
    root, src = _verify_repo(tmp_path)
    target = _existing_test(tmp_path, "def test_a():\n    assert 1\n\n\ndef test_b():\n    assert 1\n")
    new = ("def test_a():\n    assert 2\n\n\ndef test_b():\n    assert 2\n\n\n"
           "def test_c():\n    assert 1\n")
    _patch_verify(monkeypatch, gen=(new, None), run_results=[(True, "ok")])
    res = generate_verified_test(src, root, "llm", _cfg(),
                                 allow_overwrite=True, target_path=target)
    assert res.status == "pass"
    assert "test_c" in open(target, encoding="utf-8").read()    # overwritten


def test_regenerate_dropping_a_test_is_rejected(tmp_path, monkeypatch):
    root, src = _verify_repo(tmp_path)
    target = _existing_test(tmp_path, "def test_a():\n    assert 1\n\n\ndef test_b():\n    assert 1\n")
    _patch_verify(monkeypatch, gen=("def test_a():\n    assert 2\n", None),
                  run_results=[(True, "ok")])          # drops test_b but PASSES
    res = generate_verified_test(src, root, "llm", _cfg(),
                                 allow_overwrite=True, target_path=target)
    assert res.status == "fail"
    assert res.preserved_existing is True
    assert "test_b" in open(target, encoding="utf-8").read()    # original untouched


def test_replace_locked_returns_fail(tmp_path, monkeypatch):
    root, src = _verify_repo(tmp_path)
    _patch_verify(monkeypatch, run_results=[(True, "ok")])
    monkeypatch.setattr(tg.time, "sleep", lambda *_: None)
    monkeypatch.setattr(tg.os, "replace",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("locked")))
    res = generate_verified_test(src, root, "llm", _cfg())
    assert res.status == "fail"
    assert "locked" in res.report.lower()


# ── per-test pruning: _failing_test_ids ──────────────────────────────────────

def test_failing_ids_parses_class_and_func_and_param():
    out = (
        "=========================== short test summary info ===========================\n"
        "FAILED tests/test_x.py::TestCls::test_a - AssertionError: boom\n"
        "FAILED tests/test_x.py::test_b[3-4] - ValueError\n"
        "ERROR tests/test_x.py::TestCls::test_c - fixture error\n"
    )
    ids, prunable = tg._failing_test_ids(out)
    assert ids == {"TestCls.test_a", "test_b", "TestCls.test_c"}   # param bracket stripped
    assert prunable is True


def test_failing_ids_whole_file_error_not_prunable():
    out = ("ERROR tests/test_x.py - NameError: name 'pytest' is not defined\n")
    ids, prunable = tg._failing_test_ids(out)
    assert prunable is False                                       # no ::id → can't prune


def test_failing_ids_empty_when_no_failures():
    ids, prunable = tg._failing_test_ids("== 5 passed in 0.1s ==")
    assert ids == set() and prunable is True


# ── per-test pruning: _prune_test_functions ──────────────────────────────────

_PRUNE_SRC = (
    '"""t."""\n'
    'import pytest\n\n\n'
    'def helper():\n    return 1\n\n\n'
    'def test_a():\n    assert helper() == 1\n\n\n'
    'def test_bad():\n    assert False\n\n\n'
    'class TestX:\n'
    '    def test_c(self):\n        assert True\n\n'
    '    def test_d(self):\n        assert False\n'
)


def test_prune_drops_named_keeps_rest():
    out = tg._prune_test_functions(_PRUNE_SRC, {"test_bad", "TestX.test_d"})
    assert out is not None
    assert "def test_bad" not in out
    assert "def test_d" not in out
    assert "def test_a" in out and "def helper" in out      # survivors + helper kept
    assert "def test_c" in out                              # class retains passing method
    assert tg._looks_like_python(out) is None               # still valid


def test_prune_removes_whole_class_when_all_fail():
    out = tg._prune_test_functions(_PRUNE_SRC, {"TestX.test_c", "TestX.test_d"})
    assert "class TestX" not in out                         # all methods failed → drop class
    assert "def test_a" in out


def test_prune_returns_none_when_nothing_left():
    src = '"""t."""\ndef test_a():\n    assert False\n'
    assert tg._prune_test_functions(src, {"test_a"}) is None


def test_prune_keeps_decorator_with_function():
    src = ('"""t."""\nimport pytest\n\n\n'
           '@pytest.mark.parametrize("x", [1, 2])\n'
           'def test_p(x):\n    assert x\n\n\n'
           'def test_ok():\n    assert True\n')
    out = tg._prune_test_functions(src, {"test_p"})
    assert "parametrize" not in out                         # decorator removed with func
    assert "def test_ok" in out


# ── _CLI_GEN_TIMEOUT ─────────────────────────────────────────────────────────

def test_cli_dispatch_uses_long_timeout(monkeypatch):
    captured = {}
    def fake_print(**kwargs):
        captured.update(kwargs)
        return _VALID
    monkeypatch.setattr("helpers.claude_cli.call_claude_cli_print", fake_print)
    cfg = SimpleNamespace(claude_cli_exe="/usr/bin/claude", claude_cli_model="")
    tg._dispatch_claude_cli(cfg, "sys", "user", "/root")
    assert captured["timeout"] == tg._CLI_GEN_TIMEOUT == 240


# ── prune integration through generate_verified_test ─────────────────────────

_THREE = ('"""t."""\nimport pytest\n\n\ndef test_a():\n    assert True\n\n\n'
          'def test_b():\n    assert True\n\n\ndef test_bad():\n    assert False\n')


def _wire_prune(monkeypatch, *, fail_ids="test_bad", pass_on_call=3):
    monkeypatch.setattr(tg, "_dispatch", lambda *a, **k: (_THREE, None))
    monkeypatch.setattr(tg, "_build_prompts", lambda *a, **k: ("sys", "user"))
    monkeypatch.setattr(tg, "_autofix_imports", lambda c, *a, **k: c)
    monkeypatch.setattr("helpers.test_scaffold._test_filename_for",
                        lambda *a, **k: "test_foo.py")
    fail_out = ("=== short test summary info ===\n"
                + "".join(f"FAILED tests/test__aigen_foo_x.py::{i} - X\n"
                          for i in fail_ids.split(",")))
    calls = {"n": 0}
    def fake_run(project_root, test_relpath, timeout=20):
        calls["n"] += 1
        ok = calls["n"] >= pass_on_call
        return (ok, "1 passed" if ok else fail_out)
    monkeypatch.setattr("helpers.smoke_runner.run_single_test_file", fake_run)
    return calls


def test_verify_prunes_failing_keeps_passing(tmp_path, monkeypatch):
    root, src = _verify_repo(tmp_path)
    _wire_prune(monkeypatch, fail_ids="test_bad", pass_on_call=3)
    res = generate_verified_test(src, root, "llm", _cfg())
    assert res.status == "pass"
    assert res.kept == 2 and res.total == 3                 # dropped 1 of 3
    written = (tmp_path / "tests" / "test_foo.py").read_text(encoding="utf-8")
    assert "def test_bad" not in written
    assert "def test_a" in written and "def test_b" in written


def test_verify_prune_below_floor_discards(tmp_path, monkeypatch):
    root, src = _verify_repo(tmp_path)
    # 2 of 3 fail → only 1 would survive (0.33 < 0.5 floor) → discard, nothing written
    _wire_prune(monkeypatch, fail_ids="test_b,test_bad", pass_on_call=99)
    res = generate_verified_test(src, root, "llm", _cfg())
    assert res.status == "fail"
    assert not (tmp_path / "tests" / "test_foo.py").exists()


def test_verify_regenerate_is_not_pruned(tmp_path, monkeypatch):
    """Pruning is new-file only; a failing regenerate keeps the original."""
    root, src = _verify_repo(tmp_path)
    target = _existing_test(tmp_path, "def test_keep():\n    assert 1\n")
    _wire_prune(monkeypatch, fail_ids="test_bad", pass_on_call=99)
    res = generate_verified_test(src, root, "llm", _cfg(),
                                 allow_overwrite=True, target_path=target)
    assert res.status == "fail"                             # not salvaged via pruning
    assert "test_keep" in open(target, encoding="utf-8").read()   # original intact


# ── build_claude_code_handoff_prompt ─────────────────────────────────────────

def test_handoff_prompt_lists_files_and_directives():
    from helpers.test_gen_llm import build_claude_code_handoff_prompt
    sugg = [
        SimpleNamespace(source_path="/p/src/helpers/foo.py",
                        rel_path="src/helpers/foo.py", template="pure_helper"),
        SimpleNamespace(source_path="/p/src/dialogs/bar.py",
                        rel_path="src/dialogs/bar.py", template="dialog_tk"),
    ]
    out = build_claude_code_handoff_prompt(sugg, "/p")
    assert "src/helpers/foo.py" in out and "src/dialogs/bar.py" in out
    assert "pytest" in out                                  # run-and-fix directive
    assert "Only CREATE files under `tests/`" in out
    assert "tk_root" in out                                 # conventions embedded


# ── _failing_files (gate-failure → owning file attribution) ──────────────────

def test_failing_files_test_level():
    assert tg._failing_files("FAILED tests/test_x.py::TestC::test_m - X\n") == {
        "tests/test_x.py"}


def test_failing_files_collection_level_no_colons():
    # collection abort has NO ::id — must still attribute to the file
    assert tg._failing_files("ERROR tests/test_x.py - ImportError: boom\n") == {
        "tests/test_x.py"}


def test_failing_files_multiple_and_none():
    out = "FAILED tests/a.py::t - x\nERROR tests/b.py - y\n=== 1 passed ===\n"
    assert tg._failing_files(out) == {"tests/a.py", "tests/b.py"}
    assert tg._failing_files("5 passed in 0.1s") == set()


# ── reverify_against_suite (full-suite gate + rollback) ──────────────────────

def _mk_test_files(tmp_path, rels):
    for r in rels:
        p = tmp_path / r
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("def test_x():\n    assert True\n", encoding="utf-8")


def _seq_gate(monkeypatch, results):
    calls = {"n": 0}
    def fake(project_root, timeout=180):
        i = calls["n"]; calls["n"] += 1
        return results[min(i, len(results) - 1)]
    monkeypatch.setattr("helpers.smoke_runner.run_gate", fake)
    return calls


def test_reverify_all_green_keeps_all(tmp_path, monkeypatch):
    _mk_test_files(tmp_path, ["tests/test_x.py", "tests/test_y.py"])
    _seq_gate(monkeypatch, [(True, "")])
    v = tg.reverify_against_suite(str(tmp_path), ["tests/test_x.py", "tests/test_y.py"])
    assert v == {"tests/test_x.py": "ok", "tests/test_y.py": "ok"}
    assert (tmp_path / "tests/test_x.py").exists()


def test_reverify_rolls_back_owner_only(tmp_path, monkeypatch):
    _mk_test_files(tmp_path, ["tests/test_x.py", "tests/test_y.py"])
    _seq_gate(monkeypatch, [(False, "FAILED tests/test_x.py::T::m - X\n"), (True, "")])
    v = tg.reverify_against_suite(str(tmp_path), ["tests/test_x.py", "tests/test_y.py"])
    assert v["tests/test_x.py"] == "rolled_back"
    assert v["tests/test_y.py"] == "ok"
    assert not (tmp_path / "tests/test_x.py").exists()      # owner deleted
    assert (tmp_path / "tests/test_y.py").exists()          # innocent kept


def test_reverify_separator_normalization(tmp_path, monkeypatch):
    _mk_test_files(tmp_path, ["tests/test_x.py"])
    _seq_gate(monkeypatch, [(False, "FAILED tests/test_x.py::t - X\n"), (True, "")])
    # caller passes a backslash (Windows-style) relpath; pytest emits forward slashes
    v = tg.reverify_against_suite(str(tmp_path), ["tests\test_x.py"])
    assert v["tests\test_x.py"] == "rolled_back"           # matched despite separators


def test_reverify_collection_error_attributed(tmp_path, monkeypatch):
    _mk_test_files(tmp_path, ["tests/test_x.py"])
    _seq_gate(monkeypatch, [(False, "ERROR tests/test_x.py - ImportError\n"), (True, "")])
    v = tg.reverify_against_suite(str(tmp_path), ["tests/test_x.py"])
    assert v["tests/test_x.py"] == "rolled_back"            # surgical, not nuclear


def test_reverify_empty_parse_is_nuclear(tmp_path, monkeypatch):
    _mk_test_files(tmp_path, ["tests/test_x.py", "tests/test_y.py"])
    _seq_gate(monkeypatch, [(False, "INTERNALERROR — collection crashed, no summary")])
    v = tg.reverify_against_suite(str(tmp_path), ["tests/test_x.py", "tests/test_y.py"])
    assert all(s == "rolled_back" for s in v.values())      # unparseable → roll all


def test_reverify_still_red_full_rollback(tmp_path, monkeypatch):
    _mk_test_files(tmp_path, ["tests/test_x.py", "tests/test_y.py"])
    _seq_gate(monkeypatch, [
        (False, "FAILED tests/test_x.py::t - X\n"),         # owner x fails
        (False, "FAILED tests/test_other.py::t - Y\n"),     # still red, outside batch
    ])
    v = tg.reverify_against_suite(str(tmp_path), ["tests/test_x.py", "tests/test_y.py"])
    assert v["tests/test_x.py"] == "rolled_back"            # owner
    assert v["tests/test_y.py"] == "rolled_back"            # nuclear fallback
    assert not (tmp_path / "tests/test_y.py").exists()


def test_reverify_empty_input_noop(tmp_path, monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr("helpers.smoke_runner.run_gate",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or (True, ""))
    assert tg.reverify_against_suite(str(tmp_path), []) == {}
    assert called["n"] == 0                                  # no gate run for empty batch
