"""Top-of-window banner announcing this app's retirement.

This standalone JJP Asset Decryptor app is no longer being
developed — all active work has moved to the unified **Pinball
Asset Decryptor**, which covers JJP, BOF, Spooky, Pinball
Brothers, Chicago Gaming, and Williams in one place with regular
releases.

The banner is *non-modal*: the existing app remains fully usable,
so anyone in the middle of a workflow can finish it.  But it sits
at the very top of every window on every launch so the migration
path is impossible to miss, and a single click opens the unified
app's releases page in the system browser.

Keeping this in its own module so the GUI wiring is one import +
one call, and the wording can be tweaked without rummaging through
the main window file.
"""

import sys
import tkinter as tk
import webbrowser


UNIFIED_APP_URL = (
    "https://github.com/davidvanderburgh/"
    "pinball-asset-decryptor/releases/latest")

# Yellow-on-dark-brown reads as "warning, not error" in both light
# and dark themes; the colours are picked to keep the link
# unambiguously clickable (underline + pointer cursor + brighter
# blue than the body text).
_BG = "#fff4cc"      # pale yellow
_FG = "#5a4a00"      # dark amber for body text
_LINK_FG = "#1155bb"  # blue, distinct from body amber


def build_deprecation_banner(parent, app_name="this app"):
    """Build and return a yellow deprecation banner frame.

    Caller is responsible for ``.pack()``-ing the returned frame
    wherever it belongs in their layout (typically as the FIRST
    child of the main window so it's the first thing the eye
    catches).

    Parameters
    ----------
    parent : tk.Misc
        The Tk parent widget.
    app_name : str
        Human-readable name of the deprecated app, e.g.
        ``"JJP Asset Decryptor"``.  Used only in the banner
        body text.

    Returns
    -------
    tk.Frame
        The (unpacked) banner frame.
    """
    frame = tk.Frame(
        parent, bg=_BG, padx=12, pady=8,
        highlightbackground="#d4a017", highlightthickness=1)

    title = tk.Label(
        frame,
        text=(
            "⚠  This app has been replaced by "
            "Pinball Asset Decryptor"),
        bg=_BG, fg=_FG,
        font=("Segoe UI" if sys.platform == "win32"
              else "Helvetica", 11, "bold"),
        anchor=tk.W)
    title.pack(fill=tk.X, anchor=tk.W)

    body = tk.Label(
        frame,
        text=(
            f"{app_name} is no longer maintained.  All "
            "development of JJP, BOF, Spooky, Pinball Brothers, "
            "Chicago Gaming, and Williams asset decryption has "
            "moved to the unified Pinball Asset Decryptor, "
            "which includes every feature of this app plus "
            "ongoing improvements."),
        bg=_BG, fg=_FG,
        font=("Segoe UI" if sys.platform == "win32"
              else "Helvetica", 9),
        justify=tk.LEFT, anchor=tk.W, wraplength=700)
    body.pack(fill=tk.X, anchor=tk.W, pady=(4, 4))

    link = tk.Label(
        frame,
        text="→  Download Pinball Asset Decryptor",
        bg=_BG, fg=_LINK_FG,
        font=("Segoe UI" if sys.platform == "win32"
              else "Helvetica", 10, "underline"),
        cursor="hand2", anchor=tk.W)
    link.pack(fill=tk.X, anchor=tk.W)
    link.bind(
        "<Button-1>", lambda _e: webbrowser.open(UNIFIED_APP_URL))

    return frame
