#!/usr/bin/env python3
"""Grade one port attempt.

  python3 grade.py <task-id> <workspace>            -> one JSON object on stdout
  python3 grade.py <task-id> <workspace> --oracle   -> grade the original Python package from reference/ instead

Everything happens in a fresh virtual environment in a temp dir:
 1. Copy the workspace without reference/ and build output, build the wheel there with `maturin build --release`,
    then delete the copy.
 2. Check the rules below on the Rust source, Cargo.lock, the wheel and the compiled module.
 3. Install the wheel, hide the workspace, and run the graded test files from the pristine copy (<workspace>.tests)
    outside the repository: pytest plugins from installed packages are disabled, and a guard blocks imports of
    forbidden modules and reads of the reference code.
 4. Count results from pytest's JUnit XML report and compare with the count the original library scores.

A port works when it builds, breaks no rule, and passes exactly as many tests as the original Python library
(`expected.passed` in tasks.json, measured by validate.py), with none failing.

Rules:
 R1 no forbidden crate in Cargo.lock;
 R2 the wheel holds a compiled extension module and nothing outside the package: no .pth or .pyc files, no other
    top-level modules, no data directories, no pytest plugin entry point;
 R3 Python files in the package hold only docstrings, imports from inside the package, and literal constants;
 R4 the Rust source does not run Python source (PyO3 run/eval/from_code, CPython's PyRun_*, builtins
    exec/eval/compile) and does not reach into pytest or unittest;
 R5 no file in the wheel embeds Python source: no Python function definitions, and fewer than 10 lines of the
    original implementation verbatim.
"""

import ast
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
GRADER_DEPS = ["maturin==1.16.0", "pytest==9.2.0"]
REFERENCE_LINES_LIMIT = 10


def task(task_id):
    for t in json.load(open(os.path.join(HERE, "tasks.json"))):
        if t["id"] == task_id:
            return t
    sys.exit(f"unknown task {task_id}")


def sh(cmd, cwd=None, env=None, timeout=1800):
    try:
        r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout} s"
    return r.returncode, (r.stdout or "") + (r.stderr or "")


# ---------------------------------------------------------------------------------------------------------------------
# Rules


def check_python_file(name, src, pkg):
    """R3: docstrings, imports from inside the package, literal constants. __main__.py may also call imported names."""
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [f"R3 {name}: does not parse ({e.msg})"]
    is_main = name.endswith("__main__.py")

    def simple_call(n):
        return isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name)

    bad = []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if isinstance(node, ast.ImportFrom) and (node.level > 0 or (node.module or "").split(".")[0] in (pkg, "__future__")):
            continue
        if isinstance(node, ast.Import) and all(a.name.split(".")[0] == pkg for a in node.names):
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if all(isinstance(t, ast.Name) for t in targets):
                try:
                    ast.literal_eval(node.value)
                    continue
                except Exception:
                    pass
        if is_main and (simple_call(node) or (isinstance(node, ast.If) and all(simple_call(s) for s in node.body) and not node.orelse)):
            continue
        bad.append(f"R3 {name}:{node.lineno}: Python code ({type(node).__name__}) in the package")
    return bad


RUST_PATTERNS = [
    (r"\bfrom_code(_bound)?\s*\(", "PyModule::from_code runs Python source"),
    (r"\bpy\s*\.\s*(run|eval)(_bound)?\s*\(", "py.run / py.eval runs Python source"),
    (r"\bPython::(run|eval)(_bound)?\b", "Python::run / Python::eval runs Python source"),
    (r"\bpy_run!", "py_run! runs Python source"),
    (r"\b(PyRun_\w+|Py_CompileString\w*|PyEval_EvalCode\w*)\b", "CPython API that runs Python source"),
    (r'"(exec|eval|compile|__import__)"', "uses builtins exec / eval / compile / __import__"),
    (r'"(pytest|_pytest|unittest)(\.[\w.]*)?"', "reaches into the test framework"),
]


def check_rust(src_root):
    """R4 over every .rs file in the build copy (build.rs included)."""
    bad = []
    for path in glob.glob(os.path.join(src_root, "**", "*.rs"), recursive=True):
        rel = os.path.relpath(path, src_root)
        if rel.split(os.sep)[0] == "target":
            continue
        text = open(path, encoding="utf-8", errors="replace").read()
        for pattern, why in RUST_PATTERNS:
            for m in re.finditer(pattern, text):
                line = text.count("\n", 0, m.start()) + 1
                bad.append(f"R4 {rel}:{line}: {why}")
    return bad


def reference_lines(ref_dir):
    """First source line of every statement in the original implementation (no docstrings), 30+ characters long."""
    out = set()
    for path in glob.glob(os.path.join(ref_dir, "**", "*.py"), recursive=True):
        src = open(path, encoding="utf-8", errors="replace").read()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        lines = src.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.stmt):
                continue
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue
            line = lines[node.lineno - 1].strip()
            if len(line) >= 30 and not line.startswith(("#", '"', "'")):
                out.add(line.encode())
    return out


