"""The History list: filter by agent and project, group by project, keep the person's place.

Measured on one machine: 275 sessions in one flat table (200 Claude + 75 Cursor), which fold into 39
projects (OpenChem Studio alone is 129, most of them worktree names). Identity is the repository root,
never the label; a worktree is grouped under its repository only by evidence; the group a person
collapsed stays collapsed across a refresh.
"""
import os
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace

import pytest

from helpers import claude_tasks
from helpers import session_groups as sg


def clone(parent, name):
    root = parent / name
    (root / ".git").mkdir(parents=True)
    return root


def worktree(repo, name, *, gitdir=None):
    wt = repo / ".claude" / "worktrees" / name
    wt.mkdir(parents=True)
    registry = repo / ".git" / "worktrees" / name
    registry.mkdir(parents=True, exist_ok=True)
    (wt / ".git").write_text("gitdir: " + (gitdir or str(registry)).replace("\\", "/") + "\n")
    return wt


def row(sid, cwd="", activity=1.0, agent=None, **extra):
    r = {"session_id": sid, "title": f"title {sid}", "cwd": str(cwd), "last_activity": activity,
         "is_recent": False, "project_display": "x"}
    if agent:
        r["agent"] = agent
    r.update(extra)
    return r


IID = lambda r: "sess:%s:%s" % (r.get("agent") or "claude", r["session_id"])        # noqa: E731
VALUES = lambda r: (r["title"],)                                                     # noqa: E731


def plan_of(rows, agent=sg.AGENT_ALL, project=sg.PROJECT_ALL, grouped=True):
    return sg.build_plan(rows, agent, project, grouped, IID, VALUES)


# -- which project a row belongs to ------------------------------------------------------

def test_a_live_worktree_belongs_to_the_repository_named_in_its_git_file(tmp_path):
    repo = clone(tmp_path, "OpenChem Studio")
    wt = worktree(repo, "naming-round-9")
    assert sg.worktree_repository(str(wt)) == str(repo)
    assert sg.project_of(row("a", wt)) == (os.path.normcase(str(repo)), "OpenChem Studio")
    assert sg.project_of(row("b", repo)) == (os.path.normcase(str(repo)), "OpenChem Studio")


def test_a_subfolder_of_a_clone_or_a_worktree_belongs_to_the_same_project(tmp_path):
    repo = clone(tmp_path, "Repo")
    wt = worktree(repo, "wt")
    (repo / "src" / "pkg").mkdir(parents=True)
    (wt / "src").mkdir()
    key = os.path.normcase(str(repo))
    assert sg.project_of(row("a", repo / "src" / "pkg"))[0] == key
    assert sg.project_of(row("b", wt / "src"))[0] == key


def test_a_relationship_that_cannot_be_established_is_never_guessed(tmp_path):
    repo = clone(tmp_path, "Repo")
    sub = tmp_path / "Other" / "mod"
    sub.mkdir(parents=True)
    (sub / ".git").write_text("gitdir: ../.git/modules/mod\n")                  # a submodule, not a worktree
    broken = tmp_path / "Broken"
    broken.mkdir()
    (broken / ".git").write_text("not a gitdir line\n")
    lookalike = tmp_path / "Repo-extra-checkout"                                   # named like Repo, proves nothing
    lookalike.mkdir()
    for folder in (sub, broken, lookalike):
        assert sg.worktree_repository(str(folder)) == ""
        assert sg.project_of(row("x", folder))[0] == os.path.normcase(str(folder))
    assert os.path.normcase(str(repo)) not in {sg.project_of(row("x", f))[0] for f in (sub, broken, lookalike)}


def test_a_removed_claude_worktree_groups_under_its_repository_only_while_that_is_a_checkout(tmp_path):
    repo = clone(tmp_path, "Repo")
    gone = repo / ".claude" / "worktrees" / "finished"                             # not even created
    assert sg.project_of(row("a", gone)) == (os.path.normcase(str(repo)), "Repo")
    plain = tmp_path / "NotARepo"
    (plain / ".claude" / "worktrees" / "finished").mkdir(parents=True)
    key, _ = sg.project_of(row("b", plain / ".claude" / "worktrees" / "finished"))
    assert key != os.path.normcase(str(plain))                                     # not claimed as its parent


def test_a_folder_that_is_gone_and_names_no_repository_is_its_own_project(tmp_path):
    missing = tmp_path / "vanished" / "thing"
    assert sg.project_of(row("a", missing)) == (os.path.normcase(str(missing)), "thing")


