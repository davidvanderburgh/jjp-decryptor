"""Main window GUI for JJP Asset Decryptor."""

import tkinter as tk
from tkinter import ttk, filedialog
import time

from . import config


class MainWindow:
    """Single-window tkinter GUI."""

    def __init__(self, root, on_check_prereqs, on_start, on_cancel):
        self.root = root
        self._on_check_prereqs = on_check_prereqs
        self._on_start = on_start
        self._on_cancel = on_cancel

        root.title("JJP Asset Decryptor")
        root.geometry("780x720")
        root.minsize(700, 600)

        # State
        self._start_time = None
        self._timer_id = None

        self._build_ui()

    def _build_ui(self):
        # Main container with padding
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        # --- Configuration Section ---
        cfg_frame = ttk.LabelFrame(main, text=" Configuration ", padding=8)
        cfg_frame.pack(fill=tk.X, pady=(0, 6))

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

        # --- Prerequisites Section ---
        prereq_frame = ttk.LabelFrame(main, text=" Prerequisites ", padding=8)
        prereq_frame.pack(fill=tk.X, pady=(0, 6))

        self.prereq_grid = ttk.Frame(prereq_frame)
        self.prereq_grid.pack(fill=tk.X)

        self.prereq_labels = {}
        prereq_names = ["WSL2", "gcc", "usbipd-win", "HASP Dongle"]
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
        self.check_btn.pack()

        # --- Controls Section ---
        ctrl_frame = ttk.LabelFrame(main, text=" Decryption ", padding=8)
        ctrl_frame.pack(fill=tk.X, pady=(0, 6))

        # Step indicator
        step_row = ttk.Frame(ctrl_frame)
        step_row.pack(fill=tk.X, pady=(0, 6))
        self.step_labels = []
        for i, phase in enumerate(config.PHASES):
            if i > 0:
                ttk.Label(step_row, text=" > ", foreground="gray").pack(side=tk.LEFT)
            lbl = ttk.Label(step_row, text=f"{i+1}. {phase}", foreground="gray")
            lbl.pack(side=tk.LEFT)
            self.step_labels.append(lbl)

        # Progress bar
        prog_row = ttk.Frame(ctrl_frame)
        prog_row.pack(fill=tk.X, pady=(0, 6))
        self.progress_label = ttk.Label(prog_row, text="", anchor=tk.E)
        self.progress_label.pack(side=tk.RIGHT)
        self.progress = ttk.Progressbar(prog_row, mode="determinate")
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        # Buttons
        btn_row = ttk.Frame(ctrl_frame)
        btn_row.pack()
        self.start_btn = ttk.Button(btn_row, text="Start Decryption",
                                     command=self._on_start)
        self.start_btn.pack(side=tk.LEFT, padx=4)
        self.cancel_btn = ttk.Button(btn_row, text="Cancel",
                                      command=self._on_cancel, state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.LEFT, padx=4)

        # --- Log Section ---
        log_frame = ttk.LabelFrame(main, text=" Log Output ", padding=4)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 4))

        log_container = ttk.Frame(log_frame)
        log_container.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(log_container, wrap=tk.WORD, state=tk.DISABLED,
                                font=("Consolas", 9), bg="#1e1e1e", fg="#cccccc",
                                insertbackground="#cccccc", selectbackground="#264f78",
                                relief=tk.FLAT, padx=6, pady=4)
        scrollbar = ttk.Scrollbar(log_container, orient=tk.VERTICAL,
                                   command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Configure text tags for colors
        self.log_text.tag_configure("info", foreground="#cccccc")
        self.log_text.tag_configure("error", foreground="#f44747")
        self.log_text.tag_configure("success", foreground="#6a9955")
        self.log_text.tag_configure("timestamp", foreground="#808080")

        # --- Status Bar ---
        status_frame = ttk.Frame(main)
        status_frame.pack(fill=tk.X)
        self.status_label = ttk.Label(status_frame, text="Ready", anchor=tk.W)
        self.status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.elapsed_label = ttk.Label(status_frame, text="", anchor=tk.E)
        self.elapsed_label.pack(side=tk.RIGHT)

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

    def set_prereq(self, name, passed, message=""):
        """Update a prerequisite indicator."""
        label = self.prereq_labels.get(name)
        if label:
            if passed:
                label.configure(text="[OK]", foreground="#6a9955")
            else:
                label.configure(text="[  X ]", foreground="#f44747")

    def set_phase(self, phase_index):
        """Highlight the current phase in the step indicator."""
        for i, lbl in enumerate(self.step_labels):
            if i < phase_index:
                lbl.configure(foreground="#6a9955")  # completed - green
            elif i == phase_index:
                lbl.configure(foreground="#569cd6", font=("TkDefaultFont", 9, "bold"))
            else:
                lbl.configure(foreground="gray", font=("TkDefaultFont", 9))

    def set_progress(self, current, total, description=""):
        """Update the progress bar and label."""
        if total > 0:
            self.progress.configure(mode="determinate", maximum=total, value=current)
            pct = int(100 * current / total)
            self.progress_label.configure(
                text=f"{pct}%  ({current}/{total})  {description}")
        else:
            self.progress.configure(mode="indeterminate")
            self.progress_label.configure(text=description)

    def set_game_name(self, name):
        """Update the detected game label."""
        display = config.KNOWN_GAMES.get(name, name)
        self.game_label.configure(text=display, foreground="black")

    def set_running(self, running):
        """Toggle between running and idle state."""
        if running:
            self.start_btn.configure(state=tk.DISABLED)
            self.cancel_btn.configure(state=tk.NORMAL)
            self.image_entry.configure(state=tk.DISABLED)
            self.output_entry.configure(state=tk.DISABLED)
            self.check_btn.configure(state=tk.DISABLED)
            self._start_time = time.time()
            self._update_timer()
        else:
            self.start_btn.configure(state=tk.NORMAL)
            self.cancel_btn.configure(state=tk.DISABLED)
            self.image_entry.configure(state=tk.NORMAL)
            self.output_entry.configure(state=tk.NORMAL)
            self.check_btn.configure(state=tk.NORMAL)
            self._start_time = None
            if self._timer_id:
                self.root.after_cancel(self._timer_id)
                self._timer_id = None

    def set_status(self, text):
        """Update the status bar text."""
        self.status_label.configure(text=text)

    def reset_steps(self):
        """Reset all step indicators to gray."""
        for lbl in self.step_labels:
            lbl.configure(foreground="gray", font=("TkDefaultFont", 9))
        self.progress.configure(mode="determinate", value=0, maximum=100)
        self.progress_label.configure(text="")

    def _update_timer(self):
        """Update the elapsed time display."""
        if self._start_time:
            elapsed = int(time.time() - self._start_time)
            mins, secs = divmod(elapsed, 60)
            self.elapsed_label.configure(text=f"Elapsed: {mins:02d}:{secs:02d}")
            self._timer_id = self.root.after(1000, self._update_timer)
