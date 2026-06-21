"""
Backup Tool v2.0
A GUI wrapper around robocopy / xcopy / rclone for simple folder backups.

Changes from v1.0:
  - Commands are built as argument lists and run via subprocess (no shell),
    eliminating shell-injection / quoting bugs and fixing rclone's
    "<subcommand> <src> <dest> [flags]" syntax requirement.
  - robocopy exit codes 0-7 are treated as success (bitmask convention),
    matching real-world robocopy behaviour.
  - Backups run on a background thread so the GUI does not freeze.
  - Path validation guards against missing source dirs, source==target,
    and target-inside-source recursive copies.
  - Destructive modes (Mirror / rclone --delete-during) require explicit
    confirmation before running.
  - Archive mode now actually uses a timestamped backup directory.
  - Settings (tool, mode, last-used paths) persist to a JSON config file.
  - Rotating log file records every run for later debugging.
"""

import json
import logging
import logging.handlers
import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

try:
    import sv_ttk
    SV_TTK_AVAILABLE = True
except ImportError:
    SV_TTK_AVAILABLE = False

# --------------------------------------------------------------------------
# Paths / constants
# --------------------------------------------------------------------------

def get_base_dir():
    """
    Directory the app's own files live in, whether running from source
    or as a PyInstaller --onefile / --onedir build.

    --onefile unpacks bundled data files to a temp dir at sys._MEIPASS,
    but for a tool we ship *alongside* the exe (not embedded as data),
    we want the folder the exe itself sits in, which is sys.executable's
    directory when frozen, or this script's directory otherwise.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = get_base_dir()
# If you bundle rclone.exe next to the built exe (see build.py), it will
# be found here first, before falling back to PATH.
BUNDLED_RCLONE = os.path.join(BASE_DIR, "rclone.exe")
RCLONE_DOWNLOAD_URL = "https://rclone.org/downloads/"

# Logs always go to %APPDATA% — never the install folder, which may be
# read-only (e.g. Program Files) and logging shouldn't depend on settings
# resolution succeeding first.
_APPDATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "BackupTool")
os.makedirs(_APPDATA_DIR, exist_ok=True)
LOG_PATH = os.path.join(_APPDATA_DIR, "backup.log")


def _dir_is_writable(directory):
    """Best-effort check: can we actually create a file here?"""
    probe = os.path.join(directory, f".write_test_{os.getpid()}.tmp")
    try:
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        return True
    except OSError:
        return False


def resolve_settings_path():
    """
    Prefer settings.json next to the exe/script (portable — travels with
    the app, e.g. on a USB stick). Falls back to %APPDATA%\\BackupTool
    if the install folder isn't writable (e.g. installed under
    C:\\Program Files without elevation).
    """
    if _dir_is_writable(BASE_DIR):
        return os.path.join(BASE_DIR, "settings.json")
    logger_bootstrap_msg = (
        f"'{BASE_DIR}' is not writable; falling back to '{_APPDATA_DIR}' for settings.json"
    )
    print(logger_bootstrap_msg)  # logger isn't configured yet at import time
    return os.path.join(_APPDATA_DIR, "settings.json")


CONFIG_PATH = resolve_settings_path()

DEFAULT_CONFIG = {
    "copy_tool": "Robocopy",
    "backup_mode": "Update / Incremental",
    "source_path": "",
    "target_path": "",
    "theme": "auto",       # "auto" | "modern" | "classic" — see THEME section below
    "color_mode": "auto",  # "auto" | "light" | "dark"
}

MODE_DESCRIPTIONS = {
    "Mirror": "Destination = Source copy (deletes files not in Source)",
    "Update / Incremental": "Safe: adds/updates only, keeps deleted files",
    "Archive": "Creates a timestamped backup folder, preserves all versions",
    "Interactive / Preview": "Simulates the backup without making changes",
}

MODE_INFO = {
    "Mirror": "Mirror Mode: Destination becomes an exact copy of Source (deletes files not in Source)",
    "Update / Incremental": "Update Mode: Only copies new/changed files, keeps deleted files in Destination",
    "Archive": "Archive Mode: Creates a timestamped backup folder, preserves all versions",
    "Interactive / Preview": "Preview Mode: Shows what would be copied without making changes",
}

# Modes that delete or overwrite data in target and deserve a confirmation prompt.
DESTRUCTIVE_MODES = {"Mirror"}

BACKUP_TIMEOUT_SECONDS = 3600

# --------------------------------------------------------------------------
# Theme (sv-ttk "Windows 11" look, with a classic-ttk fallback/override)
# and color mode (light/dark), independent axes.
# --------------------------------------------------------------------------
# settings.json "theme" can be:
#   "auto"    - modern (sv-ttk) on Windows 11+, classic ttk on everything else
#   "modern"  - always sv-ttk, if installed
#   "classic" - always the default ttk look
#
# settings.json "color_mode" can be:
#   "auto"  - follow Windows' system-wide light/dark setting
#   "light" - always light
#   "dark"  - always dark

WIN11_BUILD_THRESHOLD = 22000  # first public Windows 11 build number

# Registry location Windows itself uses for the system-wide light/dark
# toggle (Settings > Personalization > Colors > "Choose your mode").
_PERSONALIZE_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
_APPS_LIGHT_VALUE = "AppsUseLightTheme"  # REG_DWORD: 1 = light, 0 = dark


def is_windows_11_or_later():
    """
    Windows versioning quirk: Windows 11 reports major version 10 (same as
    Windows 10) via sys.getwindowsversion() — the only reliable signal is
    the build number, which jumped to 22000+ for Win11.
    Returns False on any non-Windows OS or if the check can't be made.
    """
    if not hasattr(sys, "getwindowsversion"):
        return False  # not running on Windows (e.g. dev/test on Linux/macOS)
    try:
        return sys.getwindowsversion().build >= WIN11_BUILD_THRESHOLD
    except Exception:
        return False


def detect_windows_color_mode():
    """
    Reads Windows' system-wide light/dark preference straight from the
    registry (the same key Windows itself uses). Returns "light" or
    "dark", defaulting to "light" if unreadable (non-Windows OS, very old
    Windows without this key, headless/sandboxed environments, etc.) —
    matching Windows' own historical default before dark mode existed.
    """
    try:
        import winreg  # stdlib, Windows-only — imported lazily so this
                        # module still loads fine on non-Windows for dev/test
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PERSONALIZE_KEY) as key:
            value, _ = winreg.QueryValueEx(key, _APPS_LIGHT_VALUE)
            return "light" if value == 1 else "dark"
    except (ImportError, OSError, FileNotFoundError):
        return "light"


def resolve_theme_choice(theme_setting):
    """Turns the settings.json 'theme' value into a concrete 'modern' or
    'classic' decision."""
    if theme_setting == "modern":
        return "modern"
    if theme_setting == "classic":
        return "classic"
    # "auto" (or anything unrecognized) -> decide from OS build
    return "modern" if is_windows_11_or_later() else "classic"


def resolve_color_mode(color_mode_setting):
    """Turns the settings.json 'color_mode' value into a concrete
    'light' or 'dark' decision."""
    if color_mode_setting in ("light", "dark"):
        return color_mode_setting
    # "auto" (or anything unrecognized) -> follow Windows' system setting
    return detect_windows_color_mode()


def apply_theme(theme_setting, color_mode_setting="auto"):
    """
    Applies the resolved theme + color mode to the running app.

    Safe to call even if sv_ttk isn't installed (falls back to classic
    silently) or if the requested theme is 'classic' (sv_ttk is simply
    not engaged, or — if it was previously engaged this session — the
    original ttk theme is restored).

    Returns (theme_choice, color_choice) actually applied, e.g.
    ("modern", "dark").
    """
    theme_choice = resolve_theme_choice(theme_setting)
    color_choice = resolve_color_mode(color_mode_setting)
    style = ttk.Style(master=root)

    # Remember the platform's native ttk theme the first time this runs,
    # before sv-ttk ever gets a chance to replace it. This is what "Classic"
    # reverts to — sv_ttk itself has no built-in "turn it off" call.
    if not hasattr(root, "_native_ttk_theme"):
        root._native_ttk_theme = style.theme_use()

    if theme_choice == "modern" and SV_TTK_AVAILABLE:
        try:
            sv_ttk.set_theme(color_choice)  # sv_ttk wants exactly "light"/"dark"
            return "modern", color_choice
        except Exception as e:
            logger.warning(f"Failed to apply sv-ttk theme, falling back to classic: {e}")
            theme_choice = "classic"
    elif theme_choice == "modern" and not SV_TTK_AVAILABLE:
        logger.info("Modern theme requested but sv-ttk is not installed; using classic.")
        theme_choice = "classic"

    if theme_choice == "classic":
        try:
            style.theme_use(root._native_ttk_theme)
        except Exception as e:
            logger.warning(f"Could not restore native ttk theme: {e}")
        # Classic ttk has no built-in dark mode — it always renders in the
        # OS's native widget colors. color_choice is still returned so the
        # caller/menu state stays consistent, but it has no visual effect
        # here; only the Modern (sv-ttk) theme actually changes color mode.

    return "classic", color_choice


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

logger = logging.getLogger("backup_tool")
logger.setLevel(logging.INFO)
_handler = logging.handlers.RotatingFileHandler(
    LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
)
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_handler)
# Also echo to console for local debugging.
_console = logging.StreamHandler()
_console.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_console)


# --------------------------------------------------------------------------
# Config persistence
# --------------------------------------------------------------------------

def load_config():
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("settings.json does not contain a JSON object")
            cfg = dict(DEFAULT_CONFIG)
            cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
            return cfg
        except (json.JSONDecodeError, ValueError, OSError) as e:
            logger.warning(f"settings.json is corrupted or unreadable ({e}); recreating with defaults.")
            fresh = dict(DEFAULT_CONFIG)
            save_config(fresh)
            return fresh
    # Doesn't exist yet — create it now so the file is present from first run.
    fresh = dict(DEFAULT_CONFIG)
    save_config(fresh)
    return fresh


def save_config(cfg):
    """Atomic write: write to a temp file then replace, so a crash or power
    loss mid-write can't leave settings.json half-written/corrupted."""
    try:
        directory = os.path.dirname(CONFIG_PATH) or "."
        os.makedirs(directory, exist_ok=True)
        tmp_path = CONFIG_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp_path, CONFIG_PATH)
    except OSError as e:
        logger.warning(f"Could not save settings.json: {e}")


