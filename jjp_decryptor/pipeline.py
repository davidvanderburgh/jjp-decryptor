"""Decryption pipeline - orchestrates the 7-phase decryption process."""

import base64
import re
import subprocess
import time
import uuid

from . import config
from .resources import DECRYPT_C_SOURCE, STUB_C_SOURCE
from .wsl import WslError, WslExecutor, find_usbipd, win_to_wsl


class PipelineError(Exception):
    """User-friendly pipeline error with phase context."""
    def __init__(self, phase, message):
        self.phase = phase
        super().__init__(f"[{phase}] {message}")


class DecryptionPipeline:
    """Runs the full decryption workflow across 7 phases.

    Callbacks:
        log_cb(text, level)       - emit a log line ("info", "error", "success")
        phase_cb(phase_index)     - current phase changed (0-6)
        progress_cb(current, total, desc) - progress update
        done_cb(success, summary) - pipeline finished
    """

    def __init__(self, image_path, output_path, log_cb, phase_cb, progress_cb, done_cb):
        self.image_path = image_path
        self.output_path = output_path
        self.log = log_cb
        self.on_phase = phase_cb
        self.on_progress = progress_cb
        self.on_done = done_cb

        self.wsl = WslExecutor()
        self.mount_point = None
        self.game_name = None
        self.cancelled = False
        self._succeeded = False
        self._bind_mounted = []
        self._iso_mount = None      # temp mount for ISO
        self._raw_img_path = None   # extracted raw ext4 (cached between runs)

    def cancel(self):
        """Request cancellation. Safe to call from any thread."""
        self.cancelled = True
        self.wsl.kill()

    def _check_cancel(self):
        if self.cancelled:
            raise PipelineError("Cancelled", "Operation cancelled by user.")

    def _is_iso(self):
        """Check if the input file is an ISO image."""
        return self.image_path.lower().endswith(".iso")

    def run(self):
        """Execute the full pipeline. Call from a background thread."""
        cleanup_phase = len(config.PHASES) - 1  # last phase is always Cleanup
        try:
            self.on_phase(0)  # Extract
            self._phase_extract()
            self._check_cancel()

            self.on_phase(1)  # Mount
            self._phase_mount()
            self._check_cancel()

            self.on_phase(2)  # Chroot
            self._phase_chroot()
            self._check_cancel()

            self.on_phase(3)  # Dongle
            self._phase_dongle()
            self._check_cancel()

            self.on_phase(4)  # Compile
            self._phase_compile()
            self._check_cancel()

            self.on_phase(5)  # Decrypt
            self._phase_decrypt()
            self._check_cancel()

            self.on_phase(6)  # Copy
            self._phase_copy()

            self._succeeded = True
            self.on_phase(cleanup_phase)  # Cleanup
            self._phase_cleanup()
            self.on_done(True, f"Decryption complete! Files saved to:\n{self.output_path}")

        except PipelineError as e:
            self.log(str(e), "error")
            self.on_phase(cleanup_phase)
            self._phase_cleanup()
            self.on_done(False, str(e))
        except Exception as e:
            self.log(f"Unexpected error: {e}", "error")
            self.on_phase(cleanup_phase)
            self._phase_cleanup()
            self.on_done(False, f"Unexpected error: {e}")

    # --- Phase 0: Extract (ISO → raw ext4) ---

    def _raw_img_cache_path(self):
        """Deterministic cache path for the extracted raw image, based on ISO filename."""
        import os
        basename = os.path.splitext(os.path.basename(self.image_path))[0]
        # Sanitize for use as a Linux filename
        safe = re.sub(r'[^a-zA-Z0-9._-]', '_', basename)
        return f"/tmp/jjp_raw_{safe}.img"

    def _phase_extract(self):
        if not self._is_iso():
            self.log("Input is a raw image, skipping extraction.", "info")
            return

        # Use a deterministic cache path so we can reuse previous extractions
        self._raw_img_path = self._raw_img_cache_path()

        # Check if a cached extraction already exists and is valid ext4
        try:
            sz = self.wsl.run(f"stat -c%s '{self._raw_img_path}'", timeout=5).strip()
            size_gb = int(sz) / (1024**3)
            if int(sz) > 0:
                # Validate it's actually a valid ext4 filesystem
                try:
                    fstype = self.wsl.run(
                        f"blkid -o value -s TYPE '{self._raw_img_path}'",
                        timeout=10,
                    ).strip()
                except WslError:
                    fstype = ""
                if "ext" in fstype:
                    self.log(
                        f"Found cached extraction: {self._raw_img_path} "
                        f"({size_gb:.1f} GB, {fstype}). Skipping extract phase.",
                        "success",
                    )
                    self.on_progress(100, 100, "Cached")
                    return
                else:
                    self.log(
                        f"Cached image exists but is not valid ext4 "
                        f"(detected: {fstype or 'unknown'}). Re-extracting...",
                        "info",
                    )
                    self.wsl.run(
                        f"rm -f '{self._raw_img_path}' 2>/dev/null; true",
                        timeout=10,
                    )
        except (WslError, ValueError):
            pass  # No cache, proceed with extraction

        self.log("Extracting ext4 filesystem from ISO...", "info")
        wsl_iso = win_to_wsl(self.image_path)
        tag = uuid.uuid4().hex[:8]
        self._iso_mount = f"/tmp/jjp_iso_{tag}"

        # Mount the ISO
        try:
            self.wsl.run(f"mkdir -p {self._iso_mount}", timeout=10)
            self.wsl.run(
                f"mount -o loop,ro '{wsl_iso}' {self._iso_mount}",
                timeout=config.MOUNT_TIMEOUT,
            )
        except WslError as e:
            raise PipelineError("Extract",
                f"Failed to mount ISO: {e.output}") from e

        self.log("ISO mounted. Looking for game partition image...", "info")

        # Find the sda3 partclone parts
        partimag = f"{self._iso_mount}{config.PARTIMAG_PATH}"
        part_prefix = f"{partimag}/{config.GAME_PARTITION}.ext4-ptcl-img.gz"
        try:
            parts_out = self.wsl.run(
                f"ls -1 {part_prefix}.* 2>/dev/null | sort",
                timeout=10,
            )
        except WslError:
            parts_out = ""

        parts = [p.strip() for p in parts_out.strip().split("\n") if p.strip()]
        if not parts:
            raise PipelineError("Extract",
                f"No partclone image found for {config.GAME_PARTITION} in ISO.\n"
                f"Expected files like {config.GAME_PARTITION}.ext4-ptcl-img.gz.aa")

        total_size = 0
        for p in parts:
            try:
                sz = self.wsl.run(f"stat -c%s '{p}'", timeout=5).strip()
                total_size += int(sz)
            except (WslError, ValueError):
                pass

        self.log(
            f"Found {len(parts)} part(s), {total_size / (1024**3):.1f} GB compressed. "
            "Converting to raw ext4...",
            "info",
        )

        # Use the proven Python converter (partclone_to_raw.py) as primary method.
        # It correctly reconstructs the full raw image including empty blocks.
        # Fall back to native partclone.restore only if Python script is unavailable.
        self._check_cancel()

        import os
        script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script_path = os.path.join(script_dir, "partclone_to_raw.py")

        if os.path.isfile(script_path):
            self._extract_with_python(parts, script_path)
        else:
            self.log("Python converter not found, trying partclone.restore...", "info")
            has_partclone = False
            try:
                self.wsl.run("which partclone.restore", timeout=5)
                has_partclone = True
            except WslError:
                pass

            if not has_partclone:
                self.log("Installing partclone (one-time setup)...", "info")
                try:
                    self.wsl.run(
                        "DEBIAN_FRONTEND=noninteractive apt-get install -y partclone 2>&1",
                        timeout=120,
                    )
                    has_partclone = True
                    self.log("partclone installed.", "success")
                except WslError:
                    pass

            if has_partclone:
                self._extract_with_partclone(parts)
            else:
                raise PipelineError("Extract",
                    "No extraction method available.\n"
                    "Ensure partclone_to_raw.py is in the project directory, or\n"
                    "install partclone: wsl -u root -- apt install partclone")

        # Verify the output
        try:
            sz = self.wsl.run(f"stat -c%s '{self._raw_img_path}'", timeout=5).strip()
            size_gb = int(sz) / (1024**3)
            self.log(f"Extraction complete: {size_gb:.1f} GB raw image.", "success")
        except WslError as e:
            raise PipelineError("Extract",
                f"Raw image was not created: {e.output}") from e

    def _extract_with_partclone(self, parts):
        """Use native partclone.restore to convert compressed image to raw."""
        self.log("Using partclone.restore (native, fast)...", "info")
        # Concatenate split files and pipe through partclone
        # -C = disable size checking (needed for file output)
        # -O = overwrite output file
        cat_parts = " ".join(f"'{p}'" for p in parts)
        cmd = (
            f"cat {cat_parts} | gunzip -c | "
            f"partclone.restore -C -s - -O '{self._raw_img_path}' 2>&1"
        )

        last_pct = -1
        try:
            for line in self.wsl.stream(cmd, timeout=config.EXTRACT_TIMEOUT):
                if self.cancelled:
                    self.wsl.kill()
                    raise PipelineError("Extract", "Cancelled by user.")
                # partclone outputs progress with ANSI escapes like:
                # "Elapsed: 00:00:08, Remaining: 00:01:17, Completed:   9.33%,   3.71GB/min,"
                # Strip ANSI escape codes
                clean = re.sub(r'\x1b\[[^m]*m|\[A', '', line).strip()
                if not clean:
                    continue
                m = re.search(r'Completed:\s*(\d+\.?\d*)%', clean)
                if m:
                    pct = float(m.group(1))
                    ipct = int(pct)
                    if ipct > last_pct:
                        last_pct = ipct
                        remaining = ""
                        rm = re.search(r'Remaining:\s*([\d:]+)', clean)
                        if rm:
                            remaining = f"ETA {rm.group(1)}"
                        self.on_progress(ipct, 100, remaining)
                        # Log every 10%
                        if ipct % 10 == 0:
                            self.log(f"  Extraction: {ipct}% {remaining}", "info")
                elif any(kw in clean for kw in [
                    "File system", "Device size", "Space in use",
                    "Block size", "error", "Error", "done", "Starting"
                ]):
                    self.log(f"  {clean}", "info")
        except WslError as e:
            # partclone may exit non-zero but still produce valid output
            try:
                self.wsl.run(f"test -s '{self._raw_img_path}'", timeout=5)
            except WslError:
                raise PipelineError("Extract",
                    f"partclone.restore failed: {e.output}") from e

    def _extract_with_python(self, parts, script_path=None):
        """Use the proven Python partclone converter."""
        self.log("Using Python partclone converter...", "info")
        if script_path is None:
            import os
            script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            script_path = os.path.join(script_dir, "partclone_to_raw.py")

        wsl_script = win_to_wsl(script_path)
        parts_str = " ".join(f"'{p}'" for p in parts)
        cmd = f"PYTHONUNBUFFERED=1 python3 '{wsl_script}' '{self._raw_img_path}' {parts_str} 2>&1"

        try:
            for line in self.wsl.stream(cmd, timeout=config.EXTRACT_TIMEOUT):
                if self.cancelled:
                    self.wsl.kill()
                    raise PipelineError("Extract", "Cancelled by user.")
                self.log(f"  {line.strip()}", "info")
                if "Progress:" in line:
                    m = re.search(r'(\d+\.?\d*)%', line)
                    if m:
                        pct = float(m.group(1))
                        self.on_progress(int(pct), 100, "Extracting filesystem...")
        except WslError as e:
            raise PipelineError("Extract",
                f"Python extraction failed: {e.output}") from e

    # --- Phase 1: Mount ---

    def _phase_mount(self):
        self.log("Mounting ext4 image...", "info")
        # Use the extracted raw image if we came from an ISO, otherwise use the input directly
        if self._raw_img_path:
            wsl_img = self._raw_img_path
        else:
            wsl_img = win_to_wsl(self.image_path)

        # Clean up stale mounts and loop devices from previous runs
        self._cleanup_stale_mounts(wsl_img)

        tag = uuid.uuid4().hex[:8]
        self.mount_point = f"{config.MOUNT_PREFIX}{tag}"

        try:
            self.wsl.run(f"mkdir -p {self.mount_point}", timeout=10)
            self.wsl.run(
                f"mount -o loop '{wsl_img}' {self.mount_point}",
                timeout=config.MOUNT_TIMEOUT,
            )
            self.log(f"Mounted at {self.mount_point}", "success")
        except WslError as e:
            # If this was a cached image, it may be corrupt — delete and re-extract
            if self._raw_img_path and self._is_iso():
                self.log(
                    "Mount failed with cached image. Deleting cache and re-extracting...",
                    "info",
                )
                try:
                    self.wsl.run(f"rmdir '{self.mount_point}' 2>/dev/null; true", timeout=5)
                except WslError:
                    pass
                try:
                    self.wsl.run(f"rm -f '{self._raw_img_path}'", timeout=10)
                except WslError:
                    pass

                # Re-run extraction from scratch
                self.on_phase(0)
                self._raw_img_path = self._raw_img_cache_path()
                self._phase_extract()
                self._check_cancel()

                # Retry mount with fresh image
                self.on_phase(1)
                wsl_img = self._raw_img_path
                self._cleanup_stale_mounts(wsl_img)
                tag = uuid.uuid4().hex[:8]
                self.mount_point = f"{config.MOUNT_PREFIX}{tag}"
                try:
                    self.wsl.run(f"mkdir -p {self.mount_point}", timeout=10)
                    self.wsl.run(
                        f"mount -o loop '{wsl_img}' {self.mount_point}",
                        timeout=config.MOUNT_TIMEOUT,
                    )
                    self.log(f"Mounted at {self.mount_point}", "success")
                except WslError as e2:
                    raise PipelineError("Mount",
                        f"Failed to mount freshly extracted image: {e2.output}") from e2
            else:
                raise PipelineError("Mount",
                    f"Failed to mount image: {e.output}") from e

    def _cleanup_stale_mounts(self, wsl_img):
        """Clean up stale mount points and loop devices from previous runs."""
        # Find and unmount any existing jjp mount points
        try:
            result = self.wsl.run(
                f"mount | grep '{config.MOUNT_PREFIX}' | awk '{{print $3}}'",
                timeout=10,
            )
            for mp in result.strip().split("\n"):
                mp = mp.strip()
                if not mp:
                    continue
                self.log(f"Cleaning up stale mount: {mp}", "info")
                # Unmount any bind mounts inside it first
                try:
                    self.wsl.run(
                        f"mount | grep '{mp}/' | awk '{{print $3}}' | sort -r | "
                        f"xargs -r -I{{}} umount '{{}}' 2>/dev/null; true",
                        timeout=15,
                    )
                except WslError:
                    pass
                try:
                    self.wsl.run(f"umount '{mp}' 2>/dev/null; true", timeout=10)
                    self.wsl.run(f"rmdir '{mp}' 2>/dev/null; true", timeout=5)
                except WslError:
                    pass
        except WslError:
            pass

        # Detach any stale loop devices for this image
        try:
            loops = self.wsl.run(
                f"losetup -j '{wsl_img}' 2>/dev/null",
                timeout=10,
            ).strip()
            for line in loops.split("\n"):
                line = line.strip()
                if not line:
                    continue
                # Format: "/dev/loop3: [64769]:1234 (/tmp/jjp_raw_foo.img)"
                loop_dev = line.split(":")[0]
                self.log(f"Detaching stale loop device: {loop_dev}", "info")
                try:
                    self.wsl.run(f"losetup -d '{loop_dev}' 2>/dev/null; true", timeout=5)
                except WslError:
                    pass
        except WslError:
            pass

    # --- Phase 2: Detect game + chroot ---

    def _phase_chroot(self):
        self.log("Scanning for game...", "info")

        try:
            result = self.wsl.run(
                f"ls -1 {self.mount_point}{config.GAME_BASE_PATH}/",
                timeout=15,
            )
        except WslError as e:
            raise PipelineError("Chroot",
                f"No JJP game found at {config.GAME_BASE_PATH}/. "
                "Is this a valid JJP filesystem image?") from e

        # Find game directories (filter out plain files)
        candidates = []
        for name in result.strip().split("\n"):
            name = name.strip()
            if not name:
                continue
            game_path = f"{self.mount_point}{config.GAME_BASE_PATH}/{name}/game"
            try:
                self.wsl.run(f"test -f '{game_path}'", timeout=5)
                candidates.append(name)
            except WslError:
                pass

        if not candidates:
            raise PipelineError("Chroot",
                "No game binary found. Expected <game>/game in "
                f"{config.GAME_BASE_PATH}/")

        self.game_name = candidates[0]
        display = config.KNOWN_GAMES.get(self.game_name, self.game_name)
        self.log(f"Detected game: {display} ({self.game_name})", "success")

        # Set up bind mounts for chroot
        self.log("Setting up chroot environment...", "info")
        for target in config.BIND_MOUNTS:
            chroot_target = f"{self.mount_point}{target}"
            try:
                self.wsl.run(f"mkdir -p '{chroot_target}'", timeout=5)
                self.wsl.run(
                    f"mountpoint -q '{chroot_target}' 2>/dev/null || "
                    f"mount --bind {target} '{chroot_target}'",
                    timeout=10,
                )
                self._bind_mounted.append(target)
            except WslError as e:
                self.log(f"Warning: bind mount {target} failed: {e.output}", "error")

        # Mount /dev/bus/usb for dongle access
        usb_target = f"{self.mount_point}/dev/bus/usb"
        try:
            self.wsl.run(f"mkdir -p '{usb_target}'", timeout=5)
            self.wsl.run(
                f"mountpoint -q '{usb_target}' 2>/dev/null || "
                f"mount --bind /dev/bus/usb '{usb_target}'",
                timeout=10,
            )
            self._bind_mounted.append("/dev/bus/usb")
        except WslError as e:
            self.log(f"Warning: USB bus mount failed: {e.output}", "error")

        # Ensure /tmp exists and is writable
        self.wsl.run(f"mkdir -p {self.mount_point}/tmp && "
                     f"chmod 1777 {self.mount_point}/tmp", timeout=5)

        self.log("Chroot environment ready.", "success")

    # --- Phase 3: Dongle ---

    def _phase_dongle(self):
        self.log("Checking for HASP dongle...", "info")
        usbipd = find_usbipd()

        # Check dongle on Windows side via usbipd
        rc, stdout, stderr = self.wsl.run_win(
            [usbipd, "list"], timeout=15
        )
        if rc != 0:
            raise PipelineError("Dongle",
                "usbipd-win not found or failed. "
                "Install from: https://github.com/dorssel/usbipd-win")

        if config.HASP_VID_PID not in stdout:
            raise PipelineError("Dongle",
                f"Sentinel HASP dongle ({config.HASP_VID_PID}) not detected.\n"
                "Please plug in the correct dongle and try again.")

        self.log("Dongle detected on Windows. Attaching to WSL...", "info")

        # Detach first to ensure clean state (previous run may have left it attached)
        self.wsl.run_win(
            [usbipd, "detach", "--hardware-id", config.HASP_VID_PID],
            timeout=10,
        )
        time.sleep(1)

        # Attach to WSL
        rc, stdout, stderr = self.wsl.run_win(
            [usbipd, "attach", "--wsl", "--hardware-id", config.HASP_VID_PID],
            timeout=30,
        )
        if rc != 0:
            # May need admin elevation
            if "access" in stderr.lower() or "administrator" in stderr.lower():
                self.log("Requesting admin elevation for USB passthrough...", "info")
                rc2, _, stderr2 = self.wsl.run_win(
                    ["powershell", "-Command",
                     f"Start-Process '{usbipd}' -ArgumentList "
                     f"'attach --wsl --hardware-id {config.HASP_VID_PID}' "
                     f"-Verb RunAs -Wait"],
                    timeout=30,
                )
                if rc2 != 0:
                    raise PipelineError("Dongle",
                        f"Failed to attach dongle to WSL (admin): {stderr2}")
            elif "already" in stderr.lower():
                self.log("Dongle already attached to WSL.", "info")
            else:
                raise PipelineError("Dongle",
                    f"Failed to attach dongle to WSL: {stderr}")

        # Wait for USB device to appear in WSL (usbipd attach is async)
        self.log("Waiting for dongle to appear in WSL...", "info")
        dongle_visible = False
        for i in range(config.USB_SETTLE_TIMEOUT):
            time.sleep(1)
            try:
                self.wsl.run(
                    f"lsusb 2>/dev/null | grep -q '{config.HASP_VID_PID}'",
                    timeout=5,
                )
                dongle_visible = True
                self.log(f"Dongle visible in WSL (after {i + 1}s).", "success")
                break
            except WslError:
                if i < config.USB_SETTLE_TIMEOUT - 1:
                    self.log(f"  Not visible yet ({i + 1}s)...", "info")

        if not dongle_visible:
            self.log("Warning: Dongle not visible in lsusb after waiting. "
                     "Will try starting daemon anyway...", "error")

        # Extra wait for HASP USB interface to fully initialize beyond basic
        # device enumeration. Without this, the daemon may start before the
        # HASP-specific USB endpoints are ready and fail to discover the key.
        self.log("Letting USB interface settle...", "info")
        time.sleep(3)

        # Now start the HASP daemon (after USB device is confirmed visible)
        self._start_hasp_daemon()

    def _start_hasp_daemon(self):
        """Kill any existing HASP daemon and start a fresh one.

        Runs the daemon from the WSL host (not inside the chroot) so it has
        direct access to USB devices and udev. The game in the chroot
        connects to the daemon via localhost:1947 (shared network namespace).
        """
        self.log("Starting HASP daemon...", "info")
        mp = self.mount_point

        # Kill any existing daemon first (both host and chroot)
        try:
            self.wsl.run("killall hasplmd_x86_64 2>/dev/null; true", timeout=10)
            time.sleep(1)
        except WslError:
            pass

        # Run daemon from WSL host with LD_LIBRARY_PATH pointing into the
        # mounted image's libraries so dynamic dependencies resolve.
        daemon_bin = f"{mp}{config.HASP_DAEMON_PATH}"
        lib_paths = f"{mp}/usr/lib/x86_64-linux-gnu:{mp}/usr/lib:{mp}/lib/x86_64-linux-gnu:{mp}/lib"
        try:
            self.wsl.run(
                f"LD_LIBRARY_PATH={lib_paths} {daemon_bin} -s 2>&1",
                timeout=15,
            )
        except WslError:
            # Fallback: try inside chroot (may work if host approach fails
            # due to glibc version mismatch)
            self.log("Host daemon start failed, trying inside chroot...", "info")
            try:
                self.wsl.run(
                    f"chroot {mp} {config.HASP_DAEMON_PATH} -s 2>&1",
                    timeout=15,
                )
            except WslError as e:
                raise PipelineError("Dongle",
                    f"Failed to start HASP daemon: {e.output}") from e

        # Wait for daemon to initialize and start listening on port 1947
        self.log("Waiting for HASP daemon to initialize...", "info")
        daemon_ready = False
        for attempt in range(config.DAEMON_READY_TIMEOUT):
            time.sleep(1)
            # Check daemon is still running
            try:
                self.wsl.run("pgrep -f hasplmd", timeout=5)
            except WslError:
                raise PipelineError("Dongle",
                    "HASP daemon died unexpectedly. "
                    "Check that the dongle is properly connected.")
            # Check if daemon is listening on port 1947
            try:
                self.wsl.run(
                    "bash -c 'echo > /dev/tcp/127.0.0.1/1947' 2>/dev/null",
                    timeout=3,
                )
                daemon_ready = True
                break
            except WslError:
                if attempt < config.DAEMON_READY_TIMEOUT - 1:
                    self.log(f"  Daemon not ready yet ({attempt + 1}s)...", "info")

        if daemon_ready:
            self.log("HASP daemon running and accepting connections.", "success")
        else:
            self.log("HASP daemon running but port 1947 not detected. "
                     "Continuing anyway...", "info")

    # --- Phase 4: Compile ---

    def _phase_compile(self):
        self.log("Compiling decryptor...", "info")
        mp = self.mount_point

        # Write C source via base64 to avoid shell escaping issues
        # Compile in WSL host (gcc is NOT in the chroot), output to chroot's /tmp
        b64 = base64.b64encode(DECRYPT_C_SOURCE.encode()).decode()
        try:
            self.wsl.run(
                f"echo '{b64}' | base64 -d > {mp}/tmp/jjp_decrypt.c",
                timeout=15,
            )
        except WslError as e:
            raise PipelineError("Compile",
                f"Failed to write C source: {e.output}") from e

        # Compile using WSL host gcc, output directly into chroot's /tmp
        try:
            self.wsl.run(
                f"gcc -shared -fPIC -o {mp}/tmp/jjp_decrypt.so "
                f"{mp}/tmp/jjp_decrypt.c -ldl -nostartfiles 2>&1",
                timeout=config.COMPILE_TIMEOUT,
            )
        except WslError as e:
            raise PipelineError("Compile",
                f"gcc compilation failed: {e.output}\n"
                "Ensure gcc is installed in WSL: wsl -u root -- apt install gcc") from e

        self.log("Decryptor compiled.", "success")

        # Compile stub libraries using WSL host gcc
        self.log("Building stub libraries...", "info")
        stubs_dir = f"{mp}/tmp/stubs"
        # Clean stubs directory first to remove stale stubs from previous runs
        self.wsl.run(f"rm -rf {stubs_dir}", timeout=5)
        self.wsl.run(f"mkdir -p {stubs_dir}", timeout=5)

        # Write stub.c
        stub_b64 = base64.b64encode(STUB_C_SOURCE.encode()).decode()
        self.wsl.run(
            f"echo '{stub_b64}' | base64 -d > {stubs_dir}/stub.c",
            timeout=10,
        )

        # Only stub libraries that are MISSING from the chroot.
        # Real libraries (e.g. Allegro) must not be replaced by empty stubs.
        built = 0
        skipped = 0
        for soname in config.STUB_SONAMES:
            # Check if this library already exists in the chroot
            try:
                self.wsl.run(
                    f"chroot {mp} /bin/sh -c 'ldconfig -p 2>/dev/null | grep -q {soname} || "
                    f"test -f /usr/lib/{soname} || "
                    f"test -f /usr/lib/x86_64-linux-gnu/{soname} || "
                    f"find /usr/lib -name {soname} -quit 2>/dev/null | grep -q .'",
                    timeout=10,
                )
                skipped += 1
                continue  # Library exists in chroot, don't stub it
            except WslError:
                pass  # Library not found, create a stub

            try:
                self.wsl.run(
                    f"gcc -shared -o {stubs_dir}/{soname} "
                    f"{stubs_dir}/stub.c -Wl,-soname,{soname} -nostdlib -nodefaultlibs "
                    f"2>/dev/null || "
                    f"gcc -shared -o {stubs_dir}/{soname} "
                    f"{stubs_dir}/stub.c -Wl,-soname,{soname}",
                    timeout=15,
                )
                built += 1
            except WslError:
                pass  # Non-critical

        self._stubs_built = built
        self.log(
            f"Built {built} stub libraries ({skipped} already in chroot, skipped).",
            "success",
        )

        # Discover dongle/hasp/init symbols for debugging and init sequence
        game_path = f"{mp}{config.GAME_BASE_PATH}/{self.game_name}/game"
        try:
            result = self.wsl.run(
                f"nm -D {game_path} 2>/dev/null | grep -iE 'dongle|hasp|crypt|init' "
                f"| head -30",
                timeout=15,
            )
            if result.strip():
                self.log(f"Game symbols (dongle/hasp/crypt/init):", "info")
                for line in result.strip().split('\n'):
                    self.log(f"  {line.strip()}", "info")
        except WslError:
            pass

    # --- Phase 5: Decrypt ---

    def _phase_decrypt(self):
        self.log("Starting decryption...", "info")
        mp = self.mount_point
        game_bin = f"{config.GAME_BASE_PATH}/{self.game_name}/game"
        decrypt_dir = "/tmp/jjp_decrypted"

        # Only set LD_LIBRARY_PATH if we actually built stub libraries;
        # otherwise the stubs dir is empty and we don't want it on the path.
        ld_lib_path = f"LD_LIBRARY_PATH=/tmp/stubs " if getattr(self, '_stubs_built', 0) > 0 else ""
        cmd = (
            f"chroot {mp} /bin/bash -c '"
            f"export JJP_OUTPUT_DIR={decrypt_dir}; "
            f"unset DISPLAY; "
            f"LD_PRELOAD=/tmp/jjp_decrypt.so "
            f"{ld_lib_path}"
            f"{game_bin}"
            f"' 2>&1"
        )

        # Retry logic: the HASP daemon may need extra time to fully discover
        # the USB key, especially through usbipd. If the game exits with
        # "key not found", wait and retry.
        max_retries = 3
        retry_wait = 5  # seconds between retries

        for attempt in range(max_retries):
            total_files = 0
            final_ok = 0
            final_fail = 0
            final_total = 0
            sentinel_error = False
            output_lines = []

            total_re = re.compile(r'\[decrypt\] TOTAL_FILES=(\d+)')
            progress_re = re.compile(
                r'Progress:\s*(\d+)\s*\(ok=(\d+)\s+fail=(\d+)\s+skip=(\d+)\)')
            result_re = re.compile(
                r'Total:\s*(\d+)\s+OK:\s*(\d+)\s+Failed:\s*(\d+)\s+Skipped:\s*(\d+)')

            try:
                for line in self.wsl.stream(cmd, timeout=config.DECRYPT_TIMEOUT):
                    if self.cancelled:
                        self.wsl.kill()
                        raise PipelineError("Decrypt", "Cancelled by user.")

                    output_lines.append(line)

                    # Detect Sentinel errors (key not found, terminal services, etc.)
                    if ("key not found" in line.lower() or "H0007" in line
                            or "Terminal services" in line or "H0027" in line):
                        sentinel_error = True

                    # Log every line
                    level = "info"
                    if "[FAIL]" in line or "ERROR" in line or "FAILED" in line:
                        level = "error"
                    elif "[OK]" in line or "decrypted OK" in line:
                        level = "success"
                    self.log(line, level)

                    # Parse total files
                    m = total_re.search(line)
                    if m:
                        total_files = int(m.group(1))
                        self.on_progress(0, total_files, "Decrypting...")

                    # Parse progress
                    m = progress_re.search(line)
                    if m:
                        current = int(m.group(1))
                        ok = int(m.group(2))
                        fail = int(m.group(3))
                        skip = int(m.group(4))
                        desc = f"ok={ok} fail={fail} skip={skip}"
                        self.on_progress(current, total_files, desc)

                    # Parse final result
                    m = result_re.search(line)
                    if m:
                        final_total = int(m.group(1))
                        final_ok = int(m.group(2))
                        final_fail = int(m.group(3))

            except WslError:
                # Exit code from syscall(SYS_exit_group, 0) may show as non-zero
                # on some systems. Check if we got BATCH COMPLETE.
                if final_total > 0:
                    pass  # Completed successfully despite non-zero exit
                elif sentinel_error:
                    pass  # Handle below in retry logic
                else:
                    combined = "\n".join(output_lines[-5:]) if output_lines else ""
                    raise PipelineError("Decrypt",
                        f"Game process failed.\nLast output:\n{combined}")

            # If sentinel error and we have retries left, restart daemon and retry
            if sentinel_error and attempt < max_retries - 1:
                wait = retry_wait * (attempt + 1)
                self.log(
                    f"Sentinel key not found - restarting HASP daemon and retrying "
                    f"in {wait}s (attempt {attempt + 2}/{max_retries})...",
                    "info",
                )
                time.sleep(wait)
                self._start_hasp_daemon()
                continue

            if sentinel_error:
                raise PipelineError("Decrypt",
                    "Sentinel HASP key not found after multiple attempts.\n"
                    "Check that the correct dongle is plugged in for this game.")

            # Success path - break out of retry loop
            break

        if final_total == 0:
            raise PipelineError("Decrypt",
                "Decryption produced no output. "
                "Check that the correct dongle is connected for this game.")

        self.on_progress(final_total, final_total, "Complete")
        self.log(
            f"Decryption finished: {final_ok} OK, {final_fail} failed "
            f"out of {final_total} files.",
            "success" if final_fail == 0 else "info",
        )

    # --- Phase 6: Copy ---

    def _phase_copy(self):
        self.log("Copying decrypted files to output folder...", "info")
        mp = self.mount_point
        src = f"{mp}/tmp/jjp_decrypted"
        wsl_out = win_to_wsl(self.output_path)

        try:
            self.wsl.run(f"mkdir -p '{wsl_out}'", timeout=10)
            self.wsl.run(
                f"cp -r {src}/* '{wsl_out}/'",
                timeout=config.COPY_TIMEOUT,
            )
        except WslError as e:
            raise PipelineError("Copy",
                f"Failed to copy files: {e.output}") from e

        # Count files
        try:
            count = self.wsl.run(
                f"find '{wsl_out}' -type f | wc -l",
                timeout=30,
            ).strip()
        except WslError:
            count = "?"

        # Get total size
        try:
            size = self.wsl.run(
                f"du -sh '{wsl_out}' | cut -f1",
                timeout=30,
            ).strip()
        except WslError:
            size = "?"

        self.log(f"Copied {count} files ({size}) to output folder.", "success")

    # --- Phase 7: Cleanup ---

    def _phase_cleanup(self):
        self.log("Cleaning up...", "info")

        if self.mount_point:
            mp = self.mount_point

            # Kill HASP daemon (may be running on host or in chroot)
            try:
                self.wsl.run(
                    "killall hasplmd_x86_64 2>/dev/null; true",
                    timeout=10,
                )
            except WslError:
                pass

            # Detach USB from WSL (non-critical)
            usbipd = find_usbipd()
            self.wsl.run_win(
                [usbipd, "detach", "--hardware-id", config.HASP_VID_PID],
                timeout=10,
            )

            # Unmount bind mounts in reverse order
            for target in reversed(self._bind_mounted):
                try:
                    self.wsl.run(f"umount '{mp}{target}' 2>/dev/null; true", timeout=10)
                except WslError:
                    pass

            # Unmount the ext4 image
            try:
                self.wsl.run(f"umount '{mp}' 2>/dev/null; true", timeout=30)
            except WslError:
                pass

            # Remove mount point
            try:
                self.wsl.run(f"rmdir '{mp}' 2>/dev/null; true", timeout=5)
            except WslError:
                pass

        # Clean up ISO mount if we used one
        if self._iso_mount:
            try:
                self.wsl.run(f"umount '{self._iso_mount}' 2>/dev/null; true", timeout=15)
                self.wsl.run(f"rmdir '{self._iso_mount}' 2>/dev/null; true", timeout=5)
            except WslError:
                pass

        # Clean up extracted raw image only on success (preserve for resume on failure)
        if self._raw_img_path:
            if self._succeeded:
                self.log("Removing cached extraction (no longer needed).", "info")
                try:
                    self.wsl.run(f"rm -f '{self._raw_img_path}' 2>/dev/null; true", timeout=10)
                except WslError:
                    pass
            else:
                self.log(
                    f"Keeping cached extraction at {self._raw_img_path} for next run.",
                    "info",
                )

        self.log("Cleanup complete.", "success")


