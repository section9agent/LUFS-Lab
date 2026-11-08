"""Writes the release version into lufs_updater.py.  Usage: python stamp_version.py [1.2.3]
With no argument it uses the git tag name from GITHUB_REF_NAME (e.g. v1.2.3)."""
import os
import pathlib
import re
import sys

raw = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GITHUB_REF_NAME", "")
version = raw.strip().lstrip("vV")
if not re.fullmatch(r"\d+(\.\d+){1,3}", version):
    sys.exit(f"Not a version number: {raw!r} (expected something like 1.2.3 or v1.2.3)")
path = pathlib.Path(__file__).with_name("lufs_updater.py")
text = path.read_text(encoding="utf-8")
new, n = re.subn(r'^APP_VERSION = ".*?"', f'APP_VERSION = "{version}"', text, count=1, flags=re.M)
if n != 1:
    sys.exit("Could not find APP_VERSION in lufs_updater.py")
path.write_text(new, encoding="utf-8")
print(f"APP_VERSION set to {version}")
