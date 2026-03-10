"""Main window GUI for JJP Asset Decryptor."""

import os
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog
import time
import webbrowser

from . import config


def _is_admin():
    """Check whether the current process has Administrator privileges."""
    if sys.platform == "win32":
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    else:
        import os
        return os.geteuid() == 0


def _platform_font():
    """Return (sans_font, mono_font) appropriate for the current platform."""
    if sys.platform == "win32":
        return "Segoe UI", "Consolas"
    elif sys.platform == "darwin":
        return "SF Pro Text", "Menlo"
    else:
        return "sans-serif", "monospace"


_SANS_FONT, _MONO_FONT = _platform_font()

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
        self._tip.configure(background=c["tooltip_bg"])
        label = tk.Label(self._tip, text=self.text, background=c["tooltip_bg"],
                         foreground=c["tooltip_fg"], relief="solid", borderwidth=1,
                         font=(_SANS_FONT, 9), padx=6, pady=2,
                         wraplength=400, justify=tk.LEFT)
        label.pack()

    def _hide(self, event=None):
        if self._tip:
            self._tip.destroy()
            self._tip = None


class MainWindow:
    """Single-window tkinter GUI with Decrypt, Modify, and Write tabs."""

    def __init__(self, root, on_check_prereqs, on_start, on_cancel,
                 on_mod_apply=None, on_mod_cancel=None,
                 on_theme_change=None, initial_theme=None,
                 on_install_prereqs=None,
                 on_import=None, on_export=None):
        self.root = root
        self._on_check_prereqs = on_check_prereqs
        self._on_start = on_start
        self._on_cancel = on_cancel
        self._on_mod_apply = on_mod_apply
        self._on_mod_cancel = on_mod_cancel
        self._on_theme_change = on_theme_change
        self._on_install_prereqs = on_install_prereqs
        self._on_import = on_import
        self._on_export = on_export

        # Title is set by App (includes version); fallback here for standalone use
        if not root.title():
            root.title("JJP Asset Decryptor")
        root.geometry("780x920")
        root.minsize(700, 660)

        # Set window icon
        import os
        if sys.platform == "win32":
            icon_path = os.path.join(os.path.dirname(__file__), "icon.ico")
            if os.path.isfile(icon_path):
                try:
                    root.iconbitmap(icon_path)
                except tk.TclError:
                    pass
        else:
            icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
            if os.path.isfile(icon_path):
                try:
                    icon_img = tk.PhotoImage(file=icon_path)
                    root.iconphoto(True, icon_img)
                    self._icon_img = icon_img  # prevent GC
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
        """Detect the system theme (dark or light) on any platform."""
        if sys.platform == "win32":
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
        elif sys.platform == "darwin":
            try:
                import subprocess
                result = subprocess.run(
                    ["defaults", "read", "-g", "AppleInterfaceStyle"],
                    capture_output=True, text=True, timeout=5,
                )
                return "dark" if "Dark" in result.stdout else "light"
            except Exception:
                return "light"
        else:
            # Linux — try gsettings (GNOME)
            try:
                import subprocess
                result = subprocess.run(
                    ["gsettings", "get", "org.gnome.desktop.interface",
                     "color-scheme"],
                    capture_output=True, text=True, timeout=5,
                )
                return "dark" if "dark" in result.stdout.lower() else "light"
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
        style.configure("Sun.TButton", font=(_SANS_FONT, 14), padding=(4, 0),
                         foreground="#e6a817", **_icon_base)
        style.map("Sun.TButton", background=[("active", c["button"])])
        style.configure("Moon.TButton", font=(_SANS_FONT, 14), padding=(4, 0),
                         foreground="#7b9fd4", **_icon_base)
        style.map("Moon.TButton", background=[("active", c["button"])])
        style.configure("Help.TButton", font=(_SANS_FONT, 11), padding=(4, 0),
                         foreground=c["accent"], **_icon_base)
        style.map("Help.TButton", background=[("active", c["button"])])
        style.configure("TEntry", fieldbackground=c["field_bg"], foreground=c["fg"])
        style.configure("TCombobox", fieldbackground=c["field_bg"],
                         foreground=c["fg"], background=c["button"],
                         selectbackground=c["select_bg"],
                         selectforeground="#ffffff")
        style.map("TCombobox",
                  fieldbackground=[("readonly", c["field_bg"])],
                  foreground=[("readonly", c["fg"])],
                  background=[("readonly", c["button"])])
        # Style the dropdown listbox (not reachable via ttk.Style)
        self.root.option_add("*TCombobox*Listbox.background", c["field_bg"])
        self.root.option_add("*TCombobox*Listbox.foreground", c["fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", c["select_bg"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        style.configure("TNotebook", background=c["bg"], bordercolor=c["border"])
        style.configure("TNotebook.Tab", background=c["bg"], foreground=c["fg"],
                         padding=[8, 4])
        style.map("TNotebook.Tab",
                  background=[("selected", c["tab_selected"])],
                  foreground=[("selected", c["accent"])])
        style.configure("TRadiobutton", background=c["bg"], foreground=c["fg"])
        style.map("TRadiobutton",
                  background=[("active", c["bg"])],
                  foreground=[("active", c["accent"])])
        style.configure("TCheckbutton", background=c["bg"], foreground=c["fg"])
        style.map("TCheckbutton",
                  background=[("active", c["bg"])],
                  foreground=[("active", c["accent"])])
        style.configure("Horizontal.TProgressbar",
                         background=c["accent"], troughcolor=c["trough"],
                         bordercolor=c["border"])
        style.configure("Vertical.TScrollbar",
                         background=c["border"], troughcolor=c["trough"],
                         bordercolor=c["border"])
        style.map("Vertical.TScrollbar",
                  background=[("active", c["accent"])])
        style.configure("Treeview",
                         background=c["field_bg"], foreground=c["fg"],
                         fieldbackground=c["field_bg"],
                         bordercolor=c["border"])
        style.map("Treeview",
                  background=[("selected", c["select_bg"])],
                  foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading",
                         background=c["button"], foreground=c["fg"],
                         bordercolor=c["border"])

        # Root window
        self.root.configure(bg=c["bg"])

        # Windows title bar dark/light mode via DWM API
        if sys.platform == "win32":
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

        # Re-apply prereq indicator colors
        for name, passed in self._prereq_state.items():
            label = self.prereq_labels.get(name)
            if label:
                label.configure(
                    foreground=c["success"] if passed else c["error"])

        # SSD warning labels
        for w in (self._decrypt_ssd_warning, self._write_ssd_warning,
                  self._decrypt_admin_warning, self._write_admin_warning):
            w.configure(foreground=c["error"])

        # Description labels
        for w in (self._decrypt_desc, self._decrypt_ssd_desc,
                  self._modify_desc, self._write_desc,
                  self._write_ssd_desc):
            w.configure(foreground=c["gray"])

        # File tree tag colors
        for tree in [getattr(self, 'write_tree', None)]:
            if tree is None:
                continue
            tree.tag_configure("modified",
                foreground=c.get("accent", "#1976D2"))
            tree.tag_configure("ok",
                foreground=c.get("success", "#2E7D32"))
            tree.tag_configure("trimmed",
                foreground="#E65100" if theme == "light" else "#FF9800")
            tree.tag_configure("padded",
                foreground="#1565C0" if theme == "light" else "#64B5F6")
            tree.tag_configure("converted",
                foreground="#6A1B9A" if theme == "light" else "#CE93D8")
            tree.tag_configure("warning",
                foreground=c.get("error", "#C62828"))

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

        self._build_prerequisites(main)

        # --- Notebook (tabs) ---
        # Notebook uses fill=X only so it takes its natural height.
        # We dynamically resize the notebook height on tab change so
        # that shorter tabs don't waste space.  Log output fills all
        # remaining vertical space (like CSS flex: 1).
        self.notebook = ttk.Notebook(main)
        self.notebook.pack(fill=tk.X, pady=(0, 4))

        decrypt_frame = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(decrypt_frame, text=" Decrypt ")
        self._build_decrypt_tab(decrypt_frame)

        write_frame = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(write_frame, text=" Write ")
        self._build_write_tab(write_frame)

        modpack_frame = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(modpack_frame, text=" Mod Pack ")
        self._build_modpack_tab(modpack_frame)

        # Resize notebook to fit current tab on switch
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.root.after_idle(self._on_tab_changed)

        # --- Log Output (fills remaining space) ---
        self._build_log(main)

    def _build_prerequisites(self, parent):
        prereq_frame = ttk.LabelFrame(parent, text=" Prerequisites ", padding=6)
        prereq_frame.pack(fill=tk.X, pady=(0, 4))

        self.prereq_grid = ttk.Frame(prereq_frame)
        self.prereq_grid.pack(fill=tk.X)

        self.prereq_labels = {}
        prereq_names = config.PREREQ_NAMES
        for i, name in enumerate(prereq_names):
            col = i % 6
            row_idx = i // 6
            frame = ttk.Frame(self.prereq_grid)
            frame.grid(row=row_idx, column=col, sticky=tk.W, padx=(0, 12), pady=1)
            indicator = ttk.Label(frame, text="[ ? ]", foreground="gray", width=5)
            indicator.pack(side=tk.LEFT)
            ttk.Label(frame, text=name).pack(side=tk.LEFT)
            self.prereq_labels[name] = indicator

        btn_frame = ttk.Frame(self.prereq_grid)
        btn_frame.grid(row=0, column=len(prereq_names), sticky=tk.E,
                        padx=(12, 0))
        self.check_btn = ttk.Button(btn_frame, text="Check",
                                     command=self._on_check_prereqs)
        self.check_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.install_btn = ttk.Button(btn_frame, text="Install Missing",
                                       command=self._on_install_prereqs,
                                       state=tk.DISABLED)
        self.install_btn.pack(side=tk.LEFT)
        self.prereq_grid.columnconfigure(len(prereq_names), weight=1)

    # ------------------------------------------------------------------
    # Decrypt tab
    # ------------------------------------------------------------------

    def _build_decrypt_tab(self, parent):
        c = _THEMES[self._current_theme]

        # Radio toggle: From ISO / From SSD
        radio_row = ttk.Frame(parent)
        radio_row.pack(fill=tk.X, pady=(0, 6))
        self._decrypt_source_var = tk.StringVar(value="iso")
        ttk.Radiobutton(radio_row, text="From ISO",
                        variable=self._decrypt_source_var, value="iso"
                        ).pack(side=tk.LEFT, padx=(0, 16))
        ttk.Radiobutton(radio_row, text="From SSD",
                        variable=self._decrypt_source_var, value="ssd"
                        ).pack(side=tk.LEFT)

        # --- Per-tab config fields ---
        cfg_frame = ttk.LabelFrame(parent, text=" Configuration ", padding=6)
        cfg_frame.pack(fill=tk.X, pady=(0, 6))

        _tf = lambda: self._current_theme

        # Game Image (ISO mode)
        self._decrypt_image_row = ttk.Frame(cfg_frame)
        self._decrypt_image_row.pack(fill=tk.X, pady=2)
        lbl = ttk.Label(self._decrypt_image_row, text="Game Image:", width=20,
                        anchor=tk.W)
        lbl.pack(side=tk.LEFT)
        _Tooltip(lbl, "Clonezilla ISO or raw ext4 image \u2014 download "
                 "full installs from marketing.jerseyjackpinball.com/downloads/", _tf)
        self.image_var = tk.StringVar()
        self.image_entry = ttk.Entry(self._decrypt_image_row,
                                      textvariable=self.image_var)
        self.image_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        ttk.Button(self._decrypt_image_row, text="Browse...",
                   command=self._browse_image, width=10).pack(side=tk.LEFT)

        # Game SSD (SSD mode)
        self._decrypt_ssd_row = ttk.Frame(cfg_frame)
        lbl = ttk.Label(self._decrypt_ssd_row, text="Game SSD:", width=20,
                        anchor=tk.W)
        lbl.pack(side=tk.LEFT)
        _Tooltip(lbl, "The JJP game SSD connected via USB enclosure", _tf)
        self.ssd_device_var = tk.StringVar()
        self.ssd_device_combo = ttk.Combobox(
            self._decrypt_ssd_row, textvariable=self.ssd_device_var,
            state="readonly")
        self.ssd_device_combo.pack(side=tk.LEFT, fill=tk.X, expand=True,
                                    padx=(0, 4))
        self.ssd_device_combo.bind(
            "<<ComboboxSelected>>", self._on_ssd_device_selected)
        ttk.Button(self._decrypt_ssd_row, text="Refresh",
                   command=self._ssd_refresh_devices, width=10).pack(side=tk.LEFT)

        # Output Folder
        self._decrypt_output_row = ttk.Frame(cfg_frame)
        self._decrypt_output_row.pack(fill=tk.X, pady=2)
        lbl = ttk.Label(self._decrypt_output_row, text="Output Folder:", width=20,
                        anchor=tk.W)
        lbl.pack(side=tk.LEFT)
        _Tooltip(lbl, "Where decrypted game files will be saved \u2014 "
                 "you'll edit files here", _tf)
        self.output_var = tk.StringVar()
        self.output_entry = ttk.Entry(self._decrypt_output_row,
                                       textvariable=self.output_var)
        self.output_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        ttk.Button(self._decrypt_output_row, text="Browse...",
                   command=self._browse_output, width=10).pack(side=tk.LEFT)

        self._decrypt_cfg_frame = cfg_frame

        # Conditional content area
        self._decrypt_content_area = ttk.Frame(parent)
        self._decrypt_content_area.pack(fill=tk.X)

        self._decrypt_iso_frame = ttk.Frame(self._decrypt_content_area)
        self._decrypt_ssd_frame = ttk.Frame(self._decrypt_content_area)

        # ISO sub-section (initially shown)
        self._decrypt_desc = ttk.Label(
            self._decrypt_iso_frame,
            text="Decrypt game assets from an ISO image to editable "
                 "files in your Output Folder.",
            foreground=c["gray"], wraplength=700, justify=tk.LEFT)
        self._decrypt_desc.pack(anchor=tk.W, pady=(0, 6))

        # SSD sub-section
        self._decrypt_ssd_warning = ttk.Label(
            self._decrypt_ssd_frame,
            text=("\u26A0  Remove the SSD from the pinball machine before "
                  "connecting. Always keep the original ISO as a backup."),
            foreground=c["error"], wraplength=700, justify=tk.LEFT)
        self._decrypt_ssd_warning.pack(anchor=tk.W, pady=(0, 6))

        self._decrypt_admin_warning = ttk.Label(
            self._decrypt_ssd_frame,
            text="\u26A0  SSD mode requires Run as Administrator "
                 "(right-click the app \u2192 Run as administrator).",
            foreground=c["error"], wraplength=700, justify=tk.LEFT)
        if not _is_admin():
            self._decrypt_admin_warning.pack(anchor=tk.W, pady=(0, 6))

        self._decrypt_ssd_desc = ttk.Label(
            self._decrypt_ssd_frame,
            text="Decrypt game assets directly from the SSD to editable "
                 "files in your Output Folder.",
            foreground=c["gray"], wraplength=700, justify=tk.LEFT)
        self._decrypt_ssd_desc.pack(anchor=tk.W, pady=(0, 6))

        # Show ISO frame initially
        self._decrypt_iso_frame.pack(fill=tk.X, in_=self._decrypt_content_area)

        # Options — what to extract
        opts_label = ttk.Label(parent, text="Extract:", foreground=c["gray"])
        opts_label.pack(anchor=tk.W, pady=(0, 2))
        opts_row = ttk.Frame(parent)
        opts_row.pack(fill=tk.X, pady=(0, 6))
        self.extract_graphics_var = tk.BooleanVar(value=True)
        self.extract_graphics_chk = ttk.Checkbutton(
            opts_row, text="Graphics",
            variable=self.extract_graphics_var)
        self.extract_graphics_chk.pack(side=tk.LEFT, padx=(0, 12))
        _Tooltip(self.extract_graphics_chk,
                 "Decrypt and extract graphics assets (images, videos, "
                 "animations, fonts).",
                 lambda: self._current_theme)
        self.extract_sounds_var = tk.BooleanVar(value=True)
        self.extract_sounds_chk = ttk.Checkbutton(
            opts_row, text="Sounds",
            variable=self.extract_sounds_var)
        self.extract_sounds_chk.pack(side=tk.LEFT, padx=(0, 12))
        _Tooltip(self.extract_sounds_chk,
                 "Decrypt and extract sound assets (music, sound effects, "
                 "voice clips).",
                 lambda: self._current_theme)
        self.full_dump_var = tk.BooleanVar(value=False)
        self.full_dump_chk = ttk.Checkbutton(
            opts_row, text="File System",
            variable=self.full_dump_var)
        self.full_dump_chk.pack(side=tk.LEFT)
        _Tooltip(self.full_dump_chk,
                 "Copy the entire ext4 partition (minus encrypted assets) to "
                 "a 'system/' subfolder. Includes game binary, bash scripts, "
                 "shared libraries, OS configs, and more. These files are "
                 "unencrypted and can be modified freely.",
                 lambda: self._current_theme)

        # Step indicators
        self._decrypt_step_row = ttk.Frame(parent)
        self._decrypt_step_row.pack(fill=tk.X, pady=(0, 6))
        self.decrypt_step_labels = []
        self._build_step_labels(self._decrypt_step_row, config.STANDALONE_PHASES,
                                self.decrypt_step_labels)

        # Progress bar
        prog_row = ttk.Frame(parent)
        prog_row.pack(fill=tk.X, pady=(0, 6))
        self.decrypt_progress_label = ttk.Label(prog_row, text="", anchor=tk.E)
        self.decrypt_progress_label.pack(side=tk.RIGHT)
        self.decrypt_progress = ttk.Progressbar(prog_row, mode="determinate")
        self.decrypt_progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        # Buttons
        btn_row = ttk.Frame(parent)
        btn_row.pack()
        self.decrypt_start_btn = ttk.Button(btn_row, text="Start Decryption",
                                             command=self._on_start)
        self.decrypt_start_btn.pack(side=tk.LEFT, padx=4)
        self.decrypt_cancel_btn = ttk.Button(btn_row, text="Cancel",
                                              command=self._on_cancel,
                                              state=tk.DISABLED)
        self.decrypt_cancel_btn.pack(side=tk.LEFT, padx=4)

        # Bind radio changes
        self._decrypt_source_var.trace_add("write", self._on_decrypt_source_changed)

    def _on_decrypt_source_changed(self, *_args):
        """Show/hide widgets when Decrypt source radio changes."""
        source = self._decrypt_source_var.get()
        self._decrypt_iso_frame.pack_forget()
        self._decrypt_ssd_frame.pack_forget()

        # Show/hide config rows based on mode
        self._decrypt_image_row.pack_forget()
        self._decrypt_ssd_row.pack_forget()
        self._decrypt_output_row.pack_forget()

        if source == "iso":
            self._decrypt_image_row.pack(fill=tk.X, pady=2,
                in_=self._decrypt_cfg_frame)
            self._decrypt_output_row.pack(fill=tk.X, pady=2,
                in_=self._decrypt_cfg_frame)
            self._decrypt_iso_frame.pack(fill=tk.X,
                in_=self._decrypt_content_area)
            phases = config.STANDALONE_PHASES
        else:  # ssd
            self._decrypt_ssd_row.pack(fill=tk.X, pady=2,
                in_=self._decrypt_cfg_frame)
            self._decrypt_output_row.pack(fill=tk.X, pady=2,
                in_=self._decrypt_cfg_frame)
            self._decrypt_ssd_frame.pack(fill=tk.X,
                in_=self._decrypt_content_area)
            phases = config.DIRECT_SSD_PHASES
            # Auto-refresh device list when switching to SSD mode
            if not getattr(self, '_ssd_devices', None):
                self._ssd_refresh_devices()

        self._build_step_labels(self._decrypt_step_row, phases,
                                self.decrypt_step_labels)

        # Re-fit notebook height
        self._on_tab_changed()

    # ------------------------------------------------------------------
    # Modify tab
    # ------------------------------------------------------------------

    def _build_modpack_tab(self, parent):
        c = _THEMES[self._current_theme]

        # --- Config: Input Folder ---
        cfg_frame = ttk.LabelFrame(parent, text=" Configuration ", padding=6)
        cfg_frame.pack(fill=tk.X, pady=(0, 6))

        _tf = lambda: self._current_theme

        input_row = ttk.Frame(cfg_frame)
        input_row.pack(fill=tk.X, pady=2)
        lbl = ttk.Label(input_row, text="Game Assets:", width=20, anchor=tk.W)
        lbl.pack(side=tk.LEFT)
        _Tooltip(lbl, "Folder containing decrypted game files "
                 "(set automatically by the Decrypt tab)", _tf)
        self.modify_input_var = self.output_var  # same folder as Decrypt output
        self.modify_input_entry = ttk.Entry(input_row,
                                             textvariable=self.modify_input_var)
        self.modify_input_entry.pack(side=tk.LEFT, fill=tk.X, expand=True,
                                      padx=(0, 4))
        ttk.Button(input_row, text="Browse...",
                   command=self._browse_modify_input, width=10).pack(side=tk.LEFT)

        self._modify_desc = ttk.Label(
            parent,
            text="Export your modified files as a shareable mod pack ZIP, "
                 "or import a mod pack from another user.\n"
                 "You must decrypt the game assets first (Decrypt tab) "
                 "before exporting or importing mods.",
            foreground=c["gray"], wraplength=700, justify=tk.LEFT)
        self._modify_desc.pack(anchor=tk.W, pady=(0, 6))

        # Export / Import buttons
        btn_row = ttk.Frame(parent)
        btn_row.pack(fill=tk.X, pady=(0, 6))
        self.modify_export_btn = ttk.Button(btn_row, text="Export Mod Pack...",
                                             command=self._on_export)
        self.modify_export_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.modify_import_btn = ttk.Button(btn_row, text="Import Mod Pack...",
                                             command=self._on_import)
        self.modify_import_btn.pack(side=tk.LEFT)

    def _browse_modify_input(self):
        path = filedialog.askdirectory(
            title="Select Mod Folder (decrypted game assets)")
        if path:
            self.modify_input_var.set(path)

    # ------------------------------------------------------------------
    # Write tab
    # ------------------------------------------------------------------

    def _build_write_tab(self, parent):
        c = _THEMES[self._current_theme]

        # Radio toggle: Build USB ISO / Write to SSD
        radio_row = ttk.Frame(parent)
        radio_row.pack(fill=tk.X, pady=(0, 6))
        self._write_method_var = tk.StringVar(value="iso")
        ttk.Radiobutton(radio_row, text="Build USB ISO",
                        variable=self._write_method_var, value="iso"
                        ).pack(side=tk.LEFT, padx=(0, 16))
        ttk.Radiobutton(radio_row, text="Write to SSD",
                        variable=self._write_method_var, value="ssd"
                        ).pack(side=tk.LEFT)

        # --- Per-tab config fields ---
        cfg_frame = ttk.LabelFrame(parent, text=" Configuration ", padding=6)
        cfg_frame.pack(fill=tk.X, pady=(0, 6))

        _tf = lambda: self._current_theme

        # Input Folder
        self._write_input_row = ttk.Frame(cfg_frame)
        self._write_input_row.pack(fill=tk.X, pady=2)
        lbl = ttk.Label(self._write_input_row, text="Game Assets:", width=20,
                        anchor=tk.W)
        lbl.pack(side=tk.LEFT)
        _Tooltip(lbl, "Folder containing your modified game files "
                 "(set automatically by the Decrypt tab)", _tf)
        self.write_input_var = self.output_var  # same folder as Decrypt output
        self.write_input_entry = ttk.Entry(self._write_input_row,
                                            textvariable=self.write_input_var)
        self.write_input_entry.pack(side=tk.LEFT, fill=tk.X, expand=True,
                                     padx=(0, 4))
        ttk.Button(self._write_input_row, text="Browse...",
                   command=self._browse_write_input, width=10).pack(side=tk.LEFT)

        # Original Game Image (ISO mode only)
        self._write_orig_image_row = ttk.Frame(cfg_frame)
        self._write_orig_image_row.pack(fill=tk.X, pady=2)
        lbl = ttk.Label(self._write_orig_image_row, text="Original Game Image:",
                        width=20, anchor=tk.W)
        lbl.pack(side=tk.LEFT)
        _Tooltip(lbl, "The unmodified Clonezilla ISO \u2014 needed as a "
                 "base to build the new image", _tf)
        self.write_orig_image_var = self.image_var  # same as Decrypt tab
        self.write_orig_image_entry = ttk.Entry(
            self._write_orig_image_row,
            textvariable=self.write_orig_image_var)
        self.write_orig_image_entry.pack(side=tk.LEFT, fill=tk.X,
                                          expand=True, padx=(0, 4))
        ttk.Button(self._write_orig_image_row, text="Browse...",
                   command=self._browse_write_orig_image,
                   width=10).pack(side=tk.LEFT)

        # Output Image (ISO mode only)
        self._write_output_row = ttk.Frame(cfg_frame)
        self._write_output_row.pack(fill=tk.X, pady=2)
        lbl = ttk.Label(self._write_output_row, text="Output Image:", width=20,
                        anchor=tk.W)
        lbl.pack(side=tk.LEFT)
        _Tooltip(lbl, "Where to save the modified ISO file for "
                 "USB installation", _tf)
        self.write_output_var = tk.StringVar()
        self.write_output_entry = ttk.Entry(self._write_output_row,
                                             textvariable=self.write_output_var)
        self.write_output_entry.pack(side=tk.LEFT, fill=tk.X, expand=True,
                                      padx=(0, 4))
        ttk.Button(self._write_output_row, text="Browse...",
                   command=self._browse_write_output, width=10).pack(side=tk.LEFT)

        # Game SSD (SSD mode only)
        self._write_ssd_row = ttk.Frame(cfg_frame)
        lbl = ttk.Label(self._write_ssd_row, text="Game SSD:", width=20,
                        anchor=tk.W)
        lbl.pack(side=tk.LEFT)
        _Tooltip(lbl, "The JJP game SSD connected via USB enclosure", _tf)
        self.write_ssd_var = tk.StringVar()
        self.write_ssd_combo = ttk.Combobox(
            self._write_ssd_row, textvariable=self.write_ssd_var,
            state="readonly")
        self.write_ssd_combo.pack(side=tk.LEFT, fill=tk.X, expand=True,
                                   padx=(0, 4))
        self.write_ssd_combo.bind(
            "<<ComboboxSelected>>", self._on_write_ssd_device_selected)
        ttk.Button(self._write_ssd_row, text="Refresh",
                   command=self._ssd_refresh_write_devices,
                   width=10).pack(side=tk.LEFT)

        # Options row
        opts_row = ttk.Frame(cfg_frame)
        opts_row.pack(fill=tk.X, pady=(4, 0))
        self.skip_duration_var = tk.BooleanVar(value=False)
        cb = ttk.Checkbutton(opts_row, text="Keep original audio length",
                             variable=self.skip_duration_var)
        cb.pack(side=tk.LEFT)
        _Tooltip(cb, "Replacement audio files keep their original length "
                 "instead of being trimmed or padded to match the game's "
                 "original duration. Enabling this may cause boot loops "
                 "on some games.",
                 _tf)

        self._write_cfg_frame = cfg_frame

        # Conditional content area
        self._write_content_area = ttk.Frame(parent)
        self._write_content_area.pack(fill=tk.X)

        self._write_iso_frame = ttk.Frame(self._write_content_area)
        self._write_ssd_frame = ttk.Frame(self._write_content_area)

        # ISO sub-section
        self._write_desc = ttk.Label(
            self._write_iso_frame,
            text="Re-encrypt changed files into the game image and build "
                 "a new ISO for USB drive installation.",
            foreground=c["gray"], wraplength=700, justify=tk.LEFT)
        self._write_desc.pack(anchor=tk.W, pady=(0, 6))

        # SSD sub-section
        self._write_ssd_warning = ttk.Label(
            self._write_ssd_frame,
            text=("\u26A0  Remove the SSD from the pinball machine before "
                  "connecting. Always keep the original ISO as a backup."),
            foreground=c["error"], wraplength=700, justify=tk.LEFT)
        self._write_ssd_warning.pack(anchor=tk.W, pady=(0, 6))

        self._write_admin_warning = ttk.Label(
            self._write_ssd_frame,
            text="\u26A0  SSD mode requires Run as Administrator "
                 "(right-click the app \u2192 Run as administrator).",
            foreground=c["error"], wraplength=700, justify=tk.LEFT)
        if not _is_admin():
            self._write_admin_warning.pack(anchor=tk.W, pady=(0, 6))

        self._write_ssd_desc = ttk.Label(
            self._write_ssd_frame,
            text="Re-encrypt changed files and write them directly "
                 "to the game SSD.",
            foreground=c["gray"], wraplength=700, justify=tk.LEFT)
        self._write_ssd_desc.pack(anchor=tk.W, pady=(0, 6))

        # Show ISO frame initially
        self._write_iso_frame.pack(fill=tk.X, in_=self._write_content_area)

        # --- File tree preview of modified files ---
        write_tree_frame = ttk.LabelFrame(
            parent, text=" Modified Files Preview ", padding=4)
        write_tree_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        self.write_tree = ttk.Treeview(
            write_tree_frame, columns=("type", "status"),
            height=8, selectmode="browse")
        self.write_tree.heading("#0", text="File", anchor=tk.W)
        self.write_tree.heading("type", text="Type", anchor=tk.W)
        self.write_tree.heading("status", text="Status", anchor=tk.W)
        self.write_tree.column("#0", width=400, minwidth=200)
        self.write_tree.column("type", width=60, minwidth=40)
        self.write_tree.column("status", width=200, minwidth=100)

        write_tree_scroll = ttk.Scrollbar(
            write_tree_frame, orient=tk.VERTICAL,
            command=self.write_tree.yview)
        self.write_tree.configure(yscrollcommand=write_tree_scroll.set)
        write_tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.write_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.write_tree.tag_configure("modified", foreground="#1976D2")
        self.write_tree.tag_configure("ok", foreground="#2E7D32")
        self.write_tree.tag_configure("trimmed", foreground="#E65100")
        self.write_tree.tag_configure("padded", foreground="#1565C0")
        self.write_tree.tag_configure("converted", foreground="#6A1B9A")
        self.write_tree.tag_configure("warning", foreground="#C62828")

        self._write_tree_items = {}
        self._write_scan_id = None  # track background scan

        self._write_tree_empty = ttk.Label(
            write_tree_frame,
            text="Switch to this tab to scan for modified files",
            foreground="gray", anchor=tk.CENTER, justify=tk.CENTER)
        self._write_tree_empty.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

        # Step indicators
        self._write_step_row = ttk.Frame(parent)
        self._write_step_row.pack(fill=tk.X, pady=(0, 6))
        self.write_step_labels = []
        self._build_step_labels(self._write_step_row,
                                config.STANDALONE_MOD_PHASES,
                                self.write_step_labels)

        # Progress bar
        prog_row = ttk.Frame(parent)
        prog_row.pack(fill=tk.X, pady=(0, 6))
        self.write_progress_label = ttk.Label(prog_row, text="", anchor=tk.E)
        self.write_progress_label.pack(side=tk.RIGHT)
        self.write_progress = ttk.Progressbar(prog_row, mode="determinate")
        self.write_progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        # Action buttons
        btn_row = ttk.Frame(parent)
        btn_row.pack()
        self.write_apply_btn = ttk.Button(btn_row, text="Apply Modifications",
                                           command=self._on_mod_apply)
        self.write_apply_btn.pack(side=tk.LEFT, padx=4)
        self.write_cancel_btn = ttk.Button(btn_row, text="Cancel",
                                            command=self._on_mod_cancel,
                                            state=tk.DISABLED)
        self.write_cancel_btn.pack(side=tk.LEFT, padx=4)

        # Bind radio changes
        self._write_method_var.trace_add("write", self._on_write_method_changed)

    def _on_write_method_changed(self, *_args):
        """Show/hide widgets when Write method radio changes."""
        method = self._write_method_var.get()

        # Hide all conditional frames
        self._write_iso_frame.pack_forget()
        self._write_ssd_frame.pack_forget()

        # Show/hide config rows based on mode
        self._write_input_row.pack_forget()
        self._write_orig_image_row.pack_forget()
        self._write_output_row.pack_forget()
        self._write_ssd_row.pack_forget()

        if method == "iso":
            self._write_input_row.pack(fill=tk.X, pady=2,
                in_=self._write_cfg_frame)
            self._write_orig_image_row.pack(fill=tk.X, pady=2,
                in_=self._write_cfg_frame)
            self._write_output_row.pack(fill=tk.X, pady=2,
                in_=self._write_cfg_frame)
            self._write_iso_frame.pack(fill=tk.X,
                in_=self._write_content_area)
            phases = config.STANDALONE_MOD_PHASES
            self.write_apply_btn.configure(text="Apply Modifications")
        else:  # ssd
            self._write_input_row.pack(fill=tk.X, pady=2,
                in_=self._write_cfg_frame)
            self._write_ssd_row.pack(fill=tk.X, pady=2,
                in_=self._write_cfg_frame)
            self._write_ssd_frame.pack(fill=tk.X,
                in_=self._write_content_area)
            phases = config.DIRECT_SSD_MOD_PHASES
            self.write_apply_btn.configure(text="Apply Modifications")
            # Auto-refresh device list when switching to SSD mode
            if not getattr(self, '_write_ssd_devices', None):
                self._ssd_refresh_write_devices()

        self._build_step_labels(self._write_step_row, phases,
                                self.write_step_labels)

        # Re-fit notebook height
        self._on_tab_changed()

    # ------------------------------------------------------------------
    # Notebook dynamic height
    # ------------------------------------------------------------------

    def _on_tab_changed(self, event=None):
        """Resize notebook height to fit the currently selected tab."""
        tab = self.notebook.select()
        if tab:
            widget = self.notebook.nametowidget(tab)
            widget.update_idletasks()
            self.notebook.configure(height=widget.winfo_reqheight())
            # Auto-scan for modified files when Write tab is selected
            if self.notebook.index(tab) == 1:
                self.scan_write_preview()

    # ------------------------------------------------------------------
    # SSD device management
    # ------------------------------------------------------------------

    def _ssd_refresh_devices(self):
        """Refresh the device dropdown with detected disks."""
        from .executor import list_disk_devices
        self._ssd_devices = list_disk_devices()
        values = [str(d) for d in self._ssd_devices]
        if not values:
            values = ["(no drives detected \u2014 connect SSD and click Refresh)"]
            self._ssd_devices = []
        self.ssd_device_combo.configure(values=values)
        if values:
            self.ssd_device_combo.current(0)

    def get_ssd_device(self):
        """Return the selected DiskInfo from Decrypt tab, or None."""
        if not hasattr(self, '_ssd_devices'):
            return None
        idx = self.ssd_device_combo.current()
        if 0 <= idx < len(self._ssd_devices):
            return self._ssd_devices[idx]
        return None

    def _on_ssd_device_selected(self, event=None):
        """Confirm when user selects a device on the Decrypt tab."""
        self._confirm_ssd_selection(
            self.ssd_device_combo, "_ssd_devices", "read from")

    def _on_write_ssd_device_selected(self, event=None):
        """Confirm when user selects a device on the Write tab."""
        self._confirm_ssd_selection(
            self.write_ssd_combo, "_write_ssd_devices", "write to")

    def _confirm_ssd_selection(self, combo, devices_attr, action_verb):
        """Show a confirmation dialog after the user selects an SSD."""
        from tkinter import messagebox

        devices = getattr(self, devices_attr, [])
        idx = combo.current()
        if idx < 0 or idx >= len(devices):
            return

        device = devices[idx]
        confirmed = messagebox.askyesno(
            "Confirm Drive Selection",
            f"You selected:\n\n"
            f"  Model:   {device.model}\n"
            f"  Size:      {device.size_display}\n"
            f"  Bus:       {device.bus_type}\n"
            f"  Device:  {device.device_id}\n\n"
            f"This tool will {action_verb} this drive.\n\n"
            f"Is this the correct JJP game SSD?",
        )
        if not confirmed:
            combo.set("")

    def get_decrypt_source(self):
        """Return 'iso' or 'ssd' based on the Decrypt tab radio."""
        return self._decrypt_source_var.get()

    def get_write_method(self):
        """Return 'iso' or 'ssd' based on the Write tab radio."""
        return self._write_method_var.get()

    # ------------------------------------------------------------------
    # Log output
    # ------------------------------------------------------------------

    def _build_log(self, parent):
        log_frame = ttk.LabelFrame(parent, text=" Log Output ", padding=4)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 2))

        log_container = ttk.Frame(log_frame)
        log_container.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(log_container, wrap=tk.WORD, state=tk.DISABLED,
                                font=(_MONO_FONT, 9), relief=tk.FLAT, padx=6, pady=4)
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
                ("All Files", "*.*"),
                ("JJP Game Images", "*.iso *.img *.ext4 *.raw"),
                ("ISO Images", "*.iso"),
                ("Disk Images", "*.img *.ext4 *.raw"),
            ],
        )
        if path:
            self.image_var.set(path)

    def _browse_output(self):
        path = filedialog.askdirectory(title="Select Output Folder")
        if path:
            self.output_var.set(path)

    def _browse_write_input(self):
        path = filedialog.askdirectory(title="Select Input Folder (modified assets)")
        if path:
            self.write_input_var.set(path)

    def _browse_write_orig_image(self):
        path = filedialog.askopenfilename(
            title="Select Original Game Image (ISO or ext4)",
            filetypes=[
                ("All Files", "*.*"),
                ("JJP Game Images", "*.iso *.img *.ext4 *.raw"),
                ("ISO Images", "*.iso"),
                ("Disk Images", "*.img *.ext4 *.raw"),
            ],
        )
        if path:
            self.write_orig_image_var.set(path)

    def _browse_write_output(self):
        path = filedialog.asksaveasfilename(
            title="Save Modified ISO As",
            defaultextension=".iso",
            filetypes=[
                ("ISO Images", "*.iso"),
                ("Disk Images", "*.img"),
                ("All Files", "*.*"),
            ],
        )
        if path:
            self.write_output_var.set(path)

    def _ssd_refresh_write_devices(self):
        """Refresh the Write tab SSD device dropdown."""
        from .executor import list_disk_devices
        self._write_ssd_devices = list_disk_devices()
        values = [str(d) for d in self._write_ssd_devices]
        if not values:
            values = ["(no drives detected \u2014 connect SSD and click Refresh)"]
            self._write_ssd_devices = []
        self.write_ssd_combo.configure(values=values)
        if values:
            self.write_ssd_combo.current(0)

    def get_write_ssd_device(self):
        """Return the selected DiskInfo for Write tab, or None."""
        if not hasattr(self, '_write_ssd_devices'):
            return None
        idx = self.write_ssd_combo.current()
        if 0 <= idx < len(self._write_ssd_devices):
            return self._write_ssd_devices[idx]
        return None

    # ------------------------------------------------------------------
    # Write tab: modified-files preview
    # ------------------------------------------------------------------

    def _update_write_tree(self, rel_path, status="Modified"):
        """Insert or update a file in the Write tab's preview tree."""
        import os as _os
        ext = _os.path.splitext(rel_path)[1].upper().lstrip(".")

        status_lower = status.lower()
        if "trim" in status_lower:
            tag = "trimmed"
        elif "pad" in status_lower:
            tag = "padded"
        elif "convert" in status_lower:
            tag = "converted"
        elif "warning" in status_lower or "fail" in status_lower:
            tag = "warning"
        elif "ok" in status_lower or "encrypt" in status_lower:
            tag = "ok"
        else:
            tag = "modified"

        if self._write_tree_empty.winfo_ismapped():
            self._write_tree_empty.place_forget()

        if rel_path in self._write_tree_items:
            item_id = self._write_tree_items[rel_path]
            self.write_tree.item(item_id, values=(ext, status), tags=(tag,))
        else:
            parts = rel_path.replace("\\", "/").split("/")
            parent = ""
            for i, part in enumerate(parts[:-1]):
                folder_path = "/".join(parts[:i + 1])
                if folder_path not in self._write_tree_items:
                    node = self.write_tree.insert(
                        parent, tk.END, text=part, open=(i < 2))
                    self._write_tree_items[folder_path] = node
                parent = self._write_tree_items[folder_path]

            item_id = self.write_tree.insert(
                parent, tk.END, text=parts[-1],
                values=(ext, status), tags=(tag,))
            self._write_tree_items[rel_path] = item_id

    def scan_write_preview(self):
        """Scan the Game Assets folder for modified files (background thread).

        Populates the Write tab's preview tree with changed files.
        """
        import hashlib
        import os
        import re as _re
        import threading

        assets_path = self.output_var.get().strip()
        if not assets_path or not os.path.isdir(assets_path):
            return
        checksums_file = os.path.join(assets_path, '.checksums.md5')
        if not os.path.isfile(checksums_file):
            return

        # Bump scan ID so any in-flight scan stops posting results
        scan_id = (self._write_scan_id or 0) + 1
        self._write_scan_id = scan_id

        # Clear current tree
        self.write_tree.delete(*self.write_tree.get_children())
        self._write_tree_items = {}
        self._write_tree_empty.configure(text="Scanning for changes...")
        self._write_tree_empty.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

        def _scan():
            # Load baseline checksums
            saved = {}
            with open(checksums_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    m = _re.match(r'^([a-f0-9]{32})\s+\*?(.+)$', line)
                    if m:
                        fp = m.group(2)
                        if fp.startswith('./'):
                            fp = fp[2:]
                        saved[fp] = m.group(1)

            changed = []
            for root, _dirs, files in os.walk(assets_path):
                for name in files:
                    if name.startswith('.') or name == 'fl_decrypted.dat' \
                            or name.endswith('.img'):
                        continue
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, assets_path).replace('\\', '/')
                    if rel not in saved:
                        continue
                    h = hashlib.md5()
                    with open(full, 'rb') as fh:
                        for chunk in iter(lambda: fh.read(65536), b''):
                            h.update(chunk)
                    if h.hexdigest() != saved[rel]:
                        changed.append(rel)
                        # Post to GUI thread
                        if self._write_scan_id != scan_id:
                            return  # superseded by a newer scan
                        self.root.after(0, self._update_write_tree, rel,
                                        "Modified")

            # Final update
            if self._write_scan_id == scan_id:
                def _finish():
                    if not changed:
                        self._write_tree_empty.configure(
                            text="No modified files detected")
                        self._write_tree_empty.place(
                            relx=0.5, rely=0.5, anchor=tk.CENTER)
                self.root.after(0, _finish)

        threading.Thread(target=_scan, daemon=True).start()

    def _play_file(self, path):
        """Play/open a file with the system default handler."""
        import os as _os
        import subprocess
        if sys.platform == "win32":
            _os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def _open_in_explorer(self, path):
        """Open the containing folder and select the file."""
        import subprocess
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", path.replace("/", "\\")])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            import os as _os
            subprocess.Popen(["xdg-open", _os.path.dirname(path)])

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
        # Enable "Install Missing" if any prereq failed and handler exists
        if self._on_install_prereqs and self._prereq_state:
            has_failures = any(not v for v in self._prereq_state.values())
            self.install_btn.configure(
                state=tk.NORMAL if has_failures else tk.DISABLED)

    def _build_step_labels(self, parent, phases, label_list):
        """Build step indicator labels into a frame."""
        for widget in parent.winfo_children():
            widget.destroy()
        label_list.clear()
        c = _THEMES[self._current_theme]
        for i, phase in enumerate(phases):
            if i > 0:
                ttk.Label(parent, text=" > ",
                          foreground=c["gray"]).pack(side=tk.LEFT)
            lbl = ttk.Label(parent, text=f"{i+1}. {phase}",
                            foreground=c["gray"])
            lbl.pack(side=tk.LEFT)
            label_list.append(lbl)

    def _get_labels_for_mode(self, mode):
        """Return the appropriate step labels list for a mode."""
        if mode in ("decrypt", "decrypt_standalone", "ssd_decrypt"):
            return self.decrypt_step_labels
        return self.write_step_labels

    def set_phase(self, phase_index, mode="decrypt"):
        """Highlight the current phase in the step indicator."""
        c = _THEMES[self._current_theme]
        labels = self._get_labels_for_mode(mode)
        for i, lbl in enumerate(labels):
            if i < phase_index:
                lbl.configure(foreground=c["success"])
            elif i == phase_index:
                lbl.configure(foreground=c["accent"], font=("TkDefaultFont", 9, "bold"))
            else:
                lbl.configure(foreground=c["gray"], font=("TkDefaultFont", 9))

        # Reset progress bar to indeterminate until the phase sets its own progress
        if mode in ("decrypt", "decrypt_standalone", "ssd_decrypt"):
            self.decrypt_progress.configure(mode="indeterminate")
            self.decrypt_progress.start(15)
            self.decrypt_progress_label.configure(text="")
        else:
            self.write_progress.configure(mode="indeterminate")
            self.write_progress.start(15)
            self.write_progress_label.configure(text="")

    def set_progress(self, current, total, description="", mode="decrypt"):
        """Update the progress bar and label."""
        if mode in ("decrypt", "decrypt_standalone", "ssd_decrypt"):
            bar = self.decrypt_progress
            label = self.decrypt_progress_label
        else:
            bar = self.write_progress
            label = self.write_progress_label

        if total > 0:
            bar.stop()
            bar.configure(mode="determinate", maximum=total, value=current)
            pct = int(100 * current / total)
            label.configure(text=f"{pct}%  ({current}/{total})  {description}")
        else:
            bar.configure(mode="indeterminate")
            bar.start(15)
            label.configure(text=description)

    def set_running(self, running, mode="decrypt"):
        """Toggle between running and idle state."""
        if running:
            self.image_entry.configure(state=tk.DISABLED)
            self.output_entry.configure(state=tk.DISABLED)
            self.ssd_device_combo.configure(state=tk.DISABLED)
            self.modify_input_entry.configure(state=tk.DISABLED)
            self.write_input_entry.configure(state=tk.DISABLED)
            self.write_orig_image_entry.configure(state=tk.DISABLED)
            self.write_output_entry.configure(state=tk.DISABLED)
            self.write_ssd_combo.configure(state=tk.DISABLED)
            self.check_btn.configure(state=tk.DISABLED)
            self.decrypt_start_btn.configure(state=tk.DISABLED)
            self.modify_import_btn.configure(state=tk.DISABLED)
            self.modify_export_btn.configure(state=tk.DISABLED)
            self.write_apply_btn.configure(state=tk.DISABLED)
            if mode in ("decrypt", "decrypt_standalone", "ssd_decrypt"):
                self.decrypt_cancel_btn.configure(state=tk.NORMAL)
            else:
                self.write_cancel_btn.configure(state=tk.NORMAL)
            self._start_time = time.time()
            self._update_timer()
        else:
            self.image_entry.configure(state=tk.NORMAL)
            self.output_entry.configure(state=tk.NORMAL)
            self.ssd_device_combo.configure(state="readonly")
            self.modify_input_entry.configure(state=tk.NORMAL)
            self.write_input_entry.configure(state=tk.NORMAL)
            self.write_orig_image_entry.configure(state=tk.NORMAL)
            self.write_output_entry.configure(state=tk.NORMAL)
            self.write_ssd_combo.configure(state="readonly")
            self.check_btn.configure(state=tk.NORMAL)
            self.decrypt_start_btn.configure(state=tk.NORMAL)
            self.decrypt_cancel_btn.configure(state=tk.DISABLED)
            self.modify_import_btn.configure(state=tk.NORMAL)
            self.modify_export_btn.configure(state=tk.NORMAL)
            self.write_apply_btn.configure(state=tk.NORMAL)
            self.write_cancel_btn.configure(state=tk.DISABLED)
            # Stop any indeterminate animation and fill to 100% — only
            # on the bar that was actually running.
            if mode in ("decrypt", "decrypt_standalone", "ssd_decrypt"):
                self.decrypt_progress.stop()
                self.decrypt_progress.configure(
                    mode="determinate", maximum=100, value=100)
                self.decrypt_progress_label.configure(text="100%")
            else:
                self.write_progress.stop()
                self.write_progress.configure(
                    mode="determinate", maximum=100, value=100)
                self.write_progress_label.configure(text="100%")
            self._start_time = None
            if self._timer_id:
                self.root.after_cancel(self._timer_id)
                self._timer_id = None

    def set_status(self, text):
        """Update the status bar text."""
        self.status_label.configure(text=text)

    def reset_steps(self, mode="decrypt"):
        """Reset step indicators and progress for the given mode."""
        phase_map = {
            "decrypt": config.PHASES,
            "modify": config.MOD_PHASES,
            "decrypt_standalone": config.STANDALONE_PHASES,
            "modify_standalone": config.STANDALONE_MOD_PHASES,
            "ssd_decrypt": config.DIRECT_SSD_PHASES,
            "ssd_modify": config.DIRECT_SSD_MOD_PHASES,
        }
        phases = phase_map.get(mode, config.PHASES)

        if mode in ("decrypt", "decrypt_standalone", "ssd_decrypt"):
            if len(self.decrypt_step_labels) != len(phases):
                self._build_step_labels(self._decrypt_step_row, phases,
                                        self.decrypt_step_labels)
            labels = self.decrypt_step_labels
            self.decrypt_progress.stop()
            self.decrypt_progress.configure(mode="determinate", value=0, maximum=100)
            self.decrypt_progress_label.configure(text="")
        else:
            if len(self.write_step_labels) != len(phases):
                self._build_step_labels(self._write_step_row, phases,
                                        self.write_step_labels)
            labels = self.write_step_labels
            self.write_progress.stop()
            self.write_progress.configure(mode="determinate", value=0, maximum=100)
            self.write_progress_label.configure(text="")

        c = _THEMES[self._current_theme]
        for lbl in labels:
            lbl.configure(foreground=c["gray"], font=("TkDefaultFont", 9))

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
        win.title("JJP Asset Decryptor \u2014 Help")
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
