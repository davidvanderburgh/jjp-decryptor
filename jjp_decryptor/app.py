"""Main application class - wires GUI and pipeline together."""

import json
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox

from . import __version__
from .gui import MainWindow
from .pipeline import DecryptionPipeline, ModPipeline, check_prerequisites
from .updater import check_for_update
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

class LinkMsg:
    def __init__(self, text, url):
        self.text = text
        self.url = url

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
            on_clear_cache=self._clear_cache,
            on_theme_change=self._on_theme_change,
            initial_theme=saved_theme,
        )

        # Detect game name when file is selected (register before loading settings
        # so that restoring a saved image path triggers game detection)
        self.window.image_var.trace_add("write", self._on_image_changed)

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
        """Handle window close — offer to free cached images in WSL /tmp."""
        try:
            result = self.wsl.run(
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
                        f"There are cached game images in WSL using "
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
                        self.wsl.run(
                            "find /tmp -maxdepth 1 -name 'jjp_raw_*' -type f "
                            "-delete 2>/dev/null; true",
                            timeout=30,
                        )
        except Exception:
            pass  # Don't block close if WSL is unavailable

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
                    phases = config.PHASES if self._active_mode == "decrypt" else config.MOD_PHASES
                    if msg.index < len(phases):
                        self.window.set_status(f"{phases[msg.index]}...")
                elif isinstance(msg, ProgressMsg):
                    self.window.set_progress(
                        msg.current, msg.total, msg.desc,
                        mode=self._active_mode)
                elif isinstance(msg, GameDetectedMsg):
                    self.window.set_game_name(msg.name)
                elif isinstance(msg, DoneMsg):
                    self._on_done(msg.success, msg.summary)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _on_image_changed(self, *_args):
        """Try to detect game name from the selected filename."""
        from .gui import _THEMES
        path = self.window.image_var.get().strip()
        gray = _THEMES[self.window._current_theme]["gray"]
        if not path:
            self.window.game_label.configure(
                text="(select an image to detect)", foreground=gray)
            return

        filename = os.path.basename(path).lower()

        from . import config
        for key in config.KNOWN_GAMES:
            if key.lower() in filename:
                self.window.set_game_name(key)
                return

        self.window.game_label.configure(
            text="(will detect when pipeline starts)", foreground=gray)

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
                self.root.after(0, self.window.set_prereq, name, passed, message)

            all_ok = all(p for _, p, _ in results)
            if all_ok:
                self.msg_queue.put(LogMsg("All prerequisites met.", "success"))
            else:
                self.msg_queue.put(LogMsg(
                    "Some prerequisites are missing. Fix them before proceeding.",
                    "error"))

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

    # --- Decrypt pipeline ---

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

        self._save_settings()
        self._active_mode = "decrypt"
        self.window.set_running(True, mode="decrypt")
        self.window.reset_steps(mode="decrypt")

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
        """Cancel the running decrypt pipeline."""
        if self.pipeline:
            self.window.append_log("Cancelling...", "error")
            self.pipeline.cancel()

    # --- Mod pipeline ---

    def _mod_start(self):
        """Start the asset modification pipeline."""
        image_path = self.window.image_var.get().strip()
        output_path = self.window.output_var.get().strip()

        if not image_path:
            messagebox.showwarning("Missing Input",
                "Please select a game image file.")
            return
        if not output_path:
            messagebox.showwarning("Missing Input",
                "Please select an output folder (containing your modified assets).")
            return
        if not os.path.isdir(output_path):
            messagebox.showerror("Invalid Folder",
                f"Output folder does not exist:\n{output_path}")
            return

        checksums_file = os.path.join(output_path, '.checksums.md5')
        if not os.path.isfile(checksums_file):
            messagebox.showerror("No Baseline Checksums",
                "No .checksums.md5 file found in the output folder.\n\n"
                "Run Decrypt first to generate baseline checksums, then "
                "modify files in the output folder and try again.")
            return

        if not image_path.lower().endswith(".iso"):
            proceed = messagebox.askyesno("Non-ISO Input",
                "The selected image is not an ISO file.\n\n"
                "Modify Assets can still encrypt your changes, but the output "
                "will be a raw .img file instead of a bootable Clonezilla ISO.\n\n"
                "For a Rufus-writable ISO, select the original Clonezilla ISO.\n\n"
                "Continue anyway?")
            if not proceed:
                return

        self._save_settings()
        self._active_mode = "modify"
        self.window.set_running(True, mode="modify")
        self.window.reset_steps(mode="modify")

        def log_cb(text, level="info"):
            self.msg_queue.put(LogMsg(text, level))

        def phase_cb(index):
            self.msg_queue.put(PhaseMsg(index))

        def progress_cb(current, total, desc=""):
            self.msg_queue.put(ProgressMsg(current, total, desc))

        def done_cb(success, summary):
            self.msg_queue.put(DoneMsg(success, summary))

        self.pipeline = ModPipeline(
            image_path, output_path,
            log_cb, phase_cb, progress_cb, done_cb,
        )
        self.pipeline.log_link = lambda text, url: self.msg_queue.put(LinkMsg(text, url))

        # Intercept game detection
        orig_chroot = self.pipeline._phase_chroot
        def patched_chroot():
            orig_chroot()
            if self.pipeline.game_name:
                self.msg_queue.put(GameDetectedMsg(self.pipeline.game_name))
        self.pipeline._phase_chroot = patched_chroot

        threading.Thread(target=self.pipeline.run, daemon=True).start()

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
        if success:
            self.window.set_status("Complete!")
            title = "Decryption Complete" if mode == "decrypt" else "Modification Complete"
            messagebox.showinfo(title, summary)
        else:
            self.window.set_status("Failed")
            title = "Decryption Failed" if mode == "decrypt" else "Modification Failed"
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
            "theme": self.window._current_theme,
        }
        try:
            os.makedirs(_SETTINGS_DIR, exist_ok=True)
            with open(_SETTINGS_FILE, "w") as f:
                json.dump(settings, f, indent=2)
        except OSError:
            pass  # Non-critical

    def _clear_cache(self):
        """Remove cached extracted images from WSL /tmp/ and output folder."""
        import glob as globmod

        def _run():
            files_to_remove = []  # list of (wsl_path, display_name)

            # Check WSL /tmp/ for leftover images
            try:
                result = self.wsl.run(
                    "find /tmp -maxdepth 1 -name 'jjp_raw_*' -type f 2>/dev/null",
                    timeout=10,
                )
                for f in result.strip().split("\n"):
                    f = f.strip()
                    if f:
                        files_to_remove.append((f, f.split("/")[-1] + " (WSL /tmp/)"))
            except Exception:
                pass

            # Check output folder for .img files
            output_path = self.window.output_var.get().strip()
            if output_path:
                for win_path in globmod.glob(os.path.join(output_path, "jjp_raw_*.img")):
                    from .wsl import win_to_wsl
                    wsl_path = win_to_wsl(win_path)
                    files_to_remove.append(
                        (wsl_path, os.path.basename(win_path) + " (output folder)"))

            if not files_to_remove:
                self.msg_queue.put(LogMsg("No cached images found.", "info"))
                return

            total_size = 0
            for wsl_path, _ in files_to_remove:
                try:
                    sz = self.wsl.run(f"stat -c%s '{wsl_path}'", timeout=5).strip()
                    total_size += int(sz)
                except Exception:
                    pass

            size_gb = total_size / (1024**3)
            self.msg_queue.put(LogMsg(
                f"Removing {len(files_to_remove)} image(s) ({size_gb:.1f} GB)...",
                "info",
            ))

            for wsl_path, display in files_to_remove:
                try:
                    self.wsl.run(f"rm -f '{wsl_path}'", timeout=30)
                    self.msg_queue.put(LogMsg(f"  Removed: {display}", "info"))
                except Exception:
                    self.msg_queue.put(LogMsg(f"  Failed to remove: {display}", "error"))

            self.msg_queue.put(LogMsg(
                f"Cache cleared ({size_gb:.1f} GB freed).", "success"))

        threading.Thread(target=_run, daemon=True).start()

    def _check_stale_mounts(self):
        """Clean up leftover mounts from crashed runs on startup."""
        def _run():
            try:
                from . import config
                result = self.wsl.run(
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
                self.wsl.run(
                    f"findmnt -rn -o TARGET | grep '{config.MOUNT_PREFIX}' | sort -r | "
                    f"xargs -r -I{{}} umount -lf '{{}}' 2>/dev/null; true",
                    timeout=30,
                )

                # Remove empty mount directories
                self.wsl.run(
                    f"find /mnt -maxdepth 1 -name 'jjp_*' -type d -empty -delete 2>/dev/null; true",
                    timeout=10,
                )

                # Detach any stale loop devices
                try:
                    loops = self.wsl.run(
                        "losetup -a 2>/dev/null | grep jjp_raw",
                        timeout=10,
                    ).strip()
                    for line in loops.split("\n"):
                        line = line.strip()
                        if line:
                            loop_dev = line.split(":")[0]
                            try:
                                self.wsl.run(f"losetup -d '{loop_dev}' 2>/dev/null; true", timeout=5)
                            except Exception:
                                pass
                except Exception:
                    pass

                self.msg_queue.put(LogMsg("Stale mounts cleaned up.", "success"))
            except Exception:
                pass  # Non-critical

        threading.Thread(target=_run, daemon=True).start()