# --------------------------------------------------------------------------
# State (kept module-level, mirrors the structure of the original script)
# --------------------------------------------------------------------------

config = load_config()
copy_tool = config["copy_tool"]
backup_mode = config["backup_mode"]
flags = []  # mode/tool-specific flags, rebuilt by apply_settings()


def resolve_tool_path(tool_name):
    """
    Returns a usable path/command for the given tool, preferring a copy
    bundled next to the app (currently only done for rclone), falling
    back to PATH. Returns None if not found anywhere.
    """
    if tool_name.lower() == "rclone" and os.path.isfile(BUNDLED_RCLONE):
        return BUNDLED_RCLONE
    return shutil.which(tool_name)


def is_tool_available(tool_name):
    return resolve_tool_path(tool_name) is not None


# --------------------------------------------------------------------------
# Command construction
# --------------------------------------------------------------------------

def build_command(tool, source, target, mode_flags):
    """
    Build an argument list (NOT a shell string) for subprocess.

    rclone needs: rclone <subcommand> <source> <dest> [flags...]
    robocopy/xcopy need: <tool> <source> <dest> [flags...]
    """
    tool_lower = tool.lower()
    resolved = resolve_tool_path(tool_lower)
    if resolved is None:
        raise FileNotFoundError(tool_lower)

    if tool_lower == "rclone":
        if not mode_flags:
            raise ValueError("No rclone subcommand configured. Apply settings first.")
        subcommand, rest = mode_flags[0], mode_flags[1:]
        return [resolved, subcommand, source, target, *rest]
    else:
        return [resolved, source, target, *mode_flags]


