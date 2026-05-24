"""Pure-function helpers used across the TokenSave Manager codebase.

Each module here is a leaf in the import graph:
  - No imports from controllers/, dialogs/, app.py, or state.py
  - May import from constants.py and other helpers/
  - All functions take explicit parameters (no globals)

If a helper needs a config value, the caller passes it in. See plan
docs at .claude/plans/run-a-comprehensive-code-humming-wilkes.md for
the full rationale.
"""
