"""A file with nothing wrong with it.

The counterpart to broken.py: a suite that only ever looks at a defective
file cannot tell "reports real findings" from "reports findings about
everything".
"""


def add(a, b):
    return a + b