def is_success(tool, returncode):
    """robocopy uses a bitmask: 0-7 are all success, 8+ is failure."""
    if tool.lower() == "robocopy":
        return 0 <= returncode < 8
    return returncode == 0


def validate_paths(source, target):
    """Returns an error message string, or None if paths are OK."""
    if not source or not target:
        return "Please select both source and target directories."
    if not os.path.isdir(source):
        return f"Source folder does not exist:\n{source}"

    src_abs = os.path.normcase(os.path.abspath(source))
    tgt_abs = os.path.normcase(os.path.abspath(target))

    if src_abs == tgt_abs:
        return "Source and target cannot be the same folder."
    if tgt_abs.startswith(src_abs + os.sep):
        return "Target cannot be a subfolder of Source (would cause recursive copying)."

    target_parent = os.path.dirname(tgt_abs) or tgt_abs
    if not os.path.isdir(target_parent):
        return f"Target's parent folder does not exist:\n{target_parent}"

    return None


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

def main():
    global root
    root = tk.Tk()
    root.title("Backup Tool v2.0")
    root.geometry("420x360")
    root.resizable(False, False)

    active_theme, active_color = apply_theme(
        config.get("theme", "auto"), config.get("color_mode", "auto")
    )
    logger.info(
        f"Theme: setting='{config.get('theme', 'auto')}' color_setting='{config.get('color_mode', 'auto')}' "
        f"resolved='{active_theme}/{active_color}' sv_ttk_available={SV_TTK_AVAILABLE} "
        f"windows11_detected={is_windows_11_or_later()} windows_color_detected='{detect_windows_color_mode()}'"
    )

    grid_frame = tk.Frame(root)
    grid_frame.pack(pady=20)

    status_label = ttk.Label(root, text=f"Tool: {copy_tool}  |  Mode: {backup_mode}")
    status_label.pack()

    # Browse buttons
    button_1 = ttk.Button(
        grid_frame, text="Select Source",
        command=lambda: fileselector(source_path_box)
    )
    button_1.grid(row=0, column=1, padx=5, ipady=3)
    button_2 = ttk.Button(
        grid_frame, text="Select Target",
        command=lambda: fileselector(target_path_box)
    )
    button_2.grid(row=1, column=1, padx=5, ipady=3)

    # Entry boxes, pre-filled from saved config
    source_path_box = ttk.Entry(grid_frame, width=30)
    source_path_box.grid(row=0, column=0, padx=5, pady=5)
    source_path_box.insert(0, config.get("source_path", ""))

    target_path_box = ttk.Entry(grid_frame, width=30)
    target_path_box.grid(row=1, column=0, padx=5, pady=5)
    target_path_box.insert(0, config.get("target_path", ""))

    # Execute button (kept as a variable so we can disable it while running)
    execute_button = ttk.Button(root, text="Execute Backup")
    execute_button.config(
        command=lambda: execute_backup_async(
            source_path_box.get().strip(),
            target_path_box.get().strip(),
            flags,
            execute_button,
            status_label,
        )
    )
    execute_button.pack(pady=10)

    progress = ttk.Progressbar(root, mode="indeterminate", length=300)
    progress.pack(pady=5)

    # Menubar
    menubar = tk.Menu(root)
    file_menu = tk.Menu(menubar, tearoff=0)
    file_menu.add_command(label="Exit", command=root.quit)
    menubar.add_cascade(label="File", menu=file_menu)

    extras_menu = tk.Menu(menubar, tearoff=0)
    extras_menu.add_command(
        label="Settings",
        command=lambda: settings_dialog(status_label)
    )
    extras_menu.add_command(label="Help", command=help_dialog)
    extras_menu.add_command(
        label="About",
        command=lambda: messagebox.showinfo("About", "Backup Tool v2.0\nCreated by Not Open AI")
    )
    menubar.add_cascade(label="Extras", menu=extras_menu)

    # View menu: theme (Modern/Classic) and color mode (Light/Dark) are
    # independent axes, applied together each time either changes.
    view_menu = tk.Menu(menubar, tearoff=0)
    theme_var = tk.StringVar(value=config.get("theme", "auto"))
    color_var = tk.StringVar(value=config.get("color_mode", "auto"))

    def on_appearance_change():
        theme_choice = theme_var.get()
        color_choice = color_var.get()
        config["theme"] = theme_choice
        config["color_mode"] = color_choice
        save_config(config)
        resolved_theme, resolved_color = apply_theme(theme_choice, color_choice)
        logger.info(
            f"Appearance changed by user: theme='{theme_choice}' color='{color_choice}' "
            f"resolved='{resolved_theme}/{resolved_color}'"
        )
        if resolved_theme == "classic" and theme_choice == "modern":
            messagebox.showinfo(
                "Modern theme unavailable",
                "sv-ttk is not installed, so the classic look is being used instead.\n\n"
                "Install it with: pip install sv-ttk"
            )
        elif resolved_theme == "classic" and color_choice != "auto":
            messagebox.showinfo(
                "Color mode needs Modern theme",
                "Light/Dark only changes the look under the Modern theme.\n"
                "Classic always uses Windows' native widget colors."
            )

    theme_submenu = tk.Menu(view_menu, tearoff=0)
    theme_submenu.add_radiobutton(
        label="Auto (match Windows version)", variable=theme_var, value="auto", command=on_appearance_change
    )
    theme_submenu.add_radiobutton(
        label="Modern (Windows 11 style)", variable=theme_var, value="modern", command=on_appearance_change
    )
    theme_submenu.add_radiobutton(
        label="Classic", variable=theme_var, value="classic", command=on_appearance_change
    )
    view_menu.add_cascade(label="Theme", menu=theme_submenu)

    color_submenu = tk.Menu(view_menu, tearoff=0)
    color_submenu.add_radiobutton(
        label="Auto (match Windows)", variable=color_var, value="auto", command=on_appearance_change
    )
    color_submenu.add_radiobutton(
        label="Light", variable=color_var, value="light", command=on_appearance_change
    )
    color_submenu.add_radiobutton(
        label="Dark", variable=color_var, value="dark", command=on_appearance_change
    )
    view_menu.add_cascade(label="Color Mode", menu=color_submenu)

    menubar.add_cascade(label="View", menu=view_menu)

    root.config(menu=menubar)

    # Persist paths on close
    def on_close():
        config["source_path"] = source_path_box.get().strip()
        config["target_path"] = target_path_box.get().strip()
        save_config(config)
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    # Make sure flags reflect the loaded config on startup (no-op print suppressed)
    apply_settings(copy_tool, backup_mode, status_label, silent=True)

    root.mainloop()


