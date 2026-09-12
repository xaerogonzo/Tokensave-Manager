"""tests/test_integration_check_rules.py — the rules and state sections of the checker.

Two properties matter more than the formatting:

* ``--fix`` and ``--fix-rules`` are different flags. The Manager's Fix button
  passes ``--fix``; a write to the user-global rules file must never ride
  along with it, so ``--fix`` alone must not call ``refresh``.
* A rules write is attempted only with the sha the preview printed.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

_REPO = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "check_tokensave_integration.py"


def _load():
    spec = importlib.util.spec_from_file_location(
        "_check_tokensave_integration_rules", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_fix_rules_is_parsed_separately_from_fix(monkeypatch):
    mod = _load()
    monkeypatch.setattr(sys, "argv", ["x", "--fix"])
    assert mod._parse_args() == {"available": None, "fix": True,
                                 "fix_rules": ""}
    monkeypatch.setattr(sys, "argv", ["x", "--fix-rules=abc123"])
    assert mod._parse_args()["fix_rules"] == "abc123"
    assert mod._parse_args()["fix"] is False


def _fake_report(tr, state):
    gen = tr.Generation(tr.GEN_OK, text="## A\n", raw=b"## A\n",
                        written_files=("proj/.claude/rules/tokensave.md",))
    verdict = tr.Verdict(state, "why", "installedsha", "expectedsha",
                         "--- installed\n+++ tokensave would write\n")
    return tr.RulesReport(gen, verdict, tr.Tracked(False, 'installed_agents = ["copilot"]'),
                          (), {}, {"MANDATORY": ["tokensave_read"]})


def test_preview_prints_the_sha_to_apply_and_never_writes(monkeypatch, capsys):
    mod = _load()
    mod._src_on_path()
    from helpers import tokensave_rules as tr
    monkeypatch.setattr(tr, "evaluate", lambda *a, **k: _fake_report(tr, tr.DRIFTED))
    calls = []
    monkeypatch.setattr(tr, "refresh", lambda *a, **k: calls.append(a) or (True, ""))
    mod._print_rules_section({"tokensave_exe": "x"}, "")
    out = capsys.readouterr().out
    assert "--fix-rules=installedsha" in out
    assert 'installed_agents = ["copilot"]' in out
    assert "lacks in 'MANDATORY': tokensave_read" in out
    assert "upstream resync gap remains" in out
    assert calls == []


def test_fix_rules_passes_the_previewed_sha(monkeypatch, capsys):
    mod = _load()
    mod._src_on_path()
    from helpers import tokensave_rules as tr
    monkeypatch.setattr(tr, "evaluate", lambda *a, **k: _fake_report(tr, tr.DRIFTED))
    seen = []
    monkeypatch.setattr(tr, "refresh",
                        lambda exe, sha, **k: seen.append(sha) or (False, "nope"))
    mod._print_rules_section({"tokensave_exe": "x"}, "installedsha")
    assert seen == ["installedsha"]
    assert "--fix-rules: nope" in capsys.readouterr().out
