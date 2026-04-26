#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ModuleNotFoundError:
    tk = None
    filedialog = None
    messagebox = None
    ttk = None


STEAM_ROOT_CANDIDATES = [
    Path.home() / ".local/share/Steam",
    Path.home() / ".steam/steam",
    Path.home() / ".steam/root",
    Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
]

LOG_PATH = Path("/tmp/protontrek.log")

LAUNCH_MODES: dict[str, tuple[str, str]] = {
    "runinprefix_start": (
        "Proton runinprefix start /unix",
        "Мягкий запуск внутри префикса через start /unix",
    ),
    "runinprefix_direct": (
        "Proton runinprefix",
        "Прямой запуск через wine внутри того же префикса",
    ),
    "run": (
        "Proton run",
        "Обычный запуск через Proton run",
    ),
}

DEFAULT_LAUNCH_MODE = "runinprefix_start"
DEFAULT_DELAY_SECONDS = "0"


@dataclass
class SteamApp:
    app_id: str
    name: str
    install_dir: Path | None
    library_root: Path
    prefix_dir: Path


@dataclass
class RunningGame:
    app: SteamApp
    pid: int
    process_name: str
    command: str
    proton_path: Path | None
    source: str
    environ: dict[str, str]
    cwd: Path | None


SOURCE_PRIORITY = {
    "env": 0,
    "cmdline": 1,
    "path": 2,
}


def read_text_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def log_debug(message: str) -> None:
    try:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(message + "\n")
    except OSError:
        pass