def fileselector(entry):
    selected_path = filedialog.askdirectory()
    if selected_path:
        entry.delete(0, tk.END)
        entry.insert(0, selected_path)


# --------------------------------------------------------------------------
# Backup execution (async, off the GUI thread)
# --------------------------------------------------------------------------

def execute_backup_async(zsource_path, ztarget_path, zflags, execute_button, status_label):
    error = validate_paths(zsource_path, ztarget_path)
    if error:
        messagebox.showwarning("Cannot start backup", error)
        return

    if backup_mode in DESTRUCTIVE_MODES:
        confirmed = messagebox.askyesno(
            "Confirm destructive operation",
            f"{MODE_INFO.get(backup_mode, backup_mode)}\n\n"
            f"This may DELETE files in:\n{ztarget_path}\n\n"
            "Continue?",
            icon="warning",
        )
        if not confirmed:
            return

    # Archive mode: every tool needs a fresh, timestamped destination.
    # - rclone: keep copying into the same target, but divert anything it
    #   would overwrite/delete into --backup-dir (a separate versions folder).
    # - robocopy/xcopy: neither has a "diff into a side folder" concept, so
    #   the only way to version is to copy straight into a brand-new
    #   timestamped subfolder under the chosen target.
    run_flags = list(zflags)
    run_target = ztarget_path
    suffix = datetime.now().strftime("%Y%m%d_%H%M%S")

    if backup_mode == "Archive":
        if copy_tool.lower() == "rclone":
            archive_dir = os.path.join(os.path.dirname(os.path.abspath(ztarget_path)), f"_archive_{suffix}")
            if "--backup-dir" not in run_flags:
                run_flags.extend(["--backup-dir", archive_dir])
        else:
            # robocopy / xcopy: copy into target/<timestamp>/
            run_target = os.path.join(ztarget_path, suffix)
            try:
                os.makedirs(run_target, exist_ok=True)
            except OSError as e:
                messagebox.showerror("Error", f"Could not create archive folder:\n{run_target}\n\n{e}")
                return

    try:
        cmd = build_command(copy_tool, zsource_path, run_target, run_flags)
    except ValueError as e:
        messagebox.showwarning("Configuration error", str(e))
        return
    except FileNotFoundError as e:
        offer_tool_download(str(e) or copy_tool)
        return

    logger.info(f"Starting backup | tool={copy_tool} mode={backup_mode} cmd={cmd}")

    execute_button.config(state="disabled", text="Running...")
    status_label.config(text=f"Running {copy_tool} ({backup_mode})...")

    result_queue = queue.Queue()

    def worker():
        start = datetime.now()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=BACKUP_TIMEOUT_SECONDS,
            )
            result_queue.put(("done", result, datetime.now() - start))
        except FileNotFoundError:
            result_queue.put(("missing_tool", copy_tool, None))
        except subprocess.TimeoutExpired:
            result_queue.put(("timeout", None, None))
        except Exception as e:  # noqa: BLE001 - surface any unexpected error to the UI
            result_queue.put(("error", e, None))

    threading.Thread(target=worker, daemon=True).start()
    _poll_backup_result(result_queue, execute_button, status_label)


