"""Tkinter front end for rendering posters from local OSM data."""

from collections import Counter
import json
import os
import math
from pathlib import Path
from queue import Empty, Queue
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import tkinter as tk
from tkinter import filedialog, font, messagebox, ttk

from PIL import Image, ImageTk


PROJECT = Path(__file__).resolve().parent
MAX_PIXELS = 12000
DPI = 300


class SuggestionCombobox(ttk.Combobox):
    def __init__(self, parent, variable, values=()):
        super().__init__(parent, textvariable=variable, values=values)
        self.variable = variable
        self.choices = tuple(values)
        self.suggestions = tk.Listbox(
            self.winfo_toplevel(), exportselection=False, activestyle="none", takefocus=False
        )
        self.suggestions.bind("<Button-1>", self.choose_by_mouse)
        self.bind("<FocusOut>", self.hide_suggestions)
        self.bind("<Escape>", self.hide_suggestions)
        self.bind("<Down>", lambda event: self.move_selection(1))
        self.bind("<Up>", lambda event: self.move_selection(-1))
        self.bind("<Return>", self.choose_active)
        self.bind("<<ComboboxSelected>>", self.hide_suggestions)
        self.variable.trace_add("write", self.refresh_suggestions)

    def set_choices(self, values):
        self.choices = tuple(values)
        self.refresh_suggestions()

    def refresh_suggestions(self, *_):
        query = self.variable.get().strip().casefold()
        matches = [value for value in self.choices if query in value.casefold()]
        if query:
            matches.sort(key=lambda value: not value.casefold().startswith(query))
        self.configure(values=matches)
        # The native combobox list is a Tcl widget without a Tkinter wrapper.
        has_focus = str(self.tk.call("focus")) == str(self)
        if not query or not matches or not has_focus or matches == [self.variable.get()]:
            self.hide_suggestions()
            return

        self.suggestions.delete(0, tk.END)
        self.suggestions.insert(tk.END, *matches)
        self.suggestions.configure(height=min(len(matches), 7))
        self.suggestions.place(in_=self, rely=1, relwidth=1)
        self.suggestions.lift()

    def hide_suggestions(self, _event=None):
        self.suggestions.place_forget()

    def move_selection(self, direction):
        if not self.suggestions.winfo_manager():
            return None
        current = self.suggestions.curselection()
        index = current[0] + direction if current else 0
        index = max(0, min(index, self.suggestions.size() - 1))
        self.suggestions.selection_clear(0, tk.END)
        self.suggestions.selection_set(index)
        self.suggestions.see(index)
        return "break"

    def choose_active(self, _event):
        if not self.suggestions.winfo_manager():
            return None
        selection = self.suggestions.curselection()
        self.choose(selection[0] if selection else 0)
        return "break"

    def choose_by_mouse(self, event):
        self.choose(self.suggestions.nearest(event.y))
        return "break"

    def choose(self, index):
        self.set(self.suggestions.get(index))
        self.icursor(tk.END)
        self.hide_suggestions()


