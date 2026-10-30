"""Copies the licence files the build Python's packages carry into the release's LICENSES folder, and the texts those
packages leave out (Qt is LGPL-3.0, PyInstaller's bootloader exception is in its COPYING).

    python3 collect_licenses.py SITE_PACKAGES LICENSES_DIR
"""
import shutil
import sys
from pathlib import Path

site, out = Path(sys.argv[1]), Path(sys.argv[2])
SPDX = Path("/usr/share/licenses/spdx")

# distribution folder prefix -> name used in LICENSES
PACKAGES = {
    "numpy": "numpy", "scipy": "scipy", "pillow": "Pillow", "mutagen": "mutagen", "pypresence": "pypresence",
    "pyinstaller": "PyInstaller", "pyinstaller_hooks_contrib": "PyInstaller-hooks",
}

for dist in sorted(site.glob("*.dist-info")):
    prefix = dist.name.removesuffix(".dist-info").rsplit("-", 1)[0].lower().replace("-", "_")
    if prefix not in PACKAGES:
        continue
    for lic in sorted(dist.rglob("*")):
        if lic.is_file() and lic.name not in ("METADATA", "WHEEL", "RECORD", "INSTALLER", "REQUESTED", "top_level.txt",
                                              "entry_points.txt", "DELVEWHEEL", "zip-safe") and "sboms" not in lic.parts:
            rel = lic.relative_to(dist)
            target = out / PACKAGES[prefix] / Path(*[p for p in rel.parts if p != "licenses"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(lic, target)

python_license = site.parent.parent / "LICENSE.txt"
shutil.copyfile(python_license, out / "Python-PSF.txt")

for spdx_name, name in (("LGPL-3.0-only.txt", "Qt-and-PySide6-LGPL-3.0.txt"), ("GPL-3.0-only.txt", "GPL-3.0.txt"),
                        ("LGPL-2.1-or-later.txt", "LGPL-2.1.txt"), ("Apache-2.0.txt", "OpenSSL-Apache-2.0.txt")):
    shutil.copyfile(SPDX / spdx_name, out / name)
ofl = Path("/usr/share/licenses/ttf-fantasque-nerd/OFL.txt").read_text(encoding="utf-8")
body = ofl[ofl.index("-----------------------------------------------------------\nSIL OPEN FONT LICENSE"):]
(out / "JetBrainsMono-OFL-1.1.txt").write_text(
    "Copyright 2020 The JetBrains Mono Project Authors (https://github.com/JetBrains/JetBrainsMono)\n\n"
    "This Font Software is licensed under the SIL Open Font License, Version 1.1.\n"
    "This license is copied below, and is also available with a FAQ at:\nhttps://scripts.sil.org/OFL\n\n\n" + body, encoding="utf-8")
print(f"licenses: {sum(1 for p in out.rglob('*') if p.is_file())} files in {out}")