def test_a_cursor_chat_uses_the_project_the_resolver_proved_and_an_unresolved_one_has_none(tmp_path):
    repo = clone(tmp_path, "Source")
    assert sg.project_of(row("a", "", agent="cursor", project_path=str(repo)))[1] == "Source"
    assert sg.project_of(row("b", "", agent="cursor")) == (sg.UNRESOLVED_KEY, sg.UNRESOLVED_LABEL)


def test_two_repositories_with_one_name_stay_two_groups_told_apart_by_their_parent_folder(tmp_path):
    a = clone(tmp_path / "work", "app")
    b = clone(tmp_path / "play", "app")
    plan = plan_of([row("1", a, 2.0), row("2", b, 1.0)])
    heads = [e for e in plan.entries if e.kind == "group"]
    assert len(heads) == 2 and heads[0].iid != heads[1].iid
    assert {h.text.split("  (")[0] for h in heads} == {"app (work)", "app (play)"}
    assert set(plan.projects) == {"app (work)", "app (play)"}


# -- the plan -------------------------------------------------------------------------

def test_ungrouped_is_the_flat_list_in_the_callers_order_with_the_callers_ids(tmp_path):
    repo = clone(tmp_path, "R")
    rows = [row("b", repo, 2.0), row("a", repo, 1.0, agent="cursor")]
    plan = plan_of(rows, grouped=False)
    assert [e.iid for e in plan.entries] == ["sess:claude:b", "sess:cursor:a"]
    assert all(e.parent == "" and e.kind == "session" for e in plan.entries)


def test_grouped_rows_sit_under_a_header_with_a_count_and_groups_are_ordered_by_their_newest(tmp_path):
    old, new = clone(tmp_path, "Old"), clone(tmp_path, "New")
    rows = [row("o1", old, 5.0), row("n1", new, 9.0), row("o2", old, 4.0), row("n2", new, 1.0)]
    plan = plan_of(rows)
    heads = [e for e in plan.entries if e.kind == "group"]
    assert [h.text for h in heads] == ["New  (2)", "Old  (2)"]
    by_parent = {}
    for e in plan.entries:
        if e.kind == "session":
            by_parent.setdefault(e.parent, []).append(e.iid)
    assert by_parent[heads[0].iid] == ["sess:claude:n1", "sess:claude:n2"]       # input order kept inside a group
    assert by_parent[heads[1].iid] == ["sess:claude:o1", "sess:claude:o2"]


def test_the_agent_and_project_filters_combine_and_the_project_list_follows_the_agent(tmp_path):
    a, b = clone(tmp_path, "Alpha"), clone(tmp_path, "Beta")
    rows = [row("c1", a, 4.0), row("c2", b, 3.0), row("u1", a, 2.0, agent="cursor"), row("u2", a, 1.0, agent="cursor")]
    assert plan_of(rows, sg.AGENT_CURSOR).projects == ("Alpha",)                  # Beta has no Cursor chat
    only = plan_of(rows, sg.AGENT_CURSOR, "Alpha")
    assert [e.iid for e in only.entries if e.kind == "session"] == ["sess:cursor:u1", "sess:cursor:u2"]
    both = plan_of(rows, sg.AGENT_ALL, "Alpha")
    assert sum(e.kind == "session" for e in both.entries) == 3 and both.total == 4 and both.shown == 3
    assert plan_of(rows, sg.AGENT_CLAUDE, "Beta").shown == 1


def test_a_project_that_is_no_longer_offered_means_all_not_nothing(tmp_path):
    a = clone(tmp_path, "Alpha")
    plan = plan_of([row("c1", a)], project="Aged Out Project")
    assert plan.shown == 1


def test_a_filter_that_matches_nothing_says_so_and_a_full_list_reports_its_size(tmp_path):
    a = clone(tmp_path, "Alpha")
    rows = [row("c1", a)]
    none = plan_of(rows, sg.AGENT_CURSOR)
    assert none.shown == 0 and sg.summary(none) == "No sessions match these filters."
    assert sg.summary(plan_of(rows)) == "1 sessions."
    assert sg.summary(plan_of([row("c1", a), row("c2", a, agent="cursor")], sg.AGENT_CURSOR)).startswith("Showing 1 of 2")