class PosterWindow:
    def __init__(self, root, output):
        self.root = root
        self.output = output
        self.events = Queue()
        self.worker = None
        self.city_events = Queue()
        self.city_process = None
        self.city_choices = {}
        self.preview_command = None
        self.export_path = None
        root.title("MapToPoster · 本地地图海报")
        root.geometry("1100x730")
        root.minsize(860, 660)
        root.protocol("WM_DELETE_WINDOW", self.close)

        panel = ttk.Frame(root, padding=16)
        panel.pack(side="right", fill="y")
        panel.columnconfigure(1, weight=1)

        data = sorted(
            path for path in (PROJECT / "osm").glob("*")
            if path.is_file() and path.suffix.lower() in {".osm", ".pbf"}
        )
        self.data_files = {path.name: path for path in data}
        self.osm = tk.StringVar(value=next(iter(self.data_files), ""))
        ttk.Label(panel, text="本地地图").grid(row=0, column=0, sticky="w")
        self.osm_menu = ttk.Combobox(
            panel, textvariable=self.osm, values=list(self.data_files),
            width=27, state="readonly",
        )
        self.osm_menu.grid(
            row=0, column=1, sticky="ew", padx=8, pady=4
        )
        self.osm_menu.bind("<<ComboboxSelected>>", self.load_city_choices)
        ttk.Button(panel, text="浏览…", command=self.browse).grid(row=0, column=2)

        themes = sorted(path.stem for path in (PROJECT / "themes").glob("*.json"))
        self.theme = tk.StringVar(value="terracotta" if "terracotta" in themes else next(iter(themes), ""))
        ttk.Label(panel, text="主题").grid(row=1, column=0, sticky="w")
        ttk.Combobox(panel, textvariable=self.theme, values=themes, state="readonly").grid(
            row=1, column=1, sticky="ew", padx=8, pady=4
        )

        self.fields = {}
        fields = (
            ("city", "城市", ""), ("country", "国家", "China"),
            ("latitude", "纬度（可选）", ""), ("longitude", "经度（可选）", ""),
            ("distance", "地图范围（千米）", "18"),
            ("width", "宽度", "3600"), ("height", "高度", "4800"),
        )
        for row, (key, label, default) in enumerate(fields, start=2):
            self.fields[key] = tk.StringVar(value=default)
            ttk.Label(panel, text=label).grid(row=row, column=0, sticky="w")
            if key == "city":
                self.city_menu = SuggestionCombobox(panel, self.fields[key])
                entry = self.city_menu
            else:
                entry = ttk.Entry(panel, textvariable=self.fields[key])
            entry.grid(
                row=row, column=1, sticky="ew", padx=8, pady=4
            )

        self.size_unit = tk.StringVar(value="像素")
        self.current_unit = "像素"
        ttk.Label(panel, text="尺寸单位").grid(row=9, column=0, sticky="w")
        units = ttk.Combobox(
            panel, textvariable=self.size_unit, values=("像素", "英寸"), state="readonly"
        )
        units.grid(row=9, column=1, sticky="ew", padx=8, pady=4)
        units.bind("<<ComboboxSelected>>", self.change_size_unit)

        self.font_family = tk.StringVar()
        ttk.Label(panel, text="字体（可选）").grid(row=10, column=0, sticky="w")
        families = sorted({name for name in font.families(root) if not name.startswith("@")})
        SuggestionCombobox(panel, self.font_family, families).grid(
            row=10, column=1, sticky="ew", padx=8, pady=4
        )

        self.offset = tk.StringVar(value="0")
        ttk.Label(panel, text="文字上移（%）").grid(row=11, column=0, sticky="w")
        ttk.Spinbox(panel, from_=-4, to=70, increment=0.1, textvariable=self.offset).grid(
            row=11, column=1, sticky="ew", padx=8, pady=4
        )
        ttk.Button(panel, text="复位", command=lambda: self.offset.set("0")).grid(row=11, column=2)

        self.map_offset_x = tk.StringVar(value="0")
        self.map_offset_y = tk.StringVar(value="0")
        for row, label, variable in (
            (12, "城市中心右移（%）", self.map_offset_x),
            (13, "城市中心上移（%）", self.map_offset_y),
        ):
            ttk.Label(panel, text=label).grid(row=row, column=0, sticky="w")
            ttk.Spinbox(
                panel, from_=-50, to=50, increment=1, textvariable=variable
            ).grid(row=row, column=1, sticky="ew", padx=8, pady=4)
        ttk.Button(panel, text="复位", command=self.reset_map_offsets).grid(row=12, column=2)

        self.attribution = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            panel, text="显示 OpenStreetMap 署名", variable=self.attribution
        ).grid(row=14, column=1, sticky="w", padx=8, pady=4)
        self.show_coordinates = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            panel, text="显示经纬度文字", variable=self.show_coordinates
        ).grid(row=15, column=1, sticky="w", padx=8)

        actions = ttk.Frame(panel)
        actions.grid(row=16, column=0, columnspan=3, sticky="w", pady=8)
        self.generate = ttk.Button(actions, text="生成预览", command=self.start)
        self.generate.pack(side="left")
        self.save = ttk.Button(actions, text="保存 PNG", command=self.save_png, state="disabled")
        self.save.pack(side="left", padx=8)
        self.status = tk.StringVar(value="请选择地图数据并输入城市")
        ttk.Label(panel, textvariable=self.status, wraplength=340).grid(
            row=17, column=0, columnspan=3, sticky="w"
        )
        self.city_status = tk.StringVar()
        ttk.Label(panel, textvariable=self.city_status, wraplength=340).grid(
            row=18, column=0, columnspan=3, sticky="w"
        )
        self.preview = tk.Canvas(root, bg="#26282c", highlightthickness=0)
        self.preview.pack(side="left", fill="both", expand=True, padx=(16, 0), pady=16)
        self.preview.bind("<Configure>", self.draw_preview)
        self.image = None
        self.thumbnail = None
        self.root.after_idle(self.load_city_choices)

    def reset_map_offsets(self):
        self.map_offset_x.set("0")
        self.map_offset_y.set("0")

    def browse(self):
        filename = filedialog.askopenfilename(
            initialdir=PROJECT / "osm",
            filetypes=[("OpenStreetMap", "*.osm *.pbf"), ("所有文件", "*.*")],
        )
        if filename:
            path = Path(filename)
            self.data_files[path.name] = path
            self.osm_menu.configure(values=sorted(self.data_files))
            self.osm.set(path.name)
            self.load_city_choices()

    def load_city_choices(self, _event=None):
        if self.city_process is not None:
            self.city_process.terminate()
            self.city_process = None
        if self.fields["city"].get() in self.city_choices:
            self.fields["city"].set("")
        self.city_choices.clear()
        self.city_menu.set_choices(())
        path = self.data_files.get(self.osm.get())
        if path is None:
            self.city_status.set("")
            return

        try:
            process = subprocess.Popen(
                [sys.executable, str(PROJECT / "create_map_poster.py"),
                 "--list-local-cities", "--osm-file", str(path)],
                cwd=PROJECT, env=dict(os.environ, PYTHONIOENCODING="utf-8", MPLBACKEND="Agg"),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding="utf-8",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            self.city_status.set("无法读取城市列表，可手动输入。")
            return
        self.city_process = process
        self.city_status.set("正在读取城市列表…")
        Thread(target=self.collect_cities, args=(process,), daemon=True).start()
        self.root.after(100, self.poll_cities, process)

    def collect_cities(self, process):
        output, _ = process.communicate()
        cities = None
        if process.returncode == 0 and output.strip():
            try:
                cities = json.loads(output.strip().splitlines()[-1])
            except ValueError:
                pass
        self.city_events.put((process, cities))

    def poll_cities(self, process):
        if self.city_process is not process:
            return
        try:
            while True:
                completed, cities = self.city_events.get_nowait()
                if completed is process:
                    break
        except Empty:
            self.root.after(100, self.poll_cities, process)
            return
        self.city_process = None
        if not cities:
            message = "未找到城市，可手动输入。" if cities == [] else "无法读取城市列表，可手动输入。"
            self.city_status.set(message)
            return
        counts = Counter(name for name, _, _ in cities)
        for name, latitude, longitude in cities:
            label = name
            if counts[name] > 1:
                label = f"{name} ({latitude}, {longitude})"
            self.city_choices[label] = (name, latitude, longitude)
        self.city_menu.set_choices(self.city_choices)
        self.city_status.set(f"已找到 {len(self.city_choices)} 个城市和城镇")

    def change_size_unit(self, _event=None):
        unit = self.size_unit.get()
        if unit == self.current_unit:
            return
        try:
            width, height = self.pixel_dimensions(
                self.fields["width"].get().strip(),
                self.fields["height"].get().strip(),
            )
        except ValueError as error:
            self.size_unit.set(self.current_unit)
            messagebox.showerror("请检查尺寸", str(error), parent=self.root)
            return
        for key, size in zip(("width", "height"), (width, height)):
            value = f"{size / DPI:.6f}".rstrip("0").rstrip(".") if unit == "英寸" else str(size)
            self.fields[key].set(value)
        self.current_unit = unit

    def pixel_dimensions(self, width, height):
        if self.current_unit == "英寸":
            try:
                inches = (float(width), float(height))
            except ValueError:
                inches = ()
            max_inches = MAX_PIXELS // DPI
            if not inches or not all(1 <= size <= max_inches for size in inches):
                raise ValueError(f"宽度和高度须在 1 到 {max_inches} 英寸之间。")
            return tuple(round(size * DPI) for size in inches)
        if not all(size.isdecimal() and 300 <= int(size) <= MAX_PIXELS for size in (width, height)):
            raise ValueError(f"宽度和高度须为 300 到 {MAX_PIXELS} 之间的整数像素。")
        return int(width), int(height)

    def start(self):
        values = {key: var.get().strip() for key, var in self.fields.items()}
        path = self.data_files.get(self.osm.get())
        try:
            if path is None or not path.is_file() or path.suffix.lower() not in {".osm", ".pbf"}:
                raise ValueError("请选择有效的 .osm 或 .pbf 文件。")
            if not values["city"] or not values["country"] or not self.theme.get():
                raise ValueError("请填写城市和国家，并选择主题。")
            try:
                distance_meters = float(values["distance"]) * 1000
            except ValueError:
                distance_meters = 0
            if not math.isfinite(distance_meters) or distance_meters < 1:
                raise ValueError("地图范围请输入至少 0.001 千米的数值。")
            values["distance"] = str(round(distance_meters))
            width, height = self.pixel_dimensions(values.pop("width"), values.pop("height"))
            latitude, longitude = values["latitude"], values["longitude"]
            if bool(latitude) != bool(longitude):
                raise ValueError("请同时填写纬度和经度，或将两项留空。")
            if latitude and not (-90 <= float(latitude) <= 90 and -180 <= float(longitude) <= 180):
                raise ValueError("纬度须在 -90 到 90，经度须在 -180 到 180。")
            offset = float(self.offset.get())
            if not math.isfinite(offset) or not -4 <= offset <= 70:
                raise ValueError("文字上移百分比须在 -4 到 70 之间。")
            try:
                map_x, map_y = float(self.map_offset_x.get()), float(self.map_offset_y.get())
            except ValueError:
                map_x = map_y = math.nan
            if not all(-50 <= value <= 50 for value in (map_x, map_y)):
                raise ValueError("城市中心的右移和上移百分比须在 -50 到 50 之间。")
        except ValueError as error:
            messagebox.showerror("请检查设置", str(error), parent=self.root)
            return

        selected_city = self.city_choices.get(values["city"])
        if selected_city:
            name, latitude, longitude = selected_city
            values["city"] = name
            if not values["latitude"]:
                values["latitude"] = str(latitude)
                values["longitude"] = str(longitude)

        command = [
            sys.executable, "-u", str(PROJECT / "create_map_poster.py"),
            "--osm-file", str(path), "--theme", self.theme.get(),
            f"--text-offset-y={offset / 100}", "--pixels", str(width), str(height),
            f"--map-offset-x={map_x / 100}", f"--map-offset-y={map_y / 100}",
        ]
        if not self.attribution.get():
            command.append("--hide-attribution")
        if not self.show_coordinates.get():
            command.append("--hide-coordinates")
        for key, value in values.items():
            if value:
                command.append(f"--{key}={value}")
        if self.font_family.get().strip():
            command.append(f"--font-family={self.font_family.get().strip()}")

        self.preview_command = command
        self.export_path = None
        self.image = None
        self.draw_preview()
        self.run(command + ["--preview"], "正在生成预览…")

    def run(self, command, status):
        self.output.unlink(missing_ok=True)
        self.generate.state(["disabled"])
        self.save.state(["disabled"])
        self.status.set(status)
        self.worker = Thread(
            target=self.render, args=(command + ["--output", str(self.output)],)
        )
        self.worker.start()
        self.root.after(100, self.poll)

    def render(self, command):
        try:
            env = dict(os.environ, PYTHONIOENCODING="utf-8", MPLBACKEND="Agg")
            with subprocess.Popen(
                command, cwd=PROJECT, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ) as process:
                error = None
                last_line = "生成失败，请检查数据和设置。"
                for line in process.stdout:
                    line = line.strip()
                    if line:
                        last_line = line
                    if line.startswith("✗ Error:"):
                        error = line
                    if line.startswith(("[", "Looking up", "✓ Using cached")):
                        self.events.put(("progress", line))
                if process.wait() or not self.output.is_file():
                    raise RuntimeError(error or last_line)
            self.events.put(("done", ""))
        except (OSError, RuntimeError) as error:
            self.events.put(("error", str(error)))

    def poll(self):
        try:
            while True:
                kind, message = self.events.get_nowait()
                if kind == "progress":
                    self.status.set(message)
                    continue
                self.worker.join()
                self.worker = None
                self.generate.state(["!disabled"])
                if self.export_path is not None:
                    if kind == "done":
                        try:
                            shutil.copyfile(self.output, self.export_path)
                            self.status.set(f"已保存 {self.export_path}")
                        except OSError as error:
                            kind, message = "error", str(error)
                    self.export_path = None
                    self.save.state(["!disabled"])
                    if kind == "error":
                        self.status.set("保存失败")
                        messagebox.showerror("保存失败", message, parent=self.root)
                    return
                if kind == "done":
                    try:
                        with Image.open(self.output) as image:
                            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                            self.image = image.copy()
                        self.draw_preview()
                        self.save.state(["!disabled"])
                        self.status.set("预览已生成，可以保存 PNG")
                        return
                    except OSError as error:
                        message = str(error)
                self.status.set("生成失败")
                messagebox.showerror("无法预览", message, parent=self.root)
                return
        except Empty:
            self.root.after(100, self.poll)

    def draw_preview(self, _event=None):
        self.preview.delete("all")
        if self.image is None:
            self.thumbnail = None
            return
        width, height = self.preview.winfo_width(), self.preview.winfo_height()
        image = self.image.copy()
        image.thumbnail((max(1, width), max(1, height)), Image.Resampling.LANCZOS)
        self.thumbnail = ImageTk.PhotoImage(image)
        self.preview.create_image(width / 2, height / 2, image=self.thumbnail)

    def save_png(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".png", initialfile="poster.png",
            filetypes=[("PNG 海报", "*.png")],
        )
        if path:
            self.export_path = path
            self.run(self.preview_command, "正在渲染完整尺寸 PNG…")

    def close(self):
        if self.worker is not None:
            self.status.set("正在生成海报，请完成后再关闭窗口。")
            return
        if self.city_process is not None:
            self.city_process.terminate()
        self.root.destroy()


if __name__ == "__main__":
    with TemporaryDirectory(prefix="maptoposter-") as directory:
        window = tk.Tk()
        PosterWindow(window, Path(directory) / "preview.png")
        window.mainloop()
