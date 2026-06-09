"""Help-tab topic renderers — git concepts, workflow, GitHub.

Split out of controllers/help_tab.py (Roadmap-8 god-file split).
Each function takes the HelpTabController (``ctl``) and renders
its topic via ``ctl._hw()`` / ``ctl._help_show()`` exactly as the
original method did; the controller keeps 2-line delegates.
"""

from __future__ import annotations

import os
from constants import _BASE_DIR


def git_concepts(ctl):
    _doc = os.path.join(_BASE_DIR, "docs", "GITHUB_GUIDE.md")
    _ask = ("My git push got rejected with 'non-fast-forward' — "
            "why does this happen and how do I fix it in this project?")

    def _fill():
        h1, h2, p, warn, ok, dim, br, ins = ctl._hw()
        h1("Git: What & Why")
        p("Git is a tool that remembers the history of every change you make to "
          "your project. Think of it like infinite undo — but smarter. You decide "
          "when to save a checkpoint, and you can always go back.")
        br()
        h2("Commit — a save point")
        p("A commit is a snapshot of your project at a moment in time. Each one "
          "has a short message you write, like 'fix: typo in README' or "
          "'feat: add dark mode'. Over time, these build up into a history "
          "you can scroll through.")
        br()
        h2("Repository (repo) — the project folder + its history")
        p("When you run Git Init on a project, git creates a hidden .git folder "
          "inside it. That folder stores every commit ever made. The whole thing "
          "— your files plus that history — is called a repository.")
        br()
        h2("Branch — a parallel save slot")
        p("Imagine photocopying your entire project so you can experiment on the "
          "copy without touching the original. That's a branch. The default branch "
          "is usually called 'master' or 'main'. When you're happy with the "
          "experiment you Merge it back. If you hate it, delete the branch and "
          "nothing on main ever changed.")
        br()
        h2("Merge vs Rebase")
        p("Both bring changes from one branch into another, but they look different "
          "in history:")
        ins("  Merge  — creates a single 'merge commit' that joins both branches.\n"
            "           Safe, non-destructive, easy to understand. This manager\n"
            "           always uses merge.\n", "body")
        ins("  Rebase — rewrites your commits as if you had started from the latest\n"
            "           main. Produces a cleaner linear history but rewrites commit\n"
            "           IDs (risky on shared branches). Use with caution.\n", "body")
        br()
        h2("Remote — a copy on GitHub")
        p("A remote is a second home for your repository, stored on GitHub's "
          "servers. It acts as a backup and lets others see your work. The "
          "remote is usually called 'origin'.")
        br()
        h2("Push — upload to GitHub")
        p("After making commits on your machine, Push sends them to GitHub. "
          "Nothing leaves your computer until you Push — commits are purely local "
          "until then.")
        br()
        h2("Pull — download from GitHub")
        p("Pull fetches any commits from GitHub that you don't have yet and "
          "adds them to your local history. Useful if you work on multiple "
          "machines, or if a collaborator pushed something new.")
        br()
        h2("Working tree — uncommitted changes")
        p("The working tree is the current state of your files right now, before "
          "you've committed them. The Git tab shows a list of files that have "
          "changed since your last commit. An 'M' means modified, '?' means "
          "a new file git hasn't seen before, 'D' means deleted.")
        br()
        h2("Staging — choosing what to commit")
        p("Git lets you pick exactly which changes to include in a commit. "
          "The 'Stage all changes' checkbox in the Commit dialog does this "
          "automatically — it stages everything in the working tree, which is "
          "almost always what you want.")
        br()
        h2(".gitignore — telling git what to skip")
        p("A file called .gitignore at the root of your project lists patterns of "
          "files git should never track. Common entries:")
        ins("  __pycache__/      — Python bytecode (rebuilds automatically)\n", "body")
        ins("  *.pyc             — compiled Python files\n", "body")
        ins("  dist/             — Nuitka build output\n", "body")
        ins("  .env              — secrets / API keys (NEVER commit these)\n", "body")
        ins("  *.log             — log files\n", "body")
        p("The Scaffold dialog pre-populates a .gitignore for you based on the "
          "project type. To add a pattern manually, open .gitignore in any editor "
          "and add one pattern per line.")
        br()
        ok("Bottom line: commit often, push when you're done for the day.")
    ctl._help_show(_fill, doc_path=_doc, ask_text=_ask, explain_text=_ask)

