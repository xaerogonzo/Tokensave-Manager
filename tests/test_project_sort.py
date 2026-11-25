"""Projects-tab sort order: pure helper plus the header-click wiring."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from helpers.project_sort import SORTABLE, next_direction, sort_projects


def _p(name, mtime=0, ts=True, cg=False, git=None, has_git=True):
    return {"name": name, "path": f"D:/x/{name}", "mtime": mtime,
            "has_tokensave": ts, "has_codegraph": cg, "has_git": has_git,
            "git_status": git}


def _names(rows):
    return [p["name"] for p in rows]


def _no(_):
    return False


def test_name_is_case_insensitive_and_reversible():
    ps = [_p("beta"), _p("Alpha"), _p("gamma")]
    assert _names(sort_projects(ps, "name", False, _no)) == ["Alpha", "beta", "gamma"]
    assert _names(sort_projects(ps, "name", True, _no)) == ["gamma", "beta", "Alpha"]


def test_synced_newest_first_and_unindexed_last():
    ps = [_p("a", 10), _p("b", 99), _p("c", 500, ts=False)]
    assert _names(sort_projects(ps, "synced", True, _no)) == ["b", "a", "c"]


def test_ties_stay_alphabetical_in_both_directions():
    ps = [_p("z", cg=True), _p("a", cg=True), _p("m")]
    assert _names(sort_projects(ps, "codegraph", True, _no)) == ["a", "z", "m"]
    assert _names(sort_projects(ps, "codegraph", False, _no)) == ["m", "a", "z"]


def test_git_needs_attention_first_and_unchecked_after_clean():
    clean = {"dirty": False, "ahead": 0, "behind": 0}
    dirty = {"dirty": True, "ahead": 0, "behind": 0}
    ps = [_p("clean", git=clean), _p("nogit", has_git=False),
          _p("dirty", git=dirty), _p("pending")]
    assert _names(sort_projects(ps, "git", False, _no)) == \
        ["dirty", "clean", "pending", "nogit"]


def test_scaffold_uses_the_callable():
    ps = [_p("a"), _p("b")]
    assert _names(sort_projects(ps, "scaffold", True, lambda p: p.endswith("b"))) == ["b", "a"]


def test_next_direction():
    assert next_direction("name", False, "name") == ("name", True)
    assert next_direction("name", True, "synced") == ("synced", True)
    assert next_direction("synced", True, "path") == ("path", False)


def test_star_column_is_not_sortable():
    assert "active" not in SORTABLE
