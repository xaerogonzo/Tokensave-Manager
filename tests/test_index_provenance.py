"""tests/test_index_provenance.py — which tokensave built this graph, when known.

The module exists because ``last_indexed_version`` was read as provenance and
is not (tokensave advances it on minor upgrades without re-indexing). These
tests pin the two ways the replacement could quietly lie in the same way:

* recording provenance for a run that did not rebuild the graph, and
* letting a record vouch for a graph it no longer describes.
"""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

from helpers import index_provenance as ip


def _index(root, full_sync_at=1788905550, *, key=True):
    ts = os.path.join(root, ".tokensave")
    os.makedirs(ts, exist_ok=True)
    conn = sqlite3.connect(os.path.join(ts, "tokensave.db"))
    conn.execute("CREATE TABLE IF NOT EXISTS metadata "
                 "(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("DELETE FROM metadata")
    if key:
        conn.execute("INSERT INTO metadata VALUES ('last_full_sync_at', ?)",
                     (str(full_sync_at),))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("args,expected", [
    (["init"], True),
    (["init", "--no-git-hook"], True),
    (["sync", "--force"], True),
    (["sync"], False),                 # incremental must never launder a graph
    (["status"], False),
    ([], False),
    (None, False),
])
def test_full_index_argv(args, expected):
    assert ip.is_full_index_argv(args) is expected


def test_reads_last_full_sync_at_read_only(tmp_path):
    _index(str(tmp_path), 1788905550)
    assert ip.read_last_full_sync_at(str(tmp_path)) == (1788905550, "")


def test_missing_index_and_missing_key_are_reported_not_zero(tmp_path):
    value, problem = ip.read_last_full_sync_at(str(tmp_path))
    assert value is None and "no tokensave index" in problem
    _index(str(tmp_path), key=False)
    value, problem = ip.read_last_full_sync_at(str(tmp_path))
    assert value is None and "no last_full_sync_at" in problem


def test_record_written_only_when_the_timestamp_advanced(tmp_path):
    root = str(tmp_path)
    _index(root, 1788905550)
    written, reason = ip.record_full_index(root, "7.12.1", 1788905550)
    assert not written and "did not advance" in reason
    assert not os.path.exists(ip.provenance_path(root))

    _index(root, 1789300000)
    assert ip.record_full_index(root, "7.12.1", 1788905550) == (True, "")
    with open(ip.provenance_path(root), encoding="utf-8") as fh:
        rec = json.load(fh)
    assert rec == {"version": "7.12.1", "full_sync_at": 1789300000,
                   "db": ".tokensave/tokensave.db"}


def test_record_refuses_an_unknown_version(tmp_path):
    _index(str(tmp_path), 1789300000)
    written, reason = ip.record_full_index(str(tmp_path), "", None)
    assert not written and "version was unknown" in reason


def test_private_dir_ignores_itself(tmp_path):
    """A provenance file must not show up as untracked in the user's repo."""
    root = str(tmp_path)
    _index(root, 1789300000)
    ip.record_full_index(root, "7.12.1", None)
    with open(os.path.join(root, ".tokensave-manager", ".gitignore"),
              encoding="utf-8") as fh:
        assert fh.read() == "*\n"


def test_existing_gitignore_in_private_dir_is_left_alone(tmp_path):
    root = str(tmp_path)
    _index(root, 1789300000)
    d = os.path.join(root, ".tokensave-manager")
    os.makedirs(d)
    with open(os.path.join(d, ".gitignore"), "w", encoding="utf-8") as fh:
        fh.write("custom\n")
    ip.record_full_index(root, "7.12.1", None)
    with open(os.path.join(d, ".gitignore"), encoding="utf-8") as fh:
        assert fh.read() == "custom\n"


def test_verdict_current_and_built_by_older(tmp_path):
    root = str(tmp_path)
    _index(root, 1789300000)
    ip.record_full_index(root, "7.12.1", None)
    assert ip.verdict(root, "7.12.1").state == ip.CURRENT
    older = ip.verdict(root, "7.13.0")
    assert older.state == ip.BUILT_BY_OLDER and older.built_by == "7.12.1"


def test_verdict_unknown_without_a_record(tmp_path):
    root = str(tmp_path)
    _index(root, 1789300000)
    v = ip.verdict(root, "7.12.1")
    assert v.state == ip.UNKNOWN and "not recorded" in v.reason
    assert v.full_sync_at == 1789300000


def test_verdict_unknown_after_an_external_full_index(tmp_path):
    root = str(tmp_path)
    _index(root, 1789300000)
    ip.record_full_index(root, "7.12.1", None)
    _index(root, 1789400000)                 # someone ran `sync --force` in a shell
    v = ip.verdict(root, "7.12.1")
    assert v.state == ip.UNKNOWN and "outside the Manager" in v.reason


def test_verdict_unknown_when_the_record_names_another_db(tmp_path):
    root = str(tmp_path)
    _index(root, 1789300000)
    ip.record_full_index(root, "7.12.1", None)
    with open(ip.provenance_path(root), encoding="utf-8") as fh:
        rec = json.load(fh)
    rec["db"] = ".tokensave/branches/feature.db"
    with open(ip.provenance_path(root), "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    v = ip.verdict(root, "7.12.1")
    assert v.state == ip.UNKNOWN and "different index DB" in v.reason


@pytest.mark.parametrize("payload", ["{not json", '{"version": 7}', "[]"])
def test_malformed_record_is_unknown_not_current(tmp_path, payload):
    root = str(tmp_path)
    _index(root, 1789300000)
    os.makedirs(os.path.join(root, ".tokensave-manager"))
    with open(ip.provenance_path(root), "w", encoding="utf-8") as fh:
        fh.write(payload)
    assert ip.verdict(root, "7.12.1").state == ip.UNKNOWN


def test_verdict_unknown_when_the_running_version_is_unknown(tmp_path):
    root = str(tmp_path)
    _index(root, 1789300000)
    ip.record_full_index(root, "7.12.1", None)
    assert ip.verdict(root, "").state == ip.UNKNOWN


def test_begin_is_none_for_an_incremental_sync(tmp_path):
    assert ip.begin_full_index(["sync"], str(tmp_path), "tokensave.exe") is None


def test_begin_then_finish_records_the_version_read_before_the_run(
        tmp_path, monkeypatch):
    """The version is captured BEFORE the spawn, then proven by the timestamp."""
    from helpers import doctor_rules
    root = str(tmp_path)
    _index(root, 1788905550)
    monkeypatch.setattr(doctor_rules, "_installed_extractor_version",
                        lambda exe: ("7.12.1", ""))
    pending = ip.begin_full_index(["sync", "--force"], root, "tokensave.exe")
    assert pending.version == "7.12.1"
    assert pending.full_sync_before == 1788905550
    # an upgrade landing mid-run must not change what gets credited
    monkeypatch.setattr(doctor_rules, "_installed_extractor_version",
                        lambda exe: ("7.13.0", ""))
    _index(root, 1789300000)
    assert ip.finish_full_index(pending) == (
        "Recorded: this graph was fully built by tokensave 7.12.1.")
    assert ip.verdict(root, "7.12.1").state == ip.CURRENT


def test_finish_says_why_when_nothing_was_rebuilt(tmp_path, monkeypatch):
    from helpers import doctor_rules
    root = str(tmp_path)
    _index(root, 1788905550)
    monkeypatch.setattr(doctor_rules, "_installed_extractor_version",
                        lambda exe: ("7.12.1", ""))
    pending = ip.begin_full_index(["init"], root, "tokensave.exe")
    line = ip.finish_full_index(pending)
    assert line.startswith("Index provenance not recorded")
    assert "did not advance" in line


def test_report_is_a_no_op_for_commands_that_do_not_rebuild():
    lines = []
    ip.report_full_index(None, lines.append)
    assert lines == []
