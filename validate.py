#!/usr/bin/env python3
"""Validate the benchmark itself: are the tasks solvable, and does the grader catch cheating?

  python3 validate.py                 # all tasks; writes VALIDATION.md and the expected counts into tasks.json
  python3 validate.py tomli           # one task

For each task it grades:
  oracle          the original Python library, installed as is: every graded test must pass. Its pass count becomes
                  the task's expected count (tasks.json "expected"), which a port has to match.
  empty           the prepared workspace with nothing built: must not work.
  python-wheel    a real maturin wheel with a do-nothing Rust module that ships the original Python code: the tests
                  must pass (so the build-and-test path works end to end) and rule R3 must reject it.
  embedded-python Rust that embeds the original Python source and runs it: rules R4 and R5 must reject it.
  stdlib          (tomli) a package that re-exports the standard library's tomllib: rule R3 and the import guard must
                  stop it.
  smuggled-plugin a wheel that ships an extra top-level package registered as a pytest plugin that marks every test
                  passed: rule R2 must reject it, and the plugin must not run.
  forbidden-crate a do-nothing Rust module that depends on a forbidden crate: rule R1 must reject it.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TASKS = json.load(open(os.path.join(HERE, "tasks.json")))
FORBIDDEN_DEP = {"tomli": 'toml = "*"', "jmespath": 'jmespath = "*"', "tabulate": 'comfy-table = "*"'}

CARGO = """[package]
name = "{name}"
version = "0.0.1"
edition = "2021"

[lib]
name = "_native"
crate-type = ["cdylib"]

[dependencies]
pyo3 = {{ version = "0.30", features = ["extension-module"] }}
{extra}
"""

PYPROJECT = """[build-system]
requires = ["maturin>=1.0,<2.0"]
build-backend = "maturin"

[project]
name = "{pkg}"
version = "0.0.1"
{entry_points}
[tool.maturin]
python-source = "python"
module-name = "{pkg}._native"
{python_packages}"""

EMPTY_LIB = """use pyo3::prelude::*;

#[pymodule]
fn _native(_m: &Bound<'_, PyModule>) -> PyResult<()> {
    Ok(())
}
"""

EMBED_LIB = """use pyo3::prelude::*;
use std::ffi::CString;

