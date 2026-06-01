"""Tests for helpers/llm.py error surfacing via thread-local state.

Patching strategy
-----------------
Fix A promoted 'import urllib.request' and 'import urllib.error' to module
scope in helpers/llm.py, so helpers.llm.urllib.request is an accessible
attribute chain. We target it with string-form monkeypatch.setattr to be
explicit about the patching scope and keep tests compatible with future
refactoring.

monkeypatch restores the original after each test — global-state bleed
between sequential tests is not a concern in single-process pytest.

xdist caveat: if parallel workers are ever added, the truly isolation-safe
fix is 'from urllib.request import urlopen' in llm.py + patching
'helpers.llm.urlopen' — that rebinds the local name without touching the
global. For now, monkeypatch cleanup handles isolation.
"""
import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from helpers.llm import _call_llm, get_last_llm_error

_OLLAMA_CFG = {
    "enabled": True,
    "provider": "openai_compatible",
    "base_url": "http://localhost:11434",
    "model": "llama3.2",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

class _MockResp:
    """Context-manager response stub matching http.client.HTTPResponse protocol.

    Implements the full surface that _call_openai_compat/_call_anthropic may
    inspect — missing attributes would cause AttributeError that masks the
    actual failure being tested.
    """
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status
        self.code = status

    def __enter__(self): return self
    def __exit__(self, *a): pass
    def read(self): return self._body
    def getcode(self): return self.code
    def info(self): return {}


def _good_body(content: str = "hello") -> bytes:
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_get_last_llm_error_none_on_fresh_thread():
    """Thread-local _tls has no 'last_error' on a brand-new thread."""
    with ThreadPoolExecutor(max_workers=1) as ex:
        assert ex.submit(get_last_llm_error).result() is None


def test_call_llm_disabled_returns_none_no_error():
    """When cfg['enabled'] is False, _call_llm returns None without setting last_error."""
    def go():
        r = _call_llm({"enabled": False}, "sys", "user")
        return r, get_last_llm_error()
    with ThreadPoolExecutor(max_workers=1) as ex:
        result, err = ex.submit(go).result()
    assert result is None
    assert err is None      # disabled early-exit must NOT pollute last_error


def test_call_llm_url_error_sets_last_error(monkeypatch):
    monkeypatch.setattr(
        "helpers.llm.urllib.request.urlopen",
        lambda *a, **kw: (_ for _ in ()).throw(
            urllib.error.URLError("Connection refused")),
    )
    def go():
        r = _call_llm(_OLLAMA_CFG, "sys", "user")
        return r, get_last_llm_error()
    with ThreadPoolExecutor(max_workers=1) as ex:
        result, err = ex.submit(go).result()
    assert result is None
    assert err is not None
    assert "Connection" in err


def test_call_llm_retries_on_http_500_then_succeeds(monkeypatch):
    """Ollama cold-start 500 → retry → 200 (the reproduced bug)."""
    import helpers.llm as llm_mod
    monkeypatch.setattr(llm_mod.time, "sleep", lambda *_: None)   # no real delay
    calls = [0]

    def urlopen_side_effect(*args, **kwargs):
        calls[0] += 1
        if calls[0] == 1:
            raise urllib.error.HTTPError(
                "http://localhost:11434", 500, "Internal Server Error", {}, None)
        return _MockResp(_good_body("warm"))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen_side_effect)

    def go():
        return _call_llm(_OLLAMA_CFG, "sys", "user"), get_last_llm_error()
    with ThreadPoolExecutor(max_workers=1) as ex:
        result, err = ex.submit(go).result()
    assert result == "warm"          # second attempt succeeded
    assert err is None
    assert calls[0] == 2             # one retry


def test_call_llm_500_persists_after_retries(monkeypatch):
    """Persistent 5xx → actionable cold-start/memory message (not 'is it running')."""
    import helpers.llm as llm_mod
    monkeypatch.setattr(llm_mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda *a, **kw: (_ for _ in ()).throw(urllib.error.HTTPError(
            "http://localhost:11434", 500, "Internal Server Error", {}, None)))

    def go():
        return _call_llm(_OLLAMA_CFG, "sys", "user"), get_last_llm_error()
    with ThreadPoolExecutor(max_workers=1) as ex:
        result, err = ex.submit(go).result()
    assert result is None
    assert err is not None and "500" in err
    assert "cold start" in err.lower() or "num_ctx" in err


def test_call_llm_http_404_sets_last_error(monkeypatch):
    monkeypatch.setattr(
        "helpers.llm.urllib.request.urlopen",
        lambda *a, **kw: (_ for _ in ()).throw(
            urllib.error.HTTPError(
                "http://localhost:11434", 404, "Not Found", {}, None)),
    )
    def go():
        r = _call_llm(_OLLAMA_CFG, "sys", "user")
        return r, get_last_llm_error()
    with ThreadPoolExecutor(max_workers=1) as ex:
        result, err = ex.submit(go).result()
    assert result is None
    assert err is not None
    assert "404" in err


def test_call_llm_timeout_sets_last_error(monkeypatch):
    monkeypatch.setattr(
        "helpers.llm.urllib.request.urlopen",
        lambda *a, **kw: (_ for _ in ()).throw(TimeoutError()),
    )
    def go():
        r = _call_llm(_OLLAMA_CFG, "sys", "user", timeout=5)
        return r, get_last_llm_error()
    with ThreadPoolExecutor(max_workers=1) as ex:
        result, err = ex.submit(go).result()
    assert result is None
    assert err is not None
    assert "Timed out" in err


def test_call_llm_malformed_json_sets_last_error(monkeypatch):
    """Ollama can drop the connection mid-stream, producing truncated JSON.

    _call_llm catches json.JSONDecodeError and stores:
        _tls.last_error = f"Unexpected error: {exc}"
    Asserting "Unexpected error" in err confirms the JSONDecodeError path was
    taken — a generic 'err is not None' check would give a false pass if
    _MockResp were protocol-incomplete and raised AttributeError instead.
    """
    monkeypatch.setattr(
        "helpers.llm.urllib.request.urlopen",
        lambda *a, **kw: _MockResp(b'{ "broken_json": '),
    )
    def go():
        r = _call_llm(_OLLAMA_CFG, "sys", "user")
        return r, get_last_llm_error()
    with ThreadPoolExecutor(max_workers=1) as ex:
        result, err = ex.submit(go).result()
    assert result is None
    assert err is not None
    assert "Unexpected error" in err    # confirms JSONDecodeError path specifically


def test_call_llm_success_clears_previous_error(monkeypatch):
    """_tls.last_error = None at the start of every _call_llm call.

    A successful call after a failing one must leave last_error as None,
    proving the reset happens unconditionally on entry.
    """
    call_count = [0]

    def urlopen_side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise urllib.error.URLError("fail")
        return _MockResp(_good_body("hello"))

    monkeypatch.setattr("helpers.llm.urllib.request.urlopen", urlopen_side_effect)

    def run_both():
        _call_llm(_OLLAMA_CFG, "sys", "user")       # call 1 — fails
        err_after_fail = get_last_llm_error()
        r = _call_llm(_OLLAMA_CFG, "sys", "user")   # call 2 — succeeds
        err_after_success = get_last_llm_error()
        return err_after_fail, r, err_after_success

    with ThreadPoolExecutor(max_workers=1) as ex:
        fail_err, result, success_err = ex.submit(run_both).result()

    assert fail_err is not None     # error was set after first call
    assert result == "hello"        # second call returned content
    assert success_err is None      # error was cleared after second call
