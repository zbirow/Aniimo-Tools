import json
import os
import shutil
import struct
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import winsound
except ImportError:
    winsound = None


@dataclass
class WemEntry:
    media_id: int | None
    offset: int
    size: int
    data_start: int | None = None
    source_path: Path | None = None
    signature: str = ""
    codec: str = ""
    channels: str = ""
    sample_rate: str = ""

    @property
    def absolute_start(self) -> int:
        if self.data_start is None:
            raise ValueError("External WEM has no offset inside BNK DATA.")
        return self.data_start + self.offset

    @property
    def absolute_end(self) -> int:
        return self.absolute_start + self.size

    @property
    def file_name(self) -> str:
        if self.source_path is not None:
            return self.source_path.name
        return f"{self.media_id}.wem"

    @property
    def output_stem(self) -> str:
        if self.source_path is not None:
            return self.source_path.stem
        return str(self.media_id)

    @property
    def display_id(self) -> str:
        return str(self.media_id) if self.media_id is not None else self.output_stem


CODEC_NAMES = {
    0x0001: "PCM",
    0x0002: "Wwise ADPCM / platform ADPCM",
    0x0069: "IMA ADPCM",
    0x0161: "WMA v2",
    0x0162: "WMA Pro",
    0x0165: "XMA2",
    0x0166: "XMA2",
    0xAAC0: "AAC",
    0xFFF0: "DSP",
    0xFFFB: "HEVAG",
    0xFFFC: "ATRAC9",
    0xFFFE: "PCM (Wwise Authoring)",
    0xFFFF: "Wwise Vorbis",
    0x3039: "OpusNX",
    0x3040: "Opus",
    0x3041: "Wwise Opus",
    0x8311: "Wwise PTADPCM",
}


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KiB"
    if n < 1024 ** 3:
        return f"{n / (1024 ** 2):.2f} MiB"
    return f"{n / (1024 ** 3):.2f} GiB"


def inspect_wem(blob: bytes):
    signature = blob[:4].decode("ascii", errors="replace") if len(blob) >= 4 else ""
    codec = ""
    channels = ""
    sample_rate = ""

    if len(blob) < 12 or blob[:4] not in (b"RIFF", b"RIFX") or blob[8:12] != b"WAVE":
        return signature, codec, channels, sample_rate

    endian = ">" if blob[:4] == b"RIFX" else "<"
    pos = 12
    while pos + 8 <= len(blob):
        chunk_id = blob[pos:pos + 4]
        try:
            chunk_size = struct.unpack_from(endian + "I", blob, pos + 4)[0]
        except struct.error:
            break
        payload = pos + 8
        end = payload + chunk_size
        if end > len(blob):
            break

        if chunk_id == b"fmt " and chunk_size >= 16:
            fmt_tag, ch = struct.unpack_from(endian + "HH", blob, payload)
            rate = struct.unpack_from(endian + "I", blob, payload + 4)[0]
            codec = CODEC_NAMES.get(fmt_tag, f"0x{fmt_tag:04X}")
            channels = str(ch)
            sample_rate = str(rate)
            break

        # RIFF chunks can be word-aligned.
        pos = end + (chunk_size & 1)

    return signature, codec, channels, sample_rate