def unquote_vdf(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def parse_simple_vdf(path: Path) -> dict[str, str]:
    text = read_text_safe(path)
    pairs: dict[str, str] = {}
    for key, value in re.findall(r'"([^"]+)"\s*"([^"]*)"', text):
        pairs[key] = value
    return pairs


def discover_steam_root() -> Path | None:
    for candidate in STEAM_ROOT_CANDIDATES:
        if (candidate / "steamapps").exists():
            return candidate
    return None


def parse_libraryfolders(path: Path) -> list[Path]:
    libraries: list[Path] = []
    text = read_text_safe(path)
    if not text:
        return libraries

    for entry in re.finditer(r'"path"\s*"([^"]+)"', text):
        raw = entry.group(1).replace("\\\\", "\\")
        library_path = Path(raw).expanduser()
        if library_path.exists():
            libraries.append(library_path)

    return libraries


def discover_libraries(steam_root: Path) -> list[Path]:
    libraries = [steam_root]
    extra = parse_libraryfolders(steam_root / "steamapps" / "libraryfolders.vdf")
    for item in extra:
        if item not in libraries:
            libraries.append(item)
    return libraries


def load_installed_apps(steam_root: Path) -> dict[str, SteamApp]:
    apps: dict[str, SteamApp] = {}
    for library_root in discover_libraries(steam_root):
        steamapps_dir = library_root / "steamapps"
        common_dir = steamapps_dir / "common"
        compat_dir = steamapps_dir / "compatdata"
        if not steamapps_dir.exists():
            continue

        for manifest in steamapps_dir.glob("appmanifest_*.acf"):
            data = parse_simple_vdf(manifest)
            app_id = data.get("appid")
            name = data.get("name")
            installdir = data.get("installdir")
            if not app_id or not name:
                continue

            install_dir = common_dir / installdir if installdir else None
            prefix_dir = compat_dir / app_id
            apps[app_id] = SteamApp(
                app_id=app_id,
                name=name,
                install_dir=install_dir,
                library_root=library_root,
                prefix_dir=prefix_dir,
            )
    return apps


def read_proc_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def parse_null_delimited(raw: bytes) -> list[str]:
    parts = raw.split(b"\0")
    return [part.decode("utf-8", errors="ignore") for part in parts if part]


def parse_proc_environ(pid: int) -> dict[str, str]:
    environ_path = Path("/proc") / str(pid) / "environ"
    values: dict[str, str] = {}
    for item in parse_null_delimited(read_proc_bytes(environ_path)):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        values[key] = value
    return values


def parse_proc_cmdline(pid: int) -> list[str]:
    return parse_null_delimited(read_proc_bytes(Path("/proc") / str(pid) / "cmdline"))


def read_proc_name(pid: int) -> str:
    name = read_text_safe(Path("/proc") / str(pid) / "comm").strip()
    return name or f"pid {pid}"


def path_from_env(environ: dict[str, str], key: str) -> Path | None:
    value = environ.get(key)
    if not value:
        return None
    return Path(value).expanduser()


def app_id_from_text(items: Iterable[str]) -> str | None:
    for item in items:
        match = re.search(r"compatdata/(\d+)", item)
        if match:
            return match.group(1)
        match = re.search(r"\bSteamAppId=(\d+)\b", item)
        if match:
            return match.group(1)
    return None


def detect_app_id(
    pid: int,
    environ: dict[str, str],
    cmdline: list[str],
    installed_apps: dict[str, SteamApp],
) -> str | None:
    for key in ("SteamAppId", "STEAM_COMPAT_APP_ID", "STEAM_GAME_ID"):
        value = environ.get(key)
        if value and value.isdigit():
            return value

    from_path = path_from_env(environ, "STEAM_COMPAT_DATA_PATH")
    if from_path:
        match = re.search(r"/compatdata/(\d+)$", str(from_path))
        if match:
            return match.group(1)

    inferred = app_id_from_text(cmdline)
    if inferred:
        return inferred

    exe_path = Path("/proc") / str(pid) / "exe"
    cwd_path = Path("/proc") / str(pid) / "cwd"
    probe_paths = [readlink_safe(exe_path), readlink_safe(cwd_path)]
    for probe in probe_paths:
        if not probe:
            continue
        match = match_install_dir(probe, installed_apps)
        if match:
            return match.app_id
    return None


def readlink_safe(path: Path) -> Path | None:
    try:
        return path.resolve()
    except OSError:
        return None


def match_install_dir(path: Path, installed_apps: dict[str, SteamApp]) -> SteamApp | None:
    best: SteamApp | None = None
    best_len = -1
    for app in installed_apps.values():
        if not app.install_dir:
            continue
        try:
            path.relative_to(app.install_dir)
        except ValueError:
            continue
        current_len = len(str(app.install_dir))
        if current_len > best_len:
            best = app
            best_len = current_len
    return best


def detect_proton_path(environ: dict[str, str], steam_root: Path) -> Path | None:
    explicit = path_from_env(environ, "STEAM_COMPAT_TOOL_PATH")
    if explicit and explicit.exists():
        return explicit

    tool_paths = environ.get("STEAM_COMPAT_TOOL_PATHS", "")
    for part in tool_paths.split(":"):
        candidate = Path(part).expanduser()
        if candidate.exists():
            return candidate

    candidates: list[Path] = []
    common_dir = steam_root / "steamapps" / "common"
    if common_dir.exists():
        candidates.extend(sorted(common_dir.glob("Proton*"), reverse=True))
    compat_tools_dir = steam_root / "compatibilitytools.d"
    if compat_tools_dir.exists():
        candidates.extend(sorted(compat_tools_dir.iterdir(), reverse=True))

    for candidate in candidates:
        if (candidate / "proton").exists():
            return candidate
    return None


def find_running_games(steam_root: Path) -> list[RunningGame]:
    installed_apps = load_installed_apps(steam_root)
    running_by_app: dict[str, RunningGame] = {}

    for proc_entry in Path("/proc").iterdir():
        if not proc_entry.name.isdigit():
            continue
        pid = int(proc_entry.name)
        cmdline = parse_proc_cmdline(pid)
        if not cmdline:
            continue

        environ = parse_proc_environ(pid)
        app_id = detect_app_id(pid, environ, cmdline, installed_apps)
        if not app_id:
            continue

        app = installed_apps.get(app_id)
        if not app:
            continue

        has_proton_markers = (
            "STEAM_COMPAT_DATA_PATH" in environ
            or any("proton" in part.lower() for part in cmdline)
            or app.prefix_dir.exists()
        )
        if not has_proton_markers:
            continue

        game = RunningGame(
            app=app,
            pid=pid,
            process_name=read_proc_name(pid),
            command=" ".join(shlex.quote(part) for part in cmdline),
            proton_path=detect_proton_path(environ, steam_root),
            source=detect_source(environ, cmdline),
            environ=environ,
            cwd=readlink_safe(Path("/proc") / str(pid) / "cwd"),
        )
        current = running_by_app.get(app_id)
        if current is None or is_better_game_candidate(game, current):
            running_by_app[app_id] = game

    return sorted(running_by_app.values(), key=lambda item: (item.app.name.lower(), item.pid))


def is_better_game_candidate(candidate: RunningGame, current: RunningGame) -> bool:
    candidate_priority = SOURCE_PRIORITY.get(candidate.source, 99)
    current_priority = SOURCE_PRIORITY.get(current.source, 99)
    if candidate_priority != current_priority:
        return candidate_priority < current_priority

    candidate_name = candidate.process_name.lower()
    current_name = current.process_name.lower()
    candidate_game_name = candidate.app.name.lower()
    current_game_name = current.app.name.lower()

    candidate_matches = candidate_game_name in candidate_name
    current_matches = current_game_name in current_name
    if candidate_matches != current_matches:
        return candidate_matches

    candidate_is_wine = candidate_name.startswith(("wine", "wineserver", "steam"))
    current_is_wine = current_name.startswith(("wine", "wineserver", "steam"))
    if candidate_is_wine != current_is_wine:
        return not candidate_is_wine

    return candidate.pid < current.pid


def detect_source(environ: dict[str, str], cmdline: list[str]) -> str:
    if "SteamAppId" in environ or "STEAM_COMPAT_APP_ID" in environ:
        return "env"
    if any("compatdata" in part for part in cmdline):
        return "cmdline"
    return "path"


def trainer_default_cwd(game: RunningGame) -> Path:
    return game.cwd or game.app.install_dir or game.app.prefix_dir


def sanitize_game_environment(game: RunningGame) -> dict[str, str]:
    env = game.environ.copy()
    env.setdefault("PATH", os.environ.get("PATH", ""))
    env.setdefault("HOME", os.environ.get("HOME", str(Path.home())))
    env.setdefault("USER", os.environ.get("USER", ""))
    env.setdefault("DISPLAY", os.environ.get("DISPLAY", ""))
    env.setdefault("XAUTHORITY", os.environ.get("XAUTHORITY", ""))
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", os.environ.get("DBUS_SESSION_BUS_ADDRESS", ""))
    env.setdefault("WAYLAND_DISPLAY", os.environ.get("WAYLAND_DISPLAY", ""))
    env.setdefault("XDG_RUNTIME_DIR", os.environ.get("XDG_RUNTIME_DIR", ""))
    env.setdefault("PWD", str(trainer_default_cwd(game)))
    return env


def build_launch_env(game: RunningGame, trainer_path: Path) -> dict[str, str]:
    env = sanitize_game_environment(game)
    steam_root = discover_steam_root() or game.app.library_root
    env["STEAM_COMPAT_DATA_PATH"] = str(game.app.prefix_dir)
    env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)
    env["STEAM_COMPAT_APP_ID"] = game.app.app_id
    env["SteamAppId"] = game.app.app_id
    env["SteamGameId"] = game.app.app_id
    env["WINEPREFIX"] = str(game.app.prefix_dir / "pfx")
    env["PWD"] = str(trainer_path.parent)
    return env


