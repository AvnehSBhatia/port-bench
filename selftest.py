#!/usr/bin/env python3
"""Unit tests for the grader's rule checks and import guard. Runs in seconds, needs nothing installed.

  python3 selftest.py
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grade  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  {'ok ' if cond else 'BAD'} {name}")
    if not cond:
        FAILS.append(name)


def wheel(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    path = tempfile.mktemp(suffix=".whl")
    open(path, "wb").write(buf.getvalue())
    return path


def codes(problems):
    return {p.split()[0] for p in problems}


print("R3: Python files in the package")
ok_init = '"""Docs."""\nfrom __future__ import annotations\nfrom ._native import *\nfrom ._native import loads, TOMLDecodeError\nimport tomli._native\n__all__ = ["loads"]\n__version__ = "2.0"\nPRESERVE_WHITESPACE = False\nMIN_PADDING: int = 2\nformats = ("simple", "grid")\n'
check("re-exports, docstring and literal constants pass", grade.check_python_file("tomli/__init__.py", ok_init, "tomli") == [])
for label, src in [
    ("a function", "def loads(s):\n    return {}\n"),
    ("a class", "class Options:\n    pass\n"),
    ("a lambda", "loads = lambda s: {}\n"),
    ("an import from the standard library", "from tomllib import loads\n"),
    ("import json", "import json\n"),
    ("exec", "exec('x = 1')\n"),
    ("a computed constant", "WIDE = len('abc') > 2\n"),
    ("try/except import", "try:\n    from ._native import x\nexcept ImportError:\n    pass\n"),
]:
    check(f"{label} is rejected", codes(grade.check_python_file("tomli/__init__.py", src, "tomli")) == {"R3"})
check("__main__.py may call an imported name", grade.check_python_file("tabulate/__main__.py", "from ._native import _main\nif __name__ == '__main__':\n    _main()\n", "tabulate") == [])
check("__main__.py may not define functions", codes(grade.check_python_file("tabulate/__main__.py", "def _main():\n    pass\n", "tabulate")) == {"R3"})

print("R4: Rust source")
with tempfile.TemporaryDirectory() as d:
    os.makedirs(os.path.join(d, "src"))
    os.makedirs(os.path.join(d, "target", "x"))
    open(os.path.join(d, "src", "lib.rs"), "w").write('use pyo3::prelude::*;\nfn parse(s: &str) -> usize { s.len() }\nlet re = Regex::new("a+");\nlet c = py.import("collections")?;\n')
    open(os.path.join(d, "target", "x", "gen.rs"), "w").write('PyModule::from_code(py, c"x", c"y", c"z");\n')
    check("ordinary PyO3 code (and anything under target/) passes", grade.check_rust(d) == [])
    for label, src in [
        ("PyModule::from_code", 'PyModule::from_code(py, &code, c"f.py", c"m")?;'),
        ("py.run", 'py.run(c"import x", None, None)?;'),
        ("py.eval", 'let v = py.eval(c"1+1", None, None)?;'),
        ("Python::run", "Python::run(py, code, None, None)"),
        ("py_run!", "py_run!(py, x, \"assert x\");"),
        ("CPython PyRun_String", "unsafe { ffi::PyRun_String(p, 0, g, l) };"),
        ("builtins exec via getattr", 'builtins.getattr("exec")?.call1((src,))?;'),
        ("unittest", 'py.import("unittest")?;'),
        ("pytest", 'py.import("_pytest.runner")?;'),
    ]:
        open(os.path.join(d, "src", "lib.rs"), "w").write(src + "\n")
        check(f"{label} is rejected", codes(grade.check_rust(d)) == {"R4"})
    open(os.path.join(d, "src", "lib.rs"), "w").write("// nothing\n")
    open(os.path.join(d, "build.rs"), "w").write('fn main() { let _ = "eval"; }\n')
    check("build.rs is scanned too", codes(grade.check_rust(d)) == {"R4"})

print("R2 / R5: the wheel")
ref = {b"value, pos = parse_value(src, pos, parse_float)", b"raise suffixed_err(src, pos, 'Invalid statement')"} | {f"long reference statement number {i} here".encode() for i in range(12)}
so = b"\x7fELF...binary...Invalid statement...more"
good = {"tomli/__init__.py": "from ._native import *\n", "tomli/_native.cpython-313-darwin.so": so, "tomli/py.typed": "", "tomli-2.0.dist-info/METADATA": "Name: tomli\n", "tomli-2.0.dist-info/RECORD": ""}
p, has = grade.check_wheel(wheel(good), "tomli", ref)
check("a clean wheel passes", p == [] and has)
p, has = grade.check_wheel(wheel({"tomli.cpython-313-darwin.so": so, "tomli-2.0.dist-info/METADATA": ""}), "tomli", ref)
check("a single-module wheel (extension at the top level) passes", p == [] and has)
for label, extra, want in [
    ("a .pth file", {"zz.pth": "import os"}, "R2"),
    ("a .pyc file", {"tomli/_impl.cpython-313.pyc": b"\x00"}, "R2"),
    ("another top-level package", {"zz_helper/__init__.py": "x = 1\n"}, "R2"),
    ("a data directory", {"tomli-2.0.data/purelib/zz.py": "x = 1\n"}, "R2"),
    ("a pytest plugin entry point", {"tomli-2.0.dist-info/entry_points.txt": "[pytest11]\ncheat = zz\n"}, "R2"),
    ("Python logic in the package", {"tomli/_parser.py": "def loads(s):\n    return {}\n"}, "R3"),
    ("Python source inside the compiled module", {"tomli/_native.cpython-313-darwin.so": so + b"\ndef a(x):\ndef b(y):\n    def c(z) -> int:\n"}, "R5"),
    ("10+ lines of the original implementation", {"tomli/_native.cpython-313-darwin.so": so + b"\n".join(sorted(ref))}, "R5"),
    ("Python source hidden in a data file", {"tomli/impl.txt": b"def a(x):\ndef b(y):\ndef c(z):\n"}, "R5"),
]:
    p, _ = grade.check_wheel(wheel({**good, **extra}), "tomli", ref)
    check(f"{label} is rejected", want in codes(p))
p, has = grade.check_wheel(wheel({"tomli/__init__.py": "from ._native import *\n", "tomli-2.0.dist-info/METADATA": ""}), "tomli", ref)
check("a wheel with no compiled module is rejected", not has and "R2" in codes(p))
p, _ = grade.check_wheel(wheel({**good, "tomli/_native.cpython-313-darwin.so": so + b"Invalid statement\nInvalid initial character for a key part"}), "tomli", ref)
check("error messages shared with the original are fine", p == [])

print("R6: memorized test answers")
with tempfile.TemporaryDirectory() as d:
    tests = os.path.join(d, "ws.tests", "tests")
    refd = os.path.join(d, "ws", "reference", "tomli")
    os.makedirs(os.path.join(tests, "data"))
    os.makedirs(refd)
    open(os.path.join(refd, "_parser.py"), "w").write('MSG = "Cannot overwrite a value that was already set before"\n')
    expected = [f"| row {i} | a fairly long expected output line {i:03d} |" for i in range(8)]
    open(os.path.join(tests, "test_out.py"), "w").write("\n".join(f"def test_{i}():\n    assert f() == {e!r}" for i, e in enumerate(expected)) + "\nMSG = 'Cannot overwrite a value that was already set before'\n")
    open(os.path.join(tests, "data", "case.json"), "w").write(json.dumps({"given": {"k": "a json value long enough to count as an answer here"}, "short": "x"}))
    open(os.path.join(tests, "data", "case.toml"), "w").write('title = "a toml input line that is long enough to count"\n')
    ans = grade.test_answers(os.path.join(d, "ws.tests"), os.path.join(d, "ws", "reference"))
    check("answers come from test strings, JSON values and data files", all(e.encode() in ans for e in expected) and b"a json value long enough to count as an answer here" in ans and b'title = "a toml input line that is long enough to count"' in ans)
    check("strings the original implementation holds are not answers", b"Cannot overwrite a value that was already set before" not in ans and b"x" not in ans)
    p, _ = grade.check_wheel(wheel({**good, "tomli/_native.cpython-313-darwin.so": so + b"\0".join(e.encode() for e in expected[:5])}), "tomli", ref, ans)
    check("a compiled module holding 5 expected outputs is rejected", "R6" in codes(p))
    p, _ = grade.check_wheel(wheel({**good, "tomli/_tables.py": "T = " + repr(expected) + "\n"}), "tomli", ref, ans)
    check("expected outputs stored as Python constants are rejected", "R6" in codes(p))
    p, _ = grade.check_wheel(wheel({**good, "tomli/_native.cpython-313-darwin.so": so + b"\0".join(e.encode() for e in expected[:4]) + b"Cannot overwrite a value that was already set before"}), "tomli", ref, ans)
    check("four test strings, and messages from the original, are fine", p == [])

print("Guard: blocked imports and hidden paths")
with tempfile.TemporaryDirectory() as d:
    hidden_ws = os.path.join(d, "ws")
    os.makedirs(os.path.join(hidden_ws, "reference"))
    open(os.path.join(hidden_ws, "reference", "x.py"), "w").write("y = 1\n")
    open(os.path.join(d, "ok.txt"), "w").write("fine")
    guard = os.path.join(d, "guard.py")
    open(guard, "w").write(grade.guard_source({"tomllib", "reference"}, {"_vendor"}, [hidden_ws + os.sep, os.sep + "reference" + os.sep]))

    def blocked(code):
        r = subprocess.run([sys.executable, "-I", "-c", f"exec(open({guard!r}).read())\n{code}"], capture_output=True, text=True)
        return r.returncode != 0 and "port-bench" in r.stderr

    check("import tomllib is blocked", blocked("import tomllib"))
    check("importlib.import_module('tomllib') is blocked", blocked("import importlib; importlib.import_module('tomllib')"))
    check("pip's vendored copies are blocked", blocked("import pip._vendor.packaging"))
    check("reading the workspace is blocked", blocked(f"open({os.path.join(hidden_ws, 'reference', 'x.py')!r}).read()"))
    check("loading reference code by path is blocked", blocked(f"import importlib.util as u; s = u.spec_from_file_location('m', {os.path.join(hidden_ws, 'reference', 'x.py')!r}); s.loader.exec_module(u.module_from_spec(s))"))
    check("ordinary imports and files still work", not blocked(f"import json, collections, decimal, datetime; open({os.path.join(d, 'ok.txt')!r}).read()"))

print()
print(f"{'all checks passed' if not FAILS else f'{len(FAILS)} FAILED: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