const SOURCE: &str = include_str!("embedded.py");

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let code = CString::new(SOURCE).unwrap();
    let module = PyModule::from_code(m.py(), &code, c"embedded.py", c"embedded")?;
    m.add("embedded", module)?;
    Ok(())
}
"""

PLUGIN = """import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    outcome.get_result().outcome = "passed"
"""


def prepare(t, root, name):
    ws = os.path.join(root, t["id"], name)
    subprocess.run([sys.executable, os.path.join(HERE, "prepare.py"), t["id"], ws], check=True, capture_output=True)
    return ws


def grade(t, ws, oracle=False):
    cmd = [sys.executable, os.path.join(HERE, "grade.py"), t["id"], ws] + (["--oracle"] if oracle else [])
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return {"works": False, "build": f"grader crashed: {r.stderr[-600:]}", "rules": [], "passed": 0, "failed": 0, "errors": 0}


def rust_package(ws, t, name, lib=EMPTY_LIB, extra="", init="from ._native import *\n", copy_reference=False, plugin=False):
    pkg = t["package"]
    open(os.path.join(ws, "Cargo.toml"), "w").write(CARGO.format(name=f"{pkg}-{name}", extra=extra))
    entry = '\n[project.entry-points.pytest11]\nportbench_cheat = "zz_helper"\n' if plugin else ""
    pp = 'python-packages = ["zz_helper"]\n' if plugin else ""
    open(os.path.join(ws, "pyproject.toml"), "w").write(PYPROJECT.format(pkg=pkg, entry_points=entry, python_packages=pp))
    os.makedirs(os.path.join(ws, "src"), exist_ok=True)
    open(os.path.join(ws, "src", "lib.rs"), "w").write(lib)
    pdir = os.path.join(ws, "python", pkg)
    if copy_reference:
        shutil.copytree(os.path.join(ws, "reference", os.path.basename(t["implementation"][0])), pdir)
    else:
        os.makedirs(pdir)
        open(os.path.join(pdir, "__init__.py"), "w").write(init)
    if plugin:
        os.makedirs(os.path.join(ws, "python", "zz_helper"))
        open(os.path.join(ws, "python", "zz_helper", "__init__.py"), "w").write(PLUGIN)


def largest_py(d):
    files = [os.path.join(r, f) for r, _, fs in os.walk(d) for f in fs if f.endswith(".py")]
    return max(files, key=os.path.getsize)


def summary(g):
    s = f"{g.get('passed', 0)} passed, {g.get('failed', 0)} failed, {g.get('errors', 0)} errors"
    if g.get("subtests_passed") or g.get("subtests_failed"):
        s += f" ({g['subtests_passed']} cases passed, {g['subtests_failed']} failed)"
    if g.get("build") not in ("ok", None) and not g.get("build", "").startswith("oracle"):
        s = f"build: {g['build'][:120]}"
    if g.get("rules"):
        codes = sorted({r.split()[0] for r in g["rules"]})
        s += f"; rejected by {', '.join(codes)} ({g['rules'][0][:110]})"
    return s


def validate_task(t, root):
    rows = []

    def row(check, expect, g, ok):
        rows.append({"task": t["id"], "check": check, "expect": expect, "result": summary(g), "ok": bool(ok)})
        print(f"  {'ok ' if ok else 'BAD'} {check}: {summary(g)}", flush=True)

    print(f"{t['id']}:", flush=True)
    ws = prepare(t, root, "oracle")
    g = grade(t, ws, oracle=True)
    ok = g["passed"] > 0 and g["failed"] == 0 and g["errors"] == 0 and g.get("subtests_failed", 0) == 0
    row("oracle", "the original Python passes every graded test", g, ok)
    if ok:
        t["expected"] = {"passed": g["passed"], "skipped": g["skipped"], "cases": g.get("subtests_passed", 0)}
        save_expected(t)
    versions = g.get("versions", {})
    expected = g["passed"]

    ws = prepare(t, root, "empty")
    g = grade(t, ws)
    row("empty", "does not work", g, not g["works"])

    ws = prepare(t, root, "python-wheel")
    rust_package(ws, t, "pywheel", copy_reference=True)
    g = grade(t, ws)
    ok = g.get("build") == "ok" and g["passed"] == expected and g["failed"] == 0 and any(r.startswith("R3") for r in g["rules"]) and not g["works"]
    row("python-wheel", f"builds, passes all {expected} tests, rejected by R3", g, ok)

    ws = prepare(t, root, "embedded-python")
    rust_package(ws, t, "embed", lib=EMBED_LIB)
    shutil.copy(largest_py(os.path.join(ws, "reference")), os.path.join(ws, "src", "embedded.py"))
    g = grade(t, ws)
    codes = {r.split()[0] for r in g["rules"]}
    row("embedded-python", "builds, rejected by R4 and R5", g, g.get("build") == "ok" and {"R4", "R5"} <= codes and not g["works"])

    if t["forbidden_python"]:
        mod = t["forbidden_python"][0]
        ws = prepare(t, root, "stdlib")
        rust_package(ws, t, "stdlib", init=f"from {mod} import *\nfrom {mod} import TOMLDecodeError, load, loads\n")
        g = grade(t, ws)
        ok = any(r.startswith("R3") for r in g["rules"]) and g["passed"] == 0 and not g["works"]
        row("stdlib", f"re-exporting {mod}: rejected by R3, and the guard blocks the import (no test passes)", g, ok)

    ws = prepare(t, root, "smuggled-plugin")
    rust_package(ws, t, "plugin", plugin=True)
    g = grade(t, ws)
    ok = any(r.startswith("R2") for r in g["rules"]) and g["passed"] < expected and not g["works"]
    row("smuggled-plugin", "rejected by R2, and the plugin does not run (tests fail)", g, ok)

    ws = prepare(t, root, "forbidden-crate")
    rust_package(ws, t, "crate", extra=FORBIDDEN_DEP[t["id"]])
    g = grade(t, ws)
    row("forbidden-crate", f"depends on {FORBIDDEN_DEP[t['id']].split()[0]}: rejected by R1", g, any(r.startswith("R1") for r in g["rules"]) and not g["works"])
    return rows, versions


def save_expected(t):
    path = os.path.join(HERE, "tasks.json")
    tasks = json.load(open(path))
    for x in tasks:
        if x["id"] == t["id"]:
            x["expected"] = t["expected"]
    with open(path, "w") as f:
        json.dump(tasks, f, indent=2)
        f.write("\n")


def main():
    pick = [t for t in TASKS if len(sys.argv) < 2 or t["id"] in sys.argv[1:]]
    root = tempfile.mkdtemp(prefix="port-bench-validate-")
    os.environ.setdefault("PORT_BENCH_CARGO_TARGET", os.path.join(root, "cargo-target"))
    t0 = time.time()
    rows, versions = [], {}
    for t in pick:
        r, v = validate_task(t, root)
        rows += r
        versions = versions or v
    bad = [r for r in rows if not r["ok"]]
    lines = [
        "# Validation",
        "",
        f"`python3 validate.py`, run {time.strftime('%Y-%m-%d')} in {round((time.time() - t0) / 60, 1)} minutes. "
        + ", ".join(f"{k} {v}" for k, v in versions.items() if v)
        + ".",
        "",
        "| Task | Check | Must | Result | OK |",
        "| --- | --- | --- | --- | --- |",
        *[f"| {r['task']} | {r['check']} | {r['expect']} | {r['result'].replace('|', '/')} | {'yes' if r['ok'] else '**NO**'} |" for r in rows],
        "",
        f"{len(rows) - len(bad)} of {len(rows)} checks passed.",
        "",
    ]
    if len(pick) == len(TASKS):
        with open(os.path.join(HERE, "VALIDATION.md"), "w") as f:
            f.write("\n".join(lines))
    print("\n".join(lines))
    shutil.rmtree(root, ignore_errors=True)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
