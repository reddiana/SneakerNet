#!/usr/bin/env python3
"""sneakernet GUI (tkinter, 표준 라이브러리만 사용).

sneakernet.py 를 하위 프로세스로 실행하고 출력을 그대로 보여 준다.
화면의 글만 읽고 쓸 수 있도록 각 탭과 입력란에 설명을 붙인다.
"""
import glob
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

HERE = Path(__file__).resolve().parent
CLI = HERE / "sneakernet.py"
HINT_COLOR = "#5f6368"
WRAP = 640
_COORD_RE = re.compile(r"^[^:\s]+:[^:\s]+:[^:\s]+$")


def load_platforms() -> tuple[list[str], list[str]]:
    try:
        cfg = json.loads((HERE / "repos.json").read_text(encoding="utf-8"))
        return list(cfg["platforms"]), list(cfg["default_platforms"])
    except (OSError, ValueError, KeyError):
        return ["linux-x86_64", "windows-x86_64"], ["linux-x86_64", "windows-x86_64"]


SETTINGS = Path(os.environ.get("APPDATA") or Path.home() / ".config") / "sneakernet" / "settings.json"


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(values: dict) -> None:
    try:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(values, indent=2), encoding="utf-8")
    except OSError:
        pass   # 저장하지 못해도 동작에는 지장이 없다


