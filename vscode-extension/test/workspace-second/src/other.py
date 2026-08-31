"""A second project, so "pinned" and "follows the active editor" differ.

With one folder open those two behaviours give the same answer, and a test
asserting the pin cannot fail — which is exactly what the mutation runner
found. Opening a file in *this* folder while the status bar is pinned to the
first is the only arrangement where the difference is observable.
"""


def other():
    return "second"
