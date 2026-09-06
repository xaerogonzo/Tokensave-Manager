# TokenSave Manager — Claude Project Instructions

@BASIC_INSTRUCTIONS.md

<!--
Why this file is one line.

Claude Code auto-loads `CLAUDE.md`. It does not load `BASIC_INSTRUCTIONS.md` --
that is the filename this project's own scaffolder writes into *other* projects,
which then migrate to CLAUDE.md and discard it. PolyShield, PolyScour and
PolyBedrock all gitignore theirs.

This project went the other way: it put 532 lines of load-bearing operational
rules into BASIC_INSTRUCTIONS.md -- anti-monolith caps, the exception protocol,
the tokensave-first lookup order -- and that file says they are "read first when
this file is @-included into a session". Nothing included it. No CLAUDE.md
existed to, and no other project references it, so none of those rules loaded in
any session run on this repository.

Two ways to fix it, and this is the cheaper one:

  * Rename BASIC_INSTRUCTIONS.md -> CLAUDE.md. Cleanest, but docs/ARCHITECTURE.md
    and the scaffolder both refer to that filename as the convention they emit,
    so the rename ripples into the tool's own vocabulary.
  * A one-line CLAUDE.md that @includes it. Zero duplication, nothing to drift,
    and the scaffolded-filename convention stays intact.

If BASIC_INSTRUCTIONS.md is ever renamed, this file becomes redundant -- delete
it rather than leaving an include pointing at nothing.
-->
