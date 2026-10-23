# Releases that build, pass, and are still broken

Harvested from OpenChem Studio's release work (PRs #405, #408, #412, #418, #420).
Every entry is a release that **cannot be told from a working one by anything the
project already checked**. The shared root: each missing file, wrong layout or stale
artifact works from a source checkout, so the test suite is green and the product is
not. This is stack-neutral; the examples are PyInstaller and Inno Setup, and the rule
carries to Nuitka, a wheel or a tarball.

> **Run what you built, from where a user will run it, and make the check able to fail.**

---

## 1. A release that dies at import is invisible to every check

Seventeen data files were missing from the packaging spec. The frozen app died at
import on the first one, before any window. Every test passed, because each file
exists in a checkout. Nothing noticed until somebody launched the build.

**Fix:** the build launches what it built, on a scripted run, in a fresh folder with
its own data directory, and **fails closed**. The judge passes only when all of these
hold: exit 0, a report whose run started *after* the launch, verdict PASS, every
required assertion present and ok, no dropped records, the whole script ran. Then
prove it can fail: delete a required file from the built tree and confirm the run goes
red. A smoke test that has never been seen to fail is a decoration.

## 2. "Wait for the process" waits for the wrong process

`Start-Process -Wait` also waits on child processes (an embedded browser engine), so a
hung run hung the build. A launcher or shim can stand between the process you started
and the one that writes the report, so a pid proves nothing about freshness.

**Fix:** run it from a language with a real timeout, kill only your own process tree by
PID (never by image name, which kills the user's other windows), judge freshness by the
report's own start time, and print the tail of the app's log on failure.

## 3. A fixed folder name lets an upgrade mix two versions

A zip with one fixed top folder, unzipped over an older release, leaves a directory
holding files of both. **Fix:** one *versioned* top folder. Write the archive as
`.partial`, validate its inventory (single prefix, no path traversal, the executable,
licence and data present, no generated or cache files), extract it to a path **with a
space in it** and smoke-test *that*, then compute checksums from the final bytes and
promote. A failure leaves nothing that looks finished and never touches the previous
release; an existing artifact is not overwritten without an explicit force.

## 4. A checksum is not a signature, and an in-archive manifest is not tamper protection

A manifest of expected hashes that ships inside the same unsigned executable detects
*accidental* damage (a deleted or flipped data file) and nothing else; whoever can edit
a file can edit the manifest. Say so in the code and the UI, scope it to immutable
files only (a user's settings changing must never read as corruption), and make the
verdict closed: `verified | modified | check_failed | not_applicable`. An unreadable
file or a malformed manifest is **never** `verified`.

## 5. The automation that can publish is the dangerous part

A workflow that creates a release is a write path into your users' machines. Build it
so misuse is hard, and hold each property with a test, not a comment:

- **draft only**; a person presses Publish
- `permissions: {}` by default, write access in the one job that drafts, and only on a tag
- every third-party action pinned to a **full commit SHA**; downloaded tools pinned by
  version *and* hash
- no `${{ }}` spliced into a shell script; pass values through `env:`
- the tag must equal the version in the project file, and that version must have a
  changelog section, both checked in seconds **before** the long build

Re-verify copied pins when you adopt them; a pin that was right once is not a pin that
is current.

## 6. Smaller ones that cost a re-release

- **Dirty tree:** the build refuses uncommitted changes, and a release build refuses
  them outright. Record the state in the build stamp so About can say so.
- **No version in the executable:** product name and version were blank in Properties,
  which code-signing programs reject. Generate the version resource from the one
  version, and make the build read the *built* file back and fail on a mismatch.
- **The release contents are a document with a guard:** every tracked top-level entry
  has a row saying whether it ships, and a test fails when one is missing. Otherwise a
  new folder either ships by accident or stays out by accident.
- **Help the user can overwrite:** if the app reads its help from files beside it, a
  save there can drop an anchor and leave F1 empty. Decide deliberately between
  compiling help into the build and reading a shipped corpus; do not do both by accident.

---

**Starting points:** the Manager's `templates/release/` holds adapted, opt-in versions of the workflow,
tag/version check, zip builder, smoke judge and Inno script. They are not delivered to projects;
adopt them deliberately with `python scripts/adopt_release.py` from the Manager checkout, then edit.

**What this does not give you:** a signature. SmartScreen still says "unknown
publisher". A checksum, a provenance attestation and a green smoke test say the file is
the one the workflow built and that it starts; they do not say who you are.