def _poll_backup_result(result_queue, execute_button, status_label):
    try:
        status, payload, duration = result_queue.get_nowait()
    except queue.Empty:
        root.after(200, lambda: _poll_backup_result(result_queue, execute_button, status_label))
        return

    execute_button.config(state="normal", text="Execute Backup")
    status_label.config(text=f"Tool: {copy_tool}  |  Mode: {backup_mode}")

    if status == "missing_tool":
        logger.error(f"Tool not found at run time: {payload}")
        offer_tool_download(payload)
        return

    if status == "timeout":
        msg = f"Backup timed out after {BACKUP_TIMEOUT_SECONDS} seconds."
        logger.error(msg)
        messagebox.showerror("Timeout", msg)
        return

    if status == "error":
        logger.error(f"Unexpected error: {payload}")
        messagebox.showerror("Error", str(payload))
        return

    result = payload
    success = is_success(copy_tool, result.returncode)
    tail_out = (result.stdout or "")[-500:]
    tail_err = (result.stderr or "")[-500:]

    logger.info(
        f"Backup finished | tool={copy_tool} mode={backup_mode} "
        f"returncode={result.returncode} success={success} duration={duration}"
    )
    if tail_out:
        logger.info(f"stdout tail: {tail_out}")
    if tail_err:
        logger.info(f"stderr tail: {tail_err}")

    if success:
        if backup_mode == "Interactive / Preview":
            show_preview_window(result.stdout or "(no output)")
        else:
            messagebox.showinfo(
                "Backup complete",
                f"{MODE_INFO.get(backup_mode, backup_mode)}\n\n"
                f"Exit code: {result.returncode}\nDuration: {duration}\n\n{tail_out}"
            )
    else:
        messagebox.showerror(
            "Backup failed",
            f"Exit code: {result.returncode}\nDuration: {duration}\n\n{tail_err or tail_out}"
        )