def test_hitting_the_scan_cap_is_reported_never_hidden(tmp_path, monkeypatch):
    monkeypatch.setattr(sg, "SCAN_LIMIT", 3)
    a = clone(tmp_path, "Alpha")
    rows = [row(str(i), a, float(i)) for i in range(3)] + [row("u", a, 9.0, agent="cursor")]
    plan = plan_of(rows)
    assert plan.capped == (sg.AGENT_CLAUDE,)
    assert "newest 3 per agent" in sg.summary(plan) and "Claude reached that" in sg.summary(plan)
    assert plan_of(rows[:2]).capped == ()


def test_group_ids_follow_the_repository_not_the_label(tmp_path):
    repo = clone(tmp_path, "Repo")
    one = plan_of([row("a", repo)]).entries[0].iid
    two = plan_of([row("a", worktree(repo, "wt")), row("b", repo)]).entries[0].iid
    assert one == two and one.startswith("grp:")


# -- putting a plan in a tree ---------------------------------------------------------

class Tree:
    """Just enough ttk.Treeview: nesting, open state, selection and scroll."""

    def __init__(self):
        self.nodes, self.order, self._sel, self.top = {}, [], (), 0.0
        self.calls = []

    def get_children(self, parent=""):
        return tuple(i for i in self.order if self.nodes[i]["parent"] == parent)

    def insert(self, parent, index, iid=None, text="", values=(), open=False):
        if iid in self.nodes:
            raise tk.TclError(f"Item {iid} already exists")
        self.calls.append(("insert", iid))
        self.nodes[iid] = {"parent": parent, "text": text, "values": values, "open": open}
        self.order.append(iid)

    def delete(self, iid):
        self.calls.append(("delete", iid))
        for child in self.get_children(iid):
            self.delete(child)
        del self.nodes[iid]
        self.order.remove(iid)
        self._sel = tuple(i for i in self._sel if i != iid)

    def item(self, iid, option=None, **kw):
        node = self.nodes[iid]
        if option is not None:
            return node[option]
        self.calls.append(("item", iid))
        node.update(kw)

    def selection(self):
        return self._sel

    def selection_set(self, items):
        self._sel = tuple(items)

    def yview(self):
        return (self.top, 1.0)

    def yview_moveto(self, fraction):
        self.top = fraction


def laid_out(tmp_path, names=("Alpha", "Beta")):
    repos = {n: clone(tmp_path, n) for n in names}
    rows = [row(f"{n}-{i}", repos[n], 10.0 - i - k) for k, n in enumerate(names) for i in range(2)]
    return repos, rows


def test_a_first_apply_inserts_groups_open_with_their_rows_beneath(tmp_path):
    _, rows = laid_out(tmp_path)
    tree = Tree()
    sg.apply_plan(tree, plan_of(rows).entries, ())
    groups = [i for i in tree.order if i.startswith("grp:")]
    assert len(groups) == 2 and all(tree.nodes[g]["open"] is True for g in groups)
    assert all(len(tree.get_children(g)) == 2 for g in groups)


def test_the_same_structure_updates_text_only_so_nothing_collapses_or_moves(tmp_path):
    _, rows = laid_out(tmp_path)
    tree = Tree()
    structure = sg.apply_plan(tree, plan_of(rows).entries, ())
    first_group = next(i for i in tree.order if i.startswith("grp:"))
    tree.nodes[first_group]["open"] = False                     # the person collapsed it
    tree.selection_set(("sess:claude:Alpha-1",))
    tree.top = 0.4
    tree.calls.clear()
    again = sg.apply_plan(tree, plan_of(rows).entries, structure)
    assert again == structure
    assert not [c for c in tree.calls if c[0] in ("insert", "delete")]          # not rebuilt
    assert tree.nodes[first_group]["open"] is False and tree.selection() == ("sess:claude:Alpha-1",) and tree.top == 0.4


def test_a_changed_structure_keeps_collapsed_groups_closed_new_groups_open_and_the_selection(tmp_path):
    repos, rows = laid_out(tmp_path)
    tree = Tree()
    structure = sg.apply_plan(tree, plan_of(rows).entries, ())
    alpha = next(i for i in tree.order if i.startswith("grp:") and tree.nodes[i]["text"].startswith("Alpha"))
    tree.nodes[alpha]["open"] = False
    tree.selection_set(("sess:claude:Beta-0", "sess:claude:gone"))
    tree.top = 0.25
    gamma = clone(tmp_path, "Gamma")
    more = plan_of(rows + [row("G-0", gamma, 99.0)])
    sg.apply_plan(tree, more.entries, structure)
    nodes = {n["text"].split("  (")[0]: i for i, n in tree.nodes.items() if i.startswith("grp:")}
    assert tree.nodes[nodes["Alpha"]]["open"] is False                         # still collapsed
    assert tree.nodes[nodes["Gamma"]]["open"] is True                          # new groups start open
    assert tree.selection() == ("sess:claude:Beta-0",)                        # the vanished row is dropped
    assert tree.top == 0.25


