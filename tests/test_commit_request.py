"""tests/test_commit_request.py — helpers/commit_request.py (pure logic).

The commit-request handoff file contract: external tools write
.tokensave-manager/commit_request.json; the manager loads, validates,
and eventually consumes it.
"""
from __future__ import annotations

import json
import os

from helpers.commit_request import (
    clear_commit_request,
    load_commit_request,
    request_path,
    write_commit_request,
)


def test_round_trip(tmp_path):
    root = str(tmp_path)
    p = write_commit_request(root, ["src/app.py", r"tests\test_app.py"],
                             suggested_scope="feat(app)",
                             note="lazy-load pystray",
                             created_at="2026-06-09 14:30")
    assert os.path.isfile(p)
    req = load_commit_request(root)
    assert req["files"] == ["src/app.py", "tests/test_app.py"]  # normalised
    assert req["suggested_scope"] == "feat(app)"
    assert req["note"] == "lazy-load pystray"
    assert req["created_at"] == "2026-06-09 14:30"


def test_missing_file_returns_none(tmp_path):
    assert load_commit_request(str(tmp_path)) is None


def test_invalid_json_returns_none(tmp_path):
    p = request_path(str(tmp_path))
    os.makedirs(os.path.dirname(p))
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert load_commit_request(str(tmp_path)) is None


def test_files_not_a_list_returns_none(tmp_path):
    p = request_path(str(tmp_path))
    os.makedirs(os.path.dirname(p))
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"files": "src/app.py"}, fh)
    assert load_commit_request(str(tmp_path)) is None


def test_empty_files_returns_none(tmp_path):
    write_commit_request(str(tmp_path), [])
    assert load_commit_request(str(tmp_path)) is None


def test_optional_fields_default_to_empty(tmp_path):
    p = request_path(str(tmp_path))
    os.makedirs(os.path.dirname(p))
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"files": ["a.py"]}, fh)
    req = load_commit_request(str(tmp_path))
    assert req == {"files": ["a.py"], "suggested_scope": "",
                   "note": "", "created_at": ""}


def test_clear_is_idempotent(tmp_path):
    root = str(tmp_path)
    write_commit_request(root, ["a.py"])
    clear_commit_request(root)
    assert load_commit_request(root) is None
    clear_commit_request(root)          # second call: no error