def show_preview_window(full_output):
    """Scrollable window for Interactive/Preview output, which can be long
    (robocopy /L lists every file it *would* touch) and would otherwise be
    truncated/unreadable in a standard messagebox."""
    win = tk.Toplevel()
    win.title("Preview — no changes were made")
    win.geometry("600x450")

    ttk.Label(
        win,
        text="Preview mode: nothing was copied or deleted. This is what would happen:",
        wraplength=580, justify=tk.LEFT,
    ).pack(pady=(10, 5), padx=10, anchor="w")

    text_frame = tk.Frame(win)
    text_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    scrollbar = ttk.Scrollbar(text_frame)
    scrollbar.pack(side="right", fill="y")

    text_widget = tk.Text(text_frame, wrap="none", yscrollcommand=scrollbar.set)
    text_widget.insert("1.0", full_output)
    text_widget.config(state="disabled")
    text_widget.pack(side="left", fill="both", expand=True)
    scrollbar.config(command=text_widget.yview)

    ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 10))


# --------------------------------------------------------------------------
# Missing-tool handling
# --------------------------------------------------------------------------

def offer_tool_download(tool_name):
    """
    Shown when a configured copy tool can't be found (neither bundled
    next to the app nor on PATH). robocopy/xcopy ship with Windows, so
    in practice this mainly fires for rclone.
    """
    tool_lower = (tool_name or "").lower()

    if tool_lower == "rclone":
        go = messagebox.askyesno(
            "Rclone not found",
            "Rclone is not installed and was not found bundled with this app.\n\n"
            f"Expected a bundled copy at:\n{BUNDLED_RCLONE}\n\n"
            "Open the rclone download page now?",
        )
        if go:
            webbrowser.open(RCLONE_DOWNLOAD_URL)
            messagebox.showinfo(
                "After downloading",
                "Either:\n"
                "  • place rclone.exe next to this application's .exe, or\n"
                "  • install it and ensure it's on your system PATH,\n\n"
                "then reopen Settings and select Rclone again."
            )
    else:
        # robocopy/xcopy are part of Windows; if missing, something else is wrong.
        messagebox.showerror(
            "Tool not found",
            f"'{tool_name}' was not found.\n\n"
            "Robocopy and Xcopy ship with Windows by default — if missing, "
            "this likely means a non-standard Windows install or a PATH issue."
        )