def parse_bnk(path: Path):
    raw = path.read_bytes()
    if len(raw) < 8 or raw[:4] != b"BKHD":
        raise ValueError("This is not a Wwise BNK file: missing BKHD header.")

    bkhd_size = struct.unpack_from("<I", raw, 4)[0]
    pos = 8 + bkhd_size
    if pos > len(raw):
        raise ValueError("Invalid BKHD size.")

    didx_payload = None
    data_payload_start = None
    data_size = None

    # Wwise BNK top-level chunks are: 4-byte tag + uint32 LE size + payload.
    while pos + 8 <= len(raw):
        tag = raw[pos:pos + 4]
        size = struct.unpack_from("<I", raw, pos + 4)[0]
        payload = pos + 8
        end = payload + size

        if end > len(raw):
            raise ValueError(
                f"Chunk {tag!r} at 0x{pos:X} extends past end of file "
                f"(size=0x{size:X})."
            )

        if tag == b"DIDX":
            didx_payload = raw[payload:end]
        elif tag == b"DATA":
            data_payload_start = payload
            data_size = size

        pos = end

    if didx_payload is None and data_payload_start is None:
        # Metadata-only SoundBank. Its media is stored as separate WEM files.
        return raw, bkhd_size, None, []
    if didx_payload is None:
        raise ValueError("BNK has DATA but no DIDX index.")
    if data_payload_start is None:
        raise ValueError("BNK has DIDX but no DATA chunk.")
    if len(didx_payload) % 12 != 0:
        raise ValueError(f"Invalid DIDX size: {len(didx_payload)} bytes (must be divisible by 12).")

    entries = []
    for p in range(0, len(didx_payload), 12):
        media_id, offset, size = struct.unpack_from("<III", didx_payload, p)
        if offset + size > data_size:
            raise ValueError(
                f"WEM {media_id}: offset 0x{offset:X} + size 0x{size:X} "
                f"exceeds DATA size 0x{data_size:X}."
            )

        blob = raw[data_payload_start + offset:data_payload_start + offset + size]
        sig, codec, ch, rate = inspect_wem(blob)
        entries.append(WemEntry(
            media_id=media_id,
            offset=offset,
            size=size,
            data_start=data_payload_start,
            signature=sig,
            codec=codec,
            channels=ch,
            sample_rate=rate,
        ))

    return raw, bkhd_size, data_size, entries


