#!/usr/bin/env python3
"""Discover and run all emulator scenarios sequentially. Exit code != 0 on failure."""
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
SCENARIOS = os.path.join(HERE, "scenarios")
sys.path.insert(0, SCENARIOS)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))          # emulator/ -> repo root


def discover():
    mods = []
    for fname in sorted(os.listdir(SCENARIOS)):
        if fname.startswith("test_") and fname.endswith(".py"):
            mods.append(fname[:-3])
    return mods


def run(modname):
    mod = __import__(modname)
    fns = [(n, f) for n, f in sorted(vars(mod).items())
           if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        t0 = time.time()
        try:
            fn()
            print(f"    {name} OK ({time.time()-t0:.1f}s)")
        except Exception:
            failed += 1
            print(f"    {name} FAILED")
            traceback.print_exc()
    return failed


def main():
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    list_only = "--list" in sys.argv
    mods = only or discover()
    if list_only:
        print("\n".join(mods))
        return 0
    if not mods:
        print("no scenarios found")
        return 1
    total_failed = 0
    for m in mods:
        print(f"  {m}")
        total_failed += run(m)
    if total_failed:
        print(f"FAILED: {total_failed} test(s)")
        return 1
    print("ALL SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
