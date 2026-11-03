"""Importing generate_listings must not run the pipeline.

Regression test for the incident where ``import generate_listings`` executed
the full scrape (it rewrote tracked tier caches as a side effect). The entry
point lives in ``main()``; import time must be side-effect free.
Also covers atomic state writes (_save_seen/_save_last_hash via tmp+replace).

Run: python test_main_guard.py  (also collected by pytest)
"""
import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).with_name("generate_listings.py")


def test_no_top_level_work():
    tree = ast.parse(HERE.read_text())
    allowed = (ast.Import, ast.ImportFrom, ast.FunctionDef,
               ast.AsyncFunctionDef, ast.ClassDef,
               ast.Assign, ast.AnnAssign, ast.AugAssign)
    for node in tree.body:
        if isinstance(node, allowed):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # module docstring
        if isinstance(node, ast.Try):
            # Allow import-time stdio setup (sys.stdout.reconfigure(...))
            # but no bare calls like http_get(...): those run the pipeline.
            calls = [c for c in ast.walk(node) if isinstance(c, ast.Call)]
            if calls and all(isinstance(c.func, ast.Attribute) for c in calls):
                continue
            raise AssertionError(
                f"top-level try-block does work at import (line {node.lineno})")
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "__name__"):
            continue  # if __name__ == "__main__"
        raise AssertionError(
            f"top-level statement runs at import: {type(node).__name__} "
            f"at line {node.lineno}")


def _exec_writer(name, tmpdir):
    tree = ast.parse(HERE.read_text())
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == name)
    import os as _os
    import json as _json
    target = os.path.join(tmpdir, "state.json")
    env = {"os": _os, "json": _json,
           "CACHE_DIR": tmpdir, "_seen_path": lambda: target,
           "HASH_FILE": target}
    exec(compile(ast.Module(body=[fn], type_ignores=[]),
                 "generate_listings.py", "exec"), env)
    return env[name], target


def test_save_seen_atomic():
    with tempfile.TemporaryDirectory() as tmp:
        save_seen, target = _exec_writer("_save_seen", tmp)
        save_seen({"k": {"first": "2026-01-01", "last": "2026-11-03"}})
        assert json.load(open(target))["k"]["last"] == "2026-11-03"
        assert os.listdir(tmp) == ["state.json"], os.listdir(tmp)


def test_save_last_hash_atomic():
    with tempfile.TemporaryDirectory() as tmp:
        save_hash, target = _exec_writer("_save_last_hash", tmp)
        save_hash("abc123")
        assert json.load(open(target)) == {"hash": "abc123"}
        assert os.listdir(tmp) == ["state.json"], os.listdir(tmp)


def test_import_has_no_side_effects():
    proc = subprocess.run(
        [sys.executable, "-c", "import generate_listings"],
        cwd=str(HERE.parent), capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "Fetching jobhive manifest" not in proc.stdout
    assert "Running tier" not in proc.stdout
    assert proc.stdout.strip() == "", proc.stdout[:500]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  PASS  {name}")
    print("\n  test_main_guard: all checks passed")