def downloads_dir() -> Path:
    """사용자의 '다운로드' 폴더 (Windows 는 위치를 옮겼을 수 있어 레지스트리에서 읽는다)."""
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                return Path(os.path.expandvars(winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")[0]))
        except OSError:
            pass
    return Path.home() / "Downloads"


def find_bundle() -> str:
    """프로그램 위치 주변(반입용 zip 을 푼 자리)에서 수집 결과 폴더를 찾는다."""
    for base in (HERE.parent, HERE, Path.cwd()):
        try:
            for d in sorted(base.iterdir()):
                if d.is_dir() and (d / "_artifacts.csv").is_file():
                    return str(d)
        except OSError:
            pass
    return ""


_TOOL_GLOBS = {
    "mvn": ("mvn.cmd", [r"{pf}\*maven*\bin", r"{pf}\*\*maven*\bin", r"{drive}\*maven*\bin", r"{drive}\*\*maven*\bin",
                        r"{pf}\JetBrains\*\plugins\maven\lib\maven3\bin", r"{home}\.m2\wrapper\dists\*\*\*\bin",
                        r"{drive}\ProgramData\chocolatey\lib\maven\*\bin"]),
    "gradle": ("gradle.bat", [r"{pf}\*gradle*\bin", r"{pf}\*\*gradle*\bin", r"{drive}\*gradle*\bin",
                              r"{drive}\*\*gradle*\bin", r"{home}\.gradle\wrapper\dists\*\*\*\bin",
                              r"{drive}\ProgramData\chocolatey\lib\gradle\tools\*\bin"]),
}


def find_tool(name: str) -> str:
    """PATH 에 없더라도 흔한 설치 위치에서 mvn / gradle 을 찾아 본다. 못 찾으면 빈 문자열."""
    found = shutil.which(name)
    if found or os.name != "nt":
        return found or ""
    exe, patterns = _TOOL_GLOBS[name]
    for var in ("MAVEN_HOME", "M2_HOME") if name == "mvn" else ("GRADLE_HOME",):
        if os.environ.get(var) and (Path(os.environ[var]) / "bin" / exe).is_file():
            return str(Path(os.environ[var]) / "bin" / exe)
    places = {"pf": os.environ.get("ProgramFiles", r"C:\Program Files"), "home": str(Path.home()),
              "drive": os.environ.get("SystemDrive", "C:")}
    for pattern in patterns:
        hits = sorted(glob.glob(pattern.format(**places) + "\\" + exe))
        if hits:
            return hits[-1]
    return ""


PROGRAM_FILES = ["sneakernet.py", "sneakernet_gui.py", "repos.json", "README.md"]
START_BAT = ('@echo off\r\ncd /d "%~dp0sneakernet"\r\n'
             'where pythonw >nul 2>&1 && (start "" pythonw sneakernet_gui.py --step 2) '
             '|| (python sneakernet_gui.py --step 2)\r\n')
TRANSFER_README = """\
sneakernet transfer package
===========================

1. Extract this zip anywhere on a PC inside the air-gapped network (Python 3 required).
2. Double-click "Start sneakernet.bat"  (or run: python sneakernet\\sneakernet_gui.py).
3. Open "Step 2: Upload to Nexus", choose the "bundle" folder next to this file, fill in Nexus, press Start upload.
4. Optional: "Step 3: Check" proves that Maven / Gradle can build using only Nexus.

  sneakernet\\   the program
  bundle\\       the downloaded libraries
"""


def write_transfer_zip(bundle: Path, dest: Path, progress=lambda text: None, cancelled=lambda: False) -> int:
    """프로그램(sneakernet/)과 수집 결과(bundle/)를 zip 하나로 묶는다. 넣은 파일 수를 돌려준다."""
    program = [(HERE / n, f"sneakernet/{n}") for n in PROGRAM_FILES if (HERE / n).is_file()]
    program += [(p, "sneakernet/" + p.relative_to(HERE).as_posix()) for p in sorted((HERE / "snk").rglob("*"))
                if p.is_file() and "__pycache__" not in p.parts]
    files = [(p, "bundle/" + p.relative_to(bundle).as_posix()) for p in sorted(bundle.rglob("*")) if p.is_file()]
    stored = (".jar", ".aar", ".war", ".zip", ".exe")   # 이미 압축된 파일은 다시 압축하지 않는다
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("READ ME FIRST.txt", TRANSFER_README)
        z.writestr("Start sneakernet.bat", START_BAT)
        for i, (path, name) in enumerate(program + files, 1):
            if cancelled():
                raise InterruptedError
            z.write(path, name, zipfile.ZIP_STORED if name.endswith(stored) else zipfile.ZIP_DEFLATED)
            if i % 200 == 0:
                progress(f"  {i} / {len(program) + len(files)} files\n")
    return len(program) + len(files)


class App(tk.Tk):
    def __init__(self, step: int = 1):
        super().__init__()
        self.title("sneakernet - bring Maven / Gradle libraries into an air-gapped Nexus")
        self.geometry("900x860")
        self.minsize(760, 600)
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue = queue.Queue()
        self.on_success = None
        self.first_error: str | None = None
        self.cancel = threading.Event()  # 반입용 zip 만들기 중단
        self.last_zip: Path | None = None

        # upload / verify 가 함께 쓰는 Nexus 접속 정보
        saved = load_settings()
        self.url = tk.StringVar(value=saved.get("url", "http://localhost:8081"))
        self.repo = tk.StringVar(value=saved.get("repo", "maven-hosted"))
        self.user = tk.StringVar(value=saved.get("user", "admin"))
        self.password = tk.StringVar()   # 비밀번호는 저장하지 않는다
        self.zip_dir = tk.StringVar(value=saved.get("zip_dir", str(downloads_dir())))
        self.bundle = tk.StringVar()
        self.samples = tk.StringVar()
        self.bundle.trace_add("write", self._suggest_samples)
        self.mvn_cmd = tk.StringVar(value=find_tool("mvn"))
        self.gradle_cmd = tk.StringVar(value=find_tool("gradle"))

        tabs = ttk.Notebook(self)
        tabs.pack(fill="x", padx=8, pady=(8, 4))
        self.run_buttons: list[ttk.Button] = []
        tabs.add(self._collect_tab(tabs), text="  Step 1: Download (PC with internet)  ")
        tabs.add(self._upload_tab(tabs), text="  Step 2: Upload to Nexus (air-gapped)  ")
        tabs.add(self._verify_tab(tabs), text="  Step 3: Check (optional)  ")

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8)
        ttk.Label(bar, text="Progress:").pack(side="left")
        self.status = ttk.Label(bar, text="Ready. Fill in the step above and press its Start button.")
        self.status.pack(side="left", padx=4)
        self.stop_button = ttk.Button(bar, text="Stop", command=self.stop, state="disabled")
        self.stop_button.pack(side="right")
        ttk.Button(bar, text="Clear log", command=lambda: self._set_log("")).pack(side="right", padx=4)

        box = ttk.Frame(self)
        box.pack(fill="both", expand=True, padx=8, pady=8)
        self.log = tk.Text(box, wrap="char", state="disabled", font=("Consolas", 10), height=8)
        ys = ttk.Scrollbar(box, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=ys.set)
        self.log.tag_configure("bad", foreground="#b00020")
        self.log.tag_configure("warn", foreground="#9a6700")
        self.log.tag_configure("cmd", foreground="#0550ae")
        ys.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

        self.bundle.set(find_bundle())
        if 1 <= step <= 3:
            tabs.select(step - 1)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._drain)

    # ------------------------------------------------------------------ 위젯 보조

    def _tab(self, tabs, intro: str) -> ttk.Frame:
        f = ttk.Frame(tabs, padding=8)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text=intro, wraplength=WRAP + 200, justify="left").grid(
            row=0, column=0, columnspan=3, sticky="w", padx=6, pady=(0, 8))
        return f

    def _label(self, parent, row: int, text: str) -> None:
        ttk.Label(parent, text=text).grid(row=row, column=0, sticky="nw", padx=6, pady=4)

    def _hint(self, parent, row: int, text: str) -> int:
        ttk.Label(parent, text=text, foreground=HINT_COLOR, wraplength=WRAP, justify="left").grid(
            row=row, column=1, columnspan=2, sticky="w", pady=(0, 6))
        return row + 1

    def _field(self, parent, row: int, label: str, var: tk.StringVar, hint: str, browse=None, show=None) -> int:
        """라벨 + 입력란 + 설명 한 묶음. 다음에 쓸 행 번호를 돌려준다."""
        self._label(parent, row, label)
        ttk.Entry(parent, textvariable=var, show=show or "").grid(row=row, column=1, sticky="ew", pady=3)
        if browse:
            ttk.Button(parent, text="Choose...", command=browse).grid(row=row, column=2, padx=6)
        return self._hint(parent, row + 1, hint)

    def _check(self, parent, text: str, var: tk.BooleanVar, hint: str | None = None) -> None:
        ttk.Checkbutton(parent, text=text, variable=var).pack(anchor="w")
        if hint:
            ttk.Label(parent, text=hint, foreground=HINT_COLOR, wraplength=WRAP - 24, justify="left").pack(
                anchor="w", padx=(22, 0), pady=(0, 4))

    def _pick_dir(self, var: tk.StringVar, title: str):
        def pick():
            d = filedialog.askdirectory(title=title, initialdir=var.get() or None)
            if d:
                var.set(str(Path(d)))
        return pick

    def _run_button(self, parent, text: str, command) -> ttk.Button:
        b = ttk.Button(parent, text=text, command=command)
        self.run_buttons.append(b)
        return b

    def _pick_tool(self, var: tk.StringVar, name: str):
        def pick():
            p = filedialog.askopenfilename(
                title=f"Find {name} in the bin folder of its installation",
                filetypes=[(name, f"{name}.cmd {name}.bat {name}"), ("All files", "*.*")])
            if p:
                var.set(str(Path(p)))
        return pick

    def _tool_fields(self, parent, row: int) -> int:
        row = self._field(parent, row, "Maven program", self.mvn_cmd,
                          "The file mvn.cmd in the bin folder of your Maven installation. Filled in when it can be "
                          "found. If it is empty and Maven is installed, press Choose... and locate it.",
                          self._pick_tool(self.mvn_cmd, "mvn"))
        return self._field(parent, row, "Gradle program", self.gradle_cmd,
                           "The file gradle.bat in the bin folder of your Gradle installation. Same as above.",
                           self._pick_tool(self.gradle_cmd, "gradle"))

    def _tool_args(self) -> list[str]:
        args = []
        for flag, var in (("--mvn-cmd", self.mvn_cmd), ("--gradle-cmd", self.gradle_cmd)):
            if var.get().strip():
                args += [flag, var.get().strip()]
        return args

    def _nexus_fields(self, parent, row: int) -> int:
        row = self._field(parent, row, "Nexus address", self.url,
                          "Web address of your Nexus 3 server, for example http://nexus.mycompany.local:8081")
        row = self._field(parent, row, "Repository name", self.repo,
                          "Name of the repository in Nexus. It must already exist and be of type \"maven2 (hosted)\".")
        row = self._field(parent, row, "User name", self.user,
                          "Nexus account that may upload to that repository. Leave empty if no login is needed.")
        return self._field(parent, row, "Password", self.password, "Never saved; type it each time. The three fields above are remembered on this PC.", show="*")

    # ------------------------------------------------------------------ 탭

    def _collect_tab(self, tabs) -> ttk.Frame:
        f = self._tab(tabs, "Do this on a PC that has internet access. It downloads every library your project needs "
                            "(including the libraries those libraries need) into one folder. "
                            "Afterwards, copy that folder to the air-gapped network and continue with Step 2.")

        self._label(f, 1, "Project build files")
        self.inputs = tk.Listbox(f, height=3, selectmode="extended")
        self.inputs.grid(row=1, column=1, sticky="ew", pady=3)
        side = ttk.Frame(f)
        side.grid(row=1, column=2, sticky="n", padx=6, pady=3)
        ttk.Button(side, text="Add file...", command=self._add_inputs).pack(fill="x")
        ttk.Button(side, text="Remove", command=self._remove_inputs).pack(fill="x", pady=2)
        row = self._hint(f, 2, "The file that lists your project's libraries: pom.xml (Maven) or build.gradle / "
                               "build.gradle.kts (Gradle). You can add several, and the file name can be anything "
                               "(for example pom23.xml and pom26.xml).")

        self._label(f, row, "Single libraries\n(optional)")
        self.coords = tk.Text(f, height=3, font=("Consolas", 10))
        self.coords.grid(row=row, column=1, sticky="ew", pady=3)
        row = self._hint(f, row + 1, "Instead of (or in addition to) a build file, type libraries here, one per line, "
                                     "as group:artifact:version.  Example:  org.springframework:spring-core:6.1.14")

        self.auto_out = self._new_out()
        self.out = tk.StringVar(value=self.auto_out)
        self._label(f, row, "Target systems")
        names, defaults = load_platforms()
        self.platforms = {n: tk.BooleanVar(value=n in defaults) for n in names}
        pf = ttk.Frame(f)
        pf.grid(row=row, column=1, columnspan=2, sticky="w")
        for n, v in self.platforms.items():
            ttk.Checkbutton(pf, text=n, variable=v).pack(side="left", padx=(0, 10))
        row = self._hint(f, row + 1, "Operating systems the libraries will be used on. Some libraries ship a separate "
                                     "file per system; only the ticked ones are downloaded.")

        self.force = tk.BooleanVar()
        self.no_cross = tk.BooleanVar()
        self.no_losers = tk.BooleanVar()
        self.keep_logs = tk.BooleanVar(value=True)
        self.verbose = tk.BooleanVar()
        self.maven_goals = tk.StringVar()

        of = ttk.Frame(f)
        of.grid(row=row, column=1, columnspan=2, sticky="w")
        self._check(of, "Empty the folder first if it already contains files", self.force)
        self.adv_button = ttk.Button(of, text="Show advanced options", command=self._toggle_advanced)
        self.adv_button.pack(anchor="w", pady=(4, 2))

        self.advanced = ttk.LabelFrame(f, text="Advanced options (the defaults are fine for most cases)", padding=6)
        self.advanced_row = row + 1
        self._check(self.advanced, "Faster but less complete: resolve with one build tool only", self.no_cross,
                    "Normally libraries are resolved with both Maven and Gradle, because the two tools can pick "
                    "different versions. Tick this only if a single tool will ever use the Nexus repository.")
        self._check(self.advanced, "Do not download older versions that lost a version conflict", self.no_losers,
                    "Build tools still read the description file of a version they end up not using. "
                    "Skipping them saves little space and can break builds.")
        self._check(self.advanced, "Keep the Maven / Gradle logs", self.keep_logs,
                    "Saved next to the download folder as <folder>-work. Useful when something fails.")
        self._check(self.advanced, "Show more detail in the progress log", self.verbose)
        mg = ttk.Frame(self.advanced)
        mg.pack(fill="x", pady=(4, 0))
        ttk.Label(mg, text="Extra Maven goals").pack(side="left")
        ttk.Entry(mg, textvariable=self.maven_goals, width=40).pack(side="left", padx=6)
        ttk.Label(self.advanced, foreground=HINT_COLOR, wraplength=WRAP - 24, justify="left",
                  text="Only for Maven projects whose offline build later fails because test libraries are missing: "
                       "type  verify  to run a real build here and capture them.").pack(anchor="w")
        df = ttk.Frame(self.advanced)
        df.pack(fill="x", pady=(8, 0))
        ttk.Label(df, text="Download folder").pack(side="left")
        ttk.Entry(df, textvariable=self.out).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(df, text="Choose...", command=self._pick_dir(self.out, "Folder to download into")).pack(side="left")
        ttk.Label(self.advanced, foreground=HINT_COLOR, wraplength=WRAP - 24, justify="left",
                  text="Where the libraries are downloaded. A new folder in Downloads is used for every download "
                       "unless you change this.").pack(anchor="w")

        bf = ttk.Frame(f)
        bf.grid(row=row + 2, column=1, columnspan=2, sticky="w", pady=(8, 2))
        self._run_button(bf, "Start download", self.run_collect).pack(side="left")
        self._run_button(bf, "Create transfer zip...", self.zip_download).pack(side="left", padx=(6, 0))
        ttk.Button(bf, text="Open result folder", command=self._open_result).pack(side="left", padx=6)
        ttk.Button(bf, text="Open summary report", command=self._open_report).pack(side="left")
        self._hint(f, row + 3, "1) \"Start download\" saves the libraries in a new folder inside your Downloads folder.  "
                               "2) \"Create transfer zip...\" then asks where to save one zip file (for example on a "
                               "USB drive) that contains this program and the libraries. Carry that file.")
        return f

    def _upload_tab(self, tabs) -> ttk.Frame:
        f = self._tab(tabs, "Do this inside the air-gapped network, after copying the folder from Step 1. "
                            "It uploads every library in that folder to your Nexus 3 server, so that Maven and Gradle "
                            "builds can download them from Nexus. Only Python is needed on this PC.")
        row = self._field(f, 1, "Downloaded folder", self.bundle,
                          "The folder that Step 1 created (it contains _report.txt and _artifacts.csv). Filled in "
                          "automatically when a \"bundle\" folder is found next to this program.",
                          self._pick_dir(self.bundle, "Folder created by Step 1"))
        row = self._nexus_fields(f, row)
        self.check_exists = tk.BooleanVar()
        self.dry_run = tk.BooleanVar()
        of = ttk.Frame(f)
        of.grid(row=row, column=1, columnspan=2, sticky="w")
        self._check(of, "Skip files that are already in Nexus", self.check_exists,
                    "Asks Nexus about each file first. Slower to start, but avoids uploading the same file twice.")
        self._check(of, "Trial run: only count the files, upload nothing", self.dry_run)
        self._run_button(f, "Start upload", self.run_upload).grid(row=row + 1, column=1, sticky="w", pady=(8, 2))
        return f

    def _verify_tab(self, tabs) -> ttk.Frame:
        f = self._tab(tabs, "Optional. Do this inside the air-gapped network after Step 2. It proves the upload is "
                            "complete by resolving the original project with Maven and Gradle, using only your Nexus "
                            "server and an empty cache. Requires a JDK plus mvn and/or gradle on this PC.")
        row = self._field(f, 1, "Sample projects folder", self.samples,
                          "The _samples folder inside the downloaded folder. Filled in automatically when you choose "
                          "the folder in Step 2.", self._pick_dir(self.samples, "The _samples folder"))
        row = self._nexus_fields(f, row)
        row = self._tool_fields(f, row)
        self.use_maven = tk.BooleanVar(value=True)
        self.use_gradle = tk.BooleanVar(value=True)
        self.offline_build = tk.BooleanVar()
        self._label(f, row, "Check with")
        of = ttk.Frame(f)
        of.grid(row=row, column=1, columnspan=2, sticky="w")
        tf = ttk.Frame(of)
        tf.pack(anchor="w")
        ttk.Checkbutton(tf, text="Maven", variable=self.use_maven).pack(side="left", padx=(0, 10))
        ttk.Checkbutton(tf, text="Gradle", variable=self.use_gradle).pack(side="left")
        ttk.Label(of, text="Untick a tool that is not installed on this PC.", foreground=HINT_COLOR).pack(
            anchor="w", pady=(0, 4))
        self._check(of, "Also run a complete offline Maven build (slower, more thorough)", self.offline_build)
        self._run_button(f, "Start check", self.run_verify).grid(row=row + 1, column=1, sticky="w", pady=(8, 2))
        return f

    # ------------------------------------------------------------------ 동작

    def _toggle_advanced(self) -> None:
        if self.advanced.winfo_ismapped():
            self.advanced.grid_remove()
            self.adv_button.configure(text="Show advanced options")
        else:
            self.advanced.grid(row=self.advanced_row, column=1, columnspan=2, sticky="ew", pady=4)
            self.adv_button.configure(text="Hide advanced options")

    def _suggest_samples(self, *_):
        samples = Path(self.bundle.get().strip() or ".") / "_samples"
        if self.bundle.get().strip() and samples.is_dir():
            self.samples.set(str(samples))

    def _add_inputs(self) -> None:
        files = filedialog.askopenfilenames(
            title="Choose pom.xml or build.gradle",
            filetypes=[("Build files", "*.xml *.pom *.gradle *.gradle.kts"), ("All files", "*.*")])
        for p in files:
            self.inputs.insert("end", str(Path(p)))

    def _remove_inputs(self) -> None:
        for i in reversed(self.inputs.curselection()):
            self.inputs.delete(i)

    def _open_report(self) -> None:
        report = Path(self.out.get()) / "_report.txt" if self.out.get() else None
        if not report or not report.is_file():
            messagebox.showinfo("sneakernet", "There is no report yet. It is created when a download finishes.")
        elif os.name == "nt":
            os.startfile(report)  # noqa: S606
        else:
            self._set_log(report.read_text(encoding="utf-8", errors="replace"))

    def run_collect(self) -> None:
        inputs = list(self.inputs.get(0, "end"))
        coords = [c.strip() for c in self.coords.get("1.0", "end").splitlines() if c.strip()]
        out = self.out.get().strip()
        platforms = [n for n, v in self.platforms.items() if v.get()]
        if not inputs and not coords:
            return self._need("Nothing to download yet.\n\nAdd your project's pom.xml or build.gradle with "
                              "\"Add file...\", or type a library under \"Single libraries\".")
        bad = [c for c in coords if not _COORD_RE.match(c)]
        if bad:
            return self._need("This line is not in the form group:artifact:version:\n\n" + bad[0] +
                              "\n\nExample: org.springframework:spring-core:6.1.14")
        if out == self.auto_out and Path(out).exists():   # 자동으로 정한 폴더는 매번 새로 만든다
            out = self.auto_out = self._new_out()
            self.out.set(out)
        if not out:
            return self._need("Choose the download folder (under \"Show advanced options\").")
        if not platforms:
            return self._need("Tick at least one target system.")
        args = ["-v"] if self.verbose.get() else []
        args += ["collect", "--out", out, "--platforms", ",".join(platforms)]
        for p in inputs:
            args += ["--input", p]
        for c in coords:
            args += ["--coordinate", c]
        if self.maven_goals.get().strip():
            args += ["--maven-goals", self.maven_goals.get().strip()]
        if self.keep_logs.get():
            # 로그는 결과 디렉토리 옆에 둔다 (결과 디렉토리 안에 두면 업로드 대상이 된다).
            args += ["--work-dir", out.rstrip("\\/") + "-work"]
        for flag, var in (("--force", self.force), ("--no-cross", self.no_cross), ("--no-losers", self.no_losers)):
            if var.get():
                args.append(flag)
        args += self._tool_args()
        self._start(args, lambda: self.bundle.set(out),
                    f"Download finished: {out}  Next: press \"Create transfer zip...\".")

    @staticmethod
    def _new_out() -> str:
        return str(downloads_dir() / time.strftime("sneakernet-%Y-%m-%d_%H%M%S"))

    def _open_result(self) -> None:
        out = self.out.get().strip()
        places = [self.last_zip] + ([Path(out), Path(out).parent] if out else [])
        target = next((p for p in places if p and p.exists()), None)
        if target is None:
            messagebox.showinfo("sneakernet", "Nothing has been downloaded yet.")
        elif os.name == "nt":   # 탐색기에서 zip(없으면 폴더)을 선택한 채로 연다
            subprocess.Popen(["explorer", "/select,", str(target)])
        else:
            self._append(f"Result: {target}\n")

    def zip_download(self) -> None:
        """끝난 다운로드를 반입용 zip 으로 묶는다. 이번 실행에서 받은 것이 없으면 이전 다운로드 폴더를 고르게 한다."""
        folder = self.out.get().strip()
        if not folder or not (Path(folder) / "_report.txt").is_file():
            folder = filedialog.askdirectory(
                title="Choose the folder of a finished download (sneakernet-...)", initialdir=str(downloads_dir()))
            if not folder:
                return
            if not (Path(folder) / "_report.txt").is_file():
                return self._need("That folder is not a finished download (it has no _report.txt).\n\n"
                                  "Choose a folder named sneakernet-<date>_<time>, not the one ending in -work.")
            self.auto_out = str(Path(folder))   # 다음 Start download 는 다시 새 폴더에 받는다
            self.out.set(self.auto_out)
        bundle = Path(self.out.get().strip())
        start = self.zip_dir.get().strip()
        dest = filedialog.asksaveasfilename(
            title="Save the transfer zip as (for example on your USB drive)",
            initialdir=start if start and Path(start).is_dir() else str(downloads_dir()),
            initialfile=bundle.name + "-transfer.zip", defaultextension=".zip", filetypes=[("Zip file", "*.zip")])
        if not dest:
            return
        dest = Path(dest)
        if bundle.resolve() in dest.resolve().parents:
            return self._need("Save the zip file outside the download folder, otherwise it would be uploaded to Nexus too.")
        self.zip_dir.set(str(dest.parent))   # 다음에는 같은 곳에서 시작한다
        self._save_settings()
        self.make_package(bundle, dest)

    def make_package(self, bundle: Path, dest: Path) -> None:
        self.last_zip = dest
        self.cancel.clear()
        self.on_success = self._open_result   # 만든 zip 을 탐색기에서 보여 준다
        self.success_text = f"Finished. Carry this file to the air-gapped network: {dest}"
        self._append(f"$ creating {dest}\n")
        self._busy(True)

        def work():
            try:
                count = write_transfer_zip(bundle, dest, self.lines.put, self.cancel.is_set)
                self.lines.put(f"  {count} files, {dest.stat().st_size / 1048576:.1f} MiB\n")
                self.lines.put(0)
            except (OSError, InterruptedError) as e:
                dest.unlink(missing_ok=True)
                self.lines.put("ERROR " + ("stopped" if isinstance(e, InterruptedError) else str(e)) + "\n")
                self.lines.put(1)
        threading.Thread(target=work, daemon=True).start()

    def _save_settings(self) -> None:
        save_settings({"url": self.url.get().strip(), "repo": self.repo.get().strip(), "user": self.user.get().strip(),
                       "zip_dir": self.zip_dir.get().strip()})

    def _nexus_args(self) -> list[str] | None:
        if not self.url.get().strip() or not self.repo.get().strip():
            self._need("Fill in the Nexus address and the repository name.")
            return None
        self._save_settings()
        args = ["--url", self.url.get().strip(), "--repo", self.repo.get().strip()]
        if self.user.get().strip():
            if not self.password.get():
                self._need("Type the password for the Nexus user, or clear the user name if no login is needed.")
                return None
            args += ["--user", self.user.get().strip()]
        return args

    def run_upload(self) -> None:
        bundle = self.bundle.get().strip()
        if not bundle:
            return self._need("Choose the folder that Step 1 created (\"Downloaded folder\").")
        nexus = self._nexus_args()
        if nexus is None:
            return
        report = Path(bundle.rstrip("\\/") + "-upload-report.json")
        args = ["upload", "--dir", bundle, "--report", str(report)] + nexus
        if self.check_exists.get():
            args.append("--check-exists")
        if self.dry_run.get():
            args.append("--dry-run")
        self._start(args, None, f"Upload finished. Details are in {report}. You can now run Step 3 to check it.")

    def run_verify(self) -> None:
        samples = self.samples.get().strip()
        tools = [n for n, v in (("maven", self.use_maven), ("gradle", self.use_gradle)) if v.get()]
        if not samples:
            return self._need("Choose the _samples folder (it is inside the folder that Step 1 created).")
        if not tools:
            return self._need("Tick Maven, Gradle, or both.")
        nexus = self._nexus_args()
        if nexus is None:
            return
        args = ["verify", "--samples", samples, "--tools", ",".join(tools)] + nexus + self._tool_args()
        if self.offline_build.get():
            args.append("--offline-build")
        self._start(args, None, "Check passed: builds can get everything they need from Nexus.")

    def _need(self, message: str) -> None:
        messagebox.showwarning("sneakernet", message)

    def _start(self, args: list[str], on_success=None, success_text: str = "Finished.") -> None:
        if self.proc:
            return
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        if self.password.get():
            env["NEXUS_PASSWORD"] = self.password.get()   # 명령줄에 비밀번호가 남지 않게 환경 변수로 넘긴다
        self._append("$ sneakernet " + " ".join(args) + "\n")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen([sys.executable, str(CLI)] + args, cwd=HERE, env=env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     encoding="utf-8", errors="replace", creationflags=flags)
        self.on_success = on_success
        self.success_text = success_text
        self._busy(True)
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            self.lines.put(line)
        self.lines.put(proc.wait())

    def _drain(self) -> None:
        try:
            while True:
                item = self.lines.get_nowait()
                if isinstance(item, int):
                    self._finished(item)
                else:
                    self._append(item)
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _finished(self, rc: int) -> None:
        self.proc = None
        self._busy(False)
        if rc == 0:
            self.status.configure(text=self.success_text)
            self._append(f"== {self.success_text} ==\n\n")
            if self.on_success:
                self.on_success()
        else:
            self.status.configure(text="Failed. See the red lines in the log below."
                                  + (f"  ({self.first_error})" if self.first_error else ""))
            self._append(f"== Failed (exit code {rc}) ==\n\n")

    def stop(self) -> None:
        self.cancel.set()
        if not self.proc:
            return
        if os.name == "nt":   # mvn / gradle 자식 프로세스까지 함께 끝낸다
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            self.proc.terminate()

    def _busy(self, busy: bool) -> None:
        for b in self.run_buttons:
            b.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")
        if busy:
            self.first_error = None
            self.status.configure(text="Running... this can take several minutes.")

    def _append(self, text: str) -> None:
        tag = ()
        if text.startswith("$ "):
            tag = ("cmd",)
        elif text.startswith(("오류", "ERROR", "[문제]", "== Failed")) or "[실패]" in text:
            tag = ("bad",)
            if self.first_error is None and text.startswith("오류"):
                self.first_error = text.strip()   # 실패했을 때 상태 줄에도 보여 준다
        elif text.startswith(("WARNING", "[경고]")):
            tag = ("warn",)
        self.log.configure(state="normal")
        self.log.insert("end", text, tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("end", text)
        self.log.configure(state="disabled")

    def _close(self) -> None:
        if self.proc and not messagebox.askyesno("sneakernet", "A task is still running. Stop it and close?"):
            return
        self.stop()
        self._save_settings()
        self.destroy()


if __name__ == "__main__":
    # 반입용 zip 의 bat 은 "--step 2" 로 실행해 업로드 탭부터 연다.
    args = sys.argv[1:]
    App(int(args[1]) if len(args) == 2 and args[0] == "--step" and args[1].isdigit() else 1).mainloop()
