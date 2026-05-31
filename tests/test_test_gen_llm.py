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