def git_workflow(ctl):
    _doc = os.path.join(_BASE_DIR, "docs", "GITHUB_GUIDE.md")
    _ask = ("I merged a branch and got a merge conflict — walk me through "
            "resolving it in this project step by step")

    def _fill():
        h1, h2, p, warn, ok, dim, br, ins = ctl._hw()
        h1("Git: Daily Workflow")
        p("Here's how a typical coding session looks when using the Git tab.")
        br()
        h2("Starting a session")
        ins("  1. Switch to the Git tab\n", "body")
        ins("  2. Click ⟳ Refresh to see the current state\n", "body")
        ins("  3. If there's a remote set, click ⬇ Pull first — picks up any\n"
            "     changes from GitHub before you start editing\n", "body")
        br()
        h2("While you're working")
        p("Edit your files normally. The Working Tree list updates whenever "
          "you Refresh. Click any file in the list to see exactly what changed "
          "(green = added, red = removed).")
        br()
        h2("Saving your work (committing)")
        ins("  1. Click  📝 Commit…\n", "body")
        ins("  2. The dialog shows what files changed and suggests a message\n", "body")
        ins("  3. Edit the message if you like — keep it short and descriptive\n", "body")
        ins("  4. Click Commit\n", "body")
        br()
        p("There's no rule for how often to commit. A good rule of thumb: "
          "commit whenever you finish one thing. Small commits are better than "
          "one huge commit at the end of the day.")
        br()
        h2("Uploading to GitHub (pushing)")
        ins("  1. Click  ⬆ Push\n", "body")
        ins("  2. The output log shows whether it succeeded\n", "body")
        ins("  3. Your commits are now on GitHub — backed up and shareable\n", "body")
        br()
        h2("Trying out an idea safely (branching)")
        ins("  1. Click  🌿 New Branch  and give it a name (e.g. 'try-new-ui')\n", "body")
        ins("  2. Check 'Switch to this branch immediately'\n", "body")
        ins("  3. Make your changes and commit as normal\n", "body")
        ins("  4. If you don't like it: 🔀 Switch Branch back to master — the\n"
            "     experiment branch stays there but your main code is untouched\n", "body")
        br()
        h2("Finishing a feature branch (merge & cleanup)")
        p("Once your branch is tested and ready to bring back into master:")
        ins("  1.  🔀 Switch Branch  → master\n", "body")
        ins("  2.  ⬇ Pull            — pick up any new master commits first\n", "body")
        ins("  3.  ⇄ Merge…          → pick your feature branch\n", "body")
        ins("                          Confirmation says 'Merge X INTO master?' — yes\n", "body")
        ins("  4.  ⬆ Push            — master with the merged commits goes to GitHub\n", "body")
        ins("  5.  🗑 Delete Branch  → pick your feature branch → Yes (local)\n", "body")
        ins("                          Then: 'Also delete from GitHub?' → Yes\n", "body")
        br()
        p("If the merge produces conflicts, the manager pops a dialog telling "
          "you what to do (resolve in editor + commit, or run "
          "'git merge --abort' to undo). Conflicts only happen when both "
          "branches changed the same lines.")
        br()
        h2("Undoing mistakes")
        p("Made a bad commit? Click  ↩ Undo Last Commit. Your changes come back "
          "as uncommitted edits — you can fix them and recommit, or just discard.")
        br()
        warn("⚠  Undo Last Commit only removes the last commit. To undo older "
             "commits, use the terminal.")
        br()
        h2("Typical day in one line")
        dim("  Pull → Edit → Commit → Edit → Commit → Push")
    ctl._help_show(_fill, doc_path=_doc, ask_text=_ask, explain_text=_ask)

