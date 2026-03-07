"""Main application class - wires GUI and pipeline together."""

import json
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox

from . import __version__
from .gui import MainWindow
from .pipeline import (DecryptionPipeline, ModPipeline,
                        StandaloneDecryptPipeline, StandaloneModPipeline,
                        DirectSSDDecryptPipeline, DirectSSDModPipeline,
                        check_prerequisites, export_mod_pack,
                        import_mod_pack)
from .updater import check_for_update
from .executor import create_executor
import sys

# Settings file location — platform-aware
if sys.platform == "win32":
    _SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                                  "jjp_decryptor")
elif sys.platform == "darwin":
    _SETTINGS_DIR = os.path.join(os.path.expanduser("~/Library/Application Support"),
                                  "jjp_decryptor")
else:
    _SETTINGS_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME",
                                  os.path.expanduser("~/.config")),
                                  "jjp_decryptor")
_SETTINGS_FILE = os.path.join(_SETTINGS_DIR, "settings.json")


# Message types for the thread-safe queue
class LogMsg:
    def __init__(self, text, level="info"):
        self.text = text
        self.level = level

class PhaseMsg:
    def __init__(self, index):
        self.index = index

class ProgressMsg:
    def __init__(self, current, total, desc=""):
        self.current = current
        self.total = total
        self.desc = desc

class DoneMsg:
    def __init__(self, success, summary):
        self.success = success
        self.summary = summary

class LinkMsg:
    def __init__(self, text, url):
        self.text = text
        self.url = url

class FileTreeMsg:
    def __init__(self, rel_path, status="Modified", detail=""):
        self.rel_path = rel_path
        self.status = status
        self.detail = detail


