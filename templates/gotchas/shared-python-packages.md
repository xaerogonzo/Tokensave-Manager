# Extracting Shared Code Between Projects

Written after pulling ~2,300 lines out of a shipped application into a package
two products consume. Most of this is invisible until the second consumer
exists, which is exactly when it is expensive.

---

## 1. Re-export breaks the monkeypatch contract; module aliasing does not

**Symptom:** After extracting a module, the test suite passes — but a test that
patches an attribute and asserts a *different function* observes the patch now
silently passes for the wrong reason, or fails inexplicably.

**Cause:** The obvious shim is a re-export:

```python
from newpackage.thing import run, helper      # WRONG
```

Tests do `monkeypatch.setattr(thing, "helper", fake)` and then call
`thing.run()`. After a re-export, the patch lands on **your** module object
while `run()` keeps reading `newpackage.thing`'s globals. Two objects. The patch
is invisible, silently.

**Fix:** Replace the module rather than re-export from it:

```python
"""Moved to newpackage.thing. This module *is* that module."""
import sys
from newpackage import thing as _impl
sys.modules[__name__] = _impl
```

`from pkg import thing` now yields the real module. One object — which is what
every existing caller and test already assumed. CPython's loader re-reads
`sys.modules` after executing a module, so the replacement is what callers get.

**When aliasing is not enough:** if the shim must *add* app-specific names
alongside the generic ones, you cannot alias. Either parameterise (pass the
app-specific bits into the shared implementation) or leave the module alone.

**Use "the tests must pass unedited" as your correctness oracle.** A test that
needs changing means the extraction was not behaviour-preserving. On one module
this rule caught that a split was unsafe *before* it shipped — the right response
was to not force it, and record why.

---

## 2. Two distributions, one namespace: no `__init__.py`

**Symptom:** Installing the second package makes the first one's modules
disappear. Works fine until both are installed, so it survives testing.

**Cause:** `pkg-core` shipping `src/pkg/__init__.py` makes `pkg` a *regular*
package, which shadows `pkg-ui`'s contribution entirely.

**Fix:** Make it a [PEP 420](https://peps.python.org/pep-0420/) namespace
package — **neither** distribution ships `src/pkg/__init__.py` — and say so in
both `pyproject.toml` files:

```toml
[tool.setuptools.packages.find]
where = ["src"]
include = ["pkg*"]
namespaces = true
```

**Guard it with a test**, because the failure only appears with both installed:

```python
def test_core_ships_no_namespace_init():
    assert not (CORE / "__init__.py").exists()
```

Verify by actually installing both editable and importing from each. It is a
two-minute check that saves a confusing afternoon.

---

## 3. Nuitka cannot always follow an editable install

**Symptom:** A compiled build succeeds, then fails at startup with
`ModuleNotFoundError` for the shared package.

**Cause:** `pip install -e` resolves through an `__editable__` finder at *import*
time. Nuitka needs to see the module at *compile* time and cannot always follow
that indirection.

**Fix:** Name the modules explicitly:

```
--include-module=pkg.thing
--include-module=pkg.other
```

Prefer `--include-module=` for specific modules over `--include-package=pkg` when
`pkg` is a PEP 420 namespace spanning two distributions — naming the namespace is
the less predictable spelling.

**Watch what you drag in.** If a headless component uses `--nofollow-import-to=`
for a GUI toolkit, do not also include a shared module that imports it. Verify by
compiling a probe that imports exactly what the real target does.

---

## 4. Declare the dependency in *every* place that installs

**Symptom:** A fresh clone installs every requirement, then fails at import. Your
own working tree is fine because you installed the new package by hand.

**Cause:** Extraction moves code out but nothing tells pip. Easy to miss because
nothing in your environment is broken.

**Fix:** Enumerate the install paths and check each — there are usually more than
you remember:

- `requirements.txt` — the normal install
- `requirements-ci.txt` — the test job
- any **staged/embedded runtime** the build produces
- `pyproject.toml` `dependencies`

Not on PyPI yet? A git URL works:

```
pkg-core @ git+https://github.com/owner/repo.git#subdirectory=core
```

**Pin it to a tag before any release.** Unpinned, a build resolves whatever
`master` is on build day — so the same application tag built twice is not the
same product. This matters most when the dependency ships *inside* an installer.

---

## 5. Decide what does *not* move, and write down why

The gate worth applying before extracting anything:

1. Does the concept make sense without the original app?
2. Would a third, unrelated application plausibly consume it?
3. Does the API describe the *capability*, not one app's implementation?
4. Does extracting actually reduce duplication — **is there a second consumer
   today**, or only an imagined one?
5. Does extraction avoid coupling the two products?

Short of a clear yes, leave it. The most useful record is the list of things you
*refused* to move and why, because that is the question that recurs.

**Extract when the second consumer exists, not in anticipation.** Extraction is
cheap once the technique is proven; a speculative shared API is not.

---

## 6. Module-level registries collide once shared

**Symptom:** Two applications in one process see each other's registrations.

**Cause:** The common decorator-registry pattern uses module globals:

```python
_REGISTRY = {}
def register(name): ...
```

Fine with one consumer. Shared, both write to the same dict.

**Fix:** Make it a class, and have each application own an instance that
re-exports its own decorator — call sites do not change:

```python
REGISTRY = SceneRegistry()
scene = REGISTRY.scene
```

A test that two registries do not share entries costs three lines.
