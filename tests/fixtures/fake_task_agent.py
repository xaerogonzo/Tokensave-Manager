"""A stand-in coding agent for the orchestration tests. Not a model.

Reads its prompt from stdin; the first word picks a behaviour, so every
lifecycle path runs deterministically with no network and no tokens. It answers
in Claude's `--output-format json` shape.

    WRITE <name>     create a file, report success
    COMMIT <name>    create a file and commit it on the current branch
    LIE <name>       create one file but CLAIM many
    EXIT3 <name>     create a file, then exit 3
    ERROR            report is_error true, exit 0
    HANG             start a child process and sleep (cancel / timeout tests);
                     the child's pid goes to <cwd>/../hang-child.pid
    NOTHING          change nothing, report success
"""
import json
import os
import subprocess
import sys
import time

prompt = sys.stdin.read()
words = prompt.split()
mode = words[0] if words else "NOTHING"
name = words[1] if len(words) > 1 else "out.txt"


def report(claim="done", is_error=False):
    print(json.dumps({"type": "result", "session_id": "fake-session-1",
                      "result": claim, "is_error": is_error,
                      "usage": {"input_tokens": 11, "output_tokens": 7},
                      "total_cost_usd": 0.0123}))


def write(path, text="hello\n"):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


if mode == "WRITE":
    write(name)
    report()
elif mode == "COMMIT":
    write(name)
    for args in (["add", "-A"], ["-c", "user.email=a@b.c", "-c", "user.name=a",
                                 "commit", "-q", "-m", "task work"]):
        subprocess.run(["git", *args], check=True, capture_output=True)
    report()
elif mode == "LIE":
    write(name)
    report("I changed 99 files")
elif mode == "EXIT3":
    write(name)
    report()
    sys.exit(3)
elif mode == "ERROR":
    report("it went wrong", is_error=True)
elif mode == "HANG":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(os.path.join(os.getcwd(), "..", "hang-child.pid"), "w") as fh:
        fh.write(str(child.pid))
    time.sleep(120)
else:
    report("nothing to do")