def test_switching_between_flat_and_grouped_rebuilds_and_keeps_the_selected_row(tmp_path):
    _, rows = laid_out(tmp_path)
    tree = Tree()
    structure = sg.apply_plan(tree, plan_of(rows, grouped=False).entries, ())
    assert not any(i.startswith("grp:") for i in tree.order)
    tree.selection_set(("sess:claude:Alpha-0",))
    structure = sg.apply_plan(tree, plan_of(rows).entries, structure)
    assert any(i.startswith("grp:") for i in tree.order) and tree.selection() == ("sess:claude:Alpha-0",)
    sg.apply_plan(tree, plan_of(rows, grouped=False).entries, structure)
    assert not any(i.startswith("grp:") for i in tree.order) and tree.selection() == ("sess:claude:Alpha-0",)


def test_a_new_session_in_an_existing_group_rebuilds_without_losing_which_groups_were_open(tmp_path):
    repos, rows = laid_out(tmp_path)
    tree = Tree()
    structure = sg.apply_plan(tree, plan_of(rows).entries, ())
    first = next(i for i in tree.order if i.startswith("grp:"))
    tree.nodes[first]["open"] = False
    sg.apply_plan(tree, plan_of(rows + [row("Alpha-new", repos["Alpha"], 50.0)]).entries, structure)
    assert tree.nodes[first]["open"] is False and "sess:claude:Alpha-new" in tree.nodes


# -- the real widget (a fake can disagree with it) --------------------------------------

def test_apply_plan_works_on_a_real_treeview(tk_root, tmp_path):
    repos, rows = laid_out(tmp_path)
    tree = ttk.Treeview(tk_root, columns=("title",), show="tree headings")
    tree.heading("#0", text="Project")
    tree.column("#0", width=200, minwidth=0, stretch=False)
    structure = sg.apply_plan(tree, plan_of(rows).entries, ())
    groups = [i for i in tree.get_children() if i.startswith("grp:")]
    assert len(groups) == 2 and all(len(tree.get_children(g)) == 2 for g in groups)
    assert all(bool(tree.item(g, "open")) for g in groups)
    tree.item(groups[0], open=False)
    tree.selection_set(("sess:claude:Beta-1",))
    structure = sg.apply_plan(tree, plan_of(rows).entries, structure)               # same: text only
    assert not bool(tree.item(groups[0], "open")) and tree.selection() == ("sess:claude:Beta-1",)
    sg.apply_plan(tree, plan_of(rows + [row("Alpha-new", repos["Alpha"], 50.0)]).entries, structure)
    assert not bool(tree.item(groups[0], "open")) and tree.selection() == ("sess:claude:Beta-1",)
    tree.column("#0", width=0, stretch=False)                                       # the ungrouped look
    sg.apply_plan(tree, plan_of(rows, grouped=False).entries, structure)
    assert all(not i.startswith("grp:") for i in tree.get_children())
    tree.destroy()


# -- the controller ---------------------------------------------------------------------

def controller(tmp_path):
    from controllers.tasks_tab import TasksController
    ctrl = object.__new__(TasksController)
    ctrl._sess_tree = Tree()
    ctrl._sess_tree.column = lambda *a, **k: None
    ctrl._live_ids = frozenset()
    var = lambda value: SimpleNamespace(get=lambda: value["v"], set=lambda x: value.__setitem__("v", x))   # noqa: E731
    state = {"agent": {"v": sg.AGENT_ALL}, "project": {"v": sg.PROJECT_ALL}, "group": {"v": True}}
    ctrl._sess_agent_var, ctrl._sess_project_var, ctrl._sess_group_var = (
        var(state["agent"]), var(state["project"]), var(state["group"]))
    ctrl._sess_project_combo = SimpleNamespace(configure=lambda **k: state.update(values=k["values"]))
    ctrl._sess_note = SimpleNamespace(configure=lambda **k: state.update(note=k["text"]))
    return ctrl, state


