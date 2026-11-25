"""The PDF Squeeze window (tkinter/ttk, native look on Windows)."""

from __future__ import annotations

import contextlib
import logging
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font, messagebox, ttk

from . import __version__
from .engine import IMAGES, Cancelled, CompressionError, Result, Status, compress_pdf
from .files import human_size, is_writable_dir, next_output_path
from .presets import PRESETS, get_preset
from .settings import load_preset_key, log_path, save_preset_key

log = logging.getLogger(__name__)

APP_NAME = "PDF Squeeze"
PDF_TYPES = [("PDF files", "*.pdf"), ("All files", "*.*")]
MAX_NAME_CHARS = 52
POLL_MS = 50


def _enable_dpi_awareness() -> None:
    """Render crisply on high-DPI displays instead of being bitmap-stretched by Windows."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system DPI aware
    except (AttributeError, OSError):
        with contextlib.suppress(AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()


def _resource(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / relative


def _middle_ellipsis(text: str, limit: int = MAX_NAME_CHARS) -> str:
    if len(text) <= limit:
        return text
    keep = limit - 1
    return text[: keep - keep // 2] + "…" + text[-(keep // 2) :]


def _is_pdf(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return b"%PDF" in f.read(1024)
    except OSError:
        return False


class App:
    def __init__(self, root: tk.Tk, src: Path, screenshot_dir: Path | None = None) -> None:
        self.root = root
        self.src = src
        self.screenshot_dir = screenshot_dir
        self.exit_code = 0
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.closing = False
        self.result: Result | None = None

        self.scale = root.winfo_fpixels("1i") / 96.0
        self._init_style()
        self._build()
        self._show_file()
        self._set_state("ready")
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ layout

    def px(self, value: float) -> int:
        return round(value * self.scale)

    def _init_style(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")

        base = font.nametofont("TkDefaultFont")
        size = base.actual("size")
        family = base.actual("family")
        self.font_title = font.Font(family=family, size=size + 3, weight="bold")
        self.font_bold = font.Font(family=family, size=size, weight="bold")
        self.font_result = font.Font(family=family, size=size + 6, weight="bold")

        self.bg = style.lookup("TFrame", "background") or "#f0f0f0"
        self.muted = "#5c5c5c"
        self.success = "#0f7b0f"
        style.configure("Muted.TLabel", foreground=self.muted)
        style.configure("Title.TLabel", font=self.font_title)
        style.configure("Heading.TLabel", font=self.font_bold)
        style.configure("Result.TLabel", font=self.font_result, foreground=self.success)
        style.configure("Neutral.TLabel", font=self.font_result)
        style.configure("Preset.TRadiobutton", font=self.font_bold)

    def _build(self) -> None:
        root = self.root
        root.title(APP_NAME)
        root.resizable(False, False)
        icon = _resource("assets/icon.ico")
        if sys.platform == "win32" and icon.exists():
            root.iconbitmap(default=str(icon))

        pad = self.px(20)
        outer = ttk.Frame(root, padding=(pad, self.px(16), pad, pad))
        outer.grid(sticky="nsew")
        outer.columnconfigure(0, weight=1, minsize=self.px(420))

        # File
        file_row = ttk.Frame(outer)
        file_row.grid(row=0, column=0, sticky="ew")
        file_row.columnconfigure(0, weight=1)
        self.name_label = ttk.Label(file_row, style="Title.TLabel")
        self.name_label.grid(row=0, column=0, sticky="w")
        self.size_label = ttk.Label(file_row, style="Muted.TLabel")
        self.size_label.grid(row=1, column=0, sticky="w", pady=(self.px(2), 0))
        self.change_button = ttk.Button(file_row, text="Choose file…", command=self._choose_file)
        self.change_button.grid(row=0, column=1, rowspan=2, sticky="e", padx=(self.px(12), 0))

        ttk.Separator(outer).grid(row=1, column=0, sticky="ew", pady=self.px(14))

        # Presets
        self.preset_frame = ttk.Frame(outer)
        self.preset_frame.grid(row=2, column=0, sticky="ew")
        ttk.Label(self.preset_frame, text="Compression level", style="Heading.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, self.px(6))
        )
        self.preset_var = tk.StringVar(value=load_preset_key())
        indent = self.px(18)  # aligns with the radio label text, not the indicator
        for i, preset in enumerate(PRESETS):
            radio = ttk.Radiobutton(
                self.preset_frame,
                text=preset.label,
                value=preset.key,
                variable=self.preset_var,
                style="Preset.TRadiobutton",
            )
            radio.grid(row=1 + i * 2, column=0, sticky="w", pady=(self.px(4), 0))
            desc = ttk.Label(self.preset_frame, text=preset.description, style="Muted.TLabel")
            desc.grid(row=2 + i * 2, column=0, sticky="w", padx=(indent, 0))
            desc.bind("<Button-1>", lambda _e, key=preset.key: self._pick(key))

        # Progress (shown while working)
        self.progress_frame = ttk.Frame(outer)
        self.progress_frame.grid(row=2, column=0, sticky="ew")
        self.progress_frame.columnconfigure(0, weight=1)
        self.status_label = ttk.Label(self.progress_frame, text="Starting…")
        self.status_label.grid(row=0, column=0, sticky="w", pady=(self.px(10), self.px(8)))
        self.progress = ttk.Progressbar(self.progress_frame, mode="determinate", maximum=1)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(0, self.px(10)))

        # Result (shown when done)
        self.result_frame = ttk.Frame(outer)
        self.result_frame.grid(row=2, column=0, sticky="ew")
        self.result_title = ttk.Label(self.result_frame, style="Result.TLabel")
        self.result_title.grid(row=0, column=0, sticky="w", pady=(self.px(6), self.px(4)))
        self.result_detail = ttk.Label(
            self.result_frame, style="Muted.TLabel", wraplength=self.px(420), justify="left"
        )
        self.result_detail.grid(row=1, column=0, sticky="w", pady=(0, self.px(6)))

        # Buttons
        self.button_row = ttk.Frame(outer)
        self.button_row.grid(row=3, column=0, sticky="e", pady=(self.px(20), 0))
        self.buttons: dict[str, ttk.Button] = {
            "compress": ttk.Button(self.button_row, text="Compress", command=self._start),
            "cancel": ttk.Button(self.button_row, text="Cancel", command=self._request_cancel),
            "level": ttk.Button(
                self.button_row, text="Change level", command=lambda: self._set_state("ready")
            ),
            "folder": ttk.Button(self.button_row, text="Open folder", command=self._open_folder),
            "close": ttk.Button(self.button_row, text="Close", command=self._on_close),
        }
        # Reserve the tallest state's height so the window never jumps when switching states.
        root.update_idletasks()
        content = (self.preset_frame, self.progress_frame, self.result_frame)
        outer.rowconfigure(2, minsize=max(frame.winfo_reqheight() for frame in content))

        root.bind("<Return>", lambda _e: self._invoke_default())
        root.bind("<Escape>", lambda _e: self._on_close())

    def _pick(self, key: str) -> None:
        if self.state == "ready":
            self.preset_var.set(key)

    def _show_file(self) -> None:
        self.name_label.configure(text=_middle_ellipsis(self.src.name))
        try:
            size = human_size(self.src.stat().st_size)
        except OSError:
            size = "unknown size"
        self.size_label.configure(text=f"{size}  ·  {_middle_ellipsis(str(self.src.parent), 60)}")

    def _set_state(self, state: str) -> None:
        self.state = state
        for frame in (self.preset_frame, self.progress_frame, self.result_frame):
            frame.grid_remove()
        {"ready": self.preset_frame, "working": self.progress_frame, "done": self.result_frame}[state].grid()
        self.change_button.state(["disabled"] if state == "working" else ["!disabled"])

        if state == "ready":
            visible = ["compress", "close"]
            self.default = "compress"
        elif state == "working":
            visible = ["cancel"]
            self.default = None
        elif self.result and self.result.status is Status.COMPRESSED:
            visible = ["level", "folder", "close"]
            self.default = "folder"
        else:
            visible = ["level", "close"]
            self.default = "level"

        for button in self.buttons.values():
            button.pack_forget()
            button.configure(default="normal")
            button.state(["!disabled"])
        for name in visible:
            self.buttons[name].pack(side="left", padx=(self.px(8), 0))
        if self.default:
            self.buttons[self.default].configure(default="active")
            self.buttons[self.default].focus_set()

    def _invoke_default(self) -> None:
        if self.default:
            self.buttons[self.default].invoke()

    # ------------------------------------------------------------------ actions

    def _choose_file(self) -> None:
        chosen = ask_for_pdf(self.root, initial_dir=self.src.parent)
        if chosen:
            self.src = chosen
            self.result = None
            self._show_file()
            self._set_state("ready")

    def _start(self) -> None:
        preset = get_preset(self.preset_var.get())
        save_preset_key(preset.key)
        if not self.src.exists():
            messagebox.showerror(APP_NAME, f"The file no longer exists:\n\n{self.src}", parent=self.root)
            return

        dst = next_output_path(self.src)
        if not is_writable_dir(self.src.parent):
            chosen = filedialog.asksaveasfilename(
                parent=self.root,
                title="This folder is read-only - choose where to save",
                initialfile=dst.name,
                defaultextension=".pdf",
                filetypes=PDF_TYPES[:1],
            )
            if not chosen:
                return
            dst = Path(chosen)

        self.cancel.clear()
        self.progress.configure(mode="determinate", maximum=1, value=0)
        self.status_label.configure(text="Reading PDF…")
        self._set_state("working")
        self.worker = threading.Thread(target=self._work, args=(self.src, dst, preset), daemon=True)
        self.worker.start()
        self.root.after(POLL_MS, self._poll)

    def _work(self, src: Path, dst: Path, preset) -> None:
        def progress(stage: str, done: int, total: int) -> None:
            self.events.put(("progress", stage, done, total))

        try:
            result = compress_pdf(src, dst, preset, progress=progress, cancel=self.cancel)
            log.info(
                "%s [%s]: %s %d -> %d bytes (%d/%d images, skipped %s)",
                src.name,
                preset.key,
                result.status.value,
                result.original_size,
                result.new_size,
                result.images_recompressed,
                result.images_total,
                result.skip_reasons,
            )
            self.events.put(("done", result))
        except Cancelled:
            self.events.put(("cancelled",))
        except CompressionError as exc:
            log.warning("%s: %s", src.name, exc)
            self.events.put(("error", str(exc)))
        except MemoryError:
            log.exception("Out of memory on %s", src.name)
            self.events.put(("error", "Your computer ran out of memory while compressing this PDF."))
        except Exception as exc:
            log.exception("Unexpected error on %s", src.name)
            self.events.put(
                (
                    "error",
                    f"Something went wrong while compressing this PDF.\n\n{type(exc).__name__}: {exc}"
                    f"\n\nDetails were saved to:\n{log_path()}",
                )
            )

    def _poll(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "progress":
                    self._show_progress(*event[1:])
                elif kind == "done":
                    self._finish(event[1])
                    return
                elif kind == "cancelled":
                    if self.closing:
                        self.root.destroy()
                    else:
                        self._set_state("ready")
                    return
                elif kind == "error":
                    self._fail(event[1])
                    return
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._poll)

    def _show_progress(self, stage: str, done: int, total: int) -> None:
        if self.cancel.is_set():
            return
        if stage == IMAGES:
            self.progress.configure(maximum=max(total, 1), value=done)
            self.status_label.configure(text=f"Compressing image {done} of {total}…")
        else:
            self.progress.configure(maximum=100, value=done)
            self.status_label.configure(text="Saving the smaller copy…")

    def _finish(self, result: Result) -> None:
        self.result = result
        if result.status is Status.COMPRESSED:
            saved = round(result.saved_fraction * 100)
            self.result_title.configure(
                text=f"{human_size(result.original_size)}  →  {human_size(result.new_size)}",
                style="Result.TLabel",
            )
            assert result.output_path is not None
            self.result_detail.configure(
                text=f"{saved}% smaller. Saved as {_middle_ellipsis(result.output_path.name)}"
            )
        else:
            preset = get_preset(self.preset_var.get())
            self.result_title.configure(text="Already optimized", style="Neutral.TLabel")
            hint = (
                "Try a lower level, or keep the original."
                if preset.key != PRESETS[-1].key
                else "Keep the original - it is already as small as this tool can make it."
            )
            self.result_detail.configure(
                text=f"The {preset.label} level could not make this PDF meaningfully smaller, "
                f"so no copy was saved. {hint}"
            )
        self._set_state("done")
        if self.screenshot_dir:
            self.root.after(400, lambda: self._screenshot("2-done.png", then_quit=True))

    def _fail(self, message: str) -> None:
        self._set_state("ready")
        if self.screenshot_dir:
            log.error("Screenshot run failed: %s", message)
            self.exit_code = 1
            self.root.destroy()
            return
        messagebox.showerror(APP_NAME, message, parent=self.root)

    def _request_cancel(self) -> None:
        self.cancel.set()
        self.status_label.configure(text="Cancelling…")
        self.buttons["cancel"].state(["disabled"])

    def _on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            # Let the worker stop cleanly (it removes its temporary file), then close.
            self.closing = True
            self._request_cancel()
            return
        self.root.destroy()

    def _open_folder(self) -> None:
        if not self.result or not self.result.output_path:
            return
        path = self.result.output_path
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path.parent)])
        except OSError:
            log.warning("Could not open folder", exc_info=True)
            messagebox.showinfo(APP_NAME, f"The compressed PDF is here:\n\n{path}", parent=self.root)

    # ------------------------------------------------------------------ screenshots (CI)

    def _screenshot(self, name: str, then_quit: bool = False) -> None:
        from PIL import ImageGrab

        if sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.SetCursorPos(0, 0)
        self.root.update()
        assert self.screenshot_dir is not None
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        ImageGrab.grab(bbox=_window_bounds(self.root), all_screens=True).save(self.screenshot_dir / name)
        if then_quit:
            self.root.destroy()


def _window_bounds(root: tk.Tk) -> tuple[int, int, int, int]:
    """Visible window bounds including the title bar (falls back to the client area)."""
    x, y = root.winfo_rootx(), root.winfo_rooty()
    bounds = (x, y, x + root.winfo_width(), y + root.winfo_height())
    if sys.platform != "win32":
        return bounds
    import ctypes
    from ctypes import wintypes

    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    rect = wintypes.RECT()
    dwmwa_extended_frame_bounds = 9
    hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
        hwnd, dwmwa_extended_frame_bounds, ctypes.byref(rect), ctypes.sizeof(rect)
    )
    if hr == 0:
        return rect.left, rect.top, rect.right, rect.bottom
    return bounds


def ask_for_pdf(parent: tk.Misc, initial_dir: Path | None = None) -> Path | None:
    while True:
        chosen = filedialog.askopenfilename(
            parent=parent,
            title="Choose a PDF to compress",
            filetypes=PDF_TYPES,
            initialdir=str(initial_dir) if initial_dir else None,
        )
        if not chosen:
            return None
        path = Path(chosen)
        if _is_pdf(path):
            return path
        messagebox.showerror(APP_NAME, f"This is not a PDF file:\n\n{path.name}", parent=parent)


def _center(root: tk.Tk) -> None:
    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 3
    root.geometry(f"+{max(x, 0)}+{max(y, 0)}")


def run(src: Path | None, extra_files: int = 0, screenshot_dir: Path | None = None) -> int:
    _enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    root.title(APP_NAME)
    log.info("GUI %s, Tk %s", __version__, root.tk.call("info", "patchlevel"))

    if src is not None and not _is_pdf(src):
        messagebox.showerror(APP_NAME, f"This is not a PDF file:\n\n{src}", parent=root)
        src = None
    if src is None:
        src = ask_for_pdf(root, initial_dir=Path.home() / "Documents")
        if src is None:
            root.destroy()
            return 0
    if extra_files:
        messagebox.showinfo(
            APP_NAME,
            f"PDF Squeeze compresses one PDF at a time.\n\nOpening: {src.name}",
            parent=root,
        )

    app = App(root, src.resolve(), screenshot_dir)
    _center(root)
    root.deiconify()
    root.lift()
    root.focus_force()
    if screenshot_dir:
        root.after(800, lambda: (app._screenshot("1-ready.png"), app._start()))
    root.mainloop()
    return app.exit_code