def git_tab(ctl):
    def _fill():
        h1, h2, p, warn, ok, dim, br, ins = ctl._hw()
        h1("Git Tab")
        p("The Git tab shows live status for whichever project is selected in the "
          "Projects tab. It updates automatically when you switch projects or switch "
          "to this tab.")
        br()
        h2("Working Tree & Diff")
        p("The Working Tree panel lists every modified, added, or deleted file. "
          "Click any file to see its diff below — added lines are green, removed "
          "lines are red.")
        br()
        h2("Committing changes")
        ins("  1. Make your edits (in your editor, or via Claude)\n", "body")
        ins("  2. Click  📝 Commit… — the dialog opens with a suggested message\n", "body")
        ins("  3. Edit the message if you like, then click Commit\n", "body")
        br()
        p("The suggested message is generated from your staged changes, using "
          "a chain of strategies — highest-quality first:")
        ins("    1. CHANGELOG.md bullets (if you've added an entry)\n", "body")
        ins("    2. Diff content — added Python defs/classes, file kinds\n", "body")
        ins("    3. File-name patterns (legacy fallback)\n", "body")
        p("Each result is sanitised (subject ≤ 72 chars, imperative mood, "
          "no filename listings). When AI is enabled in Settings, an "
          "Anthropic / OpenAI / LM Studio / Ollama call runs first — silent "
          "fallback to heuristics on any failure. Click 💡 Suggest at any "
          "time to regenerate.")
        br()
        h2("Undo Last Commit")
        p("Removes the most recent commit but keeps all your changes staged — "
          "nothing is deleted. Safe to use if you committed too early or with "
          "the wrong message.")
        br()
        h2("Branches")
        ins("  🌿 New Branch    — create a branch and optionally switch to it\n", "body")
        ins("  🔀 Switch Branch — pick a branch from the list to check out\n", "body")
        ins("  ⇄ Merge…         — merge another branch INTO the current one\n", "body")
        ins("                     (use after switching to master to pull a finished feature back in)\n", "body")
        ins("  🗑 Delete Branch — safe-delete locally; then offers to also delete from GitHub\n", "body")
        ins("                     (only prompts about GitHub if a remote copy actually exists)\n", "body")
        br()
        warn("⚠  Switching branches with uncommitted changes will fail. "
             "Commit or undo first.")
        br()
        warn("⚠  Merging with uncommitted changes also fails. Same fix — commit, "
             "stash, or undo first.")
        br()
        h2("Push & Pull")
        p("Push and Pull are only enabled once a remote (GitHub URL) is set. "
          "Use  Set Remote  or the  🐙 GitHub…  wizard to connect to GitHub first.")
    ctl._help_show(_fill)

def github_setup(ctl):
    _doc = os.path.join(_BASE_DIR, "docs", "GITHUB_GUIDE.md")
    _ask = "How do I connect a project to GitHub and create my first release?"

    def _fill():
        h1, h2, p, warn, ok, dim, br, ins = ctl._hw()
        h1("GitHub Setup")
        p("The  🐙 GitHub…  button in the Git tab header opens a step-by-step "
          "wizard for getting your project onto GitHub — even if you've never used "
          "GitHub before.")
        br()
        h2("Step 1 — Git identity")
        p("Every commit is stamped with your name and email. The wizard shows your "
          "current global settings and lets you update them. These are stored in "
          "your global git config and apply to every project on this machine.")
        br()
        h2("Step 2 — Create a GitHub account")
        p("Free at github.com. The wizard has a button to open the sign-up page.")
        br()
        h2("Step 3 — Create a repository")
        p("Go to github.com/new. Give it a name, leave it Public. "
          "Do NOT check 'Add README' or 'Add .gitignore' — you already have those. "
          "Copy the HTTPS URL shown after creation (e.g. "
          "https://github.com/you/my-project.git).")
        br()
        h2("Step 4 — Paste the URL")
        p("Paste the URL into the wizard and click Set. This tells git where to "
          "send your code. The Git tab's Remote label will update immediately.")
        br()
        h2("Step 5 — Push")
        p("Click ⬆ Push to GitHub. The first time, a browser window opens asking "
          "you to log in to GitHub — this is Git Credential Manager doing its job. "
          "Log in once and future pushes happen silently.")
        br()
        warn("⚠  If Push fails with an authentication error, open a terminal in "
             "the project folder and run:  git push\n"
             "This triggers the browser login. After that, the Push button works normally.")
        br()
        h2("📦 GitHub Releases")
        p("A Release lets anyone download your .exe without needing Python "
          "installed. To create one:")
        ins("  1. Run build.bat to compile dist\\tokensave-manager.exe\n", "body")
        ins("  2. Open  🐙 GitHub…  and scroll to the Releases section\n", "body")
        ins("  3. Enter a version tag (e.g. v1.0.0) and a title\n", "body")
        ins("  4. Click  📦 Create Release — the .exe files are uploaded automatically\n", "body")
        br()
        p("Releases require the GitHub CLI (gh). If it's not installed, "
          "the wizard shows a link to cli.github.com.")
    ctl._help_show(_fill, doc_path=_doc, ask_text=_ask, explain_text=_ask)
