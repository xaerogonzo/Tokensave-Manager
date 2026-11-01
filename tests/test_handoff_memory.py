"""The ledger's decisions mirrored into tokensave's decision memory.

The memory is a derived index, so the properties that matter are that a retry
never duplicates, that a half-finished run resumes, that "could not find out" is
never treated as "nothing there", and that the ledger is never the casualty.
The fake below behaves like `tokensave tool record_decision` / `session_recall`
(measured: recall by a quoted id finds it through the reason, and returns tags).
"""
import json

import pytest

from helpers import handoff_ledger as hl
from helpers import handoff_memory as hm


class FakeTokensave:
    """An in-memory decision store behind the `_call` seam."""

    def __init__(self):
        self.rows = []
        self.fail_record_after = None     # allow N records, then fail
        self.fail_recall = False
        self.calls = []

    def __call__(self, exe, tool, project, payload):
        self.calls.append((tool, payload))
        if not exe:
            return None, "tokensave is not configured (no tokensave_exe)"
        if tool == "session_recall":
            if self.fail_recall:
                return None, "recall broke"
            needle = payload["query"].strip('"')
            found = [r for r in self.rows if needle in (r["reason"] or "")]
            return {"decisions": found}, ""
        if self.fail_record_after is not None and \
                sum(1 for c in self.calls if c[0] == "record_decision") > self.fail_record_after:
            return None, "record broke"
        self.rows.append({"id": len(self.rows) + 1, "text": payload["text"],
                          "reason": payload.get("reason"), "tags": payload["tags"],
                          "files": payload.get("files", [])})
        return {"id": len(self.rows), "status": "recorded"}, ""

    def records(self):
        return [c for c in self.calls if c[0] == "record_decision"]


@pytest.fixture
def fake(monkeypatch):
    f = FakeTokensave()
    monkeypatch.setattr(hm, "_call", f)
    return f


def _entry(decisions=("use a ledger", "keep it local"), **kw):
    return hl.build_entry(agent="cursor", session_id="s1", goal="wire it",
                          decisions=list(decisions), files=["src/a.py"], **kw)


def test_every_decision_is_written_once_with_its_handoff_tag(fake):
    entry = _entry()
    got = hm.mirror_entry("p", entry, "ts.exe")
    assert got.state == hm.STATE_MIRRORED and got.recorded == 2
    assert [r["text"] for r in fake.rows] == ["use a ledger", "keep it local"]
    assert all(hm.id_tag(entry["handoff_id"]) in r["tags"] for r in fake.rows)
    assert "handoff" in fake.rows[0]["tags"] and "agent:cursor" in fake.rows[0]["tags"]
    assert entry["handoff_id"] in fake.rows[0]["reason"] and "wire it" in fake.rows[0]["reason"]
    assert fake.rows[0]["files"] == ["src/a.py"]


def test_a_second_run_writes_nothing(fake):
    entry = _entry()
    hm.mirror_entry("p", entry, "ts.exe")
    again = hm.mirror_entry("p", entry, "ts.exe")
    assert again.state == hm.STATE_ALREADY and again.recorded == 0
    assert len(fake.rows) == 2


def test_a_half_finished_run_resumes_instead_of_duplicating(fake):
    entry = _entry(("one", "two", "three"))
    fake.fail_record_after = 1
    first = hm.mirror_entry("p", entry, "ts.exe")
    assert first.state == hm.STATE_PENDING and first.recorded == 1
    fake.fail_record_after = None
    second = hm.mirror_entry("p", entry, "ts.exe")
    assert second.state == hm.STATE_MIRRORED and second.recorded == 2
    assert sorted(r["text"] for r in fake.rows) == ["one", "three", "two"]


def test_a_failed_lookup_writes_nothing(fake):
    """Unknown is not empty: writing on a failed lookup is how a retry duplicates."""
    fake.fail_recall = True
    got = hm.mirror_entry("p", _entry(), "ts.exe")
    assert got.state == hm.STATE_PENDING and "recall broke" in got.error
    assert fake.records() == []


def test_a_wiped_memory_is_repopulated_from_the_ledger(fake):
    entry = _entry()
    hm.mirror_entry("p", entry, "ts.exe")
    fake.rows.clear()                                     # database rebuilt
    assert hm.mirror_entry("p", entry, "ts.exe").recorded == 2


def test_missing_tokensave_is_pending_not_an_exception(fake):
    got = hm.mirror_entry("p", _entry(), "")
    assert got.state == hm.STATE_PENDING and "not configured" in got.error


def test_only_decisions_are_mirrored(fake):
    only_steps = hl.build_entry(agent="claude", goal="g", next_steps=["do x"],
                                open_questions=["why?"])
    marker = hl.build_entry(agent="cursor", session_id="s", kind=hl.KIND_SESSION,
                            source=hl.SOURCE_HOOK)
    assert hm.mirror_entry("p", only_steps, "ts.exe").state == hm.STATE_NONE
    assert hm.mirror_entry("p", marker, "ts.exe").state == hm.STATE_NONE
    assert fake.calls == []


def test_mirror_walks_the_ledger_and_can_be_limited_to_one_entry(fake, tmp_path):
    project = str(tmp_path / "proj")
    import os
    os.makedirs(project)
    a, b = _entry(("alpha",)), hl.build_entry(agent="claude", goal="g2", decisions=["beta"])
    hl.append(project, a)
    hl.append(project, b)
    results, error = hm.mirror(project, "ts.exe", only=b["handoff_id"])
    assert error == "" and [r.handoff_id for r in results] == [b["handoff_id"]]
    assert [r["text"] for r in fake.rows] == ["beta"]
    results, _ = hm.mirror(project, "ts.exe")
    assert {r.handoff_id: r.state for r in results} == {
        a["handoff_id"]: hm.STATE_MIRRORED, b["handoff_id"]: hm.STATE_ALREADY}


def test_an_unreadable_ledger_is_an_error_not_an_empty_result(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(hl, "read", lambda *a, **k: hl.LedgerRead(
        hl.STATE_UNREADABLE, error="locked"))
    assert hm.mirror(str(tmp_path), "ts.exe") == ([], "locked")


def test_the_real_call_shape_and_failure_modes(monkeypatch):
    import subprocess

    seen = {}

    class Done:
        returncode, stdout, stderr = 0, '{"status": "recorded"}', ""

    def run(argv, **kw):
        seen["argv"] = argv
        return Done()
    monkeypatch.setattr(hm.subprocess, "run", run)
    reply, why = hm._call("ts.exe", "record_decision", "D:/p", {"text": "x"})
    assert reply == {"status": "recorded"} and why == ""
    assert seen["argv"][:5] == ["ts.exe", "tool", "record_decision", "--project", "D:/p"]
    assert json.loads(seen["argv"][6]) == {"text": "x"}

    Done.returncode, Done.stderr = 2, "boom\nmore"
    assert hm._call("ts.exe", "t", "p", {}) == (None, "tokensave t failed: boom")
    Done.returncode, Done.stdout = 0, "not json"
    assert hm._call("ts.exe", "t", "p", {})[0] is None

    def raises(argv, **kw):
        raise subprocess.TimeoutExpired("ts", 1)
    monkeypatch.setattr(hm.subprocess, "run", raises)
    assert hm._call("ts.exe", "t", "p", {})[0] is None
