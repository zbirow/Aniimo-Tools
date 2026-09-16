import os
import subprocess
import tempfile
import threading
import winsound
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


class WemTool:
    def __init__(self, root):
        self.root = root
        self.root.title("WEM Audio Tool")
        self.root.geometry("1000x650")

        self.folder = None
        self.vgmstream = self.find_vgmstream()
        self.temp_wav = None

        self.build_ui()

    def find_vgmstream(self):
        # Najpierw szuka obok skryptu
        local = Path(__file__).parent / "vgmstream-cli.exe"

        if local.exists():
            return local

        return None

    def build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        ttk.Button(
            top,
            text="Select WEM folder",
            command=self.select_folder
        ).pack(side="left", padx=4)

        ttk.Button(
            top,
            text="Select vgmstream-cli.exe",
            command=self.select_vgmstream
        ).pack(side="left", padx=4)

        self.vgm_label = ttk.Label(
            top,
            text=self.vgmstream_status()
        )
        self.vgm_label.pack(side="left", padx=15)

        # tabela
        table_frame = ttk.Frame(self.root, padding=(10, 0))
        table_frame.pack(fill="both", expand=True)

        columns = (
            "name",
            "size",
        )

        self.tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            selectmode="browse"
        )

        self.tree.heading("name", text="File")
        self.tree.heading("size", text="Size")

        self.tree.column("name", width=700)
        self.tree.column("size", width=120, anchor="e")

        scrollbar = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.tree.yview
        )

        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.tree.bind("<Double-1>", lambda e: self.play_selected())

        # przyciski
        buttons = ttk.Frame(self.root, padding=10)
        buttons.pack(fill="x")

        ttk.Button(
            buttons,
            text="▶ Play",
            command=self.play_selected
        ).pack(side="left", padx=3)

        ttk.Button(
            buttons,
            text="■ Stop",
            command=self.stop_audio
        ).pack(side="left", padx=3)

        ttk.Button(
            buttons,
            text="Info",
            command=self.show_info
        ).pack(side="left", padx=3)

        ttk.Button(
            buttons,
            text="Convert selected to WAV",
            command=self.convert_selected
        ).pack(side="left", padx=3)

        ttk.Button(
            buttons,
            text="Convert all to WAV",
            command=self.convert_all
        ).pack(side="left", padx=3)

        # info
        info_frame = ttk.LabelFrame(
            self.root,
            text="vgmstream info",
            padding=8
        )
        info_frame.pack(
            fill="both",
            padx=10,
            pady=(0, 10)
        )

        self.info = tk.Text(
            info_frame,
            height=10,
            wrap="word"
        )

        self.info.pack(fill="both", expand=True)

        # status
        self.status = ttk.Label(
            self.root,
            text="Ready",
            anchor="w"
        )

        self.status.pack(fill="x", padx=10, pady=(0, 8))

    def vgmstream_status(self):
        if self.vgmstream:
            return f"vgmstream: {self.vgmstream}"
        return "vgmstream: NOT FOUND"

    def select_vgmstream(self):
        file = filedialog.askopenfilename(
            title="Select vgmstream-cli.exe",
            filetypes=[
                ("vgmstream", "vgmstream-cli.exe"),
                ("Executable", "*.exe"),
                ("All files", "*.*")
            ]
        )

        if not file:
            return

        self.vgmstream = Path(file)
        self.vgm_label.config(
            text=self.vgmstream_status()
        )

    def select_folder(self):
        folder = filedialog.askdirectory(
            title="Select folder containing WEM files"
        )

        if not folder:
            return

        self.folder = Path(folder)
        self.scan_folder()

    def scan_folder(self):
        self.tree.delete(*self.tree.get_children())

        files = sorted(
            self.folder.glob("*.wem")
        )

        for file in files:
            size = self.format_size(file.stat().st_size)

            self.tree.insert(
                "",
                "end",
                values=(
                    file.name,
                    size
                )
            )

        self.status.config(
            text=f"Found {len(files)} WEM files"
        )

    @staticmethod
    def format_size(size):
        if size < 1024:
            return f"{size} B"

        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"

        return f"{size / 1024 / 1024:.2f} MB"

    def get_selected_file(self):
        selected = self.tree.selection()

        if not selected:
            messagebox.showwarning(
                "No file",
                "Select a WEM file first."
            )
            return None

        filename = self.tree.item(
            selected[0]
        )["values"][0]

        return self.folder / filename

    def check_vgmstream(self):
        if not self.vgmstream:
            messagebox.showerror(
                "vgmstream not found",
                "Select vgmstream-cli.exe first."
            )
            return False

        if not self.vgmstream.exists():
            messagebox.showerror(
                "vgmstream not found",
                "vgmstream-cli.exe does not exist."
            )
            return False

        return True

    def show_info(self):
        file = self.get_selected_file()

        if not file:
            return

        if not self.check_vgmstream():
            return

        try:
            result = subprocess.run(
                [
                    str(self.vgmstream),
                    "-m",
                    str(file)
                ],
                capture_output=True,
                text=True,
                errors="replace"
            )

            output = result.stdout

            if result.stderr:
                output += "\n" + result.stderr

            self.info.delete(
                "1.0",
                tk.END
            )

            self.info.insert(
                tk.END,
                output
            )

        except Exception as e:
            messagebox.showerror(
                "Error",
                str(e)
            )

    def play_selected(self):
        file = self.get_selected_file()

        if not file:
            return

        if not self.check_vgmstream():
            return

        threading.Thread(
            target=self._play_worker,
            args=(file,),
            daemon=True
        ).start()

    def _play_worker(self, file):
        try:
            self.stop_audio()

            temp_dir = Path(
                tempfile.gettempdir()
            )

            self.temp_wav = (
                temp_dir /
                "wem_tool_preview.wav"
            )

            self.status.config(
                text=f"Decoding {file.name}..."
            )

            result = subprocess.run(
                [
                    str(self.vgmstream),
                    "-o",
                    str(self.temp_wav),
                    str(file)
                ],
                capture_output=True
            )

            if result.returncode != 0:
                self.status.config(
                    text="Decode failed"
                )
                return

            self.status.config(
                text=f"Playing: {file.name}"
            )

            winsound.PlaySound(
                str(self.temp_wav),
                winsound.SND_FILENAME |
                winsound.SND_ASYNC
            )

        except Exception as e:
            self.root.after(
                0,
                lambda: messagebox.showerror(
                    "Playback error",
                    str(e)
                )
            )

    def stop_audio(self):
        winsound.PlaySound(
            None,
            winsound.SND_PURGE
        )

        self.status.config(
            text="Stopped"
        )

    def convert_selected(self):
        file = self.get_selected_file()

        if not file:
            return

        if not self.check_vgmstream():
            return

        output = filedialog.asksaveasfilename(
            title="Save WAV",
            defaultextension=".wav",
            initialfile=file.stem + ".wav",
            filetypes=[
                ("WAV audio", "*.wav")
            ]
        )

        if not output:
            return

        threading.Thread(
            target=self._convert_one,
            args=(file, Path(output)),
            daemon=True
        ).start()

    def _convert_one(self, source, output):
        try:
            self.status.config(
                text=f"Converting {source.name}..."
            )

            result = subprocess.run(
                [
                    str(self.vgmstream),
                    "-o",
                    str(output),
                    str(source)
                ],
                capture_output=True
            )

            if result.returncode == 0:
                self.status.config(
                    text=f"Saved: {output.name}"
                )

            else:
                self.status.config(
                    text="Conversion failed"
                )

        except Exception as e:
            self.root.after(
                0,
                lambda: messagebox.showerror(
                    "Conversion error",
                    str(e)
                )
            )

    def convert_all(self):
        if not self.folder:
            return

        if not self.check_vgmstream():
            return

        output_folder = filedialog.askdirectory(
            title="Select output folder"
        )

        if not output_folder:
            return

        files = sorted(
            self.folder.glob("*.wem")
        )

        threading.Thread(
            target=self._convert_all_worker,
            args=(files, Path(output_folder)),
            daemon=True
        ).start()

    def _convert_all_worker(self, files, output_folder):
        total = len(files)

        for i, file in enumerate(files, 1):
            output = (
                output_folder /
                (file.stem + ".wav")
            )

            self.status.config(
                text=f"[{i}/{total}] {file.name}"
            )

            subprocess.run(
                [
                    str(self.vgmstream),
                    "-o",
                    str(output),
                    str(file)
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

        self.status.config(
            text=f"Finished. Converted {total} files."
        )


if __name__ == "__main__":
    root = tk.Tk()
    app = WemTool(root)
    root.mainloop()