"""Does session context actually make a PR draft better? Three arms, one rule.

    python scripts/ab_rationale.py --project . --base origin/master
    python scripts/ab_rationale.py --project . --base origin/master --call

WHY THIS EXISTS RATHER THAN AN ARGUMENT. `enable_commit_grounding` already
defaults **OFF** because grounding was measured to make commit messages worse:
it *"ADDS prompt weight that small local models handle poorly… qwen2.5-coder
copies recent commit subjects verbatim when overwhelmed"*. Attaching more
context is a road somebody already went down and turned back from. The bet here
is that *rationale* differs in kind from *reference material* — it is short, and
it answers the exact question rather than adding a pile to search — and a bet is
not a finding. So this runs before the default moves.

THREE ARMS, BECAUSE TWO CANNOT ANSWER THE PRODUCT QUESTION.

    control          no session context at all
    prompts          the user's own words          (~1,200 tokens/day measured)
    prompts+prose    plus assistant prose          (~20,200 tokens/day measured)

"Rationale vs none" would leave prompts-vs-prose undecided, and prose is the arm
most likely to reproduce the measured failure.

FROZEN INPUTS. Every arm gets the same repository state, the same base ref, the
same model and backend, the same system prompt, and every non-experimental
grounding source. Only `InstructionPolicy` differs. Otherwise the experiment
measures model randomness and moving inputs rather than rationale.

THE WINNER RULE, FIXED HERE BEFORE ANY OUTPUT IS SEEN:

    An arm beats the control only if it improves INTENT and SCOPE accuracy
    WITHOUT increasing factual errors or hallucinated claims. Verbosity is
    recorded and is never decisive.

    An arm that does not beat the control ships defaulted OFF.

Writing the rule down first is the point. Read three drafts and then decide what
"better" meant, and the reader picks the one they liked.

Dry by default: it assembles and saves what each arm WOULD send, with prompt
sizes, and calls no model. `--call` runs the real backend once per arm.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from helpers import pr_draft                                  # noqa: E402
from helpers.instruction_policy import InstructionPolicy      # noqa: E402

#: (name, policy). The control is a real arm, not the absence of one.
ARMS = (
    ("control", InstructionPolicy(enable_session_grounding=False)),
    ("prompts", InstructionPolicy(enable_session_grounding=True,
                                  session_grounding_prompts=True,
                                  session_grounding_prose=False)),
    ("prompts+prose", InstructionPolicy(enable_session_grounding=True,
                                        session_grounding_prompts=True,
                                        session_grounding_prose=True)),
)

RUBRIC = (
    ("intent_accuracy", "Does it say WHY the change was made, correctly?"),
    ("scope_accuracy", "Does it describe what actually changed, no more?"),
    ("factual_errors", "Statements contradicted by the diff (count)"),
    ("hallucinated_claims", "Files, symbols or behaviour that do not exist (count)"),
    ("verbosity", "Length relative to the control — recorded, never decisive"),
)

WINNER_RULE = (
    "An arm beats the control only if it improves intent AND scope accuracy "
    "WITHOUT increasing factual_errors or hallucinated_claims. Verbosity is "
    "recorded and never decisive. An arm that does not beat the control ships "
    "defaulted OFF."
)


class _ArmConfig:
    """The real config, with one field overridden.

    A proxy rather than a mutated `cfg.raw`: the arms must not be able to leave
    a policy behind in `manager-config.json`, and an experiment that edits the
    user's settings is not one anybody should run twice.
    """

    def __init__(self, cfg, policy):
        self._cfg = cfg
        self._policy = policy

    @property
    def instruction_policy(self):
        return self._policy

    def __getattr__(self, name):
        return getattr(self._cfg, name)


def _grounding_for(cfg, project_path: str, policy) -> str:
    return pr_draft._session_grounding(_ArmConfig(cfg, policy), project_path)


def run(project_path: str, base: str, call: bool, out_dir: str) -> int:
    from state import ManagerConfig

    cfg = ManagerConfig.load()
    os.makedirs(out_dir, exist_ok=True)

    manifest = {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "project": project_path,
        "base": base,
        "called_model": bool(call),
        "winner_rule": WINNER_RULE,
        "arms": {},
    }

    for name, policy in ARMS:
        block = _grounding_for(cfg, project_path, policy)
        record = {
            "policy": policy.to_raw(),
            "session_block_chars": len(block),
            "session_block_lines": block.count("\n"),
        }
        with open(os.path.join(out_dir, "arm-%s-grounding.md" % name), "w",
                  encoding="utf-8") as handle:
            handle.write(block or "(no session grounding for this arm)\n")

        if call:
            draft = pr_draft.generate_pr_draft(
                _ArmConfig(cfg, policy), project_path, base)
            text = draft or "(no draft returned — is an LLM backend configured?)"
            record["draft_chars"] = len(text)
            with open(os.path.join(out_dir, "arm-%s-draft.md" % name), "w",
                      encoding="utf-8") as handle:
                handle.write(text)

        manifest["arms"][name] = record
        print("%-14s session block %5d chars%s"
              % (name, record["session_block_chars"],
                 "   draft %d chars" % record["draft_chars"] if call else ""))

    with open(os.path.join(out_dir, "manifest.json"), "w",
              encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
    _write_scoring_sheet(out_dir, call)

    print("\nwrote %s" % out_dir)
    if not call:
        print("Dry run: no model was called. Re-run with --call to produce "
              "drafts to score.")
    return 0


def _write_scoring_sheet(out_dir: str, called: bool) -> None:
    """The rubric, written BEFORE the outputs are read."""
    lines = [
        "# Scoring sheet",
        "",
        "Fill this in **after** reading the drafts and **without** changing the",
        "rule below. The rule was fixed before any output existed, which is the",
        "only thing stopping the reader from picking the one they liked.",
        "",
        "## The rule",
        "",
        WINNER_RULE,
        "",
        "## Rubric",
        "",
        "| criterion | what it asks |",
        "|---|---|",
    ]
    lines += ["| `%s` | %s |" % (key, question) for key, question in RUBRIC]
    lines += [
        "",
        "## Scores",
        "",
        "| arm | " + " | ".join(k for k, _ in RUBRIC) + " |",
        "|---" * (len(RUBRIC) + 1) + "|",
    ]
    lines += ["| %s | %s |" % (name, " | ".join("" for _ in RUBRIC))
              for name, _policy in ARMS]
    lines += [
        "",
        "## Verdict",
        "",
        "- [ ] `prompts` beats the control",
        "- [ ] `prompts+prose` beats the control",
        "- [ ] neither does — both ship defaulted OFF",
        "",
    ]
    if not called:
        lines += ["> Dry run: only the grounding blocks were produced. "
                  "Re-run with `--call` before scoring.", ""]
    with open(os.path.join(out_dir, "SCORING.md"), "w",
              encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", default=".", help="repository root")
    parser.add_argument("--base", default="", help="PR base ref, e.g. origin/master")
    parser.add_argument("--call", action="store_true",
                        help="actually call the LLM once per arm")
    parser.add_argument("--out", default="", help="output directory")
    args = parser.parse_args(argv)

    project = os.path.abspath(args.project)
    out = args.out or os.path.join(
        project, ".tokensave-manager", "ab-rationale",
        datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    return run(project, args.base, args.call, out)


if __name__ == "__main__":
    raise SystemExit(main())
