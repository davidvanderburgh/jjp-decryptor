# JJP Asset Decryptor

A Windows GUI application that decrypts and modifies game assets from Jersey Jack Pinball (JJP) machines. Automates a multi-step process that would otherwise require ~20 manual shell commands per game.

## What It Does

JJP pinball machines store encrypted game assets (graphics, videos, audio) on their internal drives. Each machine ships with a Clonezilla backup ISO containing the full filesystem image. This tool:

1. Extracts the ext4 filesystem from a Clonezilla ISO (or uses a raw ext4 image directly)
2. Mounts the image in WSL2 and sets up a chroot environment
3. Passes through the game's Sentinel HASP USB security dongle via usbipd
4. Compiles and injects a decryptor that hooks the game's own crypto functions
5. Decrypts all game assets and copies them to your chosen output folder
6. **Modify Assets**: Replace decrypted assets (images, audio, video) and re-encrypt them back into the game image, including updated CRC32 checksums and re-encrypted file list, producing a bootable Clonezilla ISO ready to flash

## Supported Games

Confirmed working:

- Willy Wonka & the Chocolate Factory
- Guns N' Roses
- Elton John
- The Hobbit

Not yet tested:

- The Godfather
- Avatar

Each game requires its own HASP USB dongle (the purple USB dongle attached to the motherboard in the game).

## Requirements

