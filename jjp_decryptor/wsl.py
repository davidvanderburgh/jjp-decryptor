"""WSL command execution wrapper with blocking and streaming modes."""

import os
import subprocess
import sys
import threading

# Prevent console windows from flashing when launched via pythonw.exe
_CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Standard install location for usbipd-win
_USBIPD_PATHS = [
    r"C:\Program Files\usbipd-win\usbipd.exe",
    "usbipd",  # fallback to PATH
]


def find_usbipd():
    """Find the usbipd executable, checking standard install locations."""
    for path in _USBIPD_PATHS:
        if os.path.isfile(path):
            return path
    return "usbipd"  # let it fail at runtime with a clear error


class WslError(Exception):
    """Raised when a WSL command fails."""
    def __init__(self, cmd, returncode, output):
        self.cmd = cmd
        self.returncode = returncode
        self.output = output
        super().__init__(f"WSL command failed (exit {returncode}): {cmd}\n{output}")


class WslExecutor:
    """Execute commands in WSL2 via subprocess."""

    def __init__(self):
        self._current_proc = None
        self._lock = threading.Lock()

    def run(self, bash_cmd, timeout=120):
        """Run a command in WSL and return stdout. Raises WslError on failure."""
        full_cmd = ["wsl", "-u", "root", "--", "bash", "-c", bash_cmd]
        try:
            result = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=_CREATE_FLAGS,
            )
        except subprocess.TimeoutExpired as e:
            raise WslError(bash_cmd, -1, f"Command timed out after {timeout}s") from e

        if result.returncode != 0:
            output = (result.stderr or "") + (result.stdout or "")
            raise WslError(bash_cmd, result.returncode, output.strip())

        return result.stdout

    def run_win(self, args, timeout=60):
        """Run a Windows command directly. Returns (returncode, stdout, stderr)."""
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=True,
                creationflags=_CREATE_FLAGS,
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return -1, "", f"Command timed out after {timeout}s"
        except FileNotFoundError:
            return -1, "", f"Command not found: {args[0] if args else '?'}"

    def stream(self, bash_cmd, timeout=600):
        """Run a command in WSL and yield output lines as they arrive.

        Merges stdout and stderr into one stream.
        Raises WslError if the command exits non-zero (after yielding all output).
        """
        full_cmd = ["wsl", "-u", "root", "--", "bash", "-c", bash_cmd]
        proc = subprocess.Popen(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=_CREATE_FLAGS,
        )

        with self._lock:
            self._current_proc = proc

        try:
            for line in proc.stdout:
                yield line.rstrip("\n\r")
            proc.wait(timeout=timeout)
            if proc.returncode != 0:
                raise WslError(bash_cmd, proc.returncode, "")
        except subprocess.TimeoutExpired:
            proc.kill()
            raise WslError(bash_cmd, -1, f"Command timed out after {timeout}s")
        finally:
            with self._lock:
                self._current_proc = None

    def kill(self):
        """Kill the currently running streaming process (for cancellation)."""
        with self._lock:
            if self._current_proc:
                try:
                    self._current_proc.terminate()
                except OSError:
                    pass


def win_to_wsl(path):
    """Convert a Windows path to a WSL path.

    e.g. C:\\Users\\david\\file.img -> /mnt/c/Users/david/file.img
    """
    path = path.replace("\\", "/")
    if len(path) >= 2 and path[1] == ":":
        drive = path[0].lower()
        return f"/mnt/{drive}{path[2:]}"
    return path
