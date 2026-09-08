"""
Fix the UTM zone bug in an installed pingverter/pingmapper.

np.floor() returns a float, so str() gives '6.0' and the zero-padding that
follows never fires: UTM zones 1-9 come out as EPSG:3266 (an Antarctic SCAR
grid) instead of 32606. Restoring int() puts the padding back in play.

Keeps a .bak of every file it touches. Undo with --revert, or by reinstalling:
    pip install --force-reinstall pingverter pingmapper
"""

import argparse
import glob
import importlib.util
import os
import shutil
import sys

OLD = "str((np.floor((lon + 180) / 6 ) % 60) + 1)"
NEW = "str(int(np.floor((lon + 180) / 6 ) % 60) + 1)"

TARGETS = [
    ("pingverter", "humminbird_class.py"),
    ("pingverter", "garmin_class.py"),
    ("pingmapper", "funcs_common.py"),
]


def package_dir(name):
    spec = importlib.util.find_spec(name)
    return os.path.dirname(spec.origin) if spec and spec.origin else None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--revert", action="store_true", help="restore the .bak files")
    args = ap.parse_args()

    print(f"interpreter: {sys.executable}\n")
    touched = 0
    for package, filename in TARGETS:
        folder = package_dir(package)
        if not folder:
            print(f"  {package}: not installed here")
            continue
        path = os.path.join(folder, filename)
        if not os.path.isfile(path):
            print(f"  {filename}: not found in {package}")
            continue

        backup = path + ".bak"
        if args.revert:
            if os.path.isfile(backup):
                shutil.copy2(backup, path)
                print(f"  reverted {path}")
                touched += 1
            else:
                print(f"  no backup for {path}")
            continue

        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if NEW in text:
            print(f"  already fixed: {path}")
            continue
        if OLD not in text:
            print(f"  pattern not found (different version?): {path}")
            continue
        if not os.path.isfile(backup):
            shutil.copy2(path, backup)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text.replace(OLD, NEW))
        print(f"  patched {path}")
        touched += 1

    print(f"\n{touched} file(s) {'reverted' if args.revert else 'patched'}")


if __name__ == "__main__":
    main()
