#!/usr/bin/env python3
"""Check that an INSTALLED harness carries this repository, not an older one.

The harness is the one thing here meant to run somewhere else, so it is the one
thing that ships a copy of what it depends on: pyproject.toml's force-include
block reproduces `schema/`, the reference decoder and the software peripheral
inside the wheel, and `refdec._find_root()` finds them there instead of in a
clone.

That copy is a snapshot, and nothing compared it against the source. A wheel
built from a stale tree is entirely self-consistent -- old schema, old decoder,
old peripheral -- so `vtp1-harness --loopback` off such an install exercises
last week's peripheral against last week's rulebook and reports green. It is
the one way this repository's CI can be right about everything and a developer
running the published tool can still be told a superseded device conforms.

Run this with the interpreter that has the wheel installed, from a clone:

  python3 tools/check_package.py          compare the installed copy to the source
  python3 tools/check_package.py --list   print what the wheel is supposed to carry

The file list is read from pyproject.toml rather than restated here, so a file
added to the force-include block is checked from the moment it is added.
"""
import hashlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _force_include():
    """The wheel's force-include map: repository path -> path inside the wheel."""
    try:
        import tomllib
    except ModuleNotFoundError:                      # Python 3.10
        import tomli as tomllib
    with open(ROOT / "pyproject.toml", "rb") as fh:
        config = tomllib.load(fh)
    return (config["tool"]["hatch"]["build"]["targets"]
                  ["wheel"]["force-include"])


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv):
    mapping = _force_include()
    if "--list" in argv:
        for source, target in sorted(mapping.items()):
            print(f"{source} -> {target}")
        return 0

    try:
        import vtp1_harness
    except ModuleNotFoundError:
        print("no installed vtp1_harness on this interpreter. Build and "
              "install the wheel first:\n"
              "  python3 -m build --wheel && pip install dist/*.whl")
        return 2

    installed = pathlib.Path(vtp1_harness.__file__).resolve().parent
    if installed == (ROOT / "harness" / "vtp1_harness"):
        # An editable install points back at the clone, where the _ref
        # directory does not exist and refdec finds the real one. Nothing to
        # compare, and reporting "identical" would be a lie about what ran.
        print("vtp1_harness is imported from this clone, not from a wheel. "
              "This check needs a real install to say anything.")
        return 2

    problems = []
    for source, target in sorted(mapping.items()):
        src = ROOT / source
        # force-include targets are wheel-root-relative and every one of them
        # starts with the package directory.
        assert target.startswith("vtp1_harness/"), target
        dst = installed / target[len("vtp1_harness/"):]
        if not src.is_file():
            problems.append(f"{source} is in the force-include block but not "
                            f"in this repository")
            continue
        if not dst.is_file():
            problems.append(f"{source} is missing from the installed package "
                            f"(expected at {target})")
            continue
        if _digest(src) != _digest(dst):
            problems.append(
                f"{source} differs from the copy the install carries. The "
                f"wheel was built from a different tree, so the installed "
                f"harness is testing something this repository no longer says")

    print(f"installed at {installed}")
    for source in sorted(mapping):
        state = "differs" if any(source in p for p in problems) else "identical"
        print(f"  {state:<9} {source}")

    if problems:
        print("\nFAILED")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"\nok: all {len(mapping)} bundled file(s) match this repository")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