DEF_LINE = re.compile(rb"(?m)^[ \t]*(async[ \t]+)?def [A-Za-z_][A-Za-z0-9_]*\([^\n]*\)[^\n]*:[ \t]*$")


def check_wheel(wheel, pkg, ref_lines):
    """R2, R3 and R5 on the wheel's contents. Returns (problems, has_extension)."""
    bad = []
    has_ext = False
    with zipfile.ZipFile(wheel) as z:
        for name in z.namelist():
            top = name.split("/")[0]
            is_ext = name.endswith((".so", ".pyd")) and (name.startswith(pkg + "/") or re.match(rf"^{re.escape(pkg)}(\.[\w-]+)*\.(so|pyd)$", name))
            has_ext = has_ext or bool(is_ext)
            if name.endswith(".pth") or name.endswith((".pyc", ".pyo")):
                bad.append(f"R2 {name}: {name.rsplit('.', 1)[1]} files are not allowed in the wheel")
            elif top.endswith(".dist-info"):
                if name.endswith("entry_points.txt") and "[pytest11]" in z.read(name).decode("utf-8", "replace"):
                    bad.append(f"R2 {name}: registers a pytest plugin")
                continue
            elif top.endswith(".data"):
                bad.append(f"R2 {name}: wheel data directories are not allowed")
            elif not (name.startswith(pkg + "/") or name.startswith(pkg + ".libs/") or is_ext):
                bad.append(f"R2 {name}: outside the {pkg} package")
            data = z.read(name)
            if name.endswith(".py"):
                bad += check_python_file(name, data.decode("utf-8", "replace"), pkg)
                continue
            if name.endswith(".pyi") or name.endswith("/py.typed"):
                continue
            if len(DEF_LINE.findall(data)) >= 3:
                bad.append(f"R5 {name}: contains Python function definitions")
            hits = sum(1 for line in ref_lines if line in data)
            if hits >= REFERENCE_LINES_LIMIT:
                bad.append(f"R5 {name}: contains {hits} lines of the original Python implementation")
    if not has_ext:
        bad.append("R2 no compiled extension module in the wheel")
    return bad, has_ext


# ---------------------------------------------------------------------------------------------------------------------
# Test run


def guard_source(blocked_exact, blocked_parts, hidden):
    return (
        "import os, sys\n"
        f"_EXACT = {sorted(blocked_exact)!r}\n"
        f"_PARTS = {sorted(blocked_parts)!r}\n"
        f"_HIDDEN = {hidden!r}\n"
        "def _port_bench_guard(event, args):\n"
        "    if event == 'import':\n"
        "        name = str(args[0] or '')\n"
        "        parts = name.split('.')\n"
        "        if parts[0] in _EXACT or any(p in _PARTS for p in parts):\n"
        "            raise ImportError('port-bench: importing ' + name + ' is not allowed while grading')\n"
        "    elif event == 'open' and args and isinstance(args[0], (str, bytes, os.PathLike)):\n"
        "        p = os.fsdecode(args[0])\n"
        "        if any(h in p for h in _HIDDEN):\n"
        "            raise PermissionError('port-bench: ' + p + ' is not readable while grading')\n"
        "sys.addaudithook(_port_bench_guard)\n"
    )


def run_tests(t, py, env, tmp, run_dir):
    junit = os.path.join(tmp, "junit.xml")
    ini = os.path.join(tmp, "pytest.ini")
    with open(ini, "w") as f:
        f.write("[pytest]\n")
    cmd = [py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", ini, "--rootdir", run_dir, f"--junitxml={junit}", *t["graded"]]
    code, log = sh(cmd, cwd=run_dir, env=env, timeout=1800)
    res = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "failing": []}
    if os.path.exists(junit):
        for case in ET.parse(junit).getroot().iter("testcase"):
            kinds = {c.tag for c in case}
            cid = f"{case.get('classname')}::{case.get('name')}"
            if "error" in kinds:
                res["errors"] += 1
                res["failing"].append(cid)
            elif "failure" in kinds:
                res["failed"] += 1
                res["failing"].append(cid)
            elif "skipped" in kinds:
                res["skipped"] += 1
            else:
                res["passed"] += 1
    # unittest subTest cases (tomli runs its 744 toml-test files this way). A failing one also fails its test above;
    # the counts only show partial progress. Taken from pytest's own summary line, the first one it prints.
    summary = next((l for l in log.splitlines() if re.search(r"^\d+ (passed|failed|error).* in [\d.]+s", l)), "")
    for k in ("passed", "failed"):
        m = re.search(rf"(\d+) subtests? {k}", summary)
        res[f"subtests_{k}"] = int(m.group(1)) if m else 0
    res["failing"] = res["failing"][:25]
    res["exit_code"] = code
    res["log_tail"] = log[-2000:] if (res["failed"] or res["errors"] or not os.path.exists(junit)) else ""
    return res


