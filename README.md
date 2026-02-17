# JJP Asset Decryptor

A Windows GUI application that decrypts game assets from Jersey Jack Pinball (JJP) machines. Automates a multi-step process that would otherwise require ~20 manual shell commands per game.

## What It Does

JJP pinball machines store encrypted game assets (graphics, videos, audio) on their internal drives. Each machine ships with a Clonezilla backup ISO containing the full filesystem image. This tool:

1. Extracts the ext4 filesystem from a Clonezilla ISO (or uses a raw ext4 image directly)
2. Mounts the image in WSL2 and sets up a chroot environment
3. Passes through the game's Sentinel HASP USB security dongle via usbipd
4. Compiles and injects a decryptor that hooks the game's own crypto functions
5. Decrypts all game assets and copies them to your chosen output folder

## Supported Games

Confirmed working:

- Willy Wonka & the Chocolate Factory
- Guns N' Roses
- Elton John
- The Hobbit

Not yet tested:

- Wizard of Oz
- Dialed In
- Toy Story
- The Godfather
- Avatar
- Harry Potter

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

## Usage

```
python -m jjp_decryptor
```

1. Click **Browse** to select your game image (ISO or ext4)
2. Click **Browse** to select an output folder for decrypted assets
3. Click **Check Prerequisites** to verify your setup
4. Click **Start Decryption**

The app remembers your last-used image and output paths between sessions.

## How It Works

The decryptor uses an LD_PRELOAD hook that intercepts the game binary's `al_install_system` call (Allegro 5 initialization). Before the game can start its normal display-dependent flow, the hook:

1. Resolves the game's crypto functions via `dlsym`: `dongle_init`, `dongle_decrypt_buffer`, `jcrypt_set_seeds_for_crypto`, and `jcrypt_rand64`
2. Calls `dongle_init()` to establish a Sentinel HASP session with the USB dongle
3. Reads and decrypts `fl.dat` (the encrypted file list) using the dongle
4. For each file entry: seeds the crypto PRNG with the file path, then XOR-decrypts with the 64-bit keystream
5. Strips filler bytes and writes the decrypted content to the output directory

The crypto algorithm is the game's own implementation, accessed through the game binary's exported symbols. The HASP dongle provides the initial decryption key for the file list.

## Pipeline Phases

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

## Troubleshooting

### "Sentinel key not found (H0007)"
The HASP dongle for the correct game is not plugged in or not detected. Each game requires its own dongle. Make sure:
- The dongle is plugged into a USB port (not a hub if possible)
- `usbipd list` shows the device (VID:PID `0529:0001`)
- You're using the dongle that came with the specific game you're decrypting

### "fl.dat decryption FAILED (not text)"
The dongle session wasn't established properly. Try running again - the app retries automatically up to 3 times with increasing delays.

### Prerequisites check fails for gcc
Run in a terminal: `wsl -u root -- apt update && wsl -u root -- apt install gcc`

### Stale mounts from a previous crash
The app detects and cleans up stale mounts automatically on startup. If you have issues, you can manually clean up:
```
wsl -u root -- bash -c "umount -R /mnt/jjp_*; rmdir /mnt/jjp_*"
```

### Extraction is slow
The first run for each ISO requires extracting the partclone image to a raw ext4 file. This can take several minutes for large images (up to 32 GB). The raw image is cached in WSL's `/tmp/` so subsequent runs skip this step.

## License

This project is provided as-is for personal use with JJP pinball machines you own.