def trainer_log_path(game: RunningGame) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", game.app.name).strip("_") or game.app.app_id
    return Path("/tmp") / f"protontrek-{safe_name}-{game.app.app_id}.log"


def parse_delay_seconds(value: str) -> int:
    raw = value.strip() or DEFAULT_DELAY_SECONDS
    try:
        seconds = int(raw)
    except ValueError as exc:
        raise RuntimeError("Задержка должна быть целым числом секунд.") from exc
    if seconds < 0 or seconds > 600:
        raise RuntimeError("Задержка должна быть в диапазоне от 0 до 600 секунд.")
    return seconds


def launch_mode_label(mode: str) -> str:
    return LAUNCH_MODES.get(mode, (mode, ""))[0]


def mode_key_from_label(label: str) -> str | None:
    for key, (mode_label, _description) in LAUNCH_MODES.items():
        if mode_label == label:
            return key
    return None


def build_launch_command(proton_script: Path, trainer_path: Path, mode: str) -> list[str]:
    if mode == "runinprefix_start":
        return [
            str(proton_script),
            "runinprefix",
            "start",
            "/unix",
            str(trainer_path),
        ]
    if mode == "runinprefix_direct":
        return [
            str(proton_script),
            "runinprefix",
            str(trainer_path),
        ]
    if mode == "run":
        return [
            str(proton_script),
            "run",
            str(trainer_path),
        ]
    raise RuntimeError(f"Неизвестный режим запуска: {mode}")


