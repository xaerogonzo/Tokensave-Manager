"""tests/test_doc_drafter_dispatch.py — helpers/doc_drafter_dispatch.py."""
from __future__ import annotations

import threading

import pytest

from helpers.doc_drafter_dispatch import dispatch_llm


# ── dispatch_llm: routing logic ───────────────────────────────────────────────

class TestDispatchLlmRouting:
    def test_claude_cli_no_exe_returns_error(self):
        """Without claude_cli_exe, the claude_cli provider must return error."""
        text, err = dispatch_llm(
            llm_cfg={"provider": "claude_cli", "model": "haiku"},
            system_prompt="sys",
            user_prompt="usr",
            claude_cli_exe="",
            cwd=None,
        )
        assert text is None
        assert err is not None
        assert "CLI" in err or "configured" in err.lower()

    def test_claude_cli_calls_call_claude_cli_print(self, monkeypatch):
        """claude_cli provider routes to call_claude_cli_print."""
        called = []

        def fake_cli(exe, user_prompt, system_prompt="", timeout=120,
                     model="", cwd=None):
            called.append({"exe": exe, "user_prompt": user_prompt,
                           "system_prompt": system_prompt})
            return "draft output"

        monkeypatch.setattr("helpers.claude_cli.call_claude_cli_print", fake_cli)

        text, err = dispatch_llm(
            llm_cfg={"provider": "claude_cli"},
            system_prompt="system",
            user_prompt="user",
            claude_cli_exe="/usr/bin/claude",
            cwd="/some/path",
        )
        assert err is None
        assert text == "draft output"
        assert len(called) == 1
        assert called[0]["exe"] == "/usr/bin/claude"

    def test_claude_cli_empty_output_returns_error(self, monkeypatch):
        """claude_cli returning empty string becomes an error."""
        monkeypatch.setattr("helpers.claude_cli.call_claude_cli_print",
                            lambda *a, **k: "")

        text, err = dispatch_llm(
            llm_cfg={"provider": "claude_cli"},
            system_prompt="sys",
            user_prompt="usr",
            claude_cli_exe="/usr/bin/claude",
            cwd=None,
        )
        assert text is None
        assert "no output" in (err or "").lower()

    def test_non_cli_provider_calls_call_llm(self, monkeypatch):
        """Non-claude_cli providers route to _call_llm."""
        monkeypatch.setattr("helpers.llm._call_llm",
                            lambda cfg, sys, usr, max_tokens=1500: "llm result")

        text, err = dispatch_llm(
            llm_cfg={"provider": "openai", "model": "gpt-4"},
            system_prompt="sys",
            user_prompt="usr",
            claude_cli_exe="",
            cwd=None,
        )
        assert err is None
        assert text == "llm result"

    def test_llm_returns_empty_becomes_error(self, monkeypatch):
        """_call_llm returning empty/None becomes an error."""
        monkeypatch.setattr("helpers.llm._call_llm",
                            lambda cfg, sys, usr, max_tokens=1500: "")

        text, err = dispatch_llm(
            llm_cfg={"provider": "openai"},
            system_prompt="sys",
            user_prompt="usr",
            claude_cli_exe="",
            cwd=None,
        )
        assert text is None
        assert "empty" in (err or "").lower()

    def test_llm_exception_returns_error(self, monkeypatch):
        """Exceptions from _call_llm are caught and returned as error."""
        monkeypatch.setattr("helpers.llm._call_llm",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network down")))

        text, err = dispatch_llm(
            llm_cfg={"provider": "openai"},
            system_prompt="sys",
            user_prompt="usr",
            claude_cli_exe="",
            cwd=None,
        )
        assert text is None
        assert "network down" in (err or "")

    def test_gen_params_merged_into_cfg(self, monkeypatch):
        """gen_params values override llm_cfg values."""
        captured = []

        def fake_llm(cfg, sys, usr, max_tokens=1500):
            captured.append(cfg)
            return "result"

        monkeypatch.setattr("helpers.llm._call_llm", fake_llm)

        dispatch_llm(
            llm_cfg={"provider": "openai", "temperature": 0.7},
            system_prompt="sys",
            user_prompt="usr",
            claude_cli_exe="",
            cwd=None,
            gen_params={"temperature": 0.2},
        )
        assert captured[0]["temperature"] == 0.2

    def test_few_shot_examples_prepended_for_ollama(self, monkeypatch):
        """Few-shot examples are prepended to user_prompt for ollama provider."""
        captured = []

        def fake_llm(cfg, sys, usr, max_tokens=1500):
            captured.append(usr)
            return "result"

        monkeypatch.setattr("helpers.llm._call_llm", fake_llm)

        dispatch_llm(
            llm_cfg={"provider": "ollama"},
            system_prompt="sys",
            user_prompt="main prompt",
            claude_cli_exe="",
            cwd=None,
            examples=[("input A", "output A")],
        )
        user = captured[0]
        assert "input A" in user
        assert "output A" in user
        assert "main prompt" in user

    def test_few_shot_examples_not_prepended_for_openai(self, monkeypatch):
        """Few-shot examples are NOT prepended for non-local providers."""
        captured = []

        def fake_llm(cfg, sys, usr, max_tokens=1500):
            captured.append(usr)
            return "result"

        monkeypatch.setattr("helpers.llm._call_llm", fake_llm)

        dispatch_llm(
            llm_cfg={"provider": "openai"},
            system_prompt="sys",
            user_prompt="main prompt",
            claude_cli_exe="",
            cwd=None,
            examples=[("input A", "output A")],
        )
        user = captured[0]
        assert "input A" not in user
        assert "main prompt" in user

    def test_agentic_mode_routes_to_dispatch_agentic(self, monkeypatch):
        """enable_tokensave_tools + ollama routes to _dispatch_agentic."""
        agentic_called = []

        def fake_agentic(llm_cfg, system_prompt, user_prompt,
                         project_path, tokensave_exe, timeout, stop_event):
            agentic_called.append(True)
            return "agentic result", None

        monkeypatch.setattr(
            "helpers.doc_drafter_dispatch._dispatch_agentic", fake_agentic)

        text, err = dispatch_llm(
            llm_cfg={"provider": "ollama"},
            system_prompt="sys",
            user_prompt="usr",
            claude_cli_exe="",
            cwd="/project",
            enable_tokensave_tools=True,
            tokensave_exe="/usr/bin/tokensave",
        )
        assert len(agentic_called) == 1
        assert text == "agentic result"
        assert err is None

    def test_max_tokens_adaptive_tiny_prompt(self, monkeypatch):
        """Tiny prompts use the 1000-token cap."""
        captured = []

        def fake_llm(cfg, sys, usr, max_tokens=1500):
            captured.append(max_tokens)
            return "result"

        monkeypatch.setattr("helpers.llm._call_llm", fake_llm)

        # Tiny prompt < 1500 chars total
        dispatch_llm(
            llm_cfg={"provider": "openai"},
            system_prompt="s",
            user_prompt="u",
            claude_cli_exe="",
            cwd=None,
        )
        assert captured[0] == 1000

    def test_max_tokens_adaptive_large_prompt(self, monkeypatch):
        """Large prompts use the 2000-token cap."""
        captured = []

        def fake_llm(cfg, sys, usr, max_tokens=1500):
            captured.append(max_tokens)
            return "result"

        monkeypatch.setattr("helpers.llm._call_llm", fake_llm)

        dispatch_llm(
            llm_cfg={"provider": "openai"},
            system_prompt="s" * 3000,
            user_prompt="u" * 2000,
            claude_cli_exe="",
            cwd=None,
        )
        assert captured[0] == 2000