class App:
    """Top-level application controller."""

    def __init__(self):
        self.root = tk.Tk()
        self.msg_queue = queue.Queue()
        self.pipeline = None
        self.executor = create_executor()
        self._active_mode = "decrypt"  # "decrypt" or "modify"

        # Pre-load theme preference (needed before window creation)
        saved_theme = None
        try:
            with open(_SETTINGS_FILE, "r") as f:
                saved_theme = json.load(f).get("theme")
        except Exception:
            pass

        self.window = MainWindow(
            self.root,
            on_check_prereqs=self._check_prereqs,
            on_start=self._start,
            on_cancel=self._cancel,
            on_mod_apply=self._mod_start,
            on_mod_cancel=self._mod_cancel,
            on_theme_change=self._on_theme_change,
            initial_theme=saved_theme,
            on_install_prereqs=self._install_prereqs,
            on_import=self._start_import,
            on_export=self._start_export,
        )

        # Load saved settings and pre-populate fields
        self._load_settings()

        # Start polling the message queue
        self._poll_queue()

        # Show version in title bar
        self.root.title(f"JJP Asset Decryptor v{__version__}")

        # Auto-check prerequisites, update, and clean up stale mounts on startup
        self.root.after(500, self._check_prereqs)
        self.root.after(500, self._check_stale_mounts)
        self.root.after(1500, self._check_for_update)

        # Intercept window close to offer cache cleanup
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def run(self):
        """Start the tkinter mainloop."""
        self.root.mainloop()

    def _on_close(self):
        """Handle window close — offer to free cached images."""
        from .executor import DockerExecutor
        # Determine cache location label for the message
        if sys.platform == "win32":
            cache_label = "WSL"
        elif sys.platform == "darwin":
            cache_label = "Docker"
        else:
            cache_label = "/tmp"

        try:
            result = self.executor.run(
                "find /tmp -maxdepth 1 -name 'jjp_raw_*' -type f "
                "-printf '%f %s\\n' 2>/dev/null",
                timeout=5,
            ).strip()
            if result:
                files = []
                total_bytes = 0
                for line in result.split("\n"):
                    parts = line.strip().rsplit(" ", 1)
                    if len(parts) == 2:
                        files.append(parts[0])
                        try:
                            total_bytes += int(parts[1])
                        except ValueError:
                            pass
                if files:
                    size_gb = total_bytes / (1024**3)
                    names = "\n".join(f"/tmp/{f}" for f in files)
                    answer = messagebox.askyesnocancel(
                        "Free Disk Space?",
                        f"There are cached game images in {cache_label} using "
                        f"{size_gb:.1f} GB of disk space:\n\n"
                        f"{names}\n\n"
                        f"Would you like to delete them to free up space?\n\n"
                        f"Keeping them speeds up future runs by skipping\n"
                        f"the extraction step. Your output folder and\n"
                        f"original ISOs are not affected either way.",
                    )
                    if answer is None:
                        return  # Cancel — don't close
                    if answer:
                        self.executor.run(
                            "find /tmp -maxdepth 1 -name 'jjp_raw_*' -type f "
                            "-delete 2>/dev/null; true",
                            timeout=30,
                        )
        except Exception:
            pass  # Don't block close if executor is unavailable

        # Stop Docker container on exit if applicable
        if isinstance(self.executor, DockerExecutor):
            try:
                self.executor.stop_container()
            except Exception:
                pass

        self._save_settings()
        self.root.destroy()

    def _poll_queue(self):
        """Process messages from background threads."""
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                if isinstance(msg, LogMsg):
                    self.window.append_log(msg.text, msg.level)
                elif isinstance(msg, LinkMsg):
                    self.window.append_log_link(msg.text, msg.url)
                elif isinstance(msg, PhaseMsg):
                    self.window.set_phase(msg.index, mode=self._active_mode)
                    from . import config
                    phase_map = {
                        "decrypt": config.PHASES,
                        "modify": config.MOD_PHASES,
                        "decrypt_standalone": config.STANDALONE_PHASES,
                        "modify_standalone": config.STANDALONE_MOD_PHASES,
                        "ssd_decrypt": config.DIRECT_SSD_PHASES,
                        "ssd_modify": config.DIRECT_SSD_MOD_PHASES,
                    }
                    phases = phase_map.get(self._active_mode, config.PHASES)
                    if msg.index < len(phases):
                        self.window.set_status(f"{phases[msg.index]}...")
                elif isinstance(msg, ProgressMsg):
                    self.window.set_progress(
                        msg.current, msg.total, msg.desc,
                        mode=self._active_mode)
                elif isinstance(msg, FileTreeMsg):
                    self.window._update_write_tree(
                        msg.rel_path, msg.status)
                elif isinstance(msg, DoneMsg):
                    self._on_done(msg.success, msg.summary)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _check_prereqs(self):
        """Run prerequisite checks in a background thread."""
        self.window.append_log("Checking prerequisites...", "info")

        def _run():
            results = check_prerequisites(self.executor, standalone=True)
            for name, passed, message in results:
                self.msg_queue.put(LogMsg(
                    f"  {name}: {'OK' if passed else 'MISSING'} - {message}",
                    "success" if passed else "error",
                ))
                self.root.after(0, self.window.set_prereq, name, passed, message)

            all_ok = all(p for _, p, _ in results)
            if all_ok:
                self.msg_queue.put(LogMsg("All prerequisites met.", "success"))
            else:
                self.msg_queue.put(LogMsg(
                    "Some prerequisites are missing. Fix them before proceeding.",
                    "error"))

        threading.Thread(target=_run, daemon=True).start()

    def _install_prereqs(self):
        """Install missing prerequisites in a background thread."""
        from .executor import WslExecutor, DockerExecutor, NativeExecutor

        if isinstance(self.executor, WslExecutor):
            self._install_prereqs_wsl()
        elif isinstance(self.executor, DockerExecutor):
            self.window.append_log(
                "Docker prerequisites are managed automatically. "
                "Please ensure Docker Desktop is running.", "info")
        elif isinstance(self.executor, NativeExecutor):
            self._install_prereqs_native()

    def _install_prereqs_wsl(self):
        """Install all WSL prerequisites and prompt for restart."""
        # Check if WSL2 itself is working first
        wsl_ok = False
        try:
            self.executor.run("echo ok", timeout=15)
            wsl_ok = True
        except Exception:
            pass

        if not wsl_ok:
            # WSL2 isn't available — launch the PowerShell installer script
            self._launch_prereqs_script()
            return

        self.window.append_log("Installing prerequisites in WSL...", "info")
        self.window.install_btn.configure(state=tk.DISABLED)

        def _run():
            try:
                for line in self.executor.stream(
                    "apt-get update -qq && "
                    "apt-get install -y partclone xorriso e2fsprogs pigz ffmpeg 2>&1",
                    timeout=300,
                ):
                    self.msg_queue.put(LogMsg(f"  {line}", "info"))
                self.msg_queue.put(LogMsg(
                    "Prerequisites installed successfully.", "success"))
            except Exception as e:
                self.msg_queue.put(LogMsg(
                    f"Installation failed: {e}", "error"))

            # Re-check and update indicators
            results = check_prerequisites(self.executor, standalone=True)
            for name, passed, message in results:
                self.root.after(0, self.window.set_prereq, name, passed, message)

            all_ok = all(p for _, p, _ in results)
            if all_ok:
                self.msg_queue.put(LogMsg("All prerequisites met.", "success"))
                self.root.after(0, lambda: messagebox.showinfo(
                    "Installation Complete",
                    "All prerequisites installed successfully.\n\n"
                    "If this is your first time installing WSL, "
                    "please restart your computer for best results."))
            else:
                self.msg_queue.put(LogMsg(
                    "Some prerequisites are still missing.", "error"))

        threading.Thread(target=_run, daemon=True).start()

    def _launch_prereqs_script(self):
        """Launch the PowerShell prerequisite installer for WSL2 setup."""
        import subprocess

        # Look for install_prerequisites.ps1 in common locations
        candidates = [
            # Installed via Inno Setup (same dir as app)
            os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "install_prerequisites.ps1"),
            # Development layout
            os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "installer",
                "install_prerequisites.ps1"),
        ]

        script_path = None
        for path in candidates:
            if os.path.isfile(path):
                script_path = path
                break

        if script_path:
            self.window.append_log(
                "WSL2 is not available. Launching prerequisite installer...",
                "info")
            try:
                subprocess.Popen(
                    ["powershell.exe", "-NoProfile", "-ExecutionPolicy",
                     "Bypass", "-File", script_path],
                    creationflags=subprocess.CREATE_NEW_CONSOLE)
                self.window.append_log(
                    "Prerequisite installer opened in a new window. "
                    "After it completes, restart this app and click Check.",
                    "info")
            except Exception as e:
                self.window.append_log(
                    f"Failed to launch installer: {e}", "error")
        else:
            # Script not found — show manual instructions
            self.window.append_log(
                "WSL2 is not available. To install manually:", "error")
            self.window.append_log(
                "  1. Open PowerShell as Administrator", "info")
            self.window.append_log(
                "  2. Run: wsl --install -d Ubuntu", "info")
            self.window.append_log(
                "  3. Restart your computer", "info")
            self.window.append_log(
                "  4. Re-open this app and click Install Missing", "info")
            messagebox.showinfo(
                "WSL2 Not Installed",
                "WSL2 is required but not installed.\n\n"
                "Open PowerShell as Administrator and run:\n"
                "  wsl --install -d Ubuntu\n\n"
                "Then restart your computer and re-open this app.")

    def _install_prereqs_native(self):
        """Install all prerequisites on native Linux."""
        self.window.append_log("Installing prerequisites...", "info")
        self.window.install_btn.configure(state=tk.DISABLED)

        def _run():
            try:
                for line in self.executor.stream(
                    "apt-get update -qq && "
                    "apt-get install -y partclone xorriso e2fsprogs pigz ffmpeg 2>&1",
                    timeout=300,
                ):
                    self.msg_queue.put(LogMsg(f"  {line}", "info"))
                self.msg_queue.put(LogMsg(
                    "Prerequisites installed successfully.", "success"))
            except Exception as e:
                self.msg_queue.put(LogMsg(
                    f"Installation failed: {e}", "error"))

            # Re-check and update indicators
            results = check_prerequisites(self.executor, standalone=True)
            for name, passed, message in results:
                self.root.after(0, self.window.set_prereq, name, passed, message)

            all_ok = all(p for _, p, _ in results)
            if all_ok:
                self.msg_queue.put(LogMsg("All prerequisites met.", "success"))

        threading.Thread(target=_run, daemon=True).start()

    def _check_for_update(self):
        """Check GitHub for a newer release in a background thread."""
        def _run():
            result = check_for_update(__version__)
            if result:
                version, url = result
                self.msg_queue.put(LogMsg(
                    f"Update available: v{version}", "info"))
                self.msg_queue.put(LinkMsg(
                    f"Download v{version}", url))

        threading.Thread(target=_run, daemon=True).start()

    # --- Create Mods (decrypt) ---

    def _find_fl_dat(self, folder=None):
        """Look for a cached fl_decrypted.dat in the given or output folder."""
        paths_to_check = []
        if folder:
            paths_to_check.append(folder)
        output_path = self.window.output_var.get().strip()
        if output_path:
            paths_to_check.append(output_path)
        modify_input = self.window.modify_input_var.get().strip()
        if modify_input:
            paths_to_check.append(modify_input)
        write_input = self.window.write_input_var.get().strip()
        if write_input:
            paths_to_check.append(write_input)
        for p in paths_to_check:
            fl_path = os.path.join(p, 'fl_decrypted.dat')
            if os.path.isfile(fl_path):
                return fl_path
        return None

    def _start(self):
        """Start the Decrypt action (dispatches based on Decrypt radio)."""
        source = self.window.get_decrypt_source()
        if source == "ssd":
            self._start_ssd_decrypt()
            return

        # ISO decrypt
        image_path = self.window.image_var.get().strip()
        output_path = self.window.output_var.get().strip()

        if not image_path:
            messagebox.showwarning("Missing Input",
                "Please select a game image file.")
            return
        if not output_path:
            messagebox.showwarning("Missing Input",
                "Please select an output folder.")
            return

        # At least one category must be selected
        if (not self.window.extract_graphics_var.get() and
                not self.window.extract_sounds_var.get() and
                not self.window.full_dump_var.get()):
            messagebox.showwarning("Nothing Selected",
                "Please select at least one category to extract "
                "(Graphics, Sounds, or File System).")
            return

        # Warn if output folder already has decrypted content
        if os.path.isdir(output_path) and os.listdir(output_path):
            proceed = messagebox.askyesno(
                "Output Folder Not Empty",
                "The output folder already contains files.\n\n"
                "Decrypting again will overwrite any existing files "
                "(including files you may have modified).\n\n"
                "Continue?")
            if not proceed:
                return

        self._save_settings()

        fl_dat_path = self._find_fl_dat()

        self._active_mode = "decrypt_standalone"
        self.window.set_running(True, mode=self._active_mode)
        self.window.reset_steps(mode=self._active_mode)

        def log_cb(text, level="info"):
            self.msg_queue.put(LogMsg(text, level))

        def phase_cb(index):
            self.msg_queue.put(PhaseMsg(index))

        def progress_cb(current, total, desc=""):
            self.msg_queue.put(ProgressMsg(current, total, desc))

        def done_cb(success, summary):
            self.msg_queue.put(DoneMsg(success, summary))

        if fl_dat_path:
            log_cb(f"Using cached file list: {fl_dat_path}", "success")
        else:
            log_cb("No cached file list found. Will scan filesystem "
                   "and auto-detect filler sizes.", "info")
        log_cb("No dongle, chroot, or gcc required.", "success")

        full_dump = self.window.full_dump_var.get()
        extract_graphics = self.window.extract_graphics_var.get()
        extract_sounds = self.window.extract_sounds_var.get()
        self.pipeline = StandaloneDecryptPipeline(
            image_path, output_path, fl_dat_path,
            log_cb, phase_cb, progress_cb, done_cb,
            full_dump=full_dump,
            extract_graphics=extract_graphics,
            extract_sounds=extract_sounds,
        )
        threading.Thread(target=self.pipeline.run, daemon=True).start()

    def _start_ssd_decrypt(self):
        """Start the direct SSD decryption pipeline."""
        device = self.window.get_ssd_device()
        output_path = self.window.output_var.get().strip()

        if device is None:
            messagebox.showwarning("No Device",
                "Please select a drive from the device list.\n\n"
                "Connect the JJP SSD via a USB enclosure, then click Refresh.")
            return
        if not output_path:
            messagebox.showwarning("Missing Input",
                "Please select an output folder.")
            return

        # Warn if output folder already has decrypted content
        if os.path.isdir(output_path) and os.listdir(output_path):
            proceed = messagebox.askyesno(
                "Output Folder Not Empty",
                "The output folder already contains files.\n\n"
                "Decrypting again will overwrite any existing files "
                "(including files you may have modified).\n\n"
                "Continue?")
            if not proceed:
                return

        # Confirm device selection
        proceed = messagebox.askyesno(
            "Confirm Device",
            f"You are about to read from:\n\n"
            f"  Model:   {device.model}\n"
            f"  Size:      {device.size_display}\n"
            f"  Bus:       {device.bus_type}\n"
            f"  Device:  {device.device_id}\n\n"
            f"This will mount the drive read-only to decrypt game assets.\n\n"
            f"Continue?")
        if not proceed:
            return

        self._save_settings()

        fl_dat_path = self._find_fl_dat()

        self._active_mode = "ssd_decrypt"
        self.window.set_running(True, mode=self._active_mode)
        self.window.reset_steps(mode=self._active_mode)

        def log_cb(text, level="info"):
            self.msg_queue.put(LogMsg(text, level))

        def phase_cb(index):
            self.msg_queue.put(PhaseMsg(index))

        def progress_cb(current, total, desc=""):
            self.msg_queue.put(ProgressMsg(current, total, desc))

        def done_cb(success, summary):
            self.msg_queue.put(DoneMsg(success, summary))

        if fl_dat_path:
            log_cb(f"Using cached file list: {fl_dat_path}", "success")
        else:
            log_cb("No cached file list found. Will scan filesystem "
                   "and auto-detect filler sizes.", "info")
        log_cb("Direct SSD mode \u2014 no ISO extraction needed.", "success")

        full_dump = self.window.full_dump_var.get()
        extract_graphics = self.window.extract_graphics_var.get()
        extract_sounds = self.window.extract_sounds_var.get()
        self.pipeline = DirectSSDDecryptPipeline(
            device.device_id, output_path, fl_dat_path,
            log_cb, phase_cb, progress_cb, done_cb,
            full_dump=full_dump,
            extract_graphics=extract_graphics,
            extract_sounds=extract_sounds,
        )
        threading.Thread(target=self.pipeline.run, daemon=True).start()

    def _cancel(self):
        """Cancel the running pipeline."""
        if self.pipeline:
            self.window.append_log("Cancelling...", "error")
            self.pipeline.cancel()

    # --- Install Mods (modify / export) ---

    def _mod_start(self):
        """Start the Write action (dispatches based on Write radio)."""
        method = self.window.get_write_method()
        if method == "ssd":
            self._start_ssd_modify()
            return

        # ISO modify — uses Write tab fields
        output_path = self.window.write_input_var.get().strip()
        image_path = self.window.write_orig_image_var.get().strip()

        if not output_path:
            messagebox.showwarning("Missing Input",
                "Please select an input folder (containing your modified assets).")
            return
        if not os.path.isdir(output_path):
            messagebox.showerror("Invalid Folder",
                f"Input folder does not exist:\n{output_path}")
            return
        if not image_path:
            messagebox.showwarning("Missing Input",
                "Please select the original game image file.")
            return

        checksums_file = os.path.join(output_path, '.checksums.md5')
        if not os.path.isfile(checksums_file):
            messagebox.showerror("No Baseline Checksums",
                "No .checksums.md5 file found in the input folder.\n\n"
                "Decrypt your game first to generate baseline checksums, then "
                "modify files in the folder and try again.")
            return

        if not image_path.lower().endswith(".iso"):
            proceed = messagebox.askyesno("Non-ISO Input",
                "The selected image is not an ISO file.\n\n"
                "Build USB ISO can still encrypt your changes, but the output "
                "will be a raw .img file instead of a bootable Clonezilla ISO.\n\n"
                "For a flashable ISO, select the original Clonezilla ISO.\n\n"
                "Continue anyway?")
            if not proceed:
                return

        self._save_settings()

        fl_dat_path = self._find_fl_dat()

        self._active_mode = "modify_standalone"
        self.window.set_running(True, mode=self._active_mode)
        self.window.reset_steps(mode=self._active_mode)

        def log_cb(text, level="info"):
            self.msg_queue.put(LogMsg(text, level))

        def phase_cb(index):
            self.msg_queue.put(PhaseMsg(index))

        def progress_cb(current, total, desc=""):
            self.msg_queue.put(ProgressMsg(current, total, desc))

        def done_cb(success, summary):
            self.msg_queue.put(DoneMsg(success, summary))

        if not fl_dat_path:
            messagebox.showerror(
                "Missing File List",
                "No fl_decrypted.dat found in the output folder.\n\n"
                "Decrypt your game first to generate the file list, then try "
                "again.")
            return

        log_cb(f"Using cached file list: {fl_dat_path}", "success")
        log_cb("No dongle, chroot, or gcc required.", "success")
        self.pipeline = StandaloneModPipeline(
            image_path, output_path, fl_dat_path,
            log_cb, phase_cb, progress_cb, done_cb,
        )

        self.pipeline.log_link = lambda text, url: self.msg_queue.put(LinkMsg(text, url))
        self.pipeline._file_tree_cb = lambda rel_path, status: \
            self.msg_queue.put(FileTreeMsg(rel_path, status))
        threading.Thread(target=self.pipeline.run, daemon=True).start()

    def _start_ssd_modify(self):
        """Start the direct SSD modification pipeline."""
        device = self.window.get_write_ssd_device()
        output_path = self.window.write_input_var.get().strip()

        if device is None:
            messagebox.showwarning("No Device",
                "Please select a drive from the device list.\n\n"
                "Connect the JJP SSD via a USB enclosure, then click Refresh.")
            return
        if not output_path:
            messagebox.showwarning("Missing Input",
                "Please select an input folder (containing your modified assets).")
            return
        if not os.path.isdir(output_path):
            messagebox.showerror("Invalid Folder",
                f"Input folder does not exist:\n{output_path}")
            return

        checksums_file = os.path.join(output_path, '.checksums.md5')
        if not os.path.isfile(checksums_file):
            messagebox.showerror("No Baseline Checksums",
                "No .checksums.md5 file found in the input folder.\n\n"
                "Decrypt your game first to generate baseline checksums, then "
                "modify files and try again.")
            return

        fl_dat_path = self._find_fl_dat(output_path)
        if not fl_dat_path:
            messagebox.showerror(
                "Missing File List",
                "No fl_decrypted.dat found in the output folder.\n\n"
                "Decrypt your game first to generate the file list, then try "
                "again.")
            return

        # Confirm device selection — this modifies a physical drive
        proceed = messagebox.askyesno(
            "Confirm Direct SSD Modification",
            f"WARNING: You are about to modify files directly on:\n\n"
            f"  Model:   {device.model}\n"
            f"  Size:      {device.size_display}\n"
            f"  Bus:       {device.bus_type}\n"
            f"  Device:  {device.device_id}\n\n"
            f"This writes encrypted files directly to the SSD.\n"
            f"Make sure you have a backup (original Clonezilla ISO).\n\n"
            f"This cannot be undone. Continue?")
        if not proceed:
            return

        self._save_settings()

        self._active_mode = "ssd_modify"
        self.window.set_running(True, mode=self._active_mode)
        self.window.reset_steps(mode=self._active_mode)

        def log_cb(text, level="info"):
            self.msg_queue.put(LogMsg(text, level))

        def phase_cb(index):
            self.msg_queue.put(PhaseMsg(index))

        def progress_cb(current, total, desc=""):
            self.msg_queue.put(ProgressMsg(current, total, desc))

        def done_cb(success, summary):
            self.msg_queue.put(DoneMsg(success, summary))

        log_cb(f"Using cached file list: {fl_dat_path}", "success")
        log_cb("Direct SSD mod mode \u2014 writing directly to drive.", "info")

        self.pipeline = DirectSSDModPipeline(
            device.device_id, output_path, fl_dat_path,
            log_cb, phase_cb, progress_cb, done_cb,
        )
        self.pipeline.log_link = lambda text, url: self.msg_queue.put(LinkMsg(text, url))
        self.pipeline._file_tree_cb = lambda rel_path, status: \
            self.msg_queue.put(FileTreeMsg(rel_path, status))
        threading.Thread(target=self.pipeline.run, daemon=True).start()

    def _start_export(self):
        """Export modified files from the mod folder into a shareable zip."""
        from tkinter import filedialog as fd

        output_path = self.window.modify_input_var.get().strip()
        if not output_path:
            messagebox.showwarning("Missing Input",
                "Please select a mod folder first.")
            return
        if not os.path.isdir(output_path):
            messagebox.showerror("Invalid Folder",
                f"Output folder does not exist:\n{output_path}")
            return

        checksums_file = os.path.join(output_path, '.checksums.md5')
        if not os.path.isfile(checksums_file):
            messagebox.showerror("No Baseline Checksums",
                "No .checksums.md5 file found in the output folder.\n\n"
                "Decrypt your game first to generate baseline checksums, then "
                "modify files and try again.")
            return

        fl_dat = os.path.join(output_path, 'fl_decrypted.dat')
        if not os.path.isfile(fl_dat):
            messagebox.showerror("Missing File List",
                "No fl_decrypted.dat found in the output folder.\n\n"
                "Decrypt your game first to generate the file list.")
            return

        # Try to detect game name for default filename
        game_name = ""
        from . import config
        folder_name = os.path.basename(output_path).lower()
        for key in config.KNOWN_GAMES:
            if key.lower() in folder_name:
                game_name = key + "_"
                break

        zip_path = fd.asksaveasfilename(
            title="Save Mod Pack As",
            defaultextension=".zip",
            initialfile=f"{game_name}mod_pack.zip",
            filetypes=[("Zip files", "*.zip"), ("All files", "*.*")],
        )
        if not zip_path:
            return  # User cancelled

        self.window.append_log("Exporting mod pack...", "info")

        def log_cb(text, level="info"):
            self.msg_queue.put(LogMsg(text, level))

        def progress_cb(current, total, desc=""):
            self.msg_queue.put(ProgressMsg(current, total, desc))

        def _run():
            try:
                num_changed, path = export_mod_pack(
                    output_path, zip_path,
                    log_cb=log_cb, progress_cb=progress_cb,
                )
                self.msg_queue.put(LogMsg(
                    f"Mod pack exported successfully with {num_changed} file(s).",
                    "success"))
                self.root.after(0, lambda: messagebox.showinfo(
                    "Export Complete",
                    f"Mod pack saved to:\n{path}\n\n"
                    f"Contains {num_changed} modified file(s).\n\n"
                    f"Share this zip with other users. They can apply it by:\n"
                    f"1. Decrypting their own game first\n"
                    f"2. Importing the mod pack on the Modify tab\n"
                    f"3. Building a USB ISO or writing to SSD on the Write tab"))
            except Exception as e:
                self.msg_queue.put(LogMsg(f"Export failed: {e}", "error"))
                self.root.after(0, lambda: messagebox.showerror(
                    "Export Failed", str(e)))

        threading.Thread(target=_run, daemon=True).start()

    def _start_import(self):
        """Import a mod pack ZIP into the mod folder."""
        from tkinter import filedialog as fd

        output_path = self.window.modify_input_var.get().strip()
        if not output_path:
            messagebox.showwarning("Missing Input",
                "Please select an input folder first.")
            return
        if not os.path.isdir(output_path):
            messagebox.showerror("Invalid Folder",
                f"Input folder does not exist:\n{output_path}")
            return

        zip_path = fd.askopenfilename(
            title="Select Mod Pack ZIP",
            filetypes=[("Zip files", "*.zip"), ("All files", "*.*")],
        )
        if not zip_path:
            return  # User cancelled

        proceed = messagebox.askyesno(
            "Import Mod Pack",
            f"This will extract the mod pack into:\n\n"
            f"  {output_path}\n\n"
            f"Existing files with the same names will be overwritten.\n\n"
            f"Continue?")
        if not proceed:
            return

        self.window.append_log("Importing mod pack...", "info")

        def log_cb(text, level="info"):
            self.msg_queue.put(LogMsg(text, level))

        def progress_cb(current, total, desc=""):
            self.msg_queue.put(ProgressMsg(current, total, desc))

        def _run():
            try:
                num_files = import_mod_pack(
                    zip_path, output_path,
                    log_cb=log_cb, progress_cb=progress_cb,
                )
                self.msg_queue.put(LogMsg(
                    f"Mod pack imported successfully ({num_files} file(s)).",
                    "success"))
                self.root.after(0, lambda: messagebox.showinfo(
                    "Import Complete",
                    f"Imported {num_files} file(s) from:\n"
                    f"{os.path.basename(zip_path)}\n\n"
                    f"Now go to the Write tab and use Build USB ISO\n"
                    f"or Write to SSD to install these mods."))
            except Exception as e:
                self.msg_queue.put(LogMsg(f"Import failed: {e}", "error"))
                self.root.after(0, lambda: messagebox.showerror(
                    "Import Failed", str(e)))

        threading.Thread(target=_run, daemon=True).start()

    def _mod_cancel(self):
        """Cancel the running mod pipeline."""
        if self.pipeline:
            self.window.append_log("Cancelling...", "error")
            self.pipeline.cancel()

    # --- Common ---

    def _on_done(self, success, summary):
        """Handle pipeline completion."""
        mode = self._active_mode
        self.window.set_running(False, mode=mode)
        is_decrypt = mode in ("decrypt", "decrypt_standalone", "ssd_decrypt")
        if success:
            self.window.set_status("Complete!")
            title = "Decryption Complete" if is_decrypt else "Modification Complete"
            messagebox.showinfo(title, summary)
        else:
            self.window.set_status("Failed")
            title = "Decryption Failed" if is_decrypt else "Modification Failed"
            messagebox.showerror(title, summary)

    def _load_settings(self):
        """Load saved settings and pre-populate GUI fields."""
        try:
            with open(_SETTINGS_FILE, "r") as f:
                settings = json.load(f)
            if settings.get("image_path"):
                self.window.image_var.set(settings["image_path"])
            if settings.get("output_path"):
                self.window.output_var.set(settings["output_path"])
            # Support both old and new key names for backwards compatibility
            write_output = (settings.get("write_output_path")
                            or settings.get("install_output_path"))
            if write_output:
                self.window.write_output_var.set(write_output)
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass  # No saved settings yet

    def _on_theme_change(self, theme):
        """Save theme preference when user toggles it."""
        self._save_settings()

    def _save_settings(self):
        """Save current field values to disk."""
        settings = {
            "image_path": self.window.image_var.get().strip(),
            "output_path": self.window.output_var.get().strip(),
            "write_output_path": self.window.write_output_var.get().strip(),
            "theme": self.window._current_theme,
        }
        try:
            os.makedirs(_SETTINGS_DIR, exist_ok=True)
            with open(_SETTINGS_FILE, "w") as f:
                json.dump(settings, f, indent=2)
        except OSError:
            pass  # Non-critical

    def _check_stale_mounts(self):
        """Clean up leftover mounts from crashed runs on startup."""
        def _run():
            try:
                from . import config
                result = self.executor.run(
                    f"findmnt -rn -o TARGET | grep '{config.MOUNT_PREFIX}'",
                    timeout=10,
                )
                mounts = [m.strip() for m in result.strip().split("\n") if m.strip()]
                if not mounts:
                    return

                self.msg_queue.put(LogMsg(
                    f"Cleaning up {len(mounts)} stale mount(s) from previous runs...",
                    "info",
                ))

                # Unmount all in reverse order (submounts before parents)
                self.executor.run(
                    f"findmnt -rn -o TARGET | grep '{config.MOUNT_PREFIX}' | sort -r | "
                    f"xargs -r -I{{}} umount -lf '{{}}' 2>/dev/null; true",
                    timeout=30,
                )

                # Remove empty mount directories
                self.executor.run(
                    f"find /mnt -maxdepth 1 -name 'jjp_*' -type d -empty -delete 2>/dev/null; true",
                    timeout=10,
                )

                # Detach any stale loop devices
                try:
                    loops = self.executor.run(
                        "losetup -a 2>/dev/null | grep jjp_raw",
                        timeout=10,
                    ).strip()
                    for line in loops.split("\n"):
                        line = line.strip()
                        if line:
                            loop_dev = line.split(":")[0]
                            try:
                                self.executor.run(f"losetup -d '{loop_dev}' 2>/dev/null; true", timeout=5)
                            except Exception:
                                pass
                except Exception:
                    pass

                self.msg_queue.put(LogMsg("Stale mounts cleaned up.", "success"))
            except Exception:
                pass  # Non-critical

        threading.Thread(target=_run, daemon=True).start()