# ---------------------------------------------------------------------------------------------------------------------


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    oracle = "--oracle" in sys.argv
    task_id, ws = args[0], os.path.abspath(args[1])
    t = task(task_id)
    pkg = t["package"]
    expected = t.get("expected", {}).get("passed")
    out = {"task": task_id, "mode": "oracle" if oracle else "port", "works": False, "build": None, "rules": [], "passed": 0, "failed": 0, "errors": 0, "skipped": 0, "total": 0, "expected_passed": expected}
    tmp = tempfile.mkdtemp(prefix="port-grade-")
    venv = os.path.join(tmp, "venv")
    py = os.path.join(venv, "bin", "python")
    hidden = ws + ".hidden-while-grading"
    moved = False
    t0 = time.time()
    try:
        sh([sys.executable, "-m", "venv", venv])
        code, log = sh([py, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *GRADER_DEPS, *t["test_deps"]], timeout=900)
        if code:
            out["build"] = f"could not set up the grader venv: {log[-400:]}"
            return out
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        env.update(VIRTUAL_ENV=venv, PATH=os.path.join(venv, "bin") + os.pathsep + os.environ["PATH"], PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
        site = sh([py, "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"])[1].strip()
        ref_dir = os.path.join(ws, "reference")

        if oracle:
            for p in t["implementation"]:
                src = os.path.join(ref_dir, os.path.basename(p))
                (shutil.copytree if os.path.isdir(src) else shutil.copy)(src, os.path.join(site, os.path.basename(p)))
            out["build"] = "oracle: the original Python package from reference/"
        else:
            if not os.path.exists(os.path.join(ws, "Cargo.toml")):
                out["build"] = "no Cargo.toml at the repository root"
                return out
            # 1. Build from a copy that has no reference/ (and no local build output).
            build = os.path.join(tmp, "build")
            skip_top = {"reference", "target", ".venv", "venv", ".git"}
            shutil.copytree(ws, build, ignore=lambda d, names: [n for n in names if (d == ws and n in skip_top) or n == "__pycache__"], symlinks=True)
            dist = os.path.join(tmp, "dist")
            benv = dict(env)
            if os.environ.get("PORT_BENCH_CARGO_TARGET"):
                benv["CARGO_TARGET_DIR"] = os.environ["PORT_BENCH_CARGO_TARGET"]
            code, log = sh([os.path.join(venv, "bin", "maturin"), "build", "--release", "-i", py, "-o", dist], cwd=build, env=benv)
            if code:
                out["build"] = f"maturin build failed: {log[-1500:]}"
                return out
            wheels = glob.glob(os.path.join(dist, "*.whl"))
            if len(wheels) != 1:
                out["build"] = f"expected one wheel, got {len(wheels)}"
                return out
            out["build"] = "ok"
            # 2. Rules.
            lock = os.path.join(build, "Cargo.lock")
            names = set(re.findall(r'^name = "([^"]+)"', open(lock).read(), re.M)) if os.path.exists(lock) else set()
            banned = sorted(names & set(t["forbidden_crates"]))
            if banned:
                out["rules"].append(f"R1 forbidden crates in Cargo.lock: {', '.join(banned)}")
            out["rules"] += check_rust(build)
            problems, _ = check_wheel(wheels[0], pkg, reference_lines(ref_dir))
            out["rules"] += problems
            shutil.rmtree(build, ignore_errors=True)
            code, log = sh([py, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--no-deps", wheels[0]], env=env)
            if code:
                out["build"] = f"wheel install failed: {log[-400:]}"
                return out

        # 3. Guard, hide the workspace, run the pristine tests from outside it.
        blocked_exact = set(t.get("forbidden_python", [])) | {"reference"}
        with open(os.path.join(site, "sitecustomize.py"), "w") as f:
            f.write(guard_source(blocked_exact, {"_vendor"}, [ws + os.sep, hidden, os.sep + "reference" + os.sep]))
        probe = t["forbidden_python"][0] if t.get("forbidden_python") else "pip._vendor.packaging"
        code, _ = sh([py, "-c", f"import {probe}"], env=env)
        if code == 0:
            out["build"] = f"grader error: the import guard did not block {probe}"
            return out
        run_dir = os.path.join(tmp, "run")
        shutil.copytree(ws + ".tests", run_dir)
        os.rename(ws, hidden)
        moved = True
        res = run_tests(t, py, env, tmp, run_dir)
        os.rename(hidden, ws)
        moved = False
        out.update(res)
        out["total"] = out["passed"] + out["failed"] + out["errors"]
        out["versions"] = {
            "python": sh([py, "-c", "import sys;print(sys.version.split()[0])"])[1].strip(),
            "pytest": sh([py, "-m", "pytest", "--version"], env=env)[1].strip().split()[-1],
            "maturin": sh([os.path.join(venv, "bin", "maturin"), "--version"])[1].strip().split()[-1],
            "rustc": sh(["rustc", "--version"])[1].strip().split()[1] if shutil.which("rustc") else None,
        }
        clean = out["failed"] == 0 and out["errors"] == 0 and out.get("subtests_failed", 0) == 0 and not out["rules"]
        out["works"] = clean and out["passed"] > 0 and (expected is None or out["passed"] == expected)
        return out
    finally:
        if moved and os.path.exists(hidden) and not os.path.exists(ws):
            os.rename(hidden, ws)
        out["grade_seconds"] = round(time.time() - t0, 1)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    print(json.dumps(main()))