def test_the_controller_groups_filters_and_still_remembers_every_row_for_a_handoff(tmp_path):
    a, b = clone(tmp_path, "Alpha"), clone(tmp_path, "Beta")
    rows = [row("1", a, 3.0), row("2", b, 2.0), row("3", a, 1.0, agent="cursor")]
    ctrl, state = controller(tmp_path)
    ctrl._update_sess_tree(rows)
    assert state["values"] == (sg.PROJECT_ALL, "Alpha", "Beta") and state["note"] == "3 sessions."
    state["agent"]["v"], state["project"]["v"] = sg.AGENT_CURSOR, "Alpha"
    ctrl._update_sess_tree(ctrl._sess_raw)
    shown = [i for i in ctrl._sess_tree.order if i.startswith("sess:")]
    assert shown == ["sess:cursor:3"] and state["note"].startswith("Showing 1 of 3")
    assert set(ctrl._sess_rows) == {"sess:claude:1", "sess:claude:2", "sess:cursor:3"}      # nothing forgotten


def test_a_project_that_vanishes_from_the_offered_list_resets_the_filter_to_all(tmp_path):
    a = clone(tmp_path, "Alpha")
    ctrl, state = controller(tmp_path)
    state["project"]["v"] = "Gone"
    ctrl._update_sess_tree([row("1", a)])
    assert state["project"]["v"] == sg.PROJECT_ALL


def test_a_group_header_is_not_a_session_for_double_click_or_right_click(tmp_path):
    a = clone(tmp_path, "Alpha")
    ctrl, _ = controller(tmp_path)
    ctrl._update_sess_tree([row("1", a)])
    header = next(i for i in ctrl._sess_tree.order if i.startswith("grp:"))
    ctrl._sess_tree.selection_set((header,))
    ctrl._sess_open_folder(None)                                                 # must not raise or open a dialog
    popped = []
    ctrl._sess_menu = SimpleNamespace(entryconfigure=lambda *a, **k: popped.append(a),
                                      tk_popup=lambda *a: popped.append("popup"), grab_release=lambda: None)
    ctrl._sess_tree.identify_row = lambda y: header
    ctrl._sess_right_click(SimpleNamespace(y=0, x_root=0, y_root=0))
    assert popped == []


def test_without_the_controls_the_list_is_flat_and_unfiltered_as_before(tmp_path):
    from controllers.tasks_tab import TasksController
    ctrl = object.__new__(TasksController)
    ctrl._sess_tree = Tree()
    ctrl._update_sess_tree([row("1", tmp_path), row("2", tmp_path, 2.0, agent="cursor")])
    assert ctrl._sess_tree.order == ["sess:claude:1", "sess:cursor:2"]


def test_the_scanners_are_asked_for_the_new_cap(monkeypatch):
    import controllers.tasks_tab as tasks_tab
    seen = []
    monkeypatch.setattr(tasks_tab, "scan_sessions", lambda known, limit=50: seen.append(("claude", limit)) or [])
    monkeypatch.setattr(tasks_tab, "scan_cursor_sessions", lambda known, limit=50: seen.append(("cursor", limit)) or [])
    monkeypatch.setattr(tasks_tab, "scan_worktrees", lambda *a, **k: [])
    ctrl = SimpleNamespace(_cfg=SimpleNamespace(git_exe="git"), _post=lambda *a: None, _apply_if_current=None)
    tasks_tab.TasksController._worker(ctrl, 1, None, [], False)
    assert seen == [("claude", sg.SCAN_LIMIT), ("cursor", sg.SCAN_LIMIT)] and sg.SCAN_LIMIT == 200


# -- the scan cache ---------------------------------------------------------------------

def test_an_unchanged_transcript_is_not_reread_and_a_changed_one_is(fake_home, monkeypatch):
    import json
    import helpers.live_sessions as ls
    folder = os.path.join(fake_home, ".claude", "projects", "enc")
    os.makedirs(folder)
    path = os.path.join(folder, "s1.jsonl")

    def write(text):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "custom-title", "customTitle": text, "sessionId": "s1"}) + "\n")
            fh.write(json.dumps({"type": "user", "cwd": os.path.join(fake_home, "P"), "message": {"content": "x"}}) + "\n")
    write("first title")
    reads = []
    real = ls.read_tail_meta
    monkeypatch.setattr(ls, "read_tail_meta", lambda p, *a, **k: reads.append(p) or real(p, *a, **k))
    claude_tasks._SESSION_CACHE.clear()
    assert claude_tasks.scan_sessions([])[0]["title"] == "first title"
    assert claude_tasks.scan_sessions([])[0]["title"] == "first title"
    assert len(reads) == 1                                                      # the second scan used the cache
    write("a second, longer title")
    assert claude_tasks.scan_sessions([])[0]["title"] == "a second, longer title" and len(reads) == 2