def help_dialog():
    help_window = tk.Toplevel()
    help_window.title("Help")
    help_window.geometry("400x300")
    help_text = ttk.Label(
        help_window,
        text=(
            "Backup Modes:\n\n"
            "Mirror: Destination = Source (deletes removed files)\n"
            "Update: Safe mode, only adds/updates files\n"
            "Archive: Versioned backups with timestamps\n"
            "Preview: Simulates without making changes\n\n"
            "Logs are written to:\n" + LOG_PATH
        ),
        justify=tk.LEFT, wraplength=350,
    )
    help_text.pack(pady=20, padx=10)


# --------------------------------------------------------------------------
# Settings dialog
# --------------------------------------------------------------------------

def settings_dialog(status_label):
    settings_window = tk.Toplevel()
    settings_window.title("Settings")
    settings_window.geometry("400x340")
    settings_window.resizable(False, False)

    # Only offer tools that are actually installed.
    candidate_tools = ["Robocopy", "Xcopy", "Rclone"]
    available_tools = [t for t in candidate_tools if is_tool_available(t.lower())]
    missing_tools = [t for t in candidate_tools if t not in available_tools]

    if not available_tools:
        ttk.Label(
            settings_window,
            text="⚠ No supported copy tools found on PATH.",
            foreground="red",
        ).pack(pady=(10, 0))

    if missing_tools:
        ttk.Label(
            settings_window,
            text=f"⚠ Not installed (hidden): {', '.join(missing_tools)}",
            foreground="red",
        ).pack(pady=(10, 0))
        if "Rclone" in missing_tools:
            ttk.Button(
                settings_window,
                text="Get Rclone...",
                command=lambda: offer_tool_download("rclone"),
            ).pack(pady=(5, 0))

    settings_label = ttk.Label(settings_window, text="Copy Tool Selection")
    settings_label.pack(pady=10)
    copytool_selection = ttk.Combobox(settings_window, values=available_tools, state="readonly")
    copytool_selection.pack(pady=5)
    if available_tools:
        current = copy_tool if copy_tool in available_tools else available_tools[0]
        copytool_selection.set(current)

    mode_label = ttk.Label(settings_window, text="Backup Mode")
    mode_label.pack(pady=(15, 5))
    mode_selection = ttk.Combobox(
        settings_window,
        values=list(MODE_DESCRIPTIONS.keys()),
        state="readonly",
        width=30,
    )
    mode_selection.pack(pady=5)
    mode_selection.set(backup_mode)

    mode_desc = ttk.Label(settings_window, text="", justify=tk.LEFT, wraplength=350, foreground="gray")
    mode_desc.pack(pady=10, padx=10)

    def update_mode_desc(event=None):
        mode_desc.config(text=MODE_DESCRIPTIONS.get(mode_selection.get(), ""))

    mode_selection.bind("<<ComboboxSelected>>", update_mode_desc)
    update_mode_desc()

    def on_apply():
        if not copytool_selection.get():
            messagebox.showwarning("No tool available", "No supported copy tool is installed.")
            return
        apply_settings(copytool_selection.get(), mode_selection.get(), status_label)
        settings_window.destroy()

    settings_apply_button = ttk.Button(settings_window, text="Apply", command=on_apply)
    settings_apply_button.pack(pady=15)


