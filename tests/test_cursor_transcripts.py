"""Cursor DESKTOP chats exist only as transcripts, and the Manager used to list none of them.

Measured on one machine: 74 transcripts under `~/.cursor/projects/*/agent-transcripts/`,
49 listed (the terminal agent's `meta.json` chats), 26 invisible, including every recent
desktop chat. A transcript records no folder, and Cursor's workspace folder name is lossy,
so the folder is RESOLVED against known folders, never decoded.

Fixtures are the real record shape with invented content.
"""
import json
import os

from helpers import cursor_tasks as cta
from helpers import cursor_transcript as ct

ID1 = "11111111-aaaa-bbbb-cccc-000000000001"
ID2 = "22222222-aaaa-bbbb-cccc-000000000002"


def _query(text):
    return {"role": "user", "message": {"content": [
        {"type": "text", "text": f"<timestamp>Mon, Nov 9</timestamp>\n<user_query>\n{text}\n</user_query>"}]}}


def _reply(text):
    return {"role": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _transcript(home, project_dir, cid, records, mtime=None):
    folder = os.path.join(home, ".cursor", "projects", project_dir, "agent-transcripts", cid)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, cid + ".jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write((rec if isinstance(rec, str) else json.dumps(rec)) + "\n")
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


def _meta(home, cid, **fields):
    folder = os.path.join(home, ".cursor", "chats", "bucket", cid)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"id": cid, **fields}, fh)


def _repo(parent, name, worktrees=()):
    """A known project; each worktree is registered the way git does it."""
    repo = parent / name
    (repo / ".git").mkdir(parents=True)
    out = []
    for wt in worktrees:
        wt_dir = parent / wt
        wt_dir.mkdir(parents=True, exist_ok=True)
        reg = repo / ".git" / "worktrees" / wt
        reg.mkdir(parents=True)
        (reg / "gitdir").write_text(str(wt_dir / ".git").replace("\\", "/") + "\n")
        out.append(str(wt_dir))
    return str(repo), out


# -- the low-level layer --------------------------------------------------------------

def test_encode_name_collapses_runs_and_ignores_case():
    assert ct.encode_name("D:\\_scratch\\Open-Chem salt") == "d-scratch-open-chem-salt"
    assert ct.encode_name("d:\\_scratch\\open-chem salt") == ct.encode_name("D:\\_SCRATCH\\Open-Chem Salt")


def test_only_a_file_named_for_its_folder_is_a_chat_and_the_newest_duplicate_wins(fake_home):
    a = _transcript(fake_home, "proj-a", ID1, [_query("hi")], mtime=1_000)
    b = _transcript(fake_home, "proj-b", ID1, [_query("hi again")], mtime=2_000)
    open(os.path.join(os.path.dirname(a), "notes.jsonl"), "w").write("{}\n")
    listed = ct.list_transcripts()
    assert [t.session_id for t in listed] == [ID1]
    assert listed[0].path == b and listed[0].project_dir == "proj-b"


def test_the_first_prompt_comes_out_of_its_wrapper(fake_home):
    p = _transcript(fake_home, "p", ID1, [_reply("preamble"), _query("Plan the salt tables"), _query("second")])
    assert ct.first_user_query(p) == "Plan the salt tables"


def test_an_unwrapped_prompt_is_kept_minus_its_timestamp(fake_home):
    rec = {"role": "user", "message": {"content": "<timestamp>t</timestamp>\nplain words"}}
    assert ct.first_user_query(_transcript(fake_home, "p", ID1, [rec])) == "plain words"


def test_a_head_that_ends_inside_the_first_prompt_still_yields_a_title(fake_home):
    long = _query("Start of a very long pasted plan " + "x" * 5000)
    p = _transcript(fake_home, "p", ID1, [long])
    assert ct.first_user_query(p, head_bytes=300).startswith("Start of a very long pasted plan")


def test_no_prompt_and_unreadable_files_are_empty_not_errors(fake_home, tmp_path):
    assert ct.first_user_query(_transcript(fake_home, "p", ID1, [_reply("only me")])) == ""
    assert ct.first_user_query(str(tmp_path / "missing.jsonl")) == ""
    assert ct.first_user_query(_transcript(fake_home, "q", ID2, ["not json", "[1,2]"])) == ""


