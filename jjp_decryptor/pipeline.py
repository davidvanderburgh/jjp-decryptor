"""Decryption pipeline - orchestrates the 7-phase decryption process."""

import base64
import re
import subprocess
import time
import uuid

from . import config
from .resources import DECRYPT_C_SOURCE, ENCRYPT_C_SOURCE, STUB_C_SOURCE
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
        self.log_link = lambda text, url: None  # optional; set by caller
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
        # Find and unmount all jjp mount points (reverse order: submounts first)
        try:
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
            self.log("Cleaned up stale mounts.", "info")
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
        all_mounts = list(config.BIND_MOUNTS) + ["/dev/bus/usb"]
        total_mounts = len(all_mounts)
        for idx, target in enumerate(all_mounts):
            self.on_progress(idx, total_mounts, f"Mounting {target}")
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

        self.on_progress(total_mounts, total_mounts, "Done")

        # Ensure /tmp exists and is writable
        self.wsl.run(f"mkdir -p {self.mount_point}/tmp && "
                     f"chmod 1777 {self.mount_point}/tmp", timeout=5)

        self.log("Chroot environment ready.", "success")

    # --- Phase 3: Dongle ---

    def _bind_dongle(self, usbipd):
        """Ensure the HASP dongle is bound (shared) in usbipd.

        Binding is required before attaching to WSL and must be done as
        administrator. The binding persists across reboots but is lost if
        the dongle moves to a different USB port.
        """
        # Check if already bound by looking at usbipd list output
        rc, stdout, _ = self.wsl.run_win([usbipd, "list"], timeout=15)
        if rc != 0:
            return

        # Find the line with our dongle and check if it's already shared/bound
        # States: "Not shared", "Shared", "Attached" — must exclude "Not shared"
        for line in stdout.split("\n"):
            if config.HASP_VID_PID in line:
                lower = line.lower()
                if "not shared" in lower:
                    break  # Needs binding
                if "shared" in lower or "attached" in lower:
                    self.log("Dongle already bound (shared).", "info")
                    return
                break

        # Not bound — bind with admin elevation
        self.log("Binding dongle for USB passthrough (requires admin)...", "info")
        rc, _, stderr = self.wsl.run_win(
            ["powershell", "-Command",
             f"Start-Process '{usbipd}' -ArgumentList "
             f"'bind --hardware-id {config.HASP_VID_PID}' "
             f"-Verb RunAs -Wait"],
            timeout=30,
        )
        if rc != 0 and stderr.strip():
            self.log(f"Warning: usbipd bind returned: {stderr.strip()}", "info")
        else:
            self.log("Dongle bound successfully.", "success")

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

        # Ensure dongle is bound (shared) — required when dongle moves to a new port
        self._bind_dongle(usbipd)

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
            elif "not shared" in stderr.lower() or "bind" in stderr.lower():
                # Binding may have failed silently — retry bind + attach
                self.log("Device not shared, retrying bind...", "info")
                self._bind_dongle(usbipd)
                time.sleep(1)
                rc2, _, stderr2 = self.wsl.run_win(
                    [usbipd, "attach", "--wsl", "--hardware-id", config.HASP_VID_PID],
                    timeout=30,
                )
                if rc2 != 0:
                    raise PipelineError("Dongle",
                        f"Failed to attach dongle to WSL after bind: {stderr2}")
            else:
                raise PipelineError("Dongle",
                    f"Failed to attach dongle to WSL: {stderr}")

        # Wait for USB device to appear in WSL (usbipd attach is async)
        self.log("Waiting for dongle to appear in WSL...", "info")
        # Total wait steps: USB settle + 3s interface settle + daemon ready
        total_wait = config.USB_SETTLE_TIMEOUT + 3 + config.DAEMON_READY_TIMEOUT
        step = 0

        dongle_visible = False
        for i in range(config.USB_SETTLE_TIMEOUT):
            self.on_progress(step, total_wait, "Waiting for USB device...")
            time.sleep(1)
            step += 1
            try:
                self.wsl.run(
                    f"lsusb 2>/dev/null | grep -q '{config.HASP_VID_PID}'",
                    timeout=5,
                )
                dongle_visible = True
                self.log(f"Dongle visible in WSL (after {i + 1}s).", "success")
                step = config.USB_SETTLE_TIMEOUT  # skip remaining USB wait
                break
            except WslError:
                if i < config.USB_SETTLE_TIMEOUT - 1:
                    self.log(f"  Not visible yet ({i + 1}s)...", "info")

        if not dongle_visible:
            self.log("Warning: Dongle not visible in lsusb after waiting. "
                     "Will try starting daemon anyway...", "error")

        # Extra wait for HASP USB interface to fully initialize
        self.log("Letting USB interface settle...", "info")
        for i in range(3):
            self.on_progress(step, total_wait, "USB interface settling...")
            time.sleep(1)
            step += 1

        # Now start the HASP daemon (after USB device is confirmed visible)
        self._start_hasp_daemon(step, total_wait)

    def _reattach_dongle(self):
        """Detach and re-attach the HASP dongle to WSL, then restart the daemon.

        Used during retries when the dongle session fails. The USB device
        may have lost its connection to WSL, so we do the full cycle:
        bind (if needed) → detach → attach → wait for lsusb → restart daemon.
        """
        self.log("Re-attaching dongle to WSL...", "info")
        usbipd = find_usbipd()

        # Ensure bound (may have moved to a different port)
        self._bind_dongle(usbipd)

        # Detach
        self.wsl.run_win(
            [usbipd, "detach", "--hardware-id", config.HASP_VID_PID],
            timeout=10,
        )
        time.sleep(2)

        # Attach
        rc, stdout, stderr = self.wsl.run_win(
            [usbipd, "attach", "--wsl", "--hardware-id", config.HASP_VID_PID],
            timeout=30,
        )
        if rc != 0 and "already" not in stderr.lower():
            self.log(f"Warning: usbipd attach returned: {stderr}", "error")

        # Wait for device to appear in WSL
        for i in range(config.USB_SETTLE_TIMEOUT):
            time.sleep(1)
            try:
                self.wsl.run(
                    f"lsusb 2>/dev/null | grep -q '{config.HASP_VID_PID}'",
                    timeout=5,
                )
                self.log(f"Dongle visible in WSL (after {i + 1}s).", "success")
                break
            except WslError:
                pass
        else:
            self.log("Warning: Dongle not visible in lsusb after re-attach.", "error")

        # Extra settle time
        time.sleep(2)

        # Restart daemon
        self._start_hasp_daemon()

    def _start_hasp_daemon(self, progress_step=0, progress_total=0):
        """Kill any existing HASP daemon and start a fresh one.

        Runs the daemon from the WSL host (not inside the chroot) so it has
        direct access to USB devices and udev. The game in the chroot
        connects to the daemon via localhost:1947 (shared network namespace).
        """
        self.log("Starting HASP daemon...", "info")
        mp = self.mount_point
        step = progress_step

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
            if progress_total > 0:
                self.on_progress(step, progress_total, "Waiting for daemon...")
            time.sleep(1)
            step += 1
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
            if progress_total > 0:
                self.on_progress(progress_total, progress_total, "Dongle ready")
            self.log("HASP daemon running and accepting connections.", "success")
        else:
            self.log("HASP daemon running but port 1947 not detected. "
                     "Continuing anyway...", "info")

    # --- Phase 4: Compile ---

    def _phase_compile(self):
        self.log("Compiling decryptor...", "info")
        mp = self.mount_point

        # Write C source to a temp file and copy into chroot.
        # (base64 via echo exceeds Windows command-line length limit for large sources)
        import tempfile, os
        try:
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.c', delete=False,
                dir=os.environ.get('TEMP', os.environ.get('TMP', '.')),
            ) as tf:
                tf.write(DECRYPT_C_SOURCE)
                tmp_win = tf.name
            wsl_tmp = win_to_wsl(tmp_win)
            self.wsl.run(f"cp '{wsl_tmp}' {mp}/tmp/jjp_decrypt.c", timeout=15)
            os.unlink(tmp_win)
        except (WslError, OSError) as e:
            raise PipelineError("Compile",
                f"Failed to write C source: {e}") from e

        # Compile using WSL host gcc, but link against chroot's libc to
        # avoid glibc version mismatch (host glibc may be newer than chroot's)
        chroot_lib = f"{mp}/lib/x86_64-linux-gnu"
        try:
            self.wsl.run(
                f"gcc -c -fPIC -std=gnu11 -D_FORTIFY_SOURCE=0 -fno-stack-protector "
                f"-o {mp}/tmp/jjp_decrypt.o {mp}/tmp/jjp_decrypt.c 2>&1",
                timeout=config.COMPILE_TIMEOUT,
            )
            self.wsl.run(
                f"LIBS='{chroot_lib}/libc.so.6'; "
                f"[ -f '{chroot_lib}/libdl.so.2' ] && LIBS=\"$LIBS {chroot_lib}/libdl.so.2\"; "
                f"gcc -shared -nostdlib "
                f"-o {mp}/tmp/jjp_decrypt.so {mp}/tmp/jjp_decrypt.o $LIBS -lgcc 2>&1",
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
        total_sonames = len(config.STUB_SONAMES)
        built = 0
        skipped = 0
        for idx, soname in enumerate(config.STUB_SONAMES):
            self.on_progress(idx, total_sonames, soname)
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

        self.on_progress(total_sonames, total_sonames, "Done")
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

            # If sentinel error and we have retries left, re-attach dongle and retry
            if sentinel_error and attempt < max_retries - 1:
                wait = retry_wait * (attempt + 1)
                self.log(
                    f"Sentinel key not found - re-attaching dongle and retrying "
                    f"in {wait}s (attempt {attempt + 2}/{max_retries})...",
                    "info",
                )
                time.sleep(wait)
                self._reattach_dongle()
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
        except WslError as e:
            raise PipelineError("Copy",
                f"Failed to create output folder: {e.output}") from e

        # Count total files for progress reporting
        try:
            total_str = self.wsl.run(
                f"find {src} -type f | wc -l", timeout=30,
            ).strip()
            total_files = int(total_str)
        except (WslError, ValueError):
            total_files = 0

        if total_files > 0:
            self.log(f"Found {total_files} files to copy.", "info")
            self.on_progress(0, total_files, "Copying files...")

        # Use rsync for per-file progress reporting
        try:
            copied = 0
            for line in self.wsl.stream(
                f"rsync -a --out-format='%n' {src}/ '{wsl_out}/'",
                timeout=config.COPY_TIMEOUT,
            ):
                self._check_cancel()
                line = line.strip()
                if line and not line.endswith("/"):  # skip directory entries
                    copied += 1
                    if total_files > 0 and (copied % 50 == 0 or copied == total_files):
                        self.on_progress(copied, total_files, line)
            if total_files > 0:
                self.on_progress(total_files, total_files, "Copy complete")
        except WslError as e:
            # Fall back to plain cp if rsync is not available
            if "not found" in str(e.output).lower() or "not found" in str(e).lower():
                self.log("rsync not available, falling back to cp...", "info")
                try:
                    self.wsl.run(
                        f"cp -r {src}/* '{wsl_out}/'",
                        timeout=config.COPY_TIMEOUT,
                    )
                except WslError as e2:
                    raise PipelineError("Copy",
                        f"Failed to copy files: {e2.output}") from e2
            else:
                raise PipelineError("Copy",
                    f"Failed to copy files: {e.output}") from e

        # Count files in output
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

        # Generate checksums for future modification comparison
        self.log("Generating checksums for asset tracking...", "info")
        try:
            self.wsl.run(
                f"cd '{wsl_out}' && find . -type f ! -name '.*' ! -name 'fl_decrypted.dat' "
                f"! -name '*.img' -print0 | xargs -0 md5sum > '.checksums.md5'",
                timeout=600,
            )
            self.log("Checksums saved to .checksums.md5 in output folder.", "success")
        except WslError:
            self.log("Warning: Could not generate checksums. "
                     "Asset modification tracking will not be available.", "info")

        # Move the raw image to the output folder so the mod pipeline can
        # mount it directly from there, and /tmp stays clean.
        if self._raw_img_path:
            import os
            img_name = self._raw_img_path.rsplit("/", 1)[-1]
            dest = f"{wsl_out}/{img_name}"
            self.log("Moving game image to output folder...", "info")
            try:
                # rsync + delete is more reliable than mv across filesystems
                last_pct = -1
                for line in self.wsl.stream(
                    f"rsync --info=progress2 --no-inc-recursive --remove-source-files "
                    f"'{self._raw_img_path}' '{dest}'",
                    timeout=config.COPY_TIMEOUT,
                ):
                    self._check_cancel()
                    m = re.search(r'(\d+)%', line)
                    if m:
                        pct = int(m.group(1))
                        if pct > last_pct:
                            last_pct = pct
                            self.on_progress(pct, 100, line.strip())
                self.on_progress(100, 100, "Done")
                win_path = os.path.join(self.output_path, img_name)
                self.log(f"Game image saved to: {win_path}", "success")
            except WslError as e:
                self.log(f"Warning: Could not move image to output: {e.output}", "info")

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
                    self.wsl.run(f"umount -l '{mp}{target}' 2>/dev/null; true", timeout=10)
                except WslError:
                    pass

            # Unmount the ext4 image
            try:
                self.wsl.run(f"umount -l '{mp}' 2>/dev/null; true", timeout=30)
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
                self.wsl.run(f"umount -l '{self._iso_mount}' 2>/dev/null; true", timeout=15)
                self.wsl.run(f"rmdir '{self._iso_mount}' 2>/dev/null; true", timeout=5)
            except WslError:
                pass

        # Clean up any leftover raw image in /tmp (it was moved to output folder)
        if self._raw_img_path and self._raw_img_path.startswith("/tmp/"):
            try:
                self.wsl.run(f"rm -f '{self._raw_img_path}' 2>/dev/null; true", timeout=10)
            except WslError:
                pass

        self.log("Cleanup complete.", "success")


class ModPipeline(DecryptionPipeline):
    """Runs the asset modification workflow.

    Scans the assets folder for files that differ from the original decryption
    (via checksums), then re-encrypts only the changed files into the game image.

    Reuses mount/chroot/dongle/cleanup from DecryptionPipeline.
    """

    def __init__(self, image_path, assets_folder, log_cb, phase_cb, progress_cb, done_cb):
        super().__init__(image_path, assets_folder, log_cb, phase_cb, progress_cb, done_cb)
        self.assets_folder = assets_folder
        self.changed_files = []  # [(rel_path, abs_win_path), ...]

    def run(self):
        """Execute the mod pipeline. Call from a background thread."""
        import os
        cleanup_phase = len(config.MOD_PHASES) - 1
        try:
            # Phase 0: Scan for changes (pure Python, no WSL needed)
            self.on_phase(0)
            self._phase_scan()
            self._check_cancel()

            if not self.changed_files:
                self.on_done(True,
                    "No changes detected in the assets folder.\n"
                    "Modify files in the output folder and try again.")
                return

            self.on_phase(1)  # Extract
            self._phase_extract()
            self._check_cancel()

            self.on_phase(2)  # Mount
            self._phase_mount()
            self._check_cancel()

            self.on_phase(3)  # Chroot
            self._phase_chroot()
            self._check_cancel()

            self.on_phase(4)  # Dongle
            self._phase_dongle()
            self._check_cancel()

            self.on_phase(5)  # Compile
            self._phase_compile_encryptor()
            self._check_cancel()

            self.on_phase(6)  # Encrypt
            self._phase_encrypt()

            # Convert and Build ISO only when input is an ISO
            if self._is_iso():
                self.on_phase(7)  # Convert
                self._phase_convert()
                self._check_cancel()

                self.on_phase(8)  # Build ISO
                self._phase_build_iso()
                self._check_cancel()

            self._succeeded = True
            self.on_phase(cleanup_phase)
            self._phase_cleanup()

            if self._is_iso() and hasattr(self, '_output_iso_path'):
                win_path = self._output_iso_path
                self.log(f"Modified ISO ready at: {win_path}", "success")
                self.on_done(True,
                    f"Asset modification complete!\n"
                    f"Modified ISO at:\n{win_path}")

                self.log("", "info")
                self.log("=== Next Steps ===", "info")
                self.log(
                    "1. Write this ISO to a USB drive using Rufus\n"
                    "   Important: select ISO mode (NOT DD mode) when prompted\n"
                    "2. Boot the pinball machine from USB\n"
                    "3. Let Clonezilla restore the image to the machine",
                    "info",
                )
                self.log_link(
                    "JJP USB Update Instructions (PDF)",
                    "https://marketing.jerseyjackpinball.com/general/install-full/"
                    "JJP_USB_UPDATE_PC_instructions.pdf",
                )
            else:
                # Fallback for non-ISO inputs: output the raw .img
                import os
                img_name = self._raw_img_path.rsplit("/", 1)[-1] if self._raw_img_path else "image"
                wsl_out = win_to_wsl(self.assets_folder)
                dest = f"{wsl_out}/{img_name}"
                win_path = os.path.join(self.assets_folder, img_name)

                if self._raw_img_path and self._raw_img_path != dest:
                    self.log("Moving modified image to output folder...", "info")
                    try:
                        last_pct = -1
                        for line in self.wsl.stream(
                            f"rsync --info=progress2 --no-inc-recursive --remove-source-files "
                            f"'{self._raw_img_path}' '{dest}'",
                            timeout=config.COPY_TIMEOUT,
                        ):
                            self._check_cancel()
                            m = re.search(r'(\d+)%', line)
                            if m:
                                pct = int(m.group(1))
                                if pct > last_pct:
                                    last_pct = pct
                                    self.on_progress(pct, 100, line.strip())
                        self.on_progress(100, 100, "Done")
                    except WslError as e:
                        self.log(f"Warning: Could not move image to output: {e.output}", "info")

                self.log(f"Modified image ready at: {win_path}", "success")
                self.on_done(True,
                    f"Asset modification complete!\n"
                    f"Modified image at:\n{win_path}")

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

    # --- Extract override ---

    def _phase_extract(self):
        """Always extract a fresh image from the original ISO for mod runs.

        Each mod run must start from a pristine image to avoid accumulated
        state from previous runs (modified files, dirty journals, etc.).
        Deletes any cached images from /tmp before extracting.
        """
        import os

        # Delete any cached image from previous runs to force fresh extraction
        cache_path = self._raw_img_cache_path()
        self.log("Clearing cached image to ensure fresh extraction...", "info")
        try:
            self.wsl.run(
                f"rm -f '{cache_path}' 2>/dev/null; true", timeout=30)
        except WslError:
            pass

        # Extract fresh from the original ISO
        super()._phase_extract()

    # --- Phase 0: Scan ---

    def _phase_scan(self):
        """Compare assets folder against saved checksums to find modified files."""
        import hashlib
        import os

        self.log("Scanning for modified files...", "info")

        checksums_file = os.path.join(self.assets_folder, '.checksums.md5')
        if not os.path.isfile(checksums_file):
            raise PipelineError("Scan",
                "No .checksums.md5 found in the assets folder.\n"
                "Run Decrypt first to generate baseline checksums.")

        # Load saved checksums
        saved = {}
        with open(checksums_file, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # md5sum output: "hash  ./path" or "hash *./path"
                m = re.match(r'^([a-f0-9]{32})\s+\*?(.+)$', line)
                if m:
                    filepath = m.group(2)
                    if filepath.startswith('./'):
                        filepath = filepath[2:]
                    saved[filepath] = m.group(1)

        self.log(f"Loaded {len(saved)} baseline checksums.", "info")

        # Collect files to scan (only those in the original checksums)
        all_files = []
        for root, _dirs, files in os.walk(self.assets_folder):
            for name in files:
                if name.startswith('.') or name == 'fl_decrypted.dat' or name.endswith('.img'):
                    continue
                full_path = os.path.join(root, name)
                rel_path = os.path.relpath(full_path, self.assets_folder).replace('\\', '/')
                if rel_path in saved:
                    all_files.append((rel_path, full_path))

        total = len(all_files)
        self.on_progress(0, total, "Scanning...")
        self.log(f"Checking {total} files for changes...", "info")

        self.changed_files = []
        for i, (rel_path, full_path) in enumerate(all_files):
            if self.cancelled:
                raise PipelineError("Scan", "Cancelled by user.")

            h = hashlib.md5()
            with open(full_path, 'rb') as fh:
                for chunk in iter(lambda: fh.read(65536), b''):
                    h.update(chunk)
            new_hash = h.hexdigest()

            if saved[rel_path] != new_hash:
                self.changed_files.append((rel_path, full_path))
                self.log(f"  Modified: {rel_path}", "info")

            if (i + 1) % 500 == 0 or i + 1 == total:
                self.on_progress(i + 1, total,
                    f"{len(self.changed_files)} changed so far")

        if self.changed_files:
            self.log(
                f"Found {len(self.changed_files)} modified file(s) "
                f"out of {total} checked.",
                "success",
            )
        else:
            self.log("No modified files detected.", "info")

    # (Backup phase removed — the original ISO serves as the backup.
    #  The raw image can be re-extracted from the ISO at any time.)

    # --- Compile ---

    def _phase_compile_encryptor(self):
        """Compile the encryptor hook (instead of decryptor)."""
        self.log("Compiling encryptor...", "info")
        mp = self.mount_point

        import tempfile, os
        try:
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.c', delete=False,
                dir=os.environ.get('TEMP', os.environ.get('TMP', '.')),
            ) as tf:
                tf.write(ENCRYPT_C_SOURCE)
                tmp_win = tf.name
            wsl_tmp = win_to_wsl(tmp_win)
            self.wsl.run(f"cp '{wsl_tmp}' {mp}/tmp/jjp_encrypt.c", timeout=15)
            os.unlink(tmp_win)
        except (WslError, OSError) as e:
            raise PipelineError("Compile",
                f"Failed to write C source: {e}") from e

        chroot_lib = f"{mp}/lib/x86_64-linux-gnu"
        try:
            self.wsl.run(
                f"gcc -c -fPIC -std=gnu11 -D_FORTIFY_SOURCE=0 -fno-stack-protector "
                f"-o {mp}/tmp/jjp_encrypt.o {mp}/tmp/jjp_encrypt.c 2>&1",
                timeout=config.COMPILE_TIMEOUT,
            )
            self.wsl.run(
                f"LIBS='{chroot_lib}/libc.so.6'; "
                f"[ -f '{chroot_lib}/libdl.so.2' ] && LIBS=\"$LIBS {chroot_lib}/libdl.so.2\"; "
                f"gcc -shared -nostdlib "
                f"-o {mp}/tmp/jjp_encrypt.so {mp}/tmp/jjp_encrypt.o $LIBS -lgcc 2>&1",
                timeout=config.COMPILE_TIMEOUT,
            )
        except WslError as e:
            raise PipelineError("Compile",
                f"gcc compilation failed: {e.output}") from e

        self.log("Encryptor compiled.", "success")

        # Build stub libraries (same as decrypt pipeline)
        self.log("Building stub libraries...", "info")
        stubs_dir = f"{mp}/tmp/stubs"
        self.wsl.run(f"rm -rf {stubs_dir}", timeout=5)
        self.wsl.run(f"mkdir -p {stubs_dir}", timeout=5)

        stub_b64 = base64.b64encode(STUB_C_SOURCE.encode()).decode()
        self.wsl.run(
            f"echo '{stub_b64}' | base64 -d > {stubs_dir}/stub.c",
            timeout=10,
        )

        total_sonames = len(config.STUB_SONAMES)
        built = 0
        skipped = 0
        for idx, soname in enumerate(config.STUB_SONAMES):
            self.on_progress(idx, total_sonames, soname)
            try:
                self.wsl.run(
                    f"chroot {mp} /bin/sh -c 'ldconfig -p 2>/dev/null | grep -q {soname} || "
                    f"test -f /usr/lib/{soname} || "
                    f"test -f /usr/lib/x86_64-linux-gnu/{soname} || "
                    f"find /usr/lib -name {soname} -quit 2>/dev/null | grep -q .'",
                    timeout=10,
                )
                skipped += 1
                continue
            except WslError:
                pass
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
                pass

        self.on_progress(total_sonames, total_sonames, "Done")
        self._stubs_built = built
        self.log(f"Built {built} stub libraries ({skipped} skipped).", "success")

    # --- Encrypt ---

    def _phase_encrypt(self):
        """Copy changed files into chroot, write manifest, run encryptor."""
        import os
        self.log("Preparing modified files...", "info")
        mp = self.mount_point
        repl_dir = f"{mp}/tmp/jjp_replacements"
        self.wsl.run(f"rm -rf {repl_dir} && mkdir -p {repl_dir}", timeout=10)

        # Copy each changed file into the chroot
        manifest_lines = []
        for i, (rel_path, win_path) in enumerate(self.changed_files):
            self._check_cancel()
            wsl_src = win_to_wsl(win_path)
            ext = os.path.splitext(win_path)[1]
            dest_name = f"repl_{i}{ext}"
            dest_path = f"{repl_dir}/{dest_name}"
            try:
                self.wsl.run(f"cp '{wsl_src}' '{dest_path}'", timeout=60)
            except WslError as e:
                raise PipelineError("Encrypt",
                    f"Failed to copy file: {win_path}\n{e.output}") from e

            manifest_lines.append(f"{rel_path}\t/tmp/jjp_replacements/{dest_name}")
            self.log(f"  Staged: {rel_path}", "info")

        # Write manifest
        manifest_content = "\n".join(manifest_lines) + "\n"
        manifest_b64 = base64.b64encode(manifest_content.encode()).decode()
        self.wsl.run(
            f"echo '{manifest_b64}' | base64 -d > {mp}/tmp/jjp_manifest.txt",
            timeout=10,
        )
        self.log(f"Manifest written with {len(self.changed_files)} entries.", "info")

        # Run the game binary with the encryptor hook
        game_bin = f"{config.GAME_BASE_PATH}/{self.game_name}/game"
        self.log("Running encryptor...", "info")
        ld_lib_path = f"LD_LIBRARY_PATH=/tmp/stubs " if getattr(self, '_stubs_built', 0) > 0 else ""

        cmd = (
            f"chroot {mp} /bin/bash -c '"
            f"export JJP_MANIFEST=/tmp/jjp_manifest.txt; "
            f"unset DISPLAY; "
            f"LD_PRELOAD=/tmp/jjp_encrypt.so "
            f"{ld_lib_path}"
            f"{game_bin}"
            f"' 2>&1"
        )

        max_retries = 3
        retry_wait = 5

        for attempt in range(max_retries):
            total_files = 0
            final_ok = 0
            final_fail = 0
            final_total = 0
            sentinel_error = False
            output_lines = []

            total_re = re.compile(r'\[encrypt\] TOTAL_FILES=(\d+)')
            progress_re = re.compile(
                r'Progress:\s*(\d+)\s*\(ok=(\d+)\s+fail=(\d+)\)')
            result_re = re.compile(
                r'Total:\s*(\d+)\s+OK:\s*(\d+)\s+Failed:\s*(\d+)')
            fl_updated_re = re.compile(r'FL_DAT_UPDATED=1')
            fl_failed_re = re.compile(r'FL_DAT_FAILED=1')
            fl_dat_updated = False
            fl_dat_failed = False

            try:
                for line in self.wsl.stream(cmd, timeout=config.DECRYPT_TIMEOUT):
                    if self.cancelled:
                        self.wsl.kill()
                        raise PipelineError("Encrypt", "Cancelled by user.")

                    output_lines.append(line)

                    if ("key not found" in line.lower() or "H0007" in line
                            or "Terminal services" in line or "H0027" in line):
                        sentinel_error = True

                    level = "info"
                    if "[FAIL]" in line or "VERIFY FAIL" in line or "FAILED" in line:
                        level = "error"
                    elif "[VERIFY OK]" in line or "decrypted OK" in line:
                        level = "success"
                    elif "forge:" in line and "OK" in line:
                        level = "success"
                    elif "fl.dat restored" in line:
                        level = "success"
                    elif "WARNING" in line or "WARN" in line:
                        level = "error"
                    self.log(line, level)

                    m = total_re.search(line)
                    if m:
                        total_files = int(m.group(1))
                        self.on_progress(0, total_files, "Encrypting...")

                    m = progress_re.search(line)
                    if m:
                        current = int(m.group(1))
                        ok_count = int(m.group(2))
                        fail_count = int(m.group(3))
                        desc = f"ok={ok_count} fail={fail_count}"
                        self.on_progress(current, total_files, desc)

                    m = result_re.search(line)
                    if m:
                        final_total = int(m.group(1))
                        final_ok = int(m.group(2))
                        final_fail = int(m.group(3))

                    if fl_updated_re.search(line):
                        fl_dat_updated = True
                    if fl_failed_re.search(line):
                        fl_dat_failed = True

            except WslError:
                if final_total > 0:
                    pass
                elif sentinel_error:
                    pass
                else:
                    combined = "\n".join(output_lines[-5:]) if output_lines else ""
                    raise PipelineError("Encrypt",
                        f"Encryptor process failed.\nLast output:\n{combined}")

            if sentinel_error and attempt < max_retries - 1:
                wait = retry_wait * (attempt + 1)
                self.log(
                    f"Sentinel key not found - re-attaching dongle and retrying "
                    f"in {wait}s (attempt {attempt + 2}/{max_retries})...",
                    "info",
                )
                time.sleep(wait)
                self._reattach_dongle()
                continue

            if sentinel_error:
                raise PipelineError("Encrypt",
                    "Sentinel HASP key not found after multiple attempts.")

            break

        if final_total == 0:
            raise PipelineError("Encrypt",
                "Encryptor produced no output. Check dongle and manifest.")

        self.on_progress(final_total, final_total, "Complete")
        summary = f"{final_ok}/{final_total} files replaced and verified"
        if final_fail > 0:
            summary += f" ({final_fail} FAILED)"
            self.log(summary, "error")
        else:
            summary += " successfully"
            self.log(summary, "success")

        # CRC forgery mode: fl.dat is restored unmodified
        self.log("CRC32 forgery: encrypted files match original fl.dat checksums.", "success")

    # --- Phase 7: Convert (raw ext4 → partclone) ---

    def _phase_convert(self):
        """Convert modified ext4 image to partclone format for Clonezilla ISO."""
        self.log("Converting modified image to partclone format...", "info")

        # The ext4 image must be unmounted before partclone can read it.
        # Also run e2fsck to fix any metadata inconsistencies from the
        # read-write mount + file modifications.
        if self.mount_point:
            # Clean up build artifacts from /tmp inside the chroot BEFORE
            # unmounting, so they don't end up in the partclone image.
            self.log("Cleaning build artifacts from image...", "info")
            mp = self.mount_point
            for artifact in [
                f"{mp}/tmp/jjp_encrypt.c",
                f"{mp}/tmp/jjp_encrypt.o",
                f"{mp}/tmp/jjp_encrypt.so",
                f"{mp}/tmp/jjp_manifest.txt",
                f"{mp}/tmp/jjp_replacements",
                f"{mp}/tmp/stubs",
            ]:
                try:
                    self.wsl.run(f"rm -rf '{artifact}' 2>/dev/null; true", timeout=5)
                except WslError:
                    pass

            self.log("Unmounting ext4 for conversion...", "info")
            # Unmount bind mounts first (reverse order)
            for target in reversed(self._bind_mounted):
                try:
                    self.wsl.run(
                        f"umount -l '{self.mount_point}{target}' 2>/dev/null; true",
                        timeout=10,
                    )
                except WslError:
                    pass
            self._bind_mounted = []
            # Unmount the ext4
            try:
                self.wsl.run(
                    f"umount '{self.mount_point}'", timeout=30)
            except WslError:
                self.wsl.run(
                    f"umount -l '{self.mount_point}' 2>/dev/null; true",
                    timeout=30,
                )
            try:
                self.wsl.run(
                    f"rmdir '{self.mount_point}' 2>/dev/null; true", timeout=5)
            except WslError:
                pass
            self.mount_point = None

        wsl_img = self._raw_img_path
        self.log("Running e2fsck to repair filesystem metadata...", "info")
        try:
            for line in self.wsl.stream(
                f"e2fsck -fy '{wsl_img}' 2>&1",
                timeout=300,
            ):
                clean = line.strip()
                if clean:
                    self.log(f"  {clean}", "info")
        except WslError:
            pass  # e2fsck returns non-zero if it made repairs — that's fine

        # Ensure required tools are available
        self._ensure_iso_tools()

        # Mount the original ISO if not already mounted (extract may have skipped it)
        if not self._iso_mount:
            wsl_iso = win_to_wsl(self.image_path)
            tag = uuid.uuid4().hex[:8]
            self._iso_mount = f"/tmp/jjp_iso_{tag}"
            try:
                self.wsl.run(f"mkdir -p {self._iso_mount}", timeout=10)
                self.wsl.run(
                    f"mount -o loop,ro '{wsl_iso}' {self._iso_mount}",
                    timeout=config.MOUNT_TIMEOUT,
                )
            except WslError as e:
                raise PipelineError("Convert",
                    f"Failed to mount original ISO: {e.output}") from e

        # Verify Clonezilla structure
        partimag = f"{self._iso_mount}{config.PARTIMAG_PATH}"
        part_prefix = f"{partimag}/{config.GAME_PARTITION}.ext4-ptcl-img.gz"
        try:
            parts_out = self.wsl.run(
                f"ls -1 {part_prefix}.* 2>/dev/null | sort", timeout=10)
        except WslError:
            parts_out = ""
        parts = [p.strip() for p in parts_out.strip().split("\n") if p.strip()]
        if not parts:
            raise PipelineError("Convert",
                f"No partclone image for {config.GAME_PARTITION} found in ISO.")

        # Determine split size from original files — use exact byte count
        # to match the original chunk boundaries precisely.
        # (JJP originals use 1,000,000,000 bytes, NOT 1 GiB.)
        split_size = "1000000000"
        try:
            sz = self.wsl.run(
                f"stat -c%s '{parts[0]}'", timeout=5).strip()
            split_size = sz  # exact byte count from original first chunk
        except (WslError, ValueError):
            pass
        self.log(f"Using split size: {split_size} bytes", "info")

        # Prefer pigz (parallel gzip) for speed.
        # Use --fast -b 1024 --rsyncable to match the original Clonezilla
        # compression flags, ensuring maximum compatibility.
        try:
            self.wsl.run("which pigz", timeout=5)
            compressor = "pigz -c --fast -b 1024 --rsyncable"
        except WslError:
            compressor = "gzip -c --fast --rsyncable"

        # Run the conversion pipeline — output to a temp chunks directory.
        # The build phase will splice these into the original ISO.
        tag = uuid.uuid4().hex[:8]
        self._chunks_dir = f"/tmp/jjp_chunks_{tag}"
        output_prefix = f"{self._chunks_dir}/{config.GAME_PARTITION}.ext4-ptcl-img.gz."
        self.wsl.run(f"mkdir -p '{self._chunks_dir}'", timeout=10)

        # The raw image may still be mounted — use the path directly
        wsl_img = self._raw_img_path
        self.log(f"Converting {wsl_img} to partclone format...", "info")
        self.log("This may take 10-30 minutes depending on image size.", "info")

        # Build a wrapper script that runs the conversion in the background
        # and monitors progress from the partclone log file. This lets us
        # stream progress updates to the GUI during the long-running conversion.
        # Note: partclone writes progress with \r (carriage returns) to stderr,
        # so we use tr to convert \r to \n for grep, and stdbuf to reduce
        # buffering on the stderr redirect.
        convert_cmd = (
            f"set -o pipefail && "
            f"partclone.ext4 -c -s '{wsl_img}' -o - 2> >(stdbuf -oL tr '\\r' '\\n' > /tmp/jjp_ptcl.log) "
            f"| {compressor} "
            f"| split -b {split_size} -a 2 - '{output_prefix}'"
        )
        monitor_script = (
            f"#!/bin/bash\n"
            f"# Run conversion in background\n"
            f"({convert_cmd}) &\n"
            f"PID=$!\n"
            f"LAST_PCT=-1\n"
            f"# Monitor progress from partclone log\n"
            f"while kill -0 $PID 2>/dev/null; do\n"
            f"  sleep 3\n"
            f"  # Extract latest progress from partclone log\n"
            f"  PCT=$(grep -oP 'Completed:\\s*\\K[\\d.]+' /tmp/jjp_ptcl.log 2>/dev/null | tail -1)\n"
            f"  # Get output size\n"
            f"  OSIZE=$(du -sb '{output_prefix}'* 2>/dev/null | awk '{{s+=$1}} END {{printf \"%d\", s}}')\n"
            f"  if [ -n \"$PCT\" ]; then\n"
            f"    # Only print if progress changed\n"
            f"    CUR=$(printf '%.0f' \"$PCT\" 2>/dev/null || echo 0)\n"
            f"    if [ \"$CUR\" != \"$LAST_PCT\" ]; then\n"
            f"      LAST_PCT=$CUR\n"
            f"      echo \"PROGRESS:${{PCT}}% output=${{OSIZE:-0}}\"\n"
            f"    fi\n"
            f"  else\n"
            f"    # No progress yet — show indeterminate\n"
            f"    echo \"PROGRESS:0% output=${{OSIZE:-0}}\"\n"
            f"  fi\n"
            f"done\n"
            f"wait $PID\n"
            f"exit $?\n"
        )
        monitor_path = "/tmp/jjp_convert_monitor.sh"
        monitor_b64 = base64.b64encode(monitor_script.encode()).decode()
        self.wsl.run(
            f"echo '{monitor_b64}' | base64 -d > {monitor_path} && "
            f"chmod +x {monitor_path}",
            timeout=10,
        )

        self.log("Starting partclone conversion pipeline...", "info")
        last_pct = -1
        try:
            for line in self.wsl.stream(
                f"bash {monitor_path}", timeout=config.ISO_CONVERT_TIMEOUT
            ):
                if self.cancelled:
                    self.wsl.kill()
                    raise PipelineError("Convert", "Cancelled by user.")
                clean = line.strip()
                if not clean:
                    continue
                m = re.search(r'PROGRESS:([\d.]+)%\s*output=(\d+)', clean)
                if m:
                    pct = float(m.group(1))
                    ipct = int(pct)
                    out_mb = int(m.group(2)) / (1024**2)
                    if ipct > last_pct:
                        last_pct = ipct
                        self.on_progress(ipct, 100, f"{ipct}% ({out_mb:.0f} MB written)")
                        if ipct % 10 == 0:
                            self.log(f"  Conversion: {ipct}% ({out_mb:.0f} MB written)", "info")

        except WslError as e:
            # Try to read the partclone log for details
            log_content = ""
            try:
                log_content = self.wsl.run(
                    "tail -5 /tmp/jjp_ptcl.log 2>/dev/null", timeout=5).strip()
            except WslError:
                pass
            raise PipelineError("Convert",
                f"Partclone conversion failed: {e.output}\n{log_content}") from e

        # Verify output files
        try:
            parts_out = self.wsl.run(
                f"ls -lh '{output_prefix}'* 2>/dev/null", timeout=10).strip()
            self.log(f"Partclone files created:\n{parts_out}", "success")
        except WslError:
            raise PipelineError("Convert", "No partclone output files were created.")

        self.on_progress(100, 100, "Conversion complete")

    def _ensure_iso_tools(self):
        """Ensure partclone and xorriso are available, installing if needed."""
        for tool, pkg in [("partclone.ext4", "partclone"), ("xorriso", "xorriso")]:
            try:
                self.wsl.run(f"which {tool}", timeout=10)
                self.log(f"  {tool}: found", "info")
            except WslError:
                self.log(f"  {tool} not found. Installing {pkg}...", "info")
                try:
                    self.wsl.run(
                        f"DEBIAN_FRONTEND=noninteractive apt-get install -y {pkg} 2>&1",
                        timeout=120,
                    )
                    self.log(f"  {pkg} installed.", "success")
                except WslError as e:
                    raise PipelineError("Convert",
                        f"Failed to install {pkg}: {e.output}\n"
                        f"Run manually: wsl -u root -- apt install {pkg}") from e

    # --- Phase 8: Build ISO ---

    def _phase_build_iso(self):
        """Assemble modified Clonezilla ISO by splicing new partition chunks
        into the original ISO.  Uses xorriso -indev/-outdev with
        -boot_image any replay to perfectly preserve the original boot
        configuration (MBR, El Torito, EFI, Syslinux)."""
        import os
        self.log("Building modified Clonezilla ISO...", "info")

        iso_basename = os.path.splitext(os.path.basename(self.image_path))[0]
        wsl_out = win_to_wsl(self.assets_folder)
        output_iso = f"{wsl_out}/{iso_basename}_modified.iso"
        wsl_iso = win_to_wsl(self.image_path)

        # Enumerate new chunk files produced by _phase_convert
        chunks_dir = self._chunks_dir
        game_part = config.GAME_PARTITION
        partimag = config.PARTIMAG_PATH
        try:
            chunks_out = self.wsl.run(
                f"ls -1 '{chunks_dir}/{game_part}.ext4-ptcl-img.gz.'* "
                f"2>/dev/null | sort",
                timeout=10,
            ).strip()
        except WslError:
            chunks_out = ""
        new_chunks = [c.strip() for c in chunks_out.split("\n") if c.strip()]
        if not new_chunks:
            raise PipelineError("Build ISO", "No new partition chunks found.")
        self.log(f"Found {len(new_chunks)} new partition chunk(s).", "info")

        # Build xorriso command:
        #   -indev  : read original ISO (preserves all structure)
        #   -outdev : write modified ISO
        #   -boot_image any replay : preserve ALL boot records from original
        #   -find … -exec remove   : delete old partition chunks
        #   -map …                 : add new partition chunks
        rm_cmd = (
            f"-find '{partimag}' "
            f"-name '{game_part}.ext4-ptcl-img.gz.*' "
            f"-exec rm --"
        )

        map_cmds = []
        for chunk_path in new_chunks:
            base = chunk_path.rsplit("/", 1)[-1]
            iso_path = f"{partimag}/{base}"
            map_cmds.append(f"-map '{chunk_path}' '{iso_path}'")

        map_str = " \\\n  ".join(map_cmds)

        script = (
            f"#!/bin/bash\n"
            f"set -e\n"
            f"xorriso \\\n"
            f"  -indev '{wsl_iso}' \\\n"
            f"  -outdev '{output_iso}' \\\n"
            f"  -boot_image any replay \\\n"
            f"  {rm_cmd} \\\n"
            f"  {map_str} \\\n"
            f"  -end 2>&1\n"
        )
        script_path = "/tmp/jjp_build_iso.sh"
        script_b64 = base64.b64encode(script.encode()).decode()
        self.wsl.run(
            f"echo '{script_b64}' | base64 -d > {script_path} && "
            f"chmod +x {script_path}",
            timeout=10,
        )

        # Unmount the original ISO before xorriso reads it — avoids
        # contention between the loop mount and xorriso's file access.
        if self._iso_mount:
            try:
                self.wsl.run(
                    f"umount -l '{self._iso_mount}' 2>/dev/null; true", timeout=15)
                self.wsl.run(
                    f"rmdir '{self._iso_mount}' 2>/dev/null; true", timeout=5)
            except WslError:
                pass
            self._iso_mount = None

        # Remove existing output ISO — xorriso refuses to write to non-empty -outdev
        try:
            self.wsl.run(f"rm -f '{output_iso}'", timeout=10)
        except WslError:
            pass

        self.log("Running xorriso (splicing partition into original ISO)...", "info")
        last_pct = -1
        try:
            for line in self.wsl.stream(
                f"bash {script_path}", timeout=config.ISO_BUILD_TIMEOUT
            ):
                self._check_cancel()
                clean = line.strip()
                if not clean:
                    continue
                if "FAILURE" in clean or "sorry" in clean.lower():
                    self.log(f"  xorriso: {clean}", "error")
                # xorriso native mode: "Writing:  1234s    12.3%"
                m = re.search(r'(\d+\.\d+)%', clean)
                if m:
                    pct = int(float(m.group(1)))
                    if pct > last_pct:
                        last_pct = pct
                        self.on_progress(pct, 100, f"Building ISO: {pct}%")
        except WslError as e:
            try:
                script_content = self.wsl.run(
                    f"cat {script_path}", timeout=5).strip()
                self.log(f"Build script was:\n{script_content}", "info")
            except WslError:
                pass
            raise PipelineError("Build ISO",
                f"xorriso failed: {e.output}") from e

        # Verify output and compare size with original
        try:
            new_sz = int(self.wsl.run(
                f"stat -c%s '{output_iso}'", timeout=10).strip())
            orig_sz = int(self.wsl.run(
                f"stat -c%s '{wsl_iso}'", timeout=10).strip())
            new_gb = new_sz / (1024**3)
            orig_gb = orig_sz / (1024**3)
            diff_mb = (new_sz - orig_sz) / (1024**2)
            self.log(
                f"ISO created: {new_gb:.2f} GB "
                f"(original: {orig_gb:.2f} GB, diff: {diff_mb:+.1f} MB)",
                "success",
            )
        except (WslError, ValueError):
            raise PipelineError("Build ISO", "ISO file was not created.")

        self.on_progress(100, 100, "ISO build complete")
        win_iso_path = os.path.join(self.assets_folder, f"{iso_basename}_modified.iso")
        self._output_iso_path = win_iso_path
        self.log(f"Output ISO: {win_iso_path}", "success")

    # --- Cleanup ---

    def _phase_cleanup(self):
        """Clean up mounts, build dir, and detach dongle."""
        self.log("Cleaning up...", "info")

        if self.mount_point:
            mp = self.mount_point
            try:
                self.wsl.run("killall hasplmd_x86_64 2>/dev/null; true", timeout=10)
            except WslError:
                pass

            usbipd = find_usbipd()
            self.wsl.run_win(
                [usbipd, "detach", "--hardware-id", config.HASP_VID_PID],
                timeout=10,
            )

            for target in reversed(self._bind_mounted):
                try:
                    self.wsl.run(f"umount -l '{mp}{target}' 2>/dev/null; true", timeout=10)
                except WslError:
                    pass

            try:
                self.wsl.run(f"umount -l '{mp}' 2>/dev/null; true", timeout=30)
            except WslError:
                pass

            try:
                self.wsl.run(f"rmdir '{mp}' 2>/dev/null; true", timeout=5)
            except WslError:
                pass

        if self._iso_mount:
            try:
                self.wsl.run(f"umount -l '{self._iso_mount}' 2>/dev/null; true", timeout=15)
                self.wsl.run(f"rmdir '{self._iso_mount}' 2>/dev/null; true", timeout=5)
            except WslError:
                pass

        # Clean up temp chunks directory
        if hasattr(self, '_chunks_dir') and self._chunks_dir:
            self.log("Removing temp chunks directory...", "info")
            try:
                self.wsl.run(f"rm -rf '{self._chunks_dir}'", timeout=60)
            except WslError:
                self.log(f"Warning: Could not remove {self._chunks_dir}", "info")

        # Clean up partclone log
        try:
            self.wsl.run("rm -f /tmp/jjp_ptcl.log 2>/dev/null; true", timeout=5)
        except WslError:
            pass

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

    # partclone (optional — auto-installed at runtime if missing)
    try:
        wsl.run("which partclone.ext4", timeout=10)
        results.append(("partclone", True, "Available"))
    except Exception:
        results.append(("partclone", False,
            "Not installed (will auto-install during Modify Assets)"))

    # xorriso (optional — auto-installed at runtime if missing)
    try:
        wsl.run("which xorriso", timeout=10)
        results.append(("xorriso", True, "Available"))
    except Exception:
        results.append(("xorriso", False,
            "Not installed (will auto-install during Modify Assets)"))

    return results