def scan_external_wems(folder: Path, bank_raw: bytes):
    """Find WEMs recursively and prefer numeric IDs referenced by the BNK.

    External Wwise media is normally named <uint32 media ID>.wem. Instead of
    assuming one HIRC layout/version, match those IDs against their 4-byte
    representation anywhere in the bank. If no reliable match is found, keep
    all WEM files so the user can still convert them manually.
    """
    files = sorted(
        (p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() == ".wem"),
        key=lambda p: str(p).lower(),
    )

    found: list[WemEntry] = []
    referenced: list[WemEntry] = []
    for file in files:
        try:
            size = file.stat().st_size
            with file.open("rb") as f:
                header = f.read(min(size, 256 * 1024))
        except OSError:
            continue

        try:
            media_id = int(file.stem, 10)
            if not 0 <= media_id <= 0xFFFFFFFF:
                media_id = None
        except ValueError:
            media_id = None

        sig, codec, ch, rate = inspect_wem(header)
        entry = WemEntry(
            media_id=media_id,
            offset=0,
            size=size,
            source_path=file,
            signature=sig,
            codec=codec,
            channels=ch,
            sample_rate=rate,
        )
        found.append(entry)

        if media_id is not None:
            le = struct.pack("<I", media_id)
            be = struct.pack(">I", media_id)
            if le in bank_raw or be in bank_raw:
                referenced.append(entry)

    if referenced:
        return referenced, len(found), True
    return found, len(found), False


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Wwise BNK -> FLAC")
        self.root.geometry("1120x700")
        self.root.minsize(900, 560)

        self.apply_dark_theme()

        self.bnk_path: Path | None = None
        self.raw: bytes | None = None
        self.entries: list[WemEntry] = []
        self.bkhd_size: int | None = None
        self.data_size: int | None = None
        self.external_mode = False
        self.preview_temp_dir = None
        self.preview_token = 0

        script_dir = Path(__file__).resolve().parent
        self.config_path = script_dir / "wwise_bnk_to_flac_gui.json"
        config = self.load_config()
        saved_vgmstream = config.get("vgmstream_cli", "")
        detected_vgmstream = self.find_tool(script_dir, "vgmstream-cli.exe", "vgmstream-cli")
        self.vgmstream_var = tk.StringVar(value=saved_vgmstream or detected_vgmstream)
        self.ffmpeg_var = tk.StringVar(value=self.find_tool(script_dir, "ffmpeg.exe", "ffmpeg"))
        self.output_var = tk.StringVar()
        self.external_folder_var = tk.StringVar()
        self.keep_wem_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Select a BNK file.")

        self.build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.save_config(show_error=False)

    def apply_dark_theme(self):
        """Apply a consistent dark theme to Tk and ttk widgets."""
        bg = "#1E1E1E"
        panel = "#252526"
        field = "#2D2D30"
        button = "#333337"
        button_active = "#45454A"
        border = "#4A4A4F"
        fg = "#F0F0F0"
        muted = "#C8C8C8"
        accent = "#0E639C"
        accent_active = "#1177BB"

        self.root.configure(background=bg)
        self.root.option_add("*Background", bg)
        self.root.option_add("*Foreground", fg)
        self.root.option_add("*selectBackground", accent)
        self.root.option_add("*selectForeground", "#FFFFFF")

        style = ttk.Style(self.root)
        # The native Windows themes ignore several background settings.
        style.theme_use("clam")

        style.configure(".", background=bg, foreground=fg)
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure(
            "TLabelframe",
            background=panel,
            bordercolor=border,
            lightcolor=border,
            darkcolor=border,
            relief="solid",
        )
        style.configure("TLabelframe.Label", background=panel, foreground=fg)
        style.configure(
            "TButton",
            background=button,
            foreground=fg,
            bordercolor=border,
            lightcolor=button,
            darkcolor=button,
            padding=(10, 5),
        )
        style.map(
            "TButton",
            background=[("pressed", accent), ("active", button_active)],
            foreground=[("disabled", "#777777"), ("!disabled", fg)],
            bordercolor=[("focus", accent_active)],
        )
        style.configure(
            "TEntry",
            fieldbackground=field,
            foreground=fg,
            insertcolor=fg,
            bordercolor=border,
            lightcolor=border,
            darkcolor=border,
            padding=5,
        )
        style.map(
            "TEntry",
            fieldbackground=[("readonly", panel), ("disabled", panel)],
            foreground=[("disabled", "#777777")],
            bordercolor=[("focus", accent_active)],
        )
        style.configure(
            "TCheckbutton",
            background=panel,
            foreground=fg,
            indicatorbackground=field,
            indicatorforeground=fg,
        )
        style.map(
            "TCheckbutton",
            background=[("active", panel)],
            foreground=[("disabled", "#777777"), ("!disabled", fg)],
            indicatorbackground=[("selected", accent), ("active", button_active)],
        )
        style.configure(
            "Treeview",
            background=field,
            fieldbackground=field,
            foreground=fg,
            bordercolor=border,
            lightcolor=border,
            darkcolor=border,
            rowheight=25,
        )
        style.map(
            "Treeview",
            background=[("selected", accent)],
            foreground=[("selected", "#FFFFFF")],
        )
        style.configure(
            "Treeview.Heading",
            background=button,
            foreground=fg,
            bordercolor=border,
            lightcolor=button_active,
            darkcolor=button,
            relief="flat",
            padding=(6, 5),
        )
        style.map("Treeview.Heading", background=[("active", button_active)])
        style.configure(
            "Vertical.TScrollbar",
            background=button,
            troughcolor=bg,
            bordercolor=bg,
            arrowcolor=muted,
            lightcolor=button,
            darkcolor=button,
        )
        style.map("Vertical.TScrollbar", background=[("active", button_active)])
        style.configure(
            "TProgressbar",
            background=accent,
            troughcolor=field,
            bordercolor=border,
            lightcolor=accent,
            darkcolor=accent,
        )

    @staticmethod
    def find_tool(script_dir: Path, windows_name: str, path_name: str) -> str:
        local = script_dir / windows_name
        if local.exists():
            return str(local)
        found = shutil.which(path_name) or shutil.which(windows_name)
        return found or ""

    def load_config(self) -> dict:
        if not self.config_path.exists():
            return {}
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def save_config(self, show_error: bool = True):
        config = {
            "vgmstream_cli": self.vgmstream_var.get().strip(),
        }
        try:
            temp_path = self.config_path.with_suffix(".json.tmp")
            temp_path.write_text(
                json.dumps(config, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            temp_path.replace(self.config_path)
        except OSError as e:
            if show_error:
                messagebox.showerror("Config error", f"Could not save configuration:\n{e}")

    def on_close(self):
        self.save_config(show_error=False)
        self.stop_playback(update_status=False)
        self.root.destroy()

    def build_ui(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        file_row = ttk.Frame(outer)
        file_row.pack(fill="x", pady=(0, 8))
        ttk.Button(file_row, text="Open BNK...", command=self.open_bnk).pack(side="left")
        self.file_label = ttk.Label(file_row, text="No file selected", anchor="w")
        self.file_label.pack(side="left", padx=10, fill="x", expand=True)

        tool_box = ttk.LabelFrame(outer, text="Tools", padding=8)
        tool_box.pack(fill="x", pady=(0, 8))
        self.path_row(tool_box, "vgmstream-cli:", self.vgmstream_var, self.pick_vgmstream, 0)
        self.path_row(tool_box, "FFmpeg:", self.ffmpeg_var, self.pick_ffmpeg, 1)
        self.path_row(tool_box, "Output folder:", self.output_var, self.pick_output, 2)
        self.path_row(tool_box, "External WEM folder:", self.external_folder_var, self.pick_external_folder, 3)
        ttk.Checkbutton(
            tool_box,
            text="Also keep extracted .wem files",
            variable=self.keep_wem_var,
        ).grid(row=4, column=1, sticky="w", pady=(5, 0))
        tool_box.columnconfigure(1, weight=1)

        info_row = ttk.Frame(outer)
        info_row.pack(fill="x", pady=(0, 6))
        self.bank_info = ttk.Label(info_row, text="")
        self.bank_info.pack(side="left")

        table_frame = ttk.Frame(outer)
        table_frame.pack(fill="both", expand=True)

        cols = ("id", "hex", "offset", "size", "sig", "codec", "channels", "rate")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="extended")
        headings = {
            "id": "Media ID",
            "hex": "ID (hex)",
            "offset": "DATA offset",
            "size": "Size",
            "sig": "Header",
            "codec": "Codec",
            "channels": "Ch",
            "rate": "Hz",
        }
        widths = {"id": 105, "hex": 105, "offset": 105, "size": 95, "sig": 70, "codec": 190, "channels": 45, "rate": 80}
        for c in cols:
            self.tree.heading(c, text=headings[c])
            self.tree.column(c, width=widths[c], anchor="center")
        self.tree.column("codec", anchor="w")

        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _event: self.play_selected())

        button_row = ttk.Frame(outer)
        button_row.pack(fill="x", pady=(8, 0))
        ttk.Button(button_row, text="▶ Play selected", command=self.play_selected).pack(side="left", padx=(0, 6))
        ttk.Button(button_row, text="■ Stop", command=self.stop_playback).pack(side="left", padx=(0, 12))
        ttk.Button(button_row, text="Extract selected WEM", command=self.extract_selected).pack(side="left", padx=(0, 6))
        ttk.Button(button_row, text="Extract all WEM", command=self.extract_all).pack(side="left", padx=(0, 6))
        ttk.Button(button_row, text="Convert selected -> FLAC", command=self.convert_selected).pack(side="left", padx=(12, 6))
        ttk.Button(button_row, text="Convert all -> FLAC", command=self.convert_all).pack(side="left", padx=(0, 6))

        self.progress = ttk.Progressbar(outer, mode="determinate")
        self.progress.pack(fill="x", pady=(10, 4))
        ttk.Label(outer, textvariable=self.status_var, anchor="w").pack(fill="x")

    def path_row(self, parent, label, var, command, row):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", pady=3)
        ttk.Button(parent, text="Browse...", command=command).grid(row=row, column=2, padx=(8, 0), pady=3)

    def pick_vgmstream(self):
        p = filedialog.askopenfilename(title="Select vgmstream-cli.exe", filetypes=[("Executable", "*.exe"), ("All files", "*.*")])
        if p:
            self.vgmstream_var.set(p)
            self.save_config()

    def pick_ffmpeg(self):
        p = filedialog.askopenfilename(title="Select ffmpeg.exe", filetypes=[("Executable", "*.exe"), ("All files", "*.*")])
        if p:
            self.ffmpeg_var.set(p)

    def pick_output(self):
        p = filedialog.askdirectory(title="Select output folder")
        if p:
            self.output_var.set(p)

    def pick_external_folder(self):
        p = filedialog.askdirectory(title="Select folder containing external WEM files")
        if not p:
            return
        self.external_folder_var.set(p)
        if self.bnk_path is not None and self.raw is not None:
            self.load_external_folder(Path(p))

    def open_bnk(self):
        p = filedialog.askopenfilename(title="Open Wwise BNK", filetypes=[("Wwise SoundBank", "*.bnk"), ("All files", "*.*")])
        if not p:
            return

        path = Path(p)
        self.stop_playback(update_status=False)
        try:
            raw, bkhd_size, data_size, entries = parse_bnk(path)
        except Exception as e:
            messagebox.showerror("BNK parse error", str(e))
            return

        self.bnk_path = path
        self.raw = raw
        self.entries = entries
        self.bkhd_size = bkhd_size
        self.data_size = data_size
        self.external_mode = not bool(entries)
        self.file_label.config(text=str(path))
        self.output_var.set(str(path.parent / f"{path.stem}_flac"))
        if entries:
            self.external_folder_var.set("")
            self.bank_info.config(
                text=f"BKHD size: 0x{bkhd_size:X} ({bkhd_size} B)    |    "
                     f"DATA size: 0x{data_size:X} ({human_size(data_size)})    |    "
                     f"Embedded media: {len(entries)}"
            )
            self.populate_tree()
            self.status_var.set(f"Loaded {path.name}: {len(entries)} embedded WEM file(s).")
            return

        self.populate_tree()
        self.bank_info.config(
            text=f"BKHD size: 0x{bkhd_size:X} ({bkhd_size} B)    |    "
                 "External-media bank (no DIDX + DATA)"
        )
        self.status_var.set(f"Loaded {path.name}: select the folder containing its external WEM files.")

        if messagebox.askyesno(
            "External WEM files",
            "This BNK contains no embedded media.\n\nSelect the folder containing external .wem files now?",
        ):
            folder = filedialog.askdirectory(title="Select folder containing external WEM files")
            if folder:
                self.external_folder_var.set(folder)
                self.load_external_folder(Path(folder))

    def load_external_folder(self, folder: Path):
        if self.raw is None or self.bnk_path is None:
            messagebox.showwarning("No BNK", "Open a BNK file first.")
            return
        if not folder.is_dir():
            messagebox.showerror("WEM folder", "The selected WEM folder does not exist.")
            return

        entries, total, matched = scan_external_wems(folder, self.raw)
        self.entries = entries
        self.external_mode = True
        self.populate_tree()

        bkhd_text = f"0x{self.bkhd_size:X}" if self.bkhd_size is not None else "?"
        if matched:
            detail = f"Referenced external media: {len(entries)} of {total} WEM file(s)"
            self.status_var.set(
                f"Matched {len(entries)} WEM file(s) referenced by {self.bnk_path.name}; "
                f"folder contains {total}."
            )
        else:
            detail = f"External media: {len(entries)} WEM file(s) (no ID matches; showing all)"
            self.status_var.set(
                f"No numeric WEM IDs could be matched to {self.bnk_path.name}; showing all {total} file(s)."
            )
        self.bank_info.config(text=f"BKHD size: {bkhd_text}    |    {detail}")

    def populate_tree(self):
        self.tree.delete(*self.tree.get_children())
        for index, e in enumerate(self.entries):
            hex_id = f"0x{e.media_id:08X}" if e.media_id is not None else "-"
            offset = "external" if e.source_path is not None else f"0x{e.offset:X}"
            self.tree.insert(
                "", "end", iid=str(index),
                values=(
                    e.display_id,
                    hex_id,
                    offset,
                    human_size(e.size),
                    e.signature,
                    e.codec,
                    e.channels,
                    e.sample_rate,
                )
            )

    def get_selected_entries(self):
        if not self.entries:
            messagebox.showwarning("No BNK", "Open a BNK file first.")
            return []
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("No selection", "Select one or more media entries first.")
            return []
        return [self.entries[int(i)] for i in selected]

    def get_single_selected_entry(self) -> WemEntry | None:
        if not self.entries:
            messagebox.showwarning("No BNK", "Open a BNK file first.")
            return None
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("No selection", "Select a media entry first.")
            return None
        if len(selected) != 1:
            messagebox.showwarning("Multiple selection", "Select exactly one media entry to play.")
            return None
        return self.entries[int(selected[0])]

    def output_dir(self) -> Path | None:
        value = self.output_var.get().strip()
        if not value:
            messagebox.showwarning("Output folder", "Choose an output folder first.")
            return None
        p = Path(value)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_blob(self, e: WemEntry) -> bytes:
        if e.source_path is not None:
            return e.source_path.read_bytes()
        assert self.raw is not None
        return self.raw[e.absolute_start:e.absolute_end]

    def extract_entries(self, entries: list[WemEntry], out_dir: Path):
        wem_dir = out_dir / "wem"
        wem_dir.mkdir(parents=True, exist_ok=True)
        for e in entries:
            (wem_dir / e.file_name).write_bytes(self.get_blob(e))
        return wem_dir

    def extract_selected(self):
        entries = self.get_selected_entries()
        out = self.output_dir()
        if entries and out:
            self.run_worker(self._extract_worker, entries, out)

    def extract_all(self):
        out = self.output_dir()
        if self.entries and out:
            self.run_worker(self._extract_worker, list(self.entries), out)

    def _extract_worker(self, entries, out):
        self.set_progress(0, len(entries))
        wem_dir = out / "wem"
        wem_dir.mkdir(parents=True, exist_ok=True)
        for i, e in enumerate(entries, 1):
            self.set_status(f"Extracting {e.file_name}  [{i}/{len(entries)}]")
            (wem_dir / e.file_name).write_bytes(self.get_blob(e))
            self.set_progress(i, len(entries))
        self.set_status(f"Done. Extracted {len(entries)} WEM file(s) to: {wem_dir}")

    def validate_vgmstream(self) -> Path | None:
        value = self.vgmstream_var.get().strip()
        if not value or not Path(value).is_file():
            messagebox.showerror("vgmstream", "Select a valid vgmstream-cli.exe first.")
            return None
        return Path(value)

    def play_selected(self):
        if winsound is None:
            messagebox.showerror("Playback", "Built-in playback is available on Windows only.")
            return

        entry = self.get_single_selected_entry()
        if entry is None:
            return
        vgmstream = self.validate_vgmstream()
        if vgmstream is None:
            return

        self.stop_playback(update_status=False)
        self.preview_token += 1
        token = self.preview_token
        threading.Thread(
            target=self._play_worker,
            args=(entry, vgmstream, token),
            daemon=True,
        ).start()

    def _play_worker(self, entry: WemEntry, vgmstream: Path, token: int):
        temp_dir_handle = tempfile.TemporaryDirectory(prefix="wwise_preview_")
        temp_dir = Path(temp_dir_handle.name)
        temp_wem = temp_dir / entry.file_name
        temp_wav = temp_dir / f"{entry.output_stem}.wav"

        try:
            self.set_status(f"Decoding preview: {entry.file_name}...")
            temp_wem.write_bytes(self.get_blob(entry))
            result = subprocess.run(
                [str(vgmstream), "-i", "-o", str(temp_wav), str(temp_wem)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

            if result.returncode != 0 or not temp_wav.exists():
                details = (result.stderr or result.stdout).strip() or "vgmstream did not create a WAV file."
                temp_dir_handle.cleanup()
                if token != self.preview_token:
                    return
                self.root.after(0, lambda text=details: messagebox.showerror("Playback error", text))
                self.set_status(f"Could not play {entry.file_name}.")
                return

            if token != self.preview_token:
                temp_dir_handle.cleanup()
                return

            self.preview_temp_dir = temp_dir_handle
            winsound.PlaySound(
                str(temp_wav),
                winsound.SND_FILENAME | winsound.SND_ASYNC,
            )
            self.set_status(f"Playing: {entry.file_name}")
        except Exception as e:
            temp_dir_handle.cleanup()
            self.root.after(0, lambda text=str(e): messagebox.showerror("Playback error", text))
            self.set_status(f"Playback error: {e}")

    def stop_playback(self, update_status: bool = True):
        self.preview_token += 1
        if winsound is not None:
            try:
                winsound.PlaySound(None, 0)
            except RuntimeError:
                pass

        if self.preview_temp_dir is not None:
            try:
                self.preview_temp_dir.cleanup()
            except OSError:
                pass
            self.preview_temp_dir = None

        if update_status:
            self.status_var.set("Playback stopped.")

    def validate_tools(self):
        vg = self.vgmstream_var.get().strip()
        ff = self.ffmpeg_var.get().strip()
        if not vg or not Path(vg).exists():
            self.root.after(0, lambda: messagebox.showerror("vgmstream", "Select a valid vgmstream-cli.exe."))
            return None
        if not ff or not Path(ff).exists():
            self.root.after(0, lambda: messagebox.showerror("FFmpeg", "Select a valid ffmpeg.exe."))
            return None
        return Path(vg), Path(ff)

    def convert_selected(self):
        entries = self.get_selected_entries()
        out = self.output_dir()
        if entries and out:
            self.run_worker(self._convert_worker, entries, out)

    def convert_all(self):
        out = self.output_dir()
        if self.entries and out:
            self.run_worker(self._convert_worker, list(self.entries), out)

    def _convert_worker(self, entries: list[WemEntry], out_dir: Path):
        tools = self.validate_tools()
        if not tools:
            return
        vg, ff = tools

        flac_dir = out_dir / "flac"
        flac_dir.mkdir(parents=True, exist_ok=True)
        if self.keep_wem_var.get():
            wem_dir = out_dir / "wem"
            wem_dir.mkdir(parents=True, exist_ok=True)
        else:
            wem_dir = None

        self.set_progress(0, len(entries))
        failures = []

        with tempfile.TemporaryDirectory(prefix="wwise_bnk_") as td:
            temp_dir = Path(td)

            for i, e in enumerate(entries, 1):
                self.set_status(f"[{i}/{len(entries)}] Converting {e.file_name}...")
                temp_wem = temp_dir / e.file_name
                temp_wav = temp_dir / f"{e.output_stem}.wav"
                out_flac = flac_dir / f"{e.output_stem}.flac"
                blob = self.get_blob(e)
                temp_wem.write_bytes(blob)

                if wem_dir is not None:
                    (wem_dir / temp_wem.name).write_bytes(blob)

                dec = subprocess.run(
                    [str(vg), "-i", "-o", str(temp_wav), str(temp_wem)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                if dec.returncode != 0 or not temp_wav.exists():
                    failures.append((e.display_id, "vgmstream", (dec.stderr or dec.stdout).strip()))
                    self.set_progress(i, len(entries))
                    continue

                enc = subprocess.run(
                    [
                        str(ff), "-y", "-loglevel", "error",
                        "-i", str(temp_wav),
                        "-map_metadata", "-1",
                        "-c:a", "flac", "-compression_level", "8",
                        str(out_flac),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                if enc.returncode != 0 or not out_flac.exists():
                    failures.append((e.display_id, "ffmpeg", (enc.stderr or enc.stdout).strip()))

                self.set_progress(i, len(entries))

        if failures:
            report = out_dir / "conversion_errors.txt"
            with report.open("w", encoding="utf-8") as f:
                for media_id, stage, text in failures:
                    f.write(f"Media {media_id} - {stage}\n{text}\n\n")
            self.set_status(
                f"Finished with {len(failures)} error(s). FLAC: {flac_dir} | Details: {report.name}"
            )
        else:
            self.set_status(f"Done. Converted {len(entries)} file(s) to FLAC: {flac_dir}")

    def run_worker(self, func, *args):
        threading.Thread(target=self._worker_guard, args=(func, args), daemon=True).start()

    def _worker_guard(self, func, args):
        try:
            func(*args)
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Error", str(e)))
            self.set_status(f"Error: {e}")

    def set_status(self, text: str):
        self.root.after(0, lambda: self.status_var.set(text))

    def set_progress(self, value: int, maximum: int):
        def apply():
            self.progress["maximum"] = max(maximum, 1)
            self.progress["value"] = value
        self.root.after(0, apply)


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