def apply_settings(selected_tool, selected_mode, status_label=None, silent=False):
    global copy_tool, backup_mode
    copy_tool = selected_tool
    backup_mode = selected_mode
    flags.clear()

    tool = selected_tool.lower()

    if selected_mode == "Mirror":
        if tool == "rclone":
            flags.extend(["sync", "--delete-during"])
        elif tool == "robocopy":
            flags.append("/MIR")
        elif tool == "xcopy":
            flags.extend(["/E", "/Y"])

    elif selected_mode == "Update / Incremental":
        if tool == "rclone":
            flags.append("sync")
        elif tool == "robocopy":
            flags.append("/E")
        elif tool == "xcopy":
            flags.extend(["/E", "/D", "/Y"])

    elif selected_mode == "Archive":
        if tool == "rclone":
            # backup-dir itself is computed at run time (needs a fresh
            # timestamp + the actual target path); just mark the subcommand
            # here. See execute_backup_async().
            flags.append("sync")
        elif tool == "robocopy":
            flags.extend(["/E", "/DCOPY:T"])
        elif tool == "xcopy":
            flags.extend(["/E", "/D", "/Y"])

    elif selected_mode == "Interactive / Preview":
        if tool == "rclone":
            flags.extend(["sync", "--dry-run"])
        elif tool == "robocopy":
            flags.append("/L")
        elif tool == "xcopy":
            flags.extend(["/L", "/E"])

    config["copy_tool"] = copy_tool
    config["backup_mode"] = backup_mode
    save_config(config)

    logger.info(f"Settings applied | tool={selected_tool} mode={selected_mode} flags={flags}")

    if status_label is not None:
        status_label.config(text=f"Tool: {copy_tool}  |  Mode: {backup_mode}")

    if not silent:
        messagebox.showinfo("Settings", f"Saved {selected_tool} with {selected_mode} mode.")


if __name__ == "__main__":
    main()