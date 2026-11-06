"""Guards on compiling Agent Policy into a Cursor User Rule card.

The card is a compiled artifact, like the baseline: tests compare rendered
bytes, never parse a policy out of the JSON. A missing or unknown store is a
skip plus paste, not a write into Cursor.exe or state.vscdb.
"""
from __future__ import annotations

import json
import os

from helpers.instruction_policy import InstructionPolicy
from helpers import cursor_user_rules as cur

OFF = InstructionPolicy()
ON = InstructionPolicy(agent_may_commit=True, agent_may_push=True)


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# -- render -----------------------------------------------------------------

def test_on_body_names_the_marker_agents_md_and_the_baked_in_sentence():
    body = cur.render_body(ON)
    assert cur.MARKER in body
    assert "AGENTS.md" in body
    assert "TOKENSAVE-MANAGED" in body
    assert "Only create commits when requested by the user" in body
    assert "force-push" in body.lower() or "force-push" in body
    assert "--no-verify" not in body
    assert "you may commit" in body.lower() or "may commit" in body.lower()


def test_off_body_keeps_the_card_and_points_at_agents_md_deny():
    body = cur.render_body(OFF)
    assert cur.MARKER in body
    assert "AGENTS.md" in body
    assert "do not create" in body.lower() or "do not commit" in body.lower()


def test_paste_starts_with_the_stable_title():
    paste = cur.render_paste(ON)
    assert paste.startswith(cur.RULE_TITLE)
    assert cur.render_body(ON) in paste


def test_on_and_off_bodies_differ():
    assert cur.render_body(ON) != cur.render_body(OFF)


# -- discover ---------------------------------------------------------------

def test_discover_store_is_empty_without_env():
    assert cur.discover_store(environ={}) == ""


def test_discover_store_reads_only_the_override_env():
    path = r"C:\tmp\rules.json"
    assert cur.discover_store(
        environ={"TOKENSAVE_CURSOR_USER_RULES": path, "APPDATA": r"C:\nope"}
    ) == path


# -- plan / apply -----------------------------------------------------------

def test_missing_store_skips_and_pastes_without_creating_a_file(tmp_path):
    result = cur.apply(ON, store_path="", environ={})
    assert result.status == cur.STATUS_SKIPPED
    assert result.changed is False
    assert cur.render_body(ON) in result.paste
    assert list(tmp_path.iterdir()) == []


def test_invalid_json_is_skipped_and_left_untouched(tmp_path):
    path = str(tmp_path / "rules.json")
    _write(path, "{not json")
    before = _read(path)
    result = cur.apply(ON, store_path=path)
    assert result.status == cur.STATUS_SKIPPED
    assert "json" in result.reason.lower() or "parse" in result.reason.lower()
    assert _read(path) == before


def test_missing_rules_key_is_skipped_and_left_untouched(tmp_path):
    path = str(tmp_path / "rules.json")
    _write(path, json.dumps({"version": 1, "other": []}))
    before = _read(path)
    result = cur.apply(ON, store_path=path)
    assert result.status == cur.STATUS_SKIPPED
    assert _read(path) == before


def test_first_apply_creates_the_card_second_is_noop(tmp_path):
    path = str(tmp_path / "rules.json")
    first = cur.apply(ON, store_path=path)
    assert first.status == cur.STATUS_UPDATED
    assert first.changed is True
    data = json.loads(_read(path))
    assert data["version"] == 1
    assert data["rules"][0]["title"] == cur.RULE_TITLE
    assert data["rules"][0]["content"] == cur.render_body(ON)

    second = cur.apply(ON, store_path=path)
    assert second.status == cur.STATUS_UNCHANGED
    assert second.changed is False


def test_other_rules_in_the_array_survive(tmp_path):
    path = str(tmp_path / "rules.json")
    _write(path, json.dumps({
        "version": 1,
        "rules": [{"id": "keep-me", "title": "Keep", "content": "stay"}],
    }))
    cur.apply(ON, store_path=path)
    titles = [row["title"] for row in json.loads(_read(path))["rules"]]
    assert "Keep" in titles
    assert cur.RULE_TITLE in titles


def test_toggle_off_rewrites_body_and_does_not_delete_the_card(tmp_path):
    path = str(tmp_path / "rules.json")
    cur.apply(ON, store_path=path)
    result = cur.apply(OFF, store_path=path)
    assert result.status == cur.STATUS_UPDATED
    rules = json.loads(_read(path))["rules"]
    ours = [row for row in rules if row["title"] == cur.RULE_TITLE]
    assert len(ours) == 1
    assert ours[0]["content"] == cur.render_body(OFF)


def test_dry_run_does_not_write(tmp_path):
    path = str(tmp_path / "rules.json")
    result = cur.apply(ON, store_path=path, dry_run=True)
    assert result.status == cur.STATUS_UPDATED
    assert result.changed is True
    assert not os.path.exists(path)


def test_backup_contains_the_previous_body(tmp_path):
    path = str(tmp_path / "rules.json")
    cur.apply(ON, store_path=path)
    result = cur.apply(OFF, store_path=path, now_ms=123)
    assert result.backup_path
    assert os.path.isfile(result.backup_path)
    backed = json.loads(_read(result.backup_path))
    ours = [row for row in backed["rules"] if row["title"] == cur.RULE_TITLE]
    assert ours[0]["content"] == cur.render_body(ON)


def test_a_path_under_programs_cursor_is_refused(tmp_path):
    path = str(tmp_path / "Programs" / "cursor" / "rules.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    result = cur.apply(ON, store_path=path)
    assert result.status == cur.STATUS_SKIPPED
    assert "Programs" in result.reason or "cursor" in result.reason.lower()
    assert not os.path.exists(path)


def test_log_line_names_status_and_path_or_reason(tmp_path):
    skipped = cur.apply(ON, store_path="", environ={})
    line = cur.log_line(skipped)
    assert "skipped" in line
    path = str(tmp_path / "rules.json")
    updated = cur.apply(ON, store_path=path)
    assert cur.STATUS_UPDATED in cur.log_line(updated)
    assert path in cur.log_line(updated)


def test_preview_lines_name_the_title_and_paste_fallback():
    lines = "\n".join(cur.preview_lines(ON, environ={}))
    assert cur.RULE_TITLE in lines
    assert "paste" in lines.lower()


def test_preview_lines_name_a_store_when_one_is_passed(tmp_path):
    path = str(tmp_path / "rules.json")
    lines = "\n".join(cur.preview_lines(ON, store_path=path))
    assert path in lines
    assert cur.RULE_TITLE in lines


def test_needs_write_is_false_without_a_store():
    assert cur.needs_write(ON, environ={}) is False


def test_needs_write_is_true_when_the_store_file_is_missing(tmp_path):
    path = str(tmp_path / "rules.json")
    assert cur.needs_write(ON, store_path=path) is True
    cur.apply(ON, store_path=path)
    assert cur.needs_write(ON, store_path=path) is False