- **Windows 10/11** with WSL2 enabled
- **WSL2** with Ubuntu (or similar) installed: `wsl --install`
- **gcc** in WSL: `wsl -u root -- apt install gcc`
- **usbipd-win**: Install from [github.com/dorssel/usbipd-win](https://github.com/dorssel/usbipd-win/releases)
- **Sentinel HASP USB dongle** for the game you want to decrypt
- **Game image**: Clonezilla ISO backup or raw ext4 filesystem image. Download "full installs" from https://marketing.jerseyjackpinball.com/downloads/
- **Python 3.10+** (Windows): [python.org](https://www.python.org/downloads/)

No additional Python packages are required (uses only the standard library).

## Platform Support

**Windows only.** The tool relies on WSL2 for Linux filesystem operations and usbipd-win for USB dongle passthrough, neither of which are available on macOS or Linux.

## Installation

1. Clone the repository:
   ```
   git clone <repo-url>
   cd jjp
   ```

2. (Optional) Create a desktop shortcut with the app icon:
   ```
   create_shortcut.bat
   ```
   This creates a "JJP Asset Decryptor" shortcut on your desktop.

3. Or launch directly:
   ```
   python -m jjp_decryptor
   ```
   You can also double-click `JJP Asset Decryptor.pyw` to launch without a console window.

## Usage

### Decrypting Assets

1. Launch the app (desktop shortcut, `.pyw` file, or `python -m jjp_decryptor`)
2. Prerequisites are checked automatically on startup
3. Click **Browse** to select your game image (ISO or ext4)
4. Click **Browse** to select an output folder for decrypted assets
5. Click **Start Decryption**

The app remembers your last-used image and output paths between sessions.

### Modifying Assets

After decrypting, you can replace game assets and re-encrypt them:

1. Switch to the **Modify Assets** tab
2. Browse to the **original Clonezilla ISO** and the output folder from decryption
3. Modify files in the output folder (replace PNGs, OGGs, WebMs, etc.)
4. Click **Apply Modifications** — the tool detects changed files via checksums, re-encrypts only what changed, updates CRC32 checksums in fl.dat, re-encrypts fl.dat via the HASP dongle, and builds a new bootable Clonezilla ISO
5. The output `<name>_modified.iso` is saved to your output folder

### Installing on the Machine

1. Write the `_modified.iso` to a USB drive using [Rufus](https://rufus.ie/)
   - **Important: Use ISO mode (not DD mode) when Rufus prompts you.** DD mode will not produce a bootable drive for Clonezilla ISOs on JJP hardware.
2. Boot the pinball machine from the USB drive
3. Let Clonezilla restore the image to the machine

### File Format Notes

- Images must be **PNG** (same dimensions as originals)
- Videos must be **WebM** (same codec/resolution as originals)
- Audio must be **WAV** or **OGG** (matching the original format)
- Format or dimension mismatches won't corrupt the image but may crash or glitch the game at runtime

## How It Works

The decryptor uses an `LD_PRELOAD` hook that intercepts the game binary's `al_install_system` call (Allegro 5 initialization). Before the game can start its normal display-dependent flow, the hook:

1. Resolves the game's crypto functions via `dlsym`: `dongle_init`, `dongle_decrypt_buffer`, `jcrypt_set_seeds_for_crypto`, and `jcrypt_rand64`
2. Calls `dongle_init()` to establish a Sentinel HASP session with the USB dongle
3. Reads and decrypts `fl.dat` (the encrypted file list) using the dongle
4. For each file entry: seeds the crypto PRNG with the file path, then XOR-decrypts with the 64-bit keystream
5. Strips filler bytes and writes the decrypted content to the output directory

The encryption process reverses this: replacement files are padded with filler, XOR-encrypted with the same keystream, and written back to the game image. CRC32 checksums in fl.dat are recomputed and fl.dat is re-encrypted using `hasp_encrypt` with the session handle extracted from `dongle_decrypt_buffer`'s machine code.

## Pipeline Phases

### Decrypt

| Phase | Description |
|-------|-------------|
| Extract | Convert Clonezilla ISO partclone images to raw ext4 (cached for reuse) |
| Mount | Loop-mount the ext4 image in WSL2 |
| Chroot | Set up bind mounts (/proc, /sys, /dev, etc.) for the game environment |
| Dongle | Attach HASP USB dongle to WSL via usbipd, start the license daemon |
| Compile | Compile the C decryptor hook and any needed stub libraries |
| Decrypt | Run the game binary with the hook, decrypt all assets |
| Copy | Copy decrypted files from WSL to your Windows output folder |
| Cleanup | Unmount everything, detach USB, remove temporary files |

### Modify Assets

| Phase | Description |
|-------|-------------|
| Scan | Compare output folder against saved checksums to find modified files |
| Extract | Locate or extract the game image from Clonezilla ISO |
| Mount | Loop-mount the ext4 image read-write in WSL2 |
| Chroot | Set up bind mounts for the game environment |
| Dongle | Attach HASP USB dongle to WSL via usbipd, start the license daemon |
| Compile | Compile the C encryptor hook |
| Encrypt | Re-encrypt modified files, update CRC32 checksums, re-encrypt fl.dat |
| Convert | Run e2fsck, then convert modified ext4 back to partclone format |
| Build ISO | Splice modified partition chunks into original ISO with xorriso |
| Cleanup | Unmount everything, detach USB, remove temporary files |

## Troubleshooting

### "Sentinel key not found (H0007)"
The HASP dongle for the correct game is not plugged in or not detected. Each game requires its own dongle. Make sure:
- The dongle is plugged into a USB port (not a hub if possible)
- `usbipd list` shows the device (VID:PID `0529:0001`)
- You're using the dongle that came with the specific game you're decrypting

### "fl.dat decryption FAILED (not text)"
The dongle session wasn't established properly. Try running again - the app retries automatically up to 3 times with increasing delays.

### "Could not re-encrypt fl.dat"
The HASP session handle could not be extracted from the game binary. This means the modified files will work but the game may show FILE CHECK ERRORs at boot. Try running again with the dongle firmly seated.

### Prerequisites check fails for gcc
Run in a terminal: `wsl -u root -- apt update && wsl -u root -- apt install gcc`

### Stale mounts from a previous crash
The app detects and cleans up stale mounts automatically on startup. If you have issues, you can manually clean up:
```
wsl -u root -- bash -c "findmnt -rn -o TARGET | grep /mnt/jjp_ | sort -r | xargs -r umount -lf; rmdir /mnt/jjp_* 2>/dev/null"
```

### Extraction is slow
The first run for each ISO requires extracting the partclone image to a raw ext4 file. This can take several minutes for large images (up to 32 GB). The raw image is cached so subsequent runs skip this step. Use the **Clear Cache** button to free up disk space.

## License

This project is provided as-is for personal use with JJP pinball machines you own.
