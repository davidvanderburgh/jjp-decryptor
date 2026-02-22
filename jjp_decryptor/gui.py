"""Main window GUI for JJP Asset Decryptor."""

import tkinter as tk
from tkinter import ttk, filedialog
import time
import webbrowser

from . import config

# Color schemes for dark and light modes
_THEMES = {
    "dark": {
        "bg": "#2d2d2d",
        "fg": "#cccccc",
        "field_bg": "#1e1e1e",
        "select_bg": "#264f78",
        "accent": "#569cd6",
        "success": "#6a9955",
        "error": "#f44747",
        "timestamp": "#808080",
        "gray": "#808080",
        "trough": "#404040",
        "border": "#555555",
        "button": "#404040",
        "tab_selected": "#1e1e1e",
        "code_bg": "#1a1a1a",
        "code_fg": "#ce9178",
        "link": "#3794ff",
        "tooltip_bg": "#404040",
        "tooltip_fg": "#cccccc",
    },
    "light": {
        "bg": "#f5f5f5",
        "fg": "#1e1e1e",
        "field_bg": "#ffffff",
        "select_bg": "#0078d7",
        "accent": "#0066cc",
        "success": "#2e7d32",
        "error": "#c62828",
        "timestamp": "#757575",
        "gray": "#888888",
        "trough": "#d0d0d0",
        "border": "#bbbbbb",
        "button": "#e0e0e0",
        "tab_selected": "#ffffff",
        "code_bg": "#e8e8e8",
        "code_fg": "#a31515",
        "link": "#0066cc",
        "tooltip_bg": "#ffffe0",
        "tooltip_fg": "#1e1e1e",
    },
}