def check_prerequisites(wsl):
    """Check all prerequisites. Returns list of (name, passed, message) tuples."""
    results = []

    # WSL2
    try:
        wsl.run("echo ok", timeout=15)
        results.append(("WSL2", True, "Available"))
    except Exception:
        results.append(("WSL2", False, "WSL2 not available. Install from Microsoft Store."))

    # gcc
    try:
        out = wsl.run("gcc --version 2>&1 | head -1", timeout=15)
        results.append(("gcc", True, out.strip()))
    except Exception:
        results.append(("gcc", False,
            "gcc not found. Run: wsl -u root -- apt install gcc"))

    # usbipd-win
    usbipd = find_usbipd()
    rc, stdout, _ = wsl.run_win([usbipd, "--version"], timeout=10)
    if rc == 0:
        results.append(("usbipd-win", True, stdout.strip()))
    else:
        results.append(("usbipd-win", False,
            "usbipd-win not found. Install from:\n"
            "https://github.com/dorssel/usbipd-win"))

    # HASP dongle
    rc, stdout, _ = wsl.run_win([usbipd, "list"], timeout=10)
    if rc == 0 and config.HASP_VID_PID in stdout:
        results.append(("HASP Dongle", True, "Detected"))
    else:
        results.append(("HASP Dongle", False,
            "Sentinel HASP dongle not detected. Plug it in."))

    return results
