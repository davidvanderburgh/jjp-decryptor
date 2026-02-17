"""Main application class - wires GUI and pipeline together."""

import json
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox

from .gui import MainWindow
from .pipeline import DecryptionPipeline, check_prerequisites
from .wsl import WslExecutor

# Settings file location
_SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
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

class GameDetectedMsg:
    def __init__(self, name):
        self.name = name


class App:
    """Top-level application controller."""

    def __init__(self):
        self.root = tk.Tk()
        self.msg_queue = queue.Queue()
        self.pipeline = None
        self.wsl = WslExecutor()

        self.window = MainWindow(
            self.root,
            on_check_prereqs=self._check_prereqs,
            on_start=self._start,
            on_cancel=self._cancel,
        )

        # Detect game name when file is selected (register before loading settings
        # so that restoring a saved image path triggers game detection)
        self.window.image_var.trace_add("write", self._on_image_changed)

        # Load saved settings and pre-populate fields
        self._load_settings()

        # Start polling the message queue
        self._poll_queue()

        # Check for stale mounts on startup
        self.root.after(500, self._check_stale_mounts)

    def run(self):
        """Start the tkinter mainloop."""
        self.root.mainloop()

    def _poll_queue(self):
        """Process messages from background threads."""
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                if isinstance(msg, LogMsg):
                    self.window.append_log(msg.text, msg.level)
                elif isinstance(msg, PhaseMsg):
                    self.window.set_phase(msg.index)
                    from . import config
                    if msg.index < len(config.PHASES):
                        self.window.set_status(f"{config.PHASES[msg.index]}...")
                elif isinstance(msg, ProgressMsg):
                    self.window.set_progress(msg.current, msg.total, msg.desc)
                elif isinstance(msg, GameDetectedMsg):
                    self.window.set_game_name(msg.name)
                elif isinstance(msg, DoneMsg):
                    self._on_done(msg.success, msg.summary)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _on_image_changed(self, *_args):
        """Try to detect game name from the selected filename."""
        path = self.window.image_var.get().strip()
        if not path:
            self.window.game_label.configure(
                text="(select an image to detect)", foreground="gray")
            return

        # Extract just the filename
        import os
        filename = os.path.basename(path).lower()

        # Check if any known game key appears in the filename
        from . import config
        for key in config.KNOWN_GAMES:
            if key.lower() in filename:
                self.window.set_game_name(key)
                return

        # No match - show that we'll detect during pipeline
        self.window.game_label.configure(
            text="(will detect when decryption starts)", foreground="gray")

    def _check_prereqs(self):
        """Run prerequisite checks in a background thread."""
        self.window.append_log("Checking prerequisites...", "info")

        def _run():
            results = check_prerequisites(self.wsl)
            for name, passed, message in results:
                self.msg_queue.put(LogMsg(
                    f"  {name}: {'OK' if passed else 'MISSING'} - {message}",
                    "success" if passed else "error",
                ))
                # Schedule prereq UI update on main thread
                self.root.after(0, self.window.set_prereq, name, passed, message)

            all_ok = all(p for _, p, _ in results)
            if all_ok:
                self.msg_queue.put(LogMsg("All prerequisites met.", "success"))
            else:
                self.msg_queue.put(LogMsg(
                    "Some prerequisites are missing. Fix them before proceeding.",
                    "error"))

        threading.Thread(target=_run, daemon=True).start()

    def _start(self):
        """Start the decryption pipeline."""
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

        # Save paths for next time
        self._save_settings()

        self.window.set_running(True)
        self.window.reset_steps()

        # Create pipeline with queue-based callbacks
        def log_cb(text, level="info"):
            self.msg_queue.put(LogMsg(text, level))

        def phase_cb(index):
            self.msg_queue.put(PhaseMsg(index))

        def progress_cb(current, total, desc=""):
            self.msg_queue.put(ProgressMsg(current, total, desc))

        def done_cb(success, summary):
            self.msg_queue.put(DoneMsg(success, summary))

        self.pipeline = DecryptionPipeline(
            image_path, output_path,
            log_cb, phase_cb, progress_cb, done_cb,
        )

        # Intercept game detection for the GUI label
        orig_chroot = self.pipeline._phase_chroot
        def patched_chroot():
            orig_chroot()
            if self.pipeline.game_name:
                self.msg_queue.put(GameDetectedMsg(self.pipeline.game_name))
        self.pipeline._phase_chroot = patched_chroot

        threading.Thread(target=self.pipeline.run, daemon=True).start()

    def _cancel(self):
        """Cancel the running pipeline."""
        if self.pipeline:
            self.window.append_log("Cancelling...", "error")
            self.pipeline.cancel()

    def _on_done(self, success, summary):
        """Handle pipeline completion."""
        self.window.set_running(False)
        if success:
            self.window.set_status("Complete!")
            messagebox.showinfo("Decryption Complete", summary)
        else:
            self.window.set_status("Failed")
            messagebox.showerror("Decryption Failed", summary)

    def _load_settings(self):
        """Load saved settings and pre-populate GUI fields."""
        try:
            with open(_SETTINGS_FILE, "r") as f:
                settings = json.load(f)
            if settings.get("image_path"):
                self.window.image_var.set(settings["image_path"])
            if settings.get("output_path"):
                self.window.output_var.set(settings["output_path"])
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass  # No saved settings yet

    def _save_settings(self):
        """Save current field values to disk."""
        settings = {
            "image_path": self.window.image_var.get().strip(),
            "output_path": self.window.output_var.get().strip(),
        }
        try:
            os.makedirs(_SETTINGS_DIR, exist_ok=True)
            with open(_SETTINGS_FILE, "w") as f:
                json.dump(settings, f, indent=2)
        except OSError:
            pass  # Non-critical

    def _check_stale_mounts(self):
        """Check for leftover mounts from crashed runs."""
        def _run():
            try:
                from . import config
                result = self.wsl.run(
                    f"mount | grep '{config.MOUNT_PREFIX}' | awk '{{print $3}}'",
                    timeout=10,
                )
                mounts = [m.strip() for m in result.strip().split("\n") if m.strip()]
                if mounts:
                    self.msg_queue.put(LogMsg(
                        f"Found {len(mounts)} stale mount(s) from previous runs. "
                        "They will be cleaned up when you start a new decryption.",
                        "info",
                    ))
            except Exception:
                pass  # Non-critical

        threading.Thread(target=_run, daemon=True).start()