class _Tooltip:
    """Simple hover tooltip for a tkinter widget."""

    def __init__(self, widget, text, theme_fn):
        self._widget = widget
        self.text = text
        self._theme_fn = theme_fn  # callable returning current theme name
        self._tip = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, event=None):
        c = _THEMES[self._theme_fn()]
        x = self._widget.winfo_rootx() + self._widget.winfo_width() // 2
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        self._tip = tk.Toplevel(self._widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        label = tk.Label(self._tip, text=self.text, background=c["tooltip_bg"],
                         foreground=c["tooltip_fg"], relief="solid", borderwidth=1,
                         font=("Segoe UI", 9), padx=6, pady=2)
        label.pack()

    def _hide(self, event=None):
        if self._tip:
            self._tip.destroy()
            self._tip = None


class MainWindow:
    """Single-window tkinter GUI with Decrypt and Modify tabs."""

    def __init__(self, root, on_check_prereqs, on_start, on_cancel,
                 on_mod_apply=None, on_mod_cancel=None, on_clear_cache=None,
                 on_theme_change=None, initial_theme=None):
        self.root = root
        self._on_check_prereqs = on_check_prereqs
        self._on_start = on_start
        self._on_cancel = on_cancel
        self._on_mod_apply = on_mod_apply
        self._on_mod_cancel = on_mod_cancel
        self._on_clear_cache = on_clear_cache
        self._on_theme_change = on_theme_change

        # Title is set by App (includes version); fallback here for standalone use
        if not root.title():
            root.title("JJP Asset Decryptor")
        root.geometry("780x720")
        root.minsize(700, 600)

        # Set window icon
        import os
        icon_path = os.path.join(os.path.dirname(__file__), "icon.ico")
        if os.path.isfile(icon_path):
            try:
                root.iconbitmap(icon_path)
            except tk.TclError:
                pass

        # State
        self._start_time = None
        self._timer_id = None
        self._current_theme = initial_theme or self._detect_system_theme()
        self._prereq_state = {}  # name -> passed (bool)

        self._build_ui()
        self._apply_theme(self._current_theme)

    @staticmethod
    def _detect_system_theme():
        """Detect the Windows system theme (dark or light)."""
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            )
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            winreg.CloseKey(key)
            return "light" if value else "dark"
        except Exception:
            return "light"

    def _apply_theme(self, theme):
        """Apply dark or light theme to all widgets."""
        c = _THEMES[theme]
        self._current_theme = theme

        style = ttk.Style()
        style.theme_use("clam")

        # Base style
        style.configure(".", background=c["bg"], foreground=c["fg"],
                         fieldbackground=c["field_bg"], bordercolor=c["border"],
                         troughcolor=c["trough"], selectbackground=c["select_bg"],
                         selectforeground="#ffffff", insertcolor=c["fg"])
        style.configure("TFrame", background=c["bg"])
        style.configure("TLabel", background=c["bg"], foreground=c["fg"])
        style.configure("TLabelframe", background=c["bg"], foreground=c["fg"])
        style.configure("TLabelframe.Label", background=c["bg"], foreground=c["fg"])
        style.configure("TButton", background=c["button"], foreground=c["fg"])
        style.map("TButton",
                  background=[("active", c["accent"]), ("pressed", c["accent"])],
                  foreground=[("active", "#ffffff"), ("pressed", "#ffffff")])
        _icon_base = {"background": c["bg"], "borderwidth": 0, "relief": "flat"}
        style.configure("Sun.TButton", font=("Segoe UI", 14), padding=(4, 0),
                         foreground="#e6a817", **_icon_base)
        style.map("Sun.TButton", background=[("active", c["button"])])
        style.configure("Moon.TButton", font=("Segoe UI", 14), padding=(4, 0),
                         foreground="#7b9fd4", **_icon_base)
        style.map("Moon.TButton", background=[("active", c["button"])])
        style.configure("Help.TButton", font=("Segoe UI", 11), padding=(4, 0),
                         foreground=c["accent"], **_icon_base)
        style.map("Help.TButton", background=[("active", c["button"])])
        style.configure("Trash.TButton", font=("Segoe MDL2 Assets", 12), padding=(4, 0),
                         foreground=c["error"], **_icon_base)
        style.map("Trash.TButton", background=[("active", c["button"])])
        style.configure("TEntry", fieldbackground=c["field_bg"], foreground=c["fg"])
        style.configure("TNotebook", background=c["bg"], bordercolor=c["border"])
        style.configure("TNotebook.Tab", background=c["bg"], foreground=c["fg"],
                         padding=[8, 4])
        style.map("TNotebook.Tab",
                  background=[("selected", c["tab_selected"])],
                  foreground=[("selected", c["accent"])])
        style.configure("Horizontal.TProgressbar",
                         background=c["accent"], troughcolor=c["trough"],
                         bordercolor=c["border"])
        style.configure("Vertical.TScrollbar",
                         background=c["border"], troughcolor=c["trough"],
                         bordercolor=c["border"])
        style.map("Vertical.TScrollbar",
                  background=[("active", c["accent"])])

        # Root window
        self.root.configure(bg=c["bg"])

        # Windows title bar dark/light mode via DWM API
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            dark_value = 1 if theme == "dark" else 0
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
                ctypes.byref(ctypes.c_int(dark_value)),
                ctypes.sizeof(ctypes.c_int),
            )
        except Exception:
            pass

        # Log text widget — always dark background (terminal-style)
        d = _THEMES["dark"]
        self.log_text.configure(
            bg=d["field_bg"], fg=d["fg"],
            insertbackground=d["fg"], selectbackground=d["select_bg"])
        self.log_text.tag_configure("info", foreground=d["fg"])
        self.log_text.tag_configure("error", foreground=d["error"])
        self.log_text.tag_configure("success", foreground=d["success"])
        self.log_text.tag_configure("timestamp", foreground=d["timestamp"])
        self.log_text.tag_configure("link", foreground=d["link"], underline=True)

        # Game label - preserve state
        game_text = self.game_label.cget("text")
        if game_text.startswith("("):
            self.game_label.configure(foreground=c["gray"])
        else:
            self.game_label.configure(foreground=c["fg"])

        # Re-apply prereq indicator colors
        for name, passed in self._prereq_state.items():
            label = self.prereq_labels.get(name)
            if label:
                label.configure(
                    foreground=c["success"] if passed else c["error"])

        # Theme toggle button: yellow sun / blue moon
        if theme == "dark":
            self.theme_btn.configure(text="\u2600", style="Sun.TButton")
            self._theme_tooltip.text = "Switch to light mode"
        else:
            self.theme_btn.configure(text="\u263E", style="Moon.TButton")
            self._theme_tooltip.text = "Switch to dark mode"

    def _toggle_theme(self):
        """Switch between dark and light mode."""
        new_theme = "light" if self._current_theme == "dark" else "dark"
        self._apply_theme(new_theme)
        if self._on_theme_change:
            self._on_theme_change(new_theme)

    def _build_ui(self):
        # Status bar packed first at bottom of root — always visible
        self._build_status_bar(self.root)

        main = ttk.Frame(self.root, padding=8)
        main.pack(fill=tk.BOTH, expand=True)

        # Top bar with icon buttons (right-aligned)
        top_bar = ttk.Frame(main)
        top_bar.pack(fill=tk.X, pady=(0, 2))
        self.help_btn = ttk.Button(top_bar, text="?", width=3,
                                    style="Help.TButton", command=self._show_help)
        self.help_btn.pack(side=tk.RIGHT)
        _Tooltip(self.help_btn, "Help / README", lambda: self._current_theme)
        self.theme_btn = ttk.Button(top_bar, text="", width=3,
                                     command=self._toggle_theme)
        self.theme_btn.pack(side=tk.RIGHT, padx=(0, 4))
        self._theme_tooltip = _Tooltip(self.theme_btn, "", lambda: self._current_theme)
        self.clear_cache_btn = ttk.Button(top_bar, text="\uE74D", width=3,
                                           style="Trash.TButton",
                                           command=self._on_clear_cache)
        self.clear_cache_btn.pack(side=tk.RIGHT, padx=(0, 4))
        _Tooltip(self.clear_cache_btn, "Clear cached images", lambda: self._current_theme)

        self._build_config(main)
        self._build_prerequisites(main)

        # --- Notebook (tabs) ---
        self.notebook = ttk.Notebook(main)
        self.notebook.pack(fill=tk.X, pady=(0, 4))

        decrypt_frame = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(decrypt_frame, text=" Decrypt Assets ")
        self._build_decrypt_tab(decrypt_frame)

        mod_frame = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(mod_frame, text=" Modify Assets ")
        self._build_mod_tab(mod_frame)

        self._build_log(main)

    def _build_config(self, parent):
        cfg_frame = ttk.LabelFrame(parent, text=" Configuration ", padding=6)
        cfg_frame.pack(fill=tk.X, pady=(0, 4))

        # Image file
        row = ttk.Frame(cfg_frame)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text="Game Image:", width=18, anchor=tk.W).pack(side=tk.LEFT)
        self.image_var = tk.StringVar()
        self.image_entry = ttk.Entry(row, textvariable=self.image_var)
        self.image_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        ttk.Button(row, text="Browse...", command=self._browse_image, width=10).pack(side=tk.LEFT)

        # Output folder
        row = ttk.Frame(cfg_frame)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text="Output Folder:", width=18, anchor=tk.W).pack(side=tk.LEFT)
        self.output_var = tk.StringVar()
        self.output_entry = ttk.Entry(row, textvariable=self.output_var)
        self.output_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        ttk.Button(row, text="Browse...", command=self._browse_output, width=10).pack(side=tk.LEFT)

        # Detected game
        row = ttk.Frame(cfg_frame)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text="Detected Game:", width=18, anchor=tk.W).pack(side=tk.LEFT)
        self.game_label = ttk.Label(row, text="(select an image to detect)", foreground="gray")
        self.game_label.pack(side=tk.LEFT)

    def _build_prerequisites(self, parent):
        prereq_frame = ttk.LabelFrame(parent, text=" Prerequisites ", padding=6)
        prereq_frame.pack(fill=tk.X, pady=(0, 4))

        self.prereq_grid = ttk.Frame(prereq_frame)
        self.prereq_grid.pack(fill=tk.X)

        self.prereq_labels = {}
        prereq_names = ["WSL2", "gcc", "usbipd-win", "HASP Dongle", "partclone", "xorriso"]
        for i, name in enumerate(prereq_names):
            col = i % 2
            row_idx = i // 2
            frame = ttk.Frame(self.prereq_grid)
            frame.grid(row=row_idx, column=col, sticky=tk.W, padx=(0, 20), pady=1)
            indicator = ttk.Label(frame, text="[ ? ]", foreground="gray", width=5)
            indicator.pack(side=tk.LEFT)
            ttk.Label(frame, text=name).pack(side=tk.LEFT)
            self.prereq_labels[name] = indicator

        btn_row = ttk.Frame(prereq_frame)
        btn_row.pack(pady=(6, 0))
        self.check_btn = ttk.Button(btn_row, text="Check Prerequisites",
                                     command=self._on_check_prereqs)
        self.check_btn.pack(side=tk.LEFT, padx=4)

    def _build_decrypt_tab(self, parent):
        # Step indicator
        step_row = ttk.Frame(parent)
        step_row.pack(fill=tk.X, pady=(0, 6))
        self.step_labels = []
        for i, phase in enumerate(config.PHASES):
            if i > 0:
                ttk.Label(step_row, text=" > ", foreground="gray").pack(side=tk.LEFT)
            lbl = ttk.Label(step_row, text=f"{i+1}. {phase}", foreground="gray")
            lbl.pack(side=tk.LEFT)
            self.step_labels.append(lbl)

        # Progress bar
        prog_row = ttk.Frame(parent)
        prog_row.pack(fill=tk.X, pady=(0, 6))
        self.progress_label = ttk.Label(prog_row, text="", anchor=tk.E)
        self.progress_label.pack(side=tk.RIGHT)
        self.progress = ttk.Progressbar(prog_row, mode="determinate")
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        # Buttons
        btn_row = ttk.Frame(parent)
        btn_row.pack()
        self.start_btn = ttk.Button(btn_row, text="Start Decryption",
                                     command=self._on_start)
        self.start_btn.pack(side=tk.LEFT, padx=4)
        self.cancel_btn = ttk.Button(btn_row, text="Cancel",
                                      command=self._on_cancel, state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.LEFT, padx=4)

    def _build_mod_tab(self, parent):
        # Description
        ttk.Label(parent,
                  text="Modify files in your Output Folder, then click Apply. "
                       "Only changed files (compared to baseline checksums from "
                       "decryption) will be re-encrypted into the game image. "
                       "A backup of the image is created automatically.",
                  wraplength=700, foreground="gray", justify=tk.LEFT
                  ).pack(anchor=tk.W, pady=(0, 6))

        # Step indicator for mod phases
        step_row = ttk.Frame(parent)
        step_row.pack(fill=tk.X, pady=(0, 6))
        self.mod_step_labels = []
        for i, phase in enumerate(config.MOD_PHASES):
            if i > 0:
                ttk.Label(step_row, text=" > ", foreground="gray").pack(side=tk.LEFT)
            lbl = ttk.Label(step_row, text=f"{i+1}. {phase}", foreground="gray")
            lbl.pack(side=tk.LEFT)
            self.mod_step_labels.append(lbl)

        # Progress bar
        prog_row = ttk.Frame(parent)
        prog_row.pack(fill=tk.X, pady=(0, 6))
        self.mod_progress_label = ttk.Label(prog_row, text="", anchor=tk.E)
        self.mod_progress_label.pack(side=tk.RIGHT)
        self.mod_progress = ttk.Progressbar(prog_row, mode="determinate")
        self.mod_progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        # Apply/Cancel buttons
        btn_row = ttk.Frame(parent)
        btn_row.pack()
        self.mod_apply_btn = ttk.Button(btn_row, text="Apply Modifications",
                                         command=self._on_mod_apply)
        self.mod_apply_btn.pack(side=tk.LEFT, padx=4)
        self.mod_cancel_btn = ttk.Button(btn_row, text="Cancel",
                                          command=self._on_mod_cancel, state=tk.DISABLED)
        self.mod_cancel_btn.pack(side=tk.LEFT, padx=4)

    def _build_log(self, parent):
        log_frame = ttk.LabelFrame(parent, text=" Log Output ", padding=4)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 2))

        log_container = ttk.Frame(log_frame)
        log_container.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(log_container, wrap=tk.WORD, state=tk.DISABLED,
                                font=("Consolas", 9), relief=tk.FLAT, padx=6, pady=4)
        scrollbar = ttk.Scrollbar(log_container, orient=tk.VERTICAL,
                                   command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Tag colors are set by _apply_theme
        self.log_text.tag_configure("info")
        self.log_text.tag_configure("error")
        self.log_text.tag_configure("success")
        self.log_text.tag_configure("timestamp")
        self.log_text.tag_configure("link", underline=True)
        self.log_text.tag_bind("link", "<Button-1>", self._on_log_link_click)
        self.log_text.tag_bind("link", "<Enter>",
                               lambda e: self.log_text.configure(cursor="hand2"))
        self.log_text.tag_bind("link", "<Leave>",
                               lambda e: self.log_text.configure(cursor=""))
        self._log_links = {}  # tag_name -> url

    def _build_status_bar(self, parent):
        status_frame = ttk.Frame(parent, padding=(8, 2))
        status_frame.pack(side=tk.BOTTOM, fill=tk.X)
        self.status_label = ttk.Label(status_frame, text="Ready", anchor=tk.W)
        self.status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.elapsed_label = ttk.Label(status_frame, text="", anchor=tk.E)
        self.elapsed_label.pack(side=tk.RIGHT)

    # --- File browse dialogs ---

    def _browse_image(self):
        path = filedialog.askopenfilename(
            title="Select JJP Game Image (ISO or ext4)",
            filetypes=[
                ("JJP Game Images", "*.iso *.img *.ext4 *.raw"),
                ("ISO Images", "*.iso"),
                ("Disk Images", "*.img *.ext4 *.raw"),
                ("All Files", "*.*"),
            ],
        )
        if path:
            self.image_var.set(path)

    def _browse_output(self):
        path = filedialog.askdirectory(title="Select Output Folder")
        if path:
            self.output_var.set(path)

    # --- Public methods called by App ---

    def append_log(self, text, level="info"):
        """Append a line to the log panel. Must be called from main thread."""
        self.log_text.configure(state=tk.NORMAL)
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] ", "timestamp")
        self.log_text.insert(tk.END, f"{text}\n", level)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def append_log_link(self, text, url):
        """Append a clickable link to the log panel."""
        tag = f"link_{len(self._log_links)}"
        self._log_links[tag] = url
        self.log_text.configure(state=tk.NORMAL)
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] ", "timestamp")
        self.log_text.insert(tk.END, f"{text}\n", ("link", tag))
        self.log_text.tag_bind(tag, "<Button-1>",
                               lambda e, u=url: webbrowser.open(u))
        self.log_text.tag_bind(tag, "<Enter>",
                               lambda e: self.log_text.configure(cursor="hand2"))
        self.log_text.tag_bind(tag, "<Leave>",
                               lambda e: self.log_text.configure(cursor=""))
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _on_log_link_click(self, event):
        """Handle click on a link tag — individual link tags handle their own URLs."""
        pass

    def set_prereq(self, name, passed, message=""):
        """Update a prerequisite indicator."""
        self._prereq_state[name] = passed
        c = _THEMES[self._current_theme]
        label = self.prereq_labels.get(name)
        if label:
            if passed:
                label.configure(text="[OK]", foreground=c["success"])
            else:
                label.configure(text="[  X ]", foreground=c["error"])

    def set_phase(self, phase_index, mode="decrypt"):
        """Highlight the current phase in the step indicator."""
        c = _THEMES[self._current_theme]
        labels = self.step_labels if mode == "decrypt" else self.mod_step_labels
        for i, lbl in enumerate(labels):
            if i < phase_index:
                lbl.configure(foreground=c["success"])
            elif i == phase_index:
                lbl.configure(foreground=c["accent"], font=("TkDefaultFont", 9, "bold"))
            else:
                lbl.configure(foreground=c["gray"], font=("TkDefaultFont", 9))

        # Reset progress bar to indeterminate until the phase sets its own progress
        if mode == "decrypt":
            self.progress.configure(mode="indeterminate")
            self.progress.start(15)
            self.progress_label.configure(text="")
        else:
            self.mod_progress.configure(mode="indeterminate")
            self.mod_progress.start(15)
            self.mod_progress_label.configure(text="")

    def set_progress(self, current, total, description="", mode="decrypt"):
        """Update the progress bar and label."""
        if mode == "decrypt":
            bar = self.progress
            label = self.progress_label
        else:
            bar = self.mod_progress
            label = self.mod_progress_label

        if total > 0:
            bar.stop()
            bar.configure(mode="determinate", maximum=total, value=current)
            pct = int(100 * current / total)
            label.configure(text=f"{pct}%  ({current}/{total})  {description}")
        else:
            bar.configure(mode="indeterminate")
            bar.start(15)
            label.configure(text=description)

    def set_game_name(self, name):
        """Update the detected game label."""
        c = _THEMES[self._current_theme]
        display = config.KNOWN_GAMES.get(name, name)
        self.game_label.configure(text=display, foreground=c["fg"])

    def set_running(self, running, mode="decrypt"):
        """Toggle between running and idle state."""
        if running:
            self.image_entry.configure(state=tk.DISABLED)
            self.output_entry.configure(state=tk.DISABLED)
            self.check_btn.configure(state=tk.DISABLED)
            self.start_btn.configure(state=tk.DISABLED)
            self.mod_apply_btn.configure(state=tk.DISABLED)
            if mode == "decrypt":
                self.cancel_btn.configure(state=tk.NORMAL)
            else:
                self.mod_cancel_btn.configure(state=tk.NORMAL)
            self._start_time = time.time()
            self._update_timer()
        else:
            self.image_entry.configure(state=tk.NORMAL)
            self.output_entry.configure(state=tk.NORMAL)
            self.check_btn.configure(state=tk.NORMAL)
            self.start_btn.configure(state=tk.NORMAL)
            self.cancel_btn.configure(state=tk.DISABLED)
            self.mod_apply_btn.configure(state=tk.NORMAL)
            self.mod_cancel_btn.configure(state=tk.DISABLED)
            # Stop any indeterminate animation and fill to 100%
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=100, value=100)
            self.progress_label.configure(text="100%")
            self.mod_progress.stop()
            self.mod_progress.configure(mode="determinate", maximum=100, value=100)
            self.mod_progress_label.configure(text="100%")
            self._start_time = None
            if self._timer_id:
                self.root.after_cancel(self._timer_id)
                self._timer_id = None

    def set_status(self, text):
        """Update the status bar text."""
        self.status_label.configure(text=text)

    def reset_steps(self, mode="decrypt"):
        """Reset step indicators and progress for the given mode."""
        c = _THEMES[self._current_theme]
        labels = self.step_labels if mode == "decrypt" else self.mod_step_labels
        for lbl in labels:
            lbl.configure(foreground=c["gray"], font=("TkDefaultFont", 9))
        if mode == "decrypt":
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0, maximum=100)
            self.progress_label.configure(text="")
        else:
            self.mod_progress.stop()
            self.mod_progress.configure(mode="determinate", value=0, maximum=100)
            self.mod_progress_label.configure(text="")

    def _show_help(self):
        """Open a window displaying the README."""
        import os, re as _re

        c = _THEMES[self._current_theme]

        readme_path = os.path.join(os.path.dirname(__file__), "..", "README.md")
        try:
            with open(readme_path, "r", encoding="utf-8") as f:
                content = f.read()
        except FileNotFoundError:
            content = "README.md not found."

        win = tk.Toplevel(self.root)
        win.title("JJP Asset Decryptor — Help")
        win.geometry("700x600")
        win.minsize(500, 400)

        # Reuse the app icon
        icon_path = os.path.join(os.path.dirname(__file__), "icon.ico")
        if os.path.isfile(icon_path):
            try:
                win.iconbitmap(icon_path)
            except tk.TclError:
                pass

        frame = ttk.Frame(win, padding=8)
        frame.pack(fill=tk.BOTH, expand=True)

        text = tk.Text(frame, wrap=tk.WORD, state=tk.DISABLED,
                       font=("Consolas", 10), bg=c["field_bg"], fg=c["fg"],
                       insertbackground=c["fg"], selectbackground=c["select_bg"],
                       relief=tk.FLAT, padx=10, pady=8)
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Tags for basic markdown rendering
        text.tag_configure("h1", font=("Consolas", 16, "bold"),
                           foreground=c["accent"], spacing3=6)
        text.tag_configure("h2", font=("Consolas", 13, "bold"),
                           foreground=c["accent"], spacing1=10, spacing3=4)
        text.tag_configure("h3", font=("Consolas", 11, "bold"),
                           foreground=c["accent"], spacing1=8, spacing3=2)
        text.tag_configure("bold", font=("Consolas", 10, "bold"),
                           foreground=c["fg"])
        text.tag_configure("code", font=("Consolas", 10),
                           foreground=c["code_fg"], background=c["code_bg"])
        text.tag_configure("bullet", foreground=c["fg"],
                           lmargin1=20, lmargin2=30)
        text.tag_configure("body", foreground=c["fg"])
        text.tag_configure("link", foreground=c["link"])
        text.tag_configure("table_header", font=("Consolas", 10, "bold"),
                           foreground=c["accent"])

        text.configure(state=tk.NORMAL)

        in_code_block = False
        for line in content.split("\n"):
            if line.startswith("```"):
                in_code_block = not in_code_block
                continue

            if in_code_block:
                text.insert(tk.END, f"  {line}\n", "code")
                continue

            # Headers
            if line.startswith("### "):
                text.insert(tk.END, line[4:] + "\n", "h3")
            elif line.startswith("## "):
                text.insert(tk.END, line[3:] + "\n", "h2")
            elif line.startswith("# "):
                text.insert(tk.END, line[2:] + "\n", "h1")
            # Table separator
            elif _re.match(r'^\|[-| ]+\|$', line):
                continue
            # Table rows
            elif line.startswith("|"):
                cells = [c_.strip() for c_ in line.split("|")[1:-1]]
                row_text = "  ".join(f"{c_:<30}" for c_ in cells)
                if any(c_.startswith("**") for c_ in cells):
                    text.insert(tk.END, row_text + "\n", "table_header")
                else:
                    text.insert(tk.END, row_text + "\n", "body")
            # Bullets
            elif _re.match(r'^(\s*[-*]\s)', line):
                text.insert(tk.END, line + "\n", "bullet")
            # Numbered lists
            elif _re.match(r'^\s*\d+\.\s', line):
                text.insert(tk.END, line + "\n", "bullet")
            else:
                # Inline rendering: bold and inline code
                parts = _re.split(r'(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))', line)
                for part in parts:
                    if part.startswith("**") and part.endswith("**"):
                        text.insert(tk.END, part[2:-2], "bold")
                    elif part.startswith("`") and part.endswith("`"):
                        text.insert(tk.END, part[1:-1], "code")
                    elif _re.match(r'\[([^\]]+)\]\(([^)]+)\)', part):
                        m = _re.match(r'\[([^\]]+)\]\(([^)]+)\)', part)
                        text.insert(tk.END, m.group(1), "link")
                    else:
                        text.insert(tk.END, part, "body")
                text.insert(tk.END, "\n")

        text.configure(state=tk.DISABLED)

    def _update_timer(self):
        """Update the elapsed time display."""
        if self._start_time:
            elapsed = int(time.time() - self._start_time)
            mins, secs = divmod(elapsed, 60)
            self.elapsed_label.configure(text=f"Elapsed: {mins:02d}:{secs:02d}")
            self._timer_id = self.root.after(1000, self._update_timer)
