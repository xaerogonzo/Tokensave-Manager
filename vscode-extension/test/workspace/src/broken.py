"""A deliberately defective module. It exists to make the Problems panel real.

Do not fix it. `test/integration/suite/diagnostics.test.js` asserts that
`checks` reports findings here and that they land on the right lines, so a
tidy-up would make every one of those assertions vacuous while leaving them
green. See test/workspace/README.md.
"""
import os
import sys


def unreachable_after_return():
    return 1
    print("this never runs")


def uses_an_undefined_name():
    return undefined_name_on_purpose + 1
