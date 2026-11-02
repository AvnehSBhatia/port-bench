#!/usr/bin/env python3
"""Build a fresh workspace for one port attempt.

  python3 prepare.py <task-id> <workspace-dir>

Clones the pinned commit, moves the Python implementation and its packaging files into reference/ (readable, but not
importable as the package), drops git history, and writes TASK.md: the only instructions either agent gets.
A pristine copy of the tests is kept next to the workspace (<workspace>.tests) for the grader.
"""

import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def task(task_id):
    for t in json.load(open(os.path.join(HERE, "tasks.json"))):
        if t["id"] == task_id:
            return t
    sys.exit(f"unknown task {task_id}")


def run(*cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


TASK_MD = """# Port {package} to Rust

This repository held {what}, written in Python. Port it to Rust.

## What to build

A Rust crate at the repository root, exposed to Python with PyO3 and built with maturin, so that `import {package}`
loads your Rust code and offers the same public API the Python library had.

## Rules

- All of the library's logic must be in Rust. Python files inside the `{package}` package may contain only docstrings,
  imports from inside the package (for example `from ._native import *`), and assignments of literal constants.
- The Rust code must not run Python source code (`py.run`, `py.eval`, `PyModule::from_code`, CPython's `PyRun_*`, or
  the builtins `exec`, `eval`, `compile`) and must not touch pytest or unittest.{forbidden_python}
- Do not depend on a crate that already implements this library or its format: {forbidden}. General-purpose crates
  (pyo3, regex, serde, unicode crates and similar) are fine.
- The wheel may contain only the `{package}` package and its metadata: no `.pth` or `.pyc` files, no other top-level
  modules, no pytest plugins.

## What you have

- `reference/`: the original Python implementation and its packaging files. Read it as much as you like. It is
  removed before your wheel is built and is out of reach while the tests run.
- {tests}: the original test suite. It is the specification. Do not change it; the grader uses a pristine copy.

## How it is graded

1. The repository is copied without `reference/`, and the copy is built with `maturin build --release`.
2. The wheel is installed into a fresh virtual environment, and these test files run, unmodified, from outside this
   repository:

{graded}

3. The port works when it follows the rules and passes {expected}, with none failing.

To check yourself:

```sh
python3 -m venv .venv && . .venv/bin/activate && pip install maturin pytest
maturin develop --release && python -m pytest {graded_args}
```
"""


def main():
    task_id, ws = sys.argv[1], os.path.abspath(sys.argv[2])
    t = task(task_id)
    if os.path.exists(ws):
        sys.exit(f"{ws} already exists")
    run("git", "clone", "--quiet", t["repo"], ws)
    run("git", "checkout", "--quiet", t["commit"], cwd=ws)
    # Pristine tests for the grader, outside the workspace.
    pristine = ws + ".tests"
    os.makedirs(pristine)
    for p in t["test_files"]:
        shutil.copytree(os.path.join(ws, p), os.path.join(pristine, p))
    # The Python implementation and packaging become reference material.
    ref = os.path.join(ws, "reference")
    os.makedirs(ref)
    for p in t["implementation"] + t["packaging"]:
        src = os.path.join(ws, p)
        if os.path.exists(src):
            shutil.move(src, os.path.join(ref, os.path.basename(p)))
    for p in t["implementation"]:
        parent = os.path.dirname(os.path.join(ws, p))
        if parent != ws and os.path.isdir(parent) and not os.listdir(parent):
            os.rmdir(parent)
    shutil.rmtree(os.path.join(ws, ".git"))
    graded = "\n".join(f"- `{g}`" for g in t["graded"])
    with open(os.path.join(ws, "TASK.md"), "w") as f:
        f.write(
            TASK_MD.format(
                package=t["package"],
                what=t["what"],
                forbidden=", ".join(f"`{c}`" for c in t["forbidden_crates"]),
                forbidden_python=(" It must not import " + ", ".join(f"`{m}`" for m in t["forbidden_python"]) + ".") if t["forbidden_python"] else "",
                expected=(f"{t['expected']['passed']} tests: every test the original Python library passes in the same environment" if t.get("expected") else "every test the original Python library passes in the same environment"),
                tests=", ".join(f"`{p}/`" for p in t["test_files"]),
                graded=graded,
                graded_args=" ".join(t["graded"]),
            )
        )
    run("git", "init", "--quiet", cwd=ws)
    run("git", "add", "-A", cwd=ws)
    run("git", "-c", "user.email=bench@heliod.dev", "-c", "user.name=bench", "commit", "--quiet", "-m", "start", cwd=ws)
    print(ws)


if __name__ == "__main__":
    main()
