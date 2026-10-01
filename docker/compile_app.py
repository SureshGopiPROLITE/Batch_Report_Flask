"""Build step (Dockerfile builder stage only): compile the application's
Python files to native extension modules with Cython and delete the source,
so the shipped image contains no readable .py code.

    python docker/compile_app.py /src

Empty __init__.py files are kept as they are (nothing to protect, and
packages need them). HTML templates and static JS/CSS cannot be compiled -
the browser has to receive them anyway.
"""
import os
import sys
from pathlib import Path

from Cython.Build import cythonize
from setuptools import Extension
from setuptools.dist import Distribution

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
SKIP_DIRS = {"docker", "tools", "deploy", "static", "templates", "data_files",
             "logs", "Backups", "release", "__pycache__", ".git"}


def sources():
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if SKIP_DIRS & set(rel.parts[:-1]):
            continue
        if path.name == "__init__.py" and not path.read_text(encoding="utf-8").strip():
            continue
        yield rel


def main():
    files = sorted(sources())
    os.chdir(ROOT)
    extensions = [Extension(".".join(rel.with_suffix("").parts), [str(rel)]) for rel in files]
    modules = cythonize(extensions, language_level=3, nthreads=os.cpu_count() or 1, quiet=True,
                        compiler_directives={"binding": True, "embedsignature": False,
                                             "always_allow_keywords": True})

    dist = Distribution({"ext_modules": modules})
    cmd = dist.get_command_obj("build_ext")
    cmd.inplace = True
    cmd.ensure_finalized()
    cmd.run()

    for rel in files:                       # ship only the compiled .so
        rel.unlink()
        rel.with_suffix(".c").unlink(missing_ok=True)
    for build_dir in ROOT.glob("build"):
        import shutil
        shutil.rmtree(build_dir, ignore_errors=True)

    left = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py")
            if not (SKIP_DIRS & set(p.relative_to(ROOT).parts[:-1])) and p.read_text(encoding="utf-8").strip()]
    if left:
        sys.exit(f"Source files left in the image: {left}")
    print(f"Compiled {len(files)} modules; no application source left.")


if __name__ == "__main__":
    main()
