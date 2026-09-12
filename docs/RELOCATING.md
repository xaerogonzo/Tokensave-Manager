<!-- help:relocating -->
# Moving TokenSave Manager

Short version: **move the folder, start the Manager, press the button it
offers.** This document exists to explain why there is a button at all, and what
it deliberately does not touch.

---

## Why moving matters more than it looks

Every retrofitted project's `BASIC_INSTRUCTIONS.md` carries an **absolute** path
to the shared baseline:

```
@D:\Claude Co worker\Token Save Manager Source\templates\project-baseline.md
```

That absolute path is the feature, not an oversight. It is what makes one edit
to `project-baseline.md` reach every wired project at once, with nothing to
re-run. The cost is that this installation's location is load-bearing for the
whole fleet.

The alternatives are worse. A relative path breaks the moment a project lives on
another drive. Copying the baseline into each project destroys the single-source
propagation that is the whole point.

## What actually breaks, and what repairs it

| what | where the old path lives | repaired by |
|---|---|---|
| `template_dir` | `manager-config.json` | the relocation offer |
| the baseline include in every project | each project's `BASIC_INSTRUCTIONS.md` | the relocation offer |
| the MCP wrapper path | Claude's own config files | Settings → Integrations → 🔌 Manage MCP wiring |

Everything else in `manager-config.json` — `tokensave_exe`, `git_exe`,
`python_exe`, `editor_cmd`, the analyzer paths — points at software installed
somewhere else and is **never** rewritten by a move. Neither are `search_roots`,
`project_categories` or any other user data, even when one of them happens to
live inside the Manager's own folder.

## The offer

On startup the Manager asks two questions, and they are not the same question:

**Where am I?** The config records where this installation last ran. Comparing
that to where it is now gives `FIRST_RUN` / `SAME` / `MOVED`. No record is *not*
a move — it just means there is nothing to compare against yet.

**Who owns the fleet?** Derived from the projects themselves, not the config:
which baseline do they actually reach? `OWNED_HERE` / `OWNED_ELSEWHERE` /
`SPLIT` / `UNOWNED`.

When a repair is available you get one dialog showing the complete change —
every config value, every project — and one button. Config is written first,
because the baseline include line is *derived* from `template_dir`; doing it the
other way round would write the old path into every project.

## When it refuses, and why

| situation | what happens |
|---|---|
| the fleet points at **two or more** baselines | refused, with a count per owner |
| **no** project reaches a baseline | refused — that is ordinary wiring, use Instructions → Wire all |
| **some** projects reach none | refused as a bulk action; they are listed |
| the two baselines have **different content** | shown with sizes, dates and hashes, and you must tick to proceed |

That last one is not hypothetical. A source checkout and a `dist/` build can
each carry their own `templates/project-baseline.md`, and if the build is old,
repointing the fleet onto it silently takes every project backwards. The Manager
shows you both files and declines to guess which is newer, because it has no
version to compare — only content.

**The fleet is never repointed automatically.** Two installations on one machine
genuinely disagree about who owns the projects, and a silent repoint would have
them rewriting the same repositories against each other on alternate launches.

## If you have two installations

This is worth knowing even if you never move anything. A source checkout and a
release build write *different* baseline paths into the same projects, so
whichever ran last owns the fleet and the other reports every project as stale —
indefinitely, and correctly.

If that is not what you want, run one of them and leave the other alone. If you
do want to switch, the relocation offer is how: it reports that another
installation owns the projects and lets you take them, which is a deliberate act
rather than something that happens because you opened the wrong shortcut.

## One thing it cannot see

Identity is compared **lexically**. An installation reached through a junction,
a symlink or an 8.3 short name reads as a *different* installation, because the
comparison deliberately does not resolve aliases — the same rule that decides
whether two spellings of a baseline path are one file. If you reach the Manager
through an alias, expect it to think it moved.
