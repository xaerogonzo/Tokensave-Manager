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
    """Make _dispatch_llm return successive *replies*; record call count."""
    calls = {"n": 0}

    def fake(cfg, system_prompt, user_prompt):
        i = calls["n"]
        calls["n"] += 1
        return replies[min(i, len(replies) - 1)]

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
    out = tg._dispatch_claude_cli(cfg, "sys", "user", "/some/project/root")
    assert out == _VALID
    # Must NOT run inside the repo (that triggers agentic Write-tool behaviour).
    assert captured["cwd"] == os.path.expanduser("~")
    assert captured["cwd"] != "/some/project/root"


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
    monkeypatch.setattr(tg, "_dispatch", lambda *a, **k: repair_reply)
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