def test_a_title_is_one_line_masked_and_bounded():
    t = ct.title_from_query("use  key\nsk-abcdefghijklmnopqrstuvwx and then " + "word " * 40)
    assert "\n" not in t and "sk-abcdefghijklmnop" not in t
    assert len(t) <= ct.TITLE_CHARS and t.endswith("...")


# -- resolving a workspace folder -----------------------------------------------------

def test_a_known_project_matches_exactly(tmp_path):
    repo, _ = _repo(tmp_path, "Source")
    res = cta.resolve_workspace(ct.encode_name(repo), cta._known_folders([repo]), [repo])
    assert res["state"] == cta.RES_MATCHED and res["cwd"] == os.path.normpath(repo)


def test_a_registered_worktree_matches_and_knows_its_project(tmp_path):
    repo, (wt,) = _repo(tmp_path, "Source", worktrees=["Source-slice-2"])
    res = cta.resolve_workspace(ct.encode_name(wt), cta._known_folders([repo]), [repo])
    assert res["state"] == cta.RES_MATCHED
    assert res["cwd"] == os.path.normpath(wt) and res["project_path"] == repo


def test_two_folders_with_one_encoding_are_ambiguous_and_none_is_chosen(tmp_path):
    a, _ = _repo(tmp_path, "Foo Bar")
    b, _ = _repo(tmp_path, "Foo-Bar")
    assert ct.encode_name(a) == ct.encode_name(b)
    res = cta.resolve_workspace(ct.encode_name(a), cta._known_folders([a, b]), [a, b])
    assert res["state"] == cta.RES_AMBIGUOUS and res["cwd"] == "" and len(res["candidates"]) == 2


def test_a_scratch_checkout_of_a_known_project_is_inferred_never_matched(tmp_path):
    repo, _ = _repo(tmp_path, "OpenChem Studio")
    res = cta.resolve_workspace("d-scratch-OpenChem-Studio-salt-composition",
                                cta._known_folders([repo]), [repo])
    assert res["state"] == cta.RES_PROJECT_ONLY
    assert res["cwd"] == "" and res["project_path"] == repo


def test_the_most_specific_project_name_wins_a_scratch_prefix(tmp_path):
    short, _ = _repo(tmp_path, "OpenChem")
    long, _ = _repo(tmp_path, "OpenChem Studio")
    res = cta.resolve_workspace("d-scratch-OpenChem-Studio-x1",
                                cta._known_folders([short, long]), [short, long])
    assert res["state"] == cta.RES_PROJECT_ONLY and res["project_path"] == long


def test_two_projects_sharing_a_name_make_a_scratch_name_ambiguous(tmp_path):
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    a, _ = _repo(tmp_path / "one", "Same Name")
    b, _ = _repo(tmp_path / "two", "Same Name")
    res = cta.resolve_workspace("d-scratch-Same-Name-x", cta._known_folders([a, b]), [a, b])
    assert res["state"] == cta.RES_AMBIGUOUS and res["project_path"] == ""


def test_inference_is_limited_to_the_scratch_layout(tmp_path):
    repo, _ = _repo(tmp_path, "Source")
    # Starts with the project's own name, which is exactly what a scratch name does
    # after its prefix: without the `_scratch` layout it must stay unmatched.
    for name in ("Source-extra-checkout", "d-elsewhere-Source-x", "c-users-me-source-x"):
        res = cta.resolve_workspace(name, cta._known_folders([repo]), [repo])
        assert res["state"] == cta.RES_UNMATCHED, name


def test_an_unknown_name_is_unmatched():
    res = cta.resolve_workspace("C-Users-me-AppData-Local-Temp-x", {}, [])
    assert res["state"] == cta.RES_UNMATCHED and res["cwd"] == ""


# -- the scan ----------------------------------------------------------------------

def test_desktop_chats_are_listed_with_title_folder_and_transcript(fake_home, tmp_path):
    repo, (wt,) = _repo(tmp_path, "Source", worktrees=["Source-slice-4"])
    path = _transcript(fake_home, ct.encode_name(wt), ID1, [_query("Build the salt tables"), _reply("ok")])
    (row,) = cta.scan_cursor_transcripts([repo])
    assert row["session_id"] == ID1 and row["title"] == "Build the salt tables"
    assert row["agent"] == "cursor" and row["path"] == path
    assert row["cwd"] == os.path.normpath(wt) and row["resolution"] == cta.RES_MATCHED
    assert row["project_display"] == "Source (worktree Source-slice-4)"