def spawn_launch_process(
    launch_cmd: list[str],
    env: dict[str, str],
    cwd: Path,
    log_path: Path,
    delay_seconds: int,
) -> subprocess.Popen[bytes]:
    log_handle = log_path.open("ab")
    try:
        if delay_seconds <= 0:
            process = subprocess.Popen(
                launch_cmd,
                env=env,
                cwd=str(cwd),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        else:
            delay_cmd = [
                sys.executable,
                "-c",
                (
                    "import os, subprocess, sys, time; "
                    "time.sleep(int(sys.argv[1])); "
                    "cmd=sys.argv[2:]; "
                    "subprocess.Popen(cmd, env=os.environ.copy(), cwd=os.getcwd(), "
                    "stdout=open(os.environ['PROTONTREK_LOG_PATH'], 'ab'), "
                    "stderr=subprocess.STDOUT, start_new_session=True)"
                ),
                str(delay_seconds),
                *launch_cmd,
            ]
            delay_env = env.copy()
            delay_env["PROTONTREK_LOG_PATH"] = str(log_path)
            process = subprocess.Popen(
                delay_cmd,
                env=delay_env,
                cwd=str(cwd),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    finally:
        log_handle.close()
    return process


def launch_trainer(
    game: RunningGame,
    trainer_path: Path,
    mode: str,
    delay_seconds: int,
) -> tuple[subprocess.Popen[bytes], Path]:
    proton_dir = game.proton_path
    if not proton_dir:
        raise RuntimeError("Не удалось определить путь к Proton для этой игры.")

    proton_script = proton_dir / "proton"
    if not proton_script.exists():
        raise RuntimeError(f"Файл Proton не найден: {proton_script}")

    env = build_launch_env(game, trainer_path)
    log_path = trainer_log_path(game)
    launch_cmd = build_launch_command(proton_script, trainer_path, mode)
    log_debug(f"launch trainer pid_target={game.pid} cmd={launch_cmd!r}")
    log_debug(
        f"launch env appid={game.app.app_id} proton={proton_dir} prefix={game.app.prefix_dir} "
        f"mode={mode} delay={delay_seconds}"
    )

    with log_path.open("ab") as log_handle:
        log_handle.write(
            (
                f"\n=== ProtonTrek launch ===\n"
                f"ts={int(time.time())}\n"
                f"game={game.app.name}\n"
                f"appid={game.app.app_id}\n"
                f"game_pid={game.pid}\n"
                f"trainer={trainer_path}\n"
                f"proton={proton_dir}\n"
                f"prefix={game.app.prefix_dir}\n"
                f"cwd={trainer_path.parent}\n"
                f"mode={mode}\n"
                f"delay_seconds={delay_seconds}\n"
            ).encode("utf-8", errors="ignore")
        )
    process = spawn_launch_process(launch_cmd, env, trainer_path.parent, log_path, delay_seconds)
    return process, log_path


class TrainerLauncherApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("ProtonTrek")
        self.root.geometry("960x560")
        self.root.minsize(820, 480)

        self.steam_root = discover_steam_root()
        self.games: list[RunningGame] = []
        self.selected_trainer = tk.StringVar()
        self.selected_mode = tk.StringVar(value=DEFAULT_LAUNCH_MODE)
        self.delay_seconds = tk.StringVar(value=DEFAULT_DELAY_SECONDS)
        self.status_text = tk.StringVar(value="Поиск Steam...")

        self._build_ui()
        self.refresh_games()

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        header = ttk.Frame(self.root, padding=12)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)

        ttk.Label(
            header,
            text="Запущенные Steam-игры и их Proton-префиксы",
            font=("TkDefaultFont", 13, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(header, text="Обновить", command=self.refresh_games).grid(row=0, column=2, sticky="e")
        ttk.Label(header, textvariable=self.status_text).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        body = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        body.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))

        left = ttk.Frame(body, padding=8)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        ttk.Label(left, text="Игры").grid(row=0, column=0, sticky="w")

        self.game_list = tk.Listbox(left, exportselection=False)
        self.game_list.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        self.game_list.bind("<<ListboxSelect>>", self.on_game_selected)

        body.add(left, weight=1)

        right = ttk.Frame(body, padding=8)
        right.columnconfigure(1, weight=1)

        ttk.Label(right, text="Префикс").grid(row=0, column=0, sticky="nw")
        self.prefix_value = tk.Text(right, height=4, wrap="word")
        self.prefix_value.grid(row=0, column=1, sticky="ew", pady=(0, 8))
        self.prefix_value.configure(state="disabled")

        ttk.Label(right, text="Proton").grid(row=1, column=0, sticky="nw")
        self.proton_value = tk.Text(right, height=3, wrap="word")
        self.proton_value.grid(row=1, column=1, sticky="ew", pady=(0, 8))
        self.proton_value.configure(state="disabled")

        ttk.Label(right, text="Процесс").grid(row=2, column=0, sticky="nw")
        self.process_value = tk.Text(right, height=6, wrap="word")
        self.process_value.grid(row=2, column=1, sticky="ew", pady=(0, 8))
        self.process_value.configure(state="disabled")

        ttk.Label(right, text="Trainer").grid(row=3, column=0, sticky="w")
        trainer_entry = ttk.Entry(right, textvariable=self.selected_trainer)
        trainer_entry.grid(row=3, column=1, sticky="ew", pady=(0, 8))

        ttk.Label(right, text="Режим запуска").grid(row=4, column=0, sticky="w")
        mode_values = [label for label, _desc in LAUNCH_MODES.values()]
        self.mode_combo = ttk.Combobox(
            right,
            state="readonly",
            values=mode_values,
        )
        self.mode_combo.grid(row=4, column=1, sticky="ew", pady=(0, 8))
        self.mode_combo.set(launch_mode_label(DEFAULT_LAUNCH_MODE))

        ttk.Label(right, text="Задержка, сек").grid(row=5, column=0, sticky="w")
        delay_entry = ttk.Entry(right, textvariable=self.delay_seconds)
        delay_entry.grid(row=5, column=1, sticky="ew", pady=(0, 8))

        buttons = ttk.Frame(right)
        buttons.grid(row=6, column=1, sticky="w", pady=(0, 8))
        ttk.Button(buttons, text="Выбрать trainer.exe", command=self.pick_trainer).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Запустить в префиксе", command=self.run_trainer).pack(side=tk.LEFT, padx=(8, 0))

        help_text = (
            "Утилита ищет процессы Steam/Proton через /proc, определяет appid, "
            "compatdata-префикс и даёт выбрать режим запуска и задержку перед стартом trainer.exe."
        )
        ttk.Label(right, text=help_text, wraplength=420, justify=tk.LEFT).grid(
            row=7,
            column=0,
            columnspan=2,
            sticky="w",
        )

        body.add(right, weight=2)

    def set_text(self, widget: tk.Text, value: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", tk.END)
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def refresh_games(self) -> None:
        self.game_list.delete(0, tk.END)
        self.games.clear()

        if not self.steam_root:
            self.status_text.set("Steam не найден. Проверьте стандартный путь установки.")
            return

        self.games = find_running_games(self.steam_root)
        for game in self.games:
            label = f"{game.app.name}  [appid {game.app.app_id}]  pid {game.pid}"
            self.game_list.insert(tk.END, label)

        if self.games:
            self.status_text.set(f"Найдено игр: {len(self.games)}")
            self.game_list.selection_set(0)
            self.on_game_selected()
        else:
            self.status_text.set("Активные Steam-игры под Proton не найдены.")
            self.clear_details()

    def clear_details(self) -> None:
        self.set_text(self.prefix_value, "")
        self.set_text(self.proton_value, "")
        self.set_text(self.process_value, "")

    def current_game(self) -> RunningGame | None:
        selection = self.game_list.curselection()
        if not selection:
            return None
        index = selection[0]
        if index >= len(self.games):
            return None
        return self.games[index]

    def on_game_selected(self, _event: object | None = None) -> None:
        game = self.current_game()
        if not game:
            self.clear_details()
            return

        self.set_text(self.prefix_value, str(game.app.prefix_dir))
        self.set_text(self.proton_value, str(game.proton_path) if game.proton_path else "Не определён")
        process_text = (
            f"PID: {game.pid}\n"
            f"Имя: {game.process_name}\n"
            f"Источник appid: {game.source}\n"
            f"Команда:\n{game.command}"
        )
        self.set_text(self.process_value, process_text)

    def pick_trainer(self) -> None:
        path = filedialog.askopenfilename(
            title="Выберите trainer.exe",
            filetypes=[("Windows executables", "*.exe"), ("All files", "*.*")],
        )
        if path:
            self.selected_trainer.set(path)

    def run_trainer(self) -> None:
        game = self.current_game()
        if not game:
            messagebox.showerror("Нет игры", "Сначала выберите игру из списка.")
            return

        trainer_raw = self.selected_trainer.get().strip()
        if not trainer_raw:
            messagebox.showerror("Нет trainer.exe", "Сначала выберите trainer.exe.")
            return

        trainer_path = Path(trainer_raw).expanduser()
        if not trainer_path.exists():
            messagebox.showerror("Файл не найден", f"Не найден файл:\n{trainer_path}")
            return

        mode = mode_key_from_label(self.mode_combo.get())
        if not mode:
            messagebox.showerror("Нет режима", "Выберите корректный режим запуска.")
            return

        try:
            delay_seconds = parse_delay_seconds(self.delay_seconds.get())
        except RuntimeError as exc:
            messagebox.showerror("Некорректная задержка", str(exc))
            return

        try:
            _process, log_path = launch_trainer(game, trainer_path, mode, delay_seconds)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Ошибка запуска", str(exc))
            return

        start_text = "сразу" if delay_seconds == 0 else f"через {delay_seconds} сек"
        messagebox.showinfo(
            "Запущено",
            f"Trainer будет запущен для игры:\n{game.app.name}\n"
            f"Режим: {launch_mode_label(mode)}\n"
            f"Старт: {start_text}\n\nЛог:\n{log_path}",
        )


def zenity_available() -> bool:
    return shutil_which("zenity") is not None


def shutil_which(binary: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(":"):
        candidate = Path(directory) / binary
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def run_zenity(args: list[str]) -> subprocess.CompletedProcess[str]:
    log_debug("zenity " + " ".join(args))
    result = subprocess.run(
        ["zenity", *args],
        check=False,
        text=True,
        capture_output=True,
    )
    log_debug(
        f"zenity exit={result.returncode} stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}"
    )
    return result


def zenity_info(text: str) -> None:
    run_zenity(["--info", "--width=520", f"--text={text}"])


def zenity_error(text: str) -> None:
    run_zenity(["--error", "--width=560", f"--text={text}"])


def zenity_pick_game(games: list[RunningGame]) -> RunningGame | None:
    cmd = [
        "--list",
        "--title=ProtonTrek",
        "--width=1100",
        "--height=500",
        "--text=Выберите запущенную Steam-игру",
        "--separator=|",
        "--print-column=1",
        "--column=appid",
        "--column=Игра",
        "--column=PID",
        "--column=Префикс",
        "--column=Proton",
    ]
    for game in games:
        cmd.extend(
            [
                game.app.app_id,
                game.app.name,
                str(game.pid),
                str(game.app.prefix_dir),
                str(game.proton_path) if game.proton_path else "Не определён",
            ]
        )

    result = run_zenity(cmd)
    if result.returncode != 0:
        return None

    app_id = result.stdout.strip()
    if not app_id:
        return None

    for game in games:
        if game.app.app_id == app_id:
            return game
    return None


def zenity_pick_trainer() -> Path | None:
    result = run_zenity(
        [
            "--file-selection",
            "--title=Выберите trainer.exe",
            "--filename=" + str(Path.home()) + "/",
        ]
    )
    if result.returncode != 0:
        return None

    value = result.stdout.strip()
    if not value:
        return None
    return Path(value).expanduser()


def zenity_pick_launch_options() -> tuple[str, int] | None:
    mode_rows: list[str] = []
    for key, (label, description) in LAUNCH_MODES.items():
        mode_rows.extend([key, label, description])

    mode_result = run_zenity(
        [
            "--list",
            "--radiolist",
            "--title=Режим запуска",
            "--width=900",
            "--height=320",
            "--text=Выберите режим запуска trainer.exe",
            "--column=Выбор",
            "--column=Ключ",
            "--column=Режим",
            "--column=Описание",
            "TRUE",
            DEFAULT_LAUNCH_MODE,
            launch_mode_label(DEFAULT_LAUNCH_MODE),
            LAUNCH_MODES[DEFAULT_LAUNCH_MODE][1],
            "FALSE",
            "runinprefix_direct",
            launch_mode_label("runinprefix_direct"),
            LAUNCH_MODES["runinprefix_direct"][1],
            "FALSE",
            "run",
            launch_mode_label("run"),
            LAUNCH_MODES["run"][1],
        ]
    )
    if mode_result.returncode != 0:
        return None

    mode = mode_result.stdout.strip()
    if mode not in LAUNCH_MODES:
        return None

    delay_result = run_zenity(
        [
            "--entry",
            "--title=Задержка запуска",
            "--width=420",
            "--text=Введите задержку перед стартом trainer.exe в секундах (0-600)",
            f"--entry-text={DEFAULT_DELAY_SECONDS}",
        ]
    )
    if delay_result.returncode != 0:
        return None

    try:
        delay_seconds = parse_delay_seconds(delay_result.stdout.strip())
    except RuntimeError as exc:
        zenity_error(str(exc))
        return None

    return mode, delay_seconds


def zenity_confirm_launch(game: RunningGame, trainer_path: Path, mode: str, delay_seconds: int) -> bool:
    proton_text = str(game.proton_path) if game.proton_path else "Не определён"
    start_text = "сразу" if delay_seconds == 0 else f"через {delay_seconds} сек"
    text = (
        f"Игра: {game.app.name}\n"
        f"AppID: {game.app.app_id}\n"
        f"PID: {game.pid}\n"
        f"Префикс: {game.app.prefix_dir}\n"
        f"Proton: {proton_text}\n"
        f"Режим: {launch_mode_label(mode)}\n"
        f"Старт: {start_text}\n"
        f"Trainer: {trainer_path}\n\n"
        "Запустить?"
    )
    result = run_zenity(["--question", "--width=700", f"--text={text}"])
    return result.returncode == 0


def run_zenity_flow() -> int:
    log_debug("starting zenity flow")
    steam_root = discover_steam_root()
    if not steam_root:
        zenity_error("Steam не найден. Проверьте стандартный путь установки.")
        return 1

    games = find_running_games(steam_root)
    log_debug(f"games found={len(games)}")
    if not games:
        zenity_error("Активные Steam-игры под Proton не найдены.")
        return 1

    game = zenity_pick_game(games)
    if not game:
        zenity_info("Игра не выбрана. Запуск отменён.")
        return 1

    zenity_info(
        "Игра выбрана.\n\n"
        "Следующим окном откроется выбор trainer.exe.\n"
        "Если окно не появилось, проверь терминал и /tmp/protontrek.log."
    )
    trainer_path = zenity_pick_trainer()
    if not trainer_path:
        zenity_info("Файл trainer.exe не выбран. Запуск отменён.")
        return 1

    if not trainer_path.exists():
        zenity_error(f"Не найден файл:\n{trainer_path}")
        return 1

    launch_options = zenity_pick_launch_options()
    if not launch_options:
        zenity_info("Параметры запуска не выбраны. Запуск отменён.")
        return 1

    mode, delay_seconds = launch_options

    if not zenity_confirm_launch(game, trainer_path, mode, delay_seconds):
        return 1

    try:
        _process, log_path = launch_trainer(game, trainer_path, mode, delay_seconds)
    except Exception as exc:  # noqa: BLE001
        zenity_error(str(exc))
        return 1

    start_text = "сразу" if delay_seconds == 0 else f"через {delay_seconds} сек"
    zenity_info(
        f"Trainer будет запущен для игры:\n{game.app.name}\n"
        f"Режим: {launch_mode_label(mode)}\n"
        f"Старт: {start_text}\n\nЛог:\n{log_path}"
    )
    return 0


def main() -> int:
    if sys.platform != "linux":
        print("Эта утилита рассчитана на Linux с Steam/Proton.", file=sys.stderr)
        return 1

    if tk is not None:
        root = tk.Tk()
        style = ttk.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        TrainerLauncherApp(root)
        root.mainloop()
        return 0

    if zenity_available():
        return run_zenity_flow()

    print(
        "Не найден tkinter, и в системе нет zenity для графического интерфейса.",
        file=sys.stderr,
    )
    print("Установите python3-tk или zenity.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
