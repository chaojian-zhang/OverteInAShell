#!/usr/bin/env python3
"""Build the installable add-on zip: dist/overte_live_link-<version>.zip

The zip holds the ``overte_live_link/`` folder, which works both as a Blender
4.2+ extension (Get Extensions > Install from Disk) and as a legacy add-on
(Preferences > Add-ons > Install).
"""

import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = "overte_live_link"


def version() -> str:
    with open(os.path.join(ROOT, PKG, "blender_manifest.toml"), encoding="utf-8") as f:
        m = re.search(r'^version\s*=\s*"([^"]+)"', f.read(), re.M)
    return m.group(1)


def build(out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{PKG}-{version()}.zip")
    src = os.path.join(ROOT, PKG)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for base, dirs, files in os.walk(src):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(files):
                if name.endswith((".pyc", ".pyo")):
                    continue
                full = os.path.join(base, name)
                z.write(full, os.path.relpath(full, ROOT))
    return path


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "dist")))