def test_an_unresolved_chat_is_still_listed_with_no_folder_claimed(fake_home):
    _transcript(fake_home, "d-scratch-gone-forever", ID1, [_query("hello")])
    (row,) = cta.scan_cursor_transcripts([])
    assert row["cwd"] == "" and row["resolution"] == cta.RES_UNMATCHED
    assert row["project_display"] == "d-scratch-gone-forever"


def test_a_changed_transcript_gets_a_fresh_title_and_a_deleted_one_disappears(fake_home):
    p = _transcript(fake_home, "p", ID1, [_query("first words")], mtime=1_000)
    assert cta.scan_cursor_transcripts([])[0]["title"] == "first words"
    _transcript(fake_home, "p", ID1, [_query("different words entirely")], mtime=2_000)
    assert cta.scan_cursor_transcripts([])[0]["title"] == "different words entirely"
    os.remove(p)
    assert cta.scan_cursor_transcripts([]) == []


def test_one_malformed_transcript_does_not_end_the_scan(fake_home):
    _transcript(fake_home, "p", ID1, ["garbage \x01 line", "{"])
    _transcript(fake_home, "q", ID2, [_query("fine")])
    rows = {r["session_id"]: r for r in cta.scan_cursor_transcripts([])}
    assert rows[ID1]["title"] == "(untitled)" and rows[ID2]["title"] == "fine"


def test_the_scan_keeps_the_newest_chats_when_capped(fake_home):
    for i, cid in enumerate((ID1, ID2)):
        _transcript(fake_home, "p", cid, [_query(f"chat {i}")], mtime=1_000 + i)
    assert [r["session_id"] for r in cta.scan_cursor_transcripts([], limit=1)] == [ID2]


# -- merging the terminal agent's rows with the desktop's ----------------------------

def test_a_chat_with_no_metadata_appears_and_one_with_no_transcript_is_untouched(fake_home, tmp_path):
    repo, _ = _repo(tmp_path, "Source")
    _meta(fake_home, ID1, title="Terminal chat", cwd=repo, updatedAtMs=5_000_000)
    _transcript(fake_home, "d-scratch-nowhere", ID2, [_query("desktop only")], mtime=6_000)
    rows = {r["session_id"]: r for r in cta.scan_cursor_sessions([repo])}
    assert set(rows) == {ID1, ID2}
    assert "path" not in rows[ID1] and rows[ID2]["title"] == "desktop only"


def test_merge_takes_metadata_title_and_valid_cwd_and_the_transcript_path(fake_home, tmp_path):
    repo, (wt,) = _repo(tmp_path, "Source", worktrees=["Source-wt"])
    _meta(fake_home, ID1, title="Named in metadata", cwd=repo, updatedAtMs=1_000_000)
    path = _transcript(fake_home, ct.encode_name(wt), ID1, [_query("typed first")], mtime=2_000)
    (row,) = cta.scan_cursor_sessions([repo])
    assert row["title"] == "Named in metadata"
    assert row["cwd"] == repo                                  # a real directory in metadata wins
    assert row["path"] == path
    assert row["last_activity"] == 1_000.0                     # the metadata's own time, not the file's


def test_a_stale_metadata_folder_gives_way_to_an_exact_transcript_match(fake_home, tmp_path):
    repo, (wt,) = _repo(tmp_path, "Source", worktrees=["Source-wt"])
    _meta(fake_home, ID1, cwd=str(tmp_path / "long-gone"), updatedAtMs=1_000_000)
    _transcript(fake_home, ct.encode_name(wt), ID1, [_query("hello there")])
    (row,) = cta.scan_cursor_sessions([repo])
    assert row["cwd"] == os.path.normpath(wt) and row["resolution"] == cta.RES_MATCHED
    assert row["title"] == "hello there"                       # metadata had none


def test_a_stale_metadata_folder_is_kept_when_nothing_better_matches(fake_home, tmp_path):
    gone = str(tmp_path / "long-gone")
    _meta(fake_home, ID1, cwd=gone, updatedAtMs=1_000_000)
    _transcript(fake_home, "d-scratch-nowhere", ID1, [_query("x")])
    (row,) = cta.scan_cursor_sessions([])
    assert row["cwd"] == gone and row["resolution"] == cta.RES_UNMATCHED   # a handoff re-checks and names it


def test_no_cursor_at_all_is_an_empty_list(fake_home):
    assert cta.scan_cursor_sessions([]) == []
