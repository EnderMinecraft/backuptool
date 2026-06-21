"""
build.py — packages backup_tool.py into a single Windows executable.

Run this ON WINDOWS (PyInstaller builds for the OS it runs on; there is
no cross-compiling a Linux-built exe into a working Windows exe).

Usage:
    1.  py -m venv venv
        venv\\Scripts\\activate
        pip install pyinstaller
        pip install sv-ttk     # optional: enables the Windows 11 "Modern"
                                # theme. If skipped, the app still runs
                                # fine and falls back to Classic.

    2.  (Optional, recommended) Download rclone for Windows from
        https://rclone.org/downloads/, unzip it, and copy rclone.exe
        into this same folder as build.py, next to backup_tool.py.
        If present, this script bundles it automatically so end users
        don't need to install rclone themselves.

    3.  python build.py

Output:
    dist/BackupTool/BackupTool.exe        (the app)
    dist/BackupTool/rclone.exe            (if you provided one — placed
                                            next to the exe so it's found
                                            automatically, see
                                            BUNDLED_RCLONE in backup_tool.py)
    dist/BackupTool/settings.json         (created on first run by the
                                            app itself, not by this script)

Distribute the whole dist/BackupTool folder (zip it) — not just the .exe —
since rclone.exe and any future bundled binaries need to stay alongside it.

Note on sv-ttk: PyInstaller bundles whatever is importable in the venv it
runs from, including sv-ttk's .tcl theme files (it's a pure-Python package
with data files, no extra --add-data flag needed). If sv-ttk was not
installed in the venv at build time, the resulting exe simply won't offer
the Modern theme — Settings will show Classic only, no crash.
"""

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "backup_tool.py"
DIST_DIR = HERE / "dist" / "BackupTool"
RCLONE_SOURCE = HERE / "rclone.exe"  # optional, see usage note above

# --onedir (not --onefile) is used deliberately:
#   - faster startup (no self-extraction to a temp dir every launch)
#   - lets us drop rclone.exe directly next to the exe, where
#     get_base_dir()/BUNDLED_RCLONE in backup_tool.py expects to find it
PYINSTALLER_ARGS = [
    sys.executable, "-m", "PyInstaller",
    "--name", "BackupTool",
    "--onedir",
    "--windowed",          # no console window behind the GUI
    "--noconfirm",
    "--clean",
    str(SCRIPT),
]


def check_pyinstaller():
    try:
        subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--version"],
            capture_output=True, check=True, text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("PyInstaller is not installed. Run: pip install pyinstaller")
        sys.exit(1)


def run_build():
    print("Building with PyInstaller...")
    subprocess.run(PYINSTALLER_ARGS, cwd=HERE, check=True)


def bundle_rclone():
    if not RCLONE_SOURCE.is_file():
        print(
            f"\nNote: no rclone.exe found at {RCLONE_SOURCE}\n"
            "Rclone mode will only work on machines where the user installs "
            "rclone separately and puts it on PATH. To bundle it instead, "
            "place rclone.exe next to build.py and re-run this script."
        )
        return

    if not DIST_DIR.is_dir():
        print(f"Expected build output at {DIST_DIR}, but it doesn't exist. Build may have failed.")
        return

    dest = DIST_DIR / "rclone.exe"
    shutil.copy2(RCLONE_SOURCE, dest)
    print(f"Bundled rclone.exe -> {dest}")


def main():
    check_pyinstaller()
    run_build()
    bundle_rclone()
    print(f"\nDone. Distributable folder: {DIST_DIR}")
    print("Zip and ship the entire folder (not just the .exe).")


if __name__ == "__main__":
    main()