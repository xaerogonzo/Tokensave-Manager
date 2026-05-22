# GitHub Guide for Complete Beginners

This guide assumes you have never used git or GitHub before.
By the end you will have your project backed up online, with a full history of every
change you made, and a downloadable release that anyone can install.

---

## Table of Contents

1. [What is git? What is GitHub?](#1-what-is-git-what-is-github)
2. [Key concepts in plain English](#2-key-concepts-in-plain-english)
3. [First-time setup](#3-first-time-setup)
4. [Putting your project on GitHub](#4-putting-your-project-on-github)
5. [Your daily workflow](#5-your-daily-workflow)
6. [Working with branches](#6-working-with-branches)
7. [Publishing a release (.exe)](#7-publishing-a-release-exe)
8. [Common problems and fixes](#8-common-problems-and-fixes)
9. [Glossary](#9-glossary)

---

## 1. What is git? What is GitHub?

**Git** is a program that runs on your computer and remembers every change you make
to a project. Think of it like infinite undo — but instead of Ctrl+Z, you manually
decide when to save a checkpoint (called a *commit*). You can always go back to any
checkpoint, compare two versions, or run experiments without risking your working code.

**GitHub** is a website that stores a copy of your git history online. It's free for
public and private projects. It does three things:

- **Backup** — your code is safe even if your hard drive dies
- **Sharing** — anyone can see your project (if you make it public)
- **Releases** — lets people download your `.exe` without installing Python

Git and GitHub are separate tools. You use git locally; GitHub is just a remote
destination you push to.

---

## 2. Key concepts in plain English

| Term | What it actually means |
|------|------------------------|
| **Repository (repo)** | Your project folder + its entire change history |
| **Commit** | A saved snapshot with a short message you write |
| **Branch** | A parallel copy of the project for safe experimenting |
| **master / main** | The name of the default branch (your "official" version) |
| **Remote** | A copy of the repo stored elsewhere — on GitHub, in this case |
| **origin** | The standard nickname for your GitHub remote |
| **Push** | Send your local commits to GitHub |
| **Pull** | Download commits from GitHub to your machine |
| **Working tree** | The current state of your files — changes not yet committed |
| **Staging** | Choosing which changes to include in the next commit |
| **Merge** | Combining changes from one branch into another |
| **Clone** | Downloading a repo from GitHub to a new machine |

---

## 3. First-time setup

### Step 1 — Install git

Download from [git-scm.com](https://git-scm.com). Use the default options during
installation. This also installs **Git Credential Manager (GCM)**, which handles
GitHub login automatically via your browser — you never have to manage passwords
or tokens manually.

### Step 2 — Set your identity

Every commit is stamped with your name and email. Open the Git tab in TokenSave
Manager, click **🐙 GitHub…**, and fill in Step 1. Or run in a terminal:

```
git config --global user.name  "Your Name"
git config --global user.email "you@example.com"
```

This is stored globally and applies to every project on this machine.

### Step 3 — Create a free GitHub account

Go to [github.com/signup](https://github.com/signup). A free account gives you
unlimited public and private repositories.

---

## 4. Putting your project on GitHub

### Step 1 — Initialise git in your project

In TokenSave Manager, right-click the project → **🔧 Git Init**. This creates a
`.git` folder and optionally makes your first commit ("Initial commit").

A `.gitignore` file is created automatically — it tells git to ignore Python cache
files, Nuitka build output, and the tokensave index (machine-specific binary files
you don't want in your history).

### Step 2 — Create a repository on GitHub

1. Go to [github.com/new](https://github.com/new)
2. Give it a name (e.g. `tokensave-manager`)
3. Leave it **Public** (or Private if you prefer)
4. **Do NOT** check "Add a README file" or "Add .gitignore" — you already have those
5. Click **Create repository**
6. Copy the **HTTPS URL** shown (looks like `https://github.com/you/tokensave-manager.git`)

### Step 3 — Connect your project to GitHub

In the Git tab, click **🐙 GitHub…** and go to Step 4. Paste the URL and click Set.
The Remote label in the header will update immediately.

### Step 4 — Push for the first time

Click **⬆ Push**. A browser window will open asking you to log in to GitHub.
Log in once — Git Credential Manager caches your credentials and future pushes
happen silently without a browser.

Your project is now on GitHub. Visit your repo URL to see it.

---

## 5. Your daily workflow

```
Pull → Edit → Commit → Edit → Commit → Push
```

### Start of session

1. Switch to the **Git tab**
2. Click **⟳ Refresh**
3. If a remote is set, click **⬇ Pull** — picks up any changes you don't have yet

### While working

Edit files normally in your editor or via Claude. The Working Tree list shows
what has changed. Click any file to see the exact diff (green = added lines,
red = removed lines).

### Saving a checkpoint (committing)

1. Click **📝 Commit…**
2. A message is suggested based on what files changed — edit it if you like
3. Keep the "Stage all changes" box checked to include everything
4. Click **Commit**

**How often should you commit?**
Whenever you finish one logical thing. "Add dark mode" is a good commit.
"Everything I did today" is a less useful commit. Small commits make your history
easy to read and make it easier to undo specific things later.

### End of session

Click **⬆ Push** to send your commits to GitHub.

---

## 6. Working with branches

Branches let you try out an idea without touching your stable code.

### Create a branch

1. Click **🌿 New Branch**
2. Give it a descriptive name: `feature/dark-mode`, `fix/crash-on-startup`
3. Check "Switch to this branch immediately"
4. Work normally — commits go to this branch only

### Switch back to master

Click **🔀 Switch Branch…**, select `master`, click Switch.

> **Important:** commit your changes before switching. If you have uncommitted
> changes that conflict with the target branch, the switch will fail.

### Merge a branch back into master

Once your experiment is tested and you want its commits in `master`:

1. **🔀 Switch Branch…** → `master`
2. **⬇ Pull** (in case the remote master moved while you were working)
3. **⇄ Merge…** → pick your feature branch
   - Confirmation will read: *"Merge 'feature/dark-mode' INTO 'master'?"*
4. **⬆ Push** — master now has the merged commits on GitHub too

> **Conflicts?** If both branches changed the same lines, git can't auto-merge
> and the manager pops a dialog telling you exactly what to do — open the
> affected files in your editor, fix the `<<<<<<` markers, then **📝 Commit**.
> Prefer to back out instead? Open a terminal in the project folder and run
> `git merge --abort`.

> **Dirty working tree?** Merge fails if you have uncommitted changes on the
> destination branch. Commit or stash them first, then re-run Merge.

### Delete a finished branch

Click **🗑 Delete Branch…**, select the branch, click Delete. If it has unmerged
changes you'll be warned and given the option to force-delete.

After the local delete succeeds, the manager checks whether `origin/<branch>`
exists on GitHub. If it does, you'll get a second prompt: **"Also delete from
GitHub?"** Say yes and the manager runs `git push origin --delete <branch>`
for you — no terminal needed, and no orphaned remote branch left behind.

---

## 7. Publishing a release (.exe)

A GitHub Release lets anyone download your compiled `.exe` without needing Python
installed. This is how you distribute your application to other people.

### Requirements

- **GitHub CLI (`gh`)** — a small command-line tool from [cli.github.com](https://cli.github.com).
  Free, installs in under a minute. The TokenSave Manager uses it to create releases.
- **A built .exe** — run `build.bat` first to compile `dist\tokensave-manager.exe`.

### Creating a release — the Release Wizard (recommended)

Since v1.0.4, the **📦 Release…** button on the Git tab opens a full release wizard:

1. Open the Git tab on the project you want to release
2. Click **📦 Release…**
3. The wizard auto-detects your last tag and pre-fills:
   - **Version**: Patch / Minor / Major radio (biased by what your commits did
     — any `feat:` defaults to Minor, anything breaking defaults to Major)
   - **Title**: derived from the highest-priority commit subject
   - **Release notes**: grouped Added / Fixed / Changed / Docs / Breaking
     sections, auto-drafted from your conventional-commit messages
     (`feat:`, `fix:`, `chore:`, etc.) since the last tag
4. Edit the notes textarea as needed (or click 🔄 Regenerate to rebuild from commits)
5. Pick whether to **run the build first** (auto-detects `build.ps1` / `build.bat`)
6. Pick whether to **update CHANGELOG.md** (auto-disabled if no `## [Unreleased]`
   anchor exists)
7. Click **🚀 Publish**

The wizard then runs locally (zero token cost, no LLM round-trip):

1. **Build** via `powershell -File build.ps1` or `cmd.exe /c build.bat`
2. **Zip** `dist/` flat as `<repo>-<tag>-windows.zip`
3. **Patch CHANGELOG.md** — insert the new section directly below `[Unreleased]`
   (idempotent — retrying replaces the section, never duplicates)
4. **Stage only CHANGELOG.md** and `git commit -m "chore: release prep for <tag>"`
   (never blanket-commits — unrelated WIP can't sneak into the release commit)
5. **Tag locally** with `git tag -a <tag> -m <title>` so your local tree records
   the release (vital — `gh release create` only tags remotely)
6. **Push** commits + tag via `git push origin HEAD --follow-tags`
7. **Create the GitHub Release** via `gh release create <tag> --notes-file <tmp> <zip>`

If any step fails, the wizard shows a copy-pasteable recovery command. The
notes-file is preserved on the final-step failure so you can retry by hand
without retyping anything.

**Pre-flight check**: the wizard refuses to open if the working tree has
uncommitted changes in anything other than `CHANGELOG.md` (the wizard owns
that file). This keeps your release-prep commit laser-focused.

### What the zip contains and where it lands

The release zip is named `<repo>-<tag>-windows.zip` (e.g. `MyApp-v1.0.0-windows.zip`)
and lands **next to your repo root** — NOT inside `dist/`. Example: a project at
`D:\projects\MyApp` releasing `v1.0.0` produces `D:\projects\MyApp\MyApp-v1.0.0-windows.zip`.

**Contents are flat.** When a user downloads the zip and extracts it, files sit
at the root — `tokensave-manager.exe`, `manager-config.json`, etc. — NOT nested
inside a `dist/` folder. (This matters: users expect to double-click the .exe
right after extracting, not hunt one folder deeper.)

**What goes in is whatever your build script puts in `dist/`.** The wizard
doesn't pick and choose — it zips everything `build.ps1` (or `build.bat`)
staged into `dist/`. If you want to exclude something from the release,
exclude it from the build's staging step.

**The zip stays on disk after publishing** — it isn't auto-deleted. Add
`*-windows.zip` to your `.gitignore` so the auto-commit Stop hook doesn't
package release artefacts into the next commit. (The Manager's own repo
does this; new projects you scaffold do not — add the line manually if
you'll be cutting releases.)

### Quick-fire releases via the GitHub Setup wizard (legacy)

The older `🐙 GitHub…` → 📦 Releases section is still there for first-time
setup. It uses `gh release create --generate-notes` (GitHub's auto-PR-title
list, not the curated `Added / Fixed / Changed` style the wizard produces).
Use the new Release Wizard for any project that already uses conventional
commit prefixes.

### Version numbering convention

- `v1.0.0` — first real release
- `v1.0.1` — small bug fix
- `v1.1.0` — new feature added, no breaking changes
- `v2.0.0` — major rewrite or breaking changes

---

## 8. Common problems and fixes

### "Push failed — authentication"

The first time you push, GitHub needs to verify who you are. Open a terminal in
the project folder and run `git push`. A browser window will open asking you to
authorise the Git Credential Manager. Log in once and you're done — all future
pushes from the manager's Push button work silently.

### "Switch branch failed — uncommitted changes"

You have edited files that conflict with the branch you're switching to. Options:
- **Commit** your changes first (📝 Commit…), then switch
- **Undo** them (↩ Undo Last Commit won't help here — use terminal: `git stash`)

### "Working tree shows files I didn't change"

Line endings can sometimes cause this on Windows. Check your `.gitattributes` file,
or add `* text=auto` to it.

### "I committed the wrong files"

Click **↩ Undo Last Commit**. Your changes come back as uncommitted edits. Fix
what you need, then commit again.

### "I accidentally deleted a file and committed it"

Find the commit hash where the file existed (visible in Recent Commits). In a
terminal: `git checkout <hash> -- path/to/file.py`

### "My branch and GitHub's branch have different commits (diverged)"

This happens if the same repo was edited in two places. You'll need to Pull first,
resolve any conflicts, then Push. The manager will show an error; for now, resolve
this in a terminal.

---

## 9. Glossary

| Word | Plain-English definition |
|------|--------------------------|
| **add** | Tell git to include a file in the next commit (staging) |
| **amend** | Edit the most recent commit instead of creating a new one |
| **branch** | A parallel version of the project |
| **checkout** | Switch to a different branch or restore a file from history |
| **clone** | Download a full copy of a repo from GitHub |
| **commit** | Save a snapshot with a message; a node in the history graph |
| **conflict** | Two edits to the same part of the same file that git can't auto-merge |
| **diff** | A view showing exactly what changed between two versions |
| **fetch** | Download new commits from GitHub without applying them yet |
| **fork** | Your own copy of someone else's public repository |
| **HEAD** | A pointer to the commit you're currently on |
| **index** | Another word for the staging area |
| **init** | Create a new empty git repository in a folder |
| **log** | The list of all commits in the history |
| **merge** | Combine changes from one branch into another |
| **origin** | The standard nickname for the GitHub remote |
| **pull** | Fetch + merge in one step — download and apply new commits |
| **pull request (PR)** | A proposal to merge your branch into someone else's repo (GitHub feature) |
| **push** | Send local commits to GitHub |
| **rebase** | Rewrite commit history (advanced — avoid until comfortable with git) |
| **remote** | A copy of the repo stored somewhere else (e.g. GitHub) |
| **repository (repo)** | A project folder plus its full git history |
| **reset** | Move HEAD to a different commit (can be destructive — use carefully) |
| **revert** | Create a new commit that undoes a previous one (safe) |
| **stash** | Temporarily set aside uncommitted changes without committing them |
| **staging area** | The set of changes selected to go into the next commit |
| **tag** | A named pointer to a specific commit — used for version numbers |
| **working tree** | The actual files on disk, including any uncommitted changes |
