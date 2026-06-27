import ctypes
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import winreg
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QSortFilterProxyModel, QThread, Signal, QFileInfo, QSize
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QComboBox,
    QFileIconProvider,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStyleFactory,
    QTableView,
    QVBoxLayout,
    QWidget,
)

REGISTRY_PATHS = [
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
]

STARTUP_REG_PATHS = [
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
]

COLUMNS = ["Name", "Type", "Publisher", "Version", "Size", "Source", "Drive", "Install Date"]
ITEM_TYPES = ["Installed Program", "Store App", "Startup Item", "Service", "Orphaned Folder"]
LAUNCHER_SOURCES = {"Steam", "Epic", "GOG", "Ubisoft", "EA"}

STEAM_LIBRARY_VDF = r"libraryfolders.vdf"
GAME_PLATFORM_REGISTRY = [
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\GOG.com\GalaxyClient\paths", "client"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Epic Games\EpicGamesLauncher", "AppDataPath"),
]

PROGRAM_DIRS = [
    os.environ.get("ProgramFiles", r"C:\Program Files"),
    os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
]

FS_SKIP = {
    "common files",
    "windows nt",
    "windows kits",
    "windowsapps",
    "microsoft",
    "microsoft office",
    "internet explorer",
    "windows defender",
    "windows mail",
    "windows media player",
    "windows multimedia platform",
    "windows portable devices",
    "windows photo viewer",
    "windows sidebar",
}

EXECUTABLE_SUFFIXES = (".exe", ".bat", ".cmd", ".com", ".msi")
UNINSTALLER_HINTS = ("unins", "uninstall", "remove")
SHARED_VENDOR_ROOTS = {
    "common files",
    "ubisoft",
    "electronic arts",
    "ea app",
    "steam",
    "epic games",
    "gog",
    "gog galaxy",
    "microsoft",
}
GENERIC_FOLDER_NAMES = {"app", "apps", "application", "client", "launcher", "program", "service", "shared", "vendor"}
KNOWN_ANTICHEATS = (
    {
        "name": "BattlEye",
        "publisher": "BattlEye Innovations e.K.",
        "tokens": ("battleye", "beservice", "bedaisy"),
        "paths": (
            r"%ProgramFiles%\Common Files\BattlEye",
            r"%ProgramFiles(x86)%\Common Files\BattlEye",
        ),
    },
    {
        "name": "Easy Anti-Cheat",
        "publisher": "Epic Games",
        "tokens": ("easyanticheat", "easyanticheateos"),
        "paths": (
            r"%ProgramFiles%\EasyAntiCheat",
            r"%ProgramFiles%\EasyAntiCheat_EOS",
            r"%ProgramFiles(x86)%\EasyAntiCheat",
            r"%ProgramFiles(x86)%\EasyAntiCheat_EOS",
        ),
    },
    {
        "name": "Riot Vanguard",
        "publisher": "Riot Games",
        "tokens": ("riotvanguard", "vgc", "vgk"),
        "paths": (r"%ProgramFiles%\Riot Vanguard",),
    },
    {
        "name": "EA AntiCheat",
        "publisher": "Electronic Arts",
        "tokens": ("eaanticheat",),
        "paths": (
            r"%ProgramFiles%\EA\AC",
            r"%ProgramFiles%\EA AntiCheat",
        ),
    },
    {
        "name": "FACEIT AC",
        "publisher": "FACEIT Ltd.",
        "tokens": ("faceitac", "faceitanticheat", "faceitservice"),
        "paths": (
            r"%ProgramFiles%\FACEIT AC",
            r"%ProgramFiles(x86)%\FACEIT AC",
        ),
    },
    {
        "name": "PunkBuster",
        "publisher": "Even Balance, Inc.",
        "tokens": ("punkbuster", "pnkbstr"),
        "paths": (
            r"%ProgramFiles%\PunkBuster",
            r"%ProgramFiles(x86)%\PunkBuster",
            r"%ProgramFiles(x86)%\PunkBuster Services",
        ),
    },
    {
        "name": "nProtect GameGuard",
        "publisher": "INCA Internet",
        "tokens": ("nprotect", "gameguard", "npggsvc"),
        "paths": (
            r"%ProgramFiles%\nProtect GameGuard",
            r"%ProgramFiles(x86)%\nProtect GameGuard",
        ),
    },
    {
        "name": "XIGNCODE3",
        "publisher": "WELLBIA",
        "tokens": ("xigncode", "xhunter1"),
        "paths": (
            r"%ProgramFiles%\Wellbia\XIGNCODE3",
            r"%ProgramFiles(x86)%\Wellbia\XIGNCODE3",
        ),
    },
    {
        "name": "Tencent ACE",
        "publisher": "Tencent",
        "tokens": ("tencentace", "acebase"),
        "paths": (
            r"%ProgramFiles%\Tencent\ACE",
            r"%ProgramFiles(x86)%\Tencent\ACE",
        ),
    },
    {
        "name": "HoYoKProtect",
        "publisher": "HoYoverse",
        "tokens": ("hoyokprotect", "mhyprot"),
        "paths": (r"%ProgramFiles%\HoYoKProtect",),
    },
)
KNOWN_ANTICHEAT_TOKENS = tuple(
    sorted({token for anticheat in KNOWN_ANTICHEATS for token in anticheat["tokens"]}, key=len, reverse=True)
)
LOG_FILE = Path(__file__).with_name("utopia-uninstaller.log")
LOG_LOCK = threading.Lock()
WINDOWS_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class InstalledApp:
    name: str
    publisher: str
    version: str
    size: str
    install_date: str
    uninstall_string: str
    quiet_uninstall_string: str
    source: str = "Registry"
    install_path: str = ""
    drive: str = ""
    registry_key: str = ""
    icon_path: str = ""
    item_type: str = "Installed Program"
    service_name: str = ""
    startup_name: str = ""
    startup_hive_name: str = ""
    startup_reg_path: str = ""
    package_name: str = ""


@dataclass
class ActionPlan:
    action_label: str
    can_execute: bool = True
    requires_admin: bool = False
    command: list[str] = field(default_factory=list)
    command_text: str = ""
    protocol_uri: str = ""
    delete_path: str = ""
    registry_key_to_remove: str = ""
    details: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pending: bool = False
    use_shell: bool = False


@dataclass
class ScanState:
    program_names: set[str] = field(default_factory=set)
    program_paths: set[str] = field(default_factory=set)
    store_names: set[str] = field(default_factory=set)
    store_paths: set[str] = field(default_factory=set)
    service_names: set[str] = field(default_factory=set)
    service_ids: set[str] = field(default_factory=set)
    startup_names: set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)


def log_event(event: str, **data):
    payload = {"timestamp": datetime.now().isoformat(timespec="seconds"), "event": event, **data}
    try:
        with LOG_LOCK:
            with open(LOG_FILE, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


def is_running_as_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def registry_hive_from_name(hive_name: str):
    if hive_name == "HKLM":
        return winreg.HKEY_LOCAL_MACHINE
    if hive_name == "HKCU":
        return winreg.HKEY_CURRENT_USER
    return None


def registry_key_requires_admin(registry_key: str) -> bool:
    return registry_key.upper().startswith("HKLM\\")


def normalize_name(value: str) -> str:
    return re.sub(r"[\W_]+", "", (value or "").casefold())


def get_known_anticheat(*values: str):
    haystacks = [normalize_name(value) for value in values if value]
    if not haystacks:
        return None
    for anticheat in KNOWN_ANTICHEATS:
        if any(token in haystack for haystack in haystacks for token in anticheat["tokens"]):
            return anticheat
    return None


def _strip_outer_quotes(value: str) -> str:
    return (value or "").strip().strip('"')


def _strip_icon_resource(value: str) -> str:
    candidate = _strip_outer_quotes(value)
    return re.sub(r",\s*-?\d+\s*$", "", candidate).strip()


def _expand_path_candidate(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(_strip_outer_quotes(value)))


def split_command_line(command_text: str) -> list[str] | None:
    text = (command_text or "").strip()
    if not text:
        return None
    try:
        parts = shlex.split(text, posix=False)
    except ValueError:
        return None
    if not parts:
        return None
    cleaned = []
    for index, part in enumerate(parts):
        cleaned.append(_expand_path_candidate(part) if index == 0 else part)
    return cleaned


def extract_protocol_uri(command_text: str) -> str:
    raw = (command_text or "").strip()
    lowered = raw.lower()
    for prefix in ("steam://uninstall/", "com.epicgames.launcher://", "goggalaxy://"):
        start = lowered.find(prefix)
        if start >= 0:
            return raw[start:].strip().strip('"')
    return ""


def is_executable_path(path: str) -> bool:
    candidate = _expand_path_candidate(_strip_icon_resource(path))
    return bool(candidate) and os.path.isfile(candidate) and candidate.lower().endswith(EXECUTABLE_SUFFIXES)


def extract_exe_from_command(path_or_cmd: str) -> str:
    if not path_or_cmd:
        return ""

    direct = _expand_path_candidate(_strip_icon_resource(path_or_cmd))
    if os.path.isfile(direct) and direct.lower().endswith(EXECUTABLE_SUFFIXES):
        return direct

    parts = split_command_line(_strip_icon_resource(path_or_cmd))
    if parts:
        executable = _expand_path_candidate(parts[0])
        if executable.lower() in {"msiexec", "msiexec.exe"}:
            return "msiexec.exe"
        if os.path.isfile(executable) and executable.lower().endswith(EXECUTABLE_SUFFIXES):
            return executable

    quoted = re.match(r'^"([^"]+\.(?:exe|bat|cmd|com|msi))"', path_or_cmd, flags=re.IGNORECASE)
    if quoted:
        candidate = _expand_path_candidate(quoted.group(1))
        if os.path.isfile(candidate):
            return candidate

    bare = re.match(r"^([a-zA-Z]:\\[^\"]+\.(?:exe|bat|cmd|com|msi))", path_or_cmd, flags=re.IGNORECASE)
    if bare:
        candidate = _expand_path_candidate(bare.group(1))
        if os.path.isfile(candidate):
            return candidate

    return ""


def is_real_directory_path(path: str) -> bool:
    candidate = _expand_path_candidate(path)
    return bool(candidate) and os.path.isdir(candidate)


def get_directory_from_path(path_or_cmd: str) -> str:
    if is_real_directory_path(path_or_cmd):
        return os.path.normpath(_expand_path_candidate(path_or_cmd))
    executable = extract_exe_from_command(path_or_cmd)
    if executable and os.path.isabs(executable):
        return os.path.dirname(executable)
    return ""


def normalize_path(value: str) -> str:
    candidate = _expand_path_candidate(value)
    if not candidate:
        return ""
    return os.path.normcase(os.path.normpath(candidate))


def get_path_identity(path_or_cmd: str) -> str:
    directory = get_directory_from_path(path_or_cmd)
    if directory:
        return normalize_path(directory)
    return normalize_path(path_or_cmd)


def is_path_within(path: str, base: str) -> bool:
    if not path or not base:
        return False
    try:
        return os.path.commonpath([normalize_path(path), normalize_path(base)]) == normalize_path(base)
    except ValueError:
        return False


def get_allowed_delete_bases() -> list[str]:
    bases = []
    for base in PROGRAM_DIRS:
        if base:
            normalized = normalize_path(base)
            if normalized:
                bases.append(normalized)
    return bases


def get_protected_paths() -> set[str]:
    appdata = os.environ.get("APPDATA", "")
    localappdata = os.environ.get("LOCALAPPDATA", "")
    appdata_root = str(Path(appdata).parent) if appdata else ""
    protected = [
        os.environ.get("SYSTEMDRIVE", "C:") + "\\",
        os.environ.get("WINDIR", r"C:\Windows"),
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        str(Path.home()),
        appdata_root,
        appdata,
        localappdata,
        os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
    ]
    return {normalize_path(path) for path in protected if path}


def requires_admin_for_path(path: str) -> bool:
    if not path:
        return False
    candidate = normalize_path(path)
    admin_roots = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
    ]
    return any(is_path_within(candidate, root) for root in admin_roots if root)


def count_child_directories(path: str) -> int:
    try:
        return sum(1 for entry in os.scandir(path) if entry.is_dir())
    except (PermissionError, OSError):
        return 0


def assess_delete_target(path: str) -> tuple[str, list[str], list[str]]:
    warnings = ["Direct folder deletion is irreversible and may leave configuration data behind."]
    reasons = []

    if not is_real_directory_path(path):
        return "", ["Target is not a real directory."], warnings

    candidate = normalize_path(path)
    allowed_bases = get_allowed_delete_bases()
    protected = get_protected_paths()
    folder_name = os.path.basename(candidate).casefold()

    if candidate in protected:
        reasons.append("Target is a protected system or profile folder.")

    if not any(is_path_within(candidate, base) and candidate != base for base in allowed_bases):
        reasons.append("Folder is outside the allowed software directories.")

    if folder_name in SHARED_VENDOR_ROOTS:
        reasons.append("Folder name matches a known shared vendor root.")

    if folder_name in GENERIC_FOLDER_NAMES:
        reasons.append("Folder name is too generic for safe deletion.")

    if count_child_directories(candidate) >= 5:
        reasons.append("Folder contains many child directories and may hold multiple apps.")

    return candidate, reasons, warnings


def read_registry_value(key, value_name):
    try:
        return winreg.QueryValueEx(key, value_name)[0]
    except OSError:
        return ""


def parse_size_to_bytes(size_str: str) -> int:
    s = (size_str or "").strip()
    try:
        if s.endswith(" GB"):
            return int(float(s[:-3]) * 1_073_741_824)
        if s.endswith(" MB"):
            return int(float(s[:-3]) * 1_048_576)
        if s.endswith(" KB"):
            return int(float(s[:-3]) * 1024)
    except ValueError:
        pass
    return 0


def format_size(size_kb):
    if not size_kb:
        return ""
    try:
        size_kb = int(size_kb)
    except (ValueError, TypeError):
        return ""
    if size_kb >= 1_048_576:
        return f"{size_kb / 1_048_576:.1f} GB"
    if size_kb >= 1024:
        return f"{size_kb / 1024:.1f} MB"
    return f"{size_kb} KB"


def format_date(raw):
    if not raw or len(raw) != 8:
        return str(raw) if raw else ""
    try:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    except Exception:
        return str(raw)


def get_drive(path: str) -> str:
    candidate = get_directory_from_path(path) or extract_exe_from_command(path) or _expand_path_candidate(path)
    if len(candidate) >= 2 and candidate[1] == ":":
        return candidate[:2].upper()
    return ""


def _resolve_icon_path(raw: str) -> str:
    cleaned = _strip_icon_resource(raw)
    if is_executable_path(cleaned):
        return _expand_path_candidate(cleaned)
    executable = extract_exe_from_command(cleaned)
    if executable and os.path.isfile(executable):
        return executable
    return ""


def _find_exe_icon(install_path: str) -> str:
    executable = extract_exe_from_command(install_path)
    if executable and os.path.isfile(executable):
        return executable
    if not is_real_directory_path(install_path):
        return ""
    try:
        for entry in os.scandir(install_path):
            if entry.is_file() and entry.name.lower().endswith(".exe"):
                return entry.path
    except (PermissionError, OSError):
        pass
    return ""


def _folder_contains_signature(path: str, match_keys: set[str]) -> bool:
    scanned = 0
    try:
        for entry in os.scandir(path):
            scanned += 1
            if scanned > 25:
                break
            entry_key = normalize_name(os.path.splitext(entry.name)[0])
            if any(key and (entry_key == key or key in entry_key) for key in match_keys):
                return True
        return scanned > 0
    except (PermissionError, OSError):
        return False


def find_possible_leftover_folders(app: InstalledApp) -> list[str]:
    if app.item_type not in {"Installed Program", "Store App"}:
        return []

    match_keys = {normalize_name(app.name)}
    publisher_key = normalize_name(app.publisher)
    if publisher_key and publisher_key not in {"microsoft", "steam", "epicgames", "gogcom"}:
        match_keys.add(publisher_key)

    bases = [
        os.environ.get("LOCALAPPDATA", ""),
        os.environ.get("APPDATA", ""),
        os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
    ]
    matches = []
    seen = set()

    for base in bases:
        if not base or not os.path.isdir(base):
            continue
        try:
            for entry in os.scandir(base):
                if not entry.is_dir():
                    continue
                folder_key = normalize_name(entry.name)
                if folder_key not in match_keys:
                    continue
                if not _folder_contains_signature(entry.path, match_keys):
                    continue
                normalized = normalize_path(entry.path)
                if normalized in seen:
                    continue
                seen.add(normalized)
                matches.append(entry.path)
                if len(matches) >= 5:
                    return matches
        except (PermissionError, OSError):
            continue

    return matches


def _claim_entry(name_set: set[str], path_set: set[str] | None, name: str = "", path: str = "") -> bool:
    name_key = normalize_name(name)
    path_key = get_path_identity(path)
    if name_key and name_key in name_set:
        return False
    if path_set is not None and path_key and path_key in path_set:
        return False
    if name_key:
        name_set.add(name_key)
    if path_set is not None and path_key:
        path_set.add(path_key)
    return True


def claim_program_entry(state: ScanState, name: str, path: str = "") -> bool:
    with state.lock:
        return _claim_entry(state.program_names, state.program_paths, name, path)


def claim_store_entry(state: ScanState, name: str, path: str = "") -> bool:
    with state.lock:
        return _claim_entry(state.store_names, state.store_paths, name, path)


def claim_service_entry(state: ScanState, name: str, path: str = "") -> bool:
    name_key = normalize_name(name)
    service_key = normalize_name(path)
    with state.lock:
        if name_key and name_key in state.service_names:
            return False
        if service_key and service_key in state.service_ids:
            return False
        if name_key:
            state.service_names.add(name_key)
        if service_key:
            state.service_ids.add(service_key)
        return True


def claim_startup_entry(state: ScanState, name: str) -> bool:
    with state.lock:
        return _claim_entry(state.startup_names, None, name)


def _parse_steam_libraries() -> list[str]:
    steam_path = ""
    for hive, key_path, value_name in GAME_PLATFORM_REGISTRY:
        if "Steam" not in key_path:
            continue
        try:
            key = winreg.OpenKey(hive, key_path)
            steam_path = read_registry_value(key, value_name)
            winreg.CloseKey(key)
            if steam_path:
                break
        except OSError:
            continue

    if not steam_path:
        return []

    libraries = [os.path.join(steam_path, "steamapps")]
    vdf_path = os.path.join(steam_path, "steamapps", STEAM_LIBRARY_VDF)
    if os.path.isfile(vdf_path):
        try:
            with open(vdf_path, "r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    if '"path"' not in line:
                        continue
                    parts = line.strip().split('"')
                    if len(parts) >= 4:
                        libraries.append(os.path.join(parts[3], "steamapps"))
        except OSError:
            pass
    return libraries


def _scan_steam_games(state: ScanState) -> list[InstalledApp]:
    apps = []
    for library_dir in _parse_steam_libraries():
        common_path = os.path.join(library_dir, "common")
        if not os.path.isdir(common_path):
            continue

        manifests = {}
        try:
            for entry in os.scandir(library_dir):
                if not (entry.name.startswith("appmanifest_") and entry.name.endswith(".acf")):
                    continue
                data = {}
                with open(entry.path, "r", encoding="utf-8", errors="ignore") as handle:
                    for line in handle:
                        parts = line.strip().split('"')
                        if len(parts) >= 4:
                            data[parts[1].lower()] = parts[3]
                if data.get("installdir"):
                    manifests[data["installdir"].lower()] = data
        except OSError:
            pass

        try:
            for entry in os.scandir(common_path):
                if not entry.is_dir():
                    continue
                if not claim_program_entry(state, entry.name, entry.path):
                    continue

                manifest = manifests.get(entry.name.lower(), {})
                if not manifest:
                    for candidate in manifests.values():
                        install_dir = candidate.get("installdir", "")
                        manifest_path = os.path.normpath(os.path.join(common_path, install_dir)).lower()
                        if install_dir and manifest_path == os.path.normpath(entry.path).lower():
                            manifest = candidate
                            break

                size_str = ""
                if manifest.get("sizeondisk"):
                    size_str = format_size(int(manifest["sizeondisk"]) // 1024)

                app_id = manifest.get("appid", "")
                apps.append(
                    InstalledApp(
                        name=entry.name,
                        publisher="Steam",
                        version=manifest.get("buildid", ""),
                        size=size_str,
                        install_date="",
                        uninstall_string=f"steam://uninstall/{app_id}" if app_id else "",
                        quiet_uninstall_string="",
                        source="Steam",
                        install_path=entry.path,
                        drive=get_drive(entry.path),
                        item_type="Installed Program",
                    )
                )
        except (PermissionError, OSError):
            continue
    return apps


def _scan_epic_games(state: ScanState) -> list[InstalledApp]:
    apps = []
    manifests_dir = os.path.join(
        os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
        "Epic",
        "EpicGamesLauncher",
        "Data",
        "Manifests",
    )
    if not os.path.isdir(manifests_dir):
        return apps

    try:
        for entry in os.scandir(manifests_dir):
            if not entry.name.endswith(".item"):
                continue
            try:
                with open(entry.path, "r", encoding="utf-8", errors="ignore") as handle:
                    data = json.load(handle)
            except (json.JSONDecodeError, OSError):
                continue

            name = data.get("DisplayName", "")
            install_location = data.get("InstallLocation", "")
            if not name or not claim_program_entry(state, name, install_location):
                continue

            install_size = data.get("InstallSize", 0)
            size_str = format_size(int(install_size) // 1024) if install_size else ""
            app_name = data.get("AppName", "")
            apps.append(
                InstalledApp(
                    name=name,
                    publisher="Epic Games",
                    version=data.get("AppVersionString", ""),
                    size=size_str,
                    install_date="",
                    uninstall_string=f"com.epicgames.launcher://apps/{app_name}?action=uninstall" if app_name else "",
                    quiet_uninstall_string="",
                    source="Epic",
                    install_path=install_location,
                    drive=get_drive(install_location),
                    item_type="Installed Program",
                )
            )
    except (PermissionError, OSError):
        pass
    return apps


def _scan_gog_games(state: ScanState) -> list[InstalledApp]:
    apps = []
    try:
        reg_key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\GOG.com\Games")
    except OSError:
        return apps

    try:
        index = 0
        while True:
            try:
                subkey_name = winreg.EnumKey(reg_key, index)
                index += 1
            except OSError:
                break

            try:
                subkey = winreg.OpenKey(reg_key, subkey_name)
                name = read_registry_value(subkey, "gameName") or read_registry_value(subkey, "GAMENAME")
                path = read_registry_value(subkey, "path") or read_registry_value(subkey, "PATH")
                uninstall = read_registry_value(subkey, "uninstallCommand") or ""
                winreg.CloseKey(subkey)
            except OSError:
                continue

            if not name or not claim_program_entry(state, name, path):
                continue

            apps.append(
                InstalledApp(
                    name=name,
                    publisher="GOG.com",
                    version="",
                    size="",
                    install_date="",
                    uninstall_string=uninstall,
                    quiet_uninstall_string="",
                    source="GOG",
                    install_path=path or "",
                    drive=get_drive(path or ""),
                    item_type="Installed Program",
                )
            )
    finally:
        winreg.CloseKey(reg_key)

    return apps


def _scan_store_and_services(state: ScanState) -> list[InstalledApp]:
    apps = []
    anticheat_pattern = "|".join(re.escape(token) for token in KNOWN_ANTICHEAT_TOKENS)
    script = """
$store = Get-AppxPackage | Select-Object Name, Publisher, Version, InstallLocation, PackageFamilyName | ConvertTo-Json -Compress
$anticheatPattern = '__ANTICHEAT_PATTERN__'
$matchesKnownAnticheat = {
    param($item)
    $needle = ((@($item.Name, $item.DisplayName, $item.PathName) -join ' ') -replace '[\\W_]+', '').ToLowerInvariant()
    $needle -match $anticheatPattern
}
$serviceItems = @(
    Get-CimInstance Win32_Service | Where-Object {
        ($_.PathName -and $_.PathName -notmatch 'Windows|System32|svchost|Microsoft') -or (& $matchesKnownAnticheat $_)
    } | Select-Object Name, DisplayName, PathName
    Get-CimInstance Win32_SystemDriver | Where-Object { & $matchesKnownAnticheat $_ } | Select-Object Name, DisplayName, PathName
)
$services = if ($serviceItems) { $serviceItems | Sort-Object Name, PathName -Unique | ConvertTo-Json -Compress } else { '[]' }
'{"store":' + $store + ',"services":' + $services + '}'
""".replace("__ANTICHEAT_PATTERN__", anticheat_pattern)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=WINDOWS_NO_WINDOW,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return apps
        payload = json.loads(result.stdout)
    except Exception as exc:
        log_event("scan_error", source="powershell", error=str(exc))
        return apps

    store_apps = payload.get("store", [])
    if isinstance(store_apps, dict):
        store_apps = [store_apps]

    skip_keywords = {
        "framework",
        ".net",
        "vclibs",
        "microsoft.ui",
        "microsoft.services",
        "microsoft.windows",
        "inputapp",
        "microsoft.desktopappinstaller",
        "microsoft.screensketch",
        "microsoft.webp",
        "microsoft.heif",
        "microsoft.vp9",
        "microsoft.hevc",
        "microsoft.webmedia",
        "microsoft.raw",
        "microsoft.av1",
    }

    for store_app in store_apps:
        raw_name = store_app.get("Name", "")
        if not raw_name or any(token in raw_name.lower() for token in skip_keywords):
            continue

        display_name = raw_name.split(".")[-1] if "." in raw_name else raw_name
        pretty_name = ""
        for char in display_name:
            if char.isupper() and pretty_name and pretty_name[-1] != " ":
                pretty_name += " "
            pretty_name += char

        install_location = store_app.get("InstallLocation", "")
        if not claim_store_entry(state, pretty_name, install_location or raw_name):
            continue

        publisher = store_app.get("Publisher", "")
        if publisher.startswith("CN="):
            publisher = publisher[3:].split(",")[0]

        apps.append(
            InstalledApp(
                name=pretty_name,
                publisher=publisher,
                version=store_app.get("Version", ""),
                size="",
                install_date="",
                uninstall_string="",
                quiet_uninstall_string="",
                source="Store",
                install_path=install_location,
                drive=get_drive(install_location),
                item_type="Store App",
                package_name=raw_name,
            )
        )

    services = payload.get("services", [])
    if isinstance(services, dict):
        services = [services]

    for service in services:
        display_name = service.get("DisplayName", "")
        service_name = service.get("Name", "")
        service_path = service.get("PathName", "")
        if not display_name or not service_name:
            anticheat = get_known_anticheat(service_name, service_path)
            if not anticheat or not service_name:
                continue
            display_name = anticheat["name"]
        else:
            anticheat = get_known_anticheat(display_name, service_name, service_path)

        service_label = display_name
        if anticheat:
            service_label = anticheat["name"] if normalize_name(display_name) == normalize_name(anticheat["name"]) else f"{anticheat['name']} ({display_name})"

        if not claim_service_entry(state, service_label, service_name):
            continue

        apps.append(
            InstalledApp(
                name=service_label,
                publisher=anticheat["publisher"] if anticheat else "",
                version="",
                size="",
                install_date="",
                uninstall_string="",
                quiet_uninstall_string="",
                source="Service",
                install_path=service_path,
                drive=get_drive(service_path),
                item_type="Service",
                service_name=service_name,
            )
        )

    return apps


def _scan_known_anticheat_paths(state: ScanState) -> list[InstalledApp]:
    apps = []
    for anticheat in KNOWN_ANTICHEATS:
        for raw_path in anticheat["paths"]:
            install_path = _expand_path_candidate(raw_path)
            if not is_real_directory_path(install_path):
                continue

            has_exe = False
            uninstall_exe = ""
            try:
                for entry in os.scandir(install_path):
                    if not entry.is_file() or not entry.name.lower().endswith(".exe"):
                        continue
                    has_exe = True
                    lower_name = entry.name.lower()
                    if not uninstall_exe and any(hint in lower_name for hint in UNINSTALLER_HINTS):
                        uninstall_exe = entry.path
            except (PermissionError, OSError):
                continue

            if not has_exe:
                continue
            if not claim_program_entry(state, anticheat["name"], install_path):
                break

            apps.append(
                InstalledApp(
                    name=anticheat["name"],
                    publisher=anticheat["publisher"],
                    version="",
                    size="",
                    install_date="",
                    uninstall_string=uninstall_exe,
                    quiet_uninstall_string="",
                    source="Filesystem",
                    install_path=install_path,
                    drive=get_drive(install_path),
                    item_type="Installed Program" if uninstall_exe else "Orphaned Folder",
                )
            )
            break

    return apps


def _scan_filesystem(state: ScanState) -> list[InstalledApp]:
    apps = []
    for base_dir in PROGRAM_DIRS:
        if not base_dir or not os.path.isdir(base_dir):
            continue
        try:
            for entry in os.scandir(base_dir):
                if not entry.is_dir():
                    continue
                folder_name = entry.name.casefold()
                if folder_name in FS_SKIP or folder_name in SHARED_VENDOR_ROOTS or folder_name.startswith("."):
                    continue
                if folder_name in GENERIC_FOLDER_NAMES:
                    continue

                has_exe = False
                uninstall_exe = ""
                try:
                    for sub_entry in os.scandir(entry.path):
                        if not sub_entry.is_file() or not sub_entry.name.lower().endswith(".exe"):
                            continue
                        has_exe = True
                        lower_name = sub_entry.name.lower()
                        if not uninstall_exe and any(hint in lower_name for hint in UNINSTALLER_HINTS):
                            uninstall_exe = sub_entry.path
                except (PermissionError, OSError):
                    continue

                anticheat = get_known_anticheat(entry.name, entry.path)
                display_name = anticheat["name"] if anticheat else entry.name
                publisher = anticheat["publisher"] if anticheat else ""
                if not has_exe or not claim_program_entry(state, display_name, entry.path):
                    continue

                apps.append(
                    InstalledApp(
                        name=display_name,
                        publisher=publisher,
                        version="",
                        size="",
                        install_date="",
                        uninstall_string=uninstall_exe,
                        quiet_uninstall_string="",
                        source="Filesystem",
                        install_path=entry.path,
                        drive=get_drive(entry.path),
                        item_type="Installed Program" if uninstall_exe else "Orphaned Folder",
                    )
                )
        except (PermissionError, OSError):
            continue
    apps.extend(_scan_known_anticheat_paths(state))
    return apps


def _scan_registry(state: ScanState) -> list[InstalledApp]:
    apps = []
    for hive, path in REGISTRY_PATHS:
        try:
            reg_key = winreg.OpenKey(hive, path)
        except OSError:
            continue

        try:
            index = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(reg_key, index)
                    index += 1
                except OSError:
                    break

                try:
                    subkey = winreg.OpenKey(reg_key, subkey_name)
                except OSError:
                    continue

                system_component = read_registry_value(subkey, "SystemComponent")
                install_location = read_registry_value(subkey, "InstallLocation") or ""
                uninstall_string = read_registry_value(subkey, "UninstallString") or ""
                quiet_uninstall_string = read_registry_value(subkey, "QuietUninstallString") or ""
                publisher = read_registry_value(subkey, "Publisher") or ""
                display_icon = read_registry_value(subkey, "DisplayIcon") or ""
                known_anticheat = get_known_anticheat(
                    read_registry_value(subkey, "DisplayName"),
                    publisher,
                    install_location,
                    uninstall_string,
                    quiet_uninstall_string,
                    display_icon,
                    subkey_name,
                )
                name = known_anticheat["name"] if known_anticheat else read_registry_value(subkey, "DisplayName")
                if not name or (system_component == 1 and not known_anticheat):
                    winreg.CloseKey(subkey)
                    continue

                if not claim_program_entry(state, name, install_location or subkey_name):
                    winreg.CloseKey(subkey)
                    continue

                if known_anticheat and not publisher:
                    publisher = known_anticheat["publisher"]

                uninstall_lower = uninstall_string.lower()
                source = "Registry"
                if "steam" in uninstall_lower or "steam://uninstall" in uninstall_lower:
                    source = "Steam"
                    if "steam://uninstall" not in uninstall_string and subkey_name.startswith("Steam App "):
                        app_id = subkey_name.replace("Steam App ", "")
                        uninstall_string = f"steam://uninstall/{app_id}"
                elif "epicgames" in uninstall_lower or "epic games" in publisher.lower():
                    source = "Epic"
                elif "gog" in uninstall_lower or "gog.com" in publisher.lower():
                    source = "GOG"
                elif "ubisoft" in uninstall_lower or "ubisoft" in publisher.lower():
                    source = "Ubisoft"
                elif "ea app" in uninstall_lower or "origin" in uninstall_lower or "electronic arts" in publisher.lower():
                    source = "EA"

                hive_name = "HKLM" if hive == winreg.HKEY_LOCAL_MACHINE else "HKCU"
                registry_key = f"{hive_name}\\{path}\\{subkey_name}"

                apps.append(
                    InstalledApp(
                        name=name,
                        publisher=publisher,
                        version=read_registry_value(subkey, "DisplayVersion") or "",
                        size=format_size(read_registry_value(subkey, "EstimatedSize")),
                        install_date=format_date(read_registry_value(subkey, "InstallDate")),
                        uninstall_string=uninstall_string,
                        quiet_uninstall_string=quiet_uninstall_string,
                        source=source,
                        install_path=install_location,
                        drive=get_drive(install_location or display_icon),
                        registry_key=registry_key,
                        icon_path=display_icon,
                        item_type="Installed Program",
                    )
                )
                winreg.CloseKey(subkey)
        finally:
            winreg.CloseKey(reg_key)

    return apps


def _scan_startup(state: ScanState) -> list[InstalledApp]:
    apps = []
    for hive, path in STARTUP_REG_PATHS:
        try:
            reg_key = winreg.OpenKey(hive, path)
        except OSError:
            continue

        hive_name = "HKLM" if hive == winreg.HKEY_LOCAL_MACHINE else "HKCU"
        try:
            index = 0
            while True:
                try:
                    value_name, value_data, _ = winreg.EnumValue(reg_key, index)
                    index += 1
                except OSError:
                    break

                if not value_name or not claim_startup_entry(state, f"{hive_name}:{value_name}"):
                    continue

                apps.append(
                    InstalledApp(
                        name=value_name,
                        publisher="",
                        version="",
                        size="",
                        install_date="",
                        uninstall_string="",
                        quiet_uninstall_string="",
                        source="Startup",
                        install_path=str(value_data),
                        drive=get_drive(str(value_data)),
                        item_type="Startup Item",
                        startup_name=value_name,
                        startup_hive_name=hive_name,
                        startup_reg_path=path,
                    )
                )
        finally:
            winreg.CloseKey(reg_key)

    return apps


def _calc_folder_size(path: str) -> int:
    total = 0
    try:
        with os.scandir(path) as iterator:
            for entry in iterator:
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                    elif entry.is_dir(follow_symlinks=False):
                        total += _calc_folder_size(entry.path)
                except (PermissionError, OSError):
                    pass
    except (PermissionError, OSError):
        pass
    return total


def get_installed_apps() -> list[InstalledApp]:
    state = ScanState()
    apps = _scan_registry(state)

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [
            pool.submit(_scan_store_and_services, state),
            pool.submit(_scan_steam_games, state),
            pool.submit(_scan_epic_games, state),
            pool.submit(_scan_gog_games, state),
            pool.submit(_scan_filesystem, state),
            pool.submit(_scan_startup, state),
        ]
        for future in as_completed(futures):
            try:
                apps.extend(future.result())
            except Exception as exc:
                log_event("scan_error", source="executor", error=str(exc))

    apps.sort(key=lambda app: (ITEM_TYPES.index(app.item_type) if app.item_type in ITEM_TYPES else len(ITEM_TYPES), app.name.casefold()))
    return apps


def prepare_command(command_text: str) -> tuple[list[str], str, bool]:
    raw = (command_text or "").strip()
    if not raw:
        return [], "", False

    parts = split_command_line(raw)
    if not parts:
        return [], raw, True

    executable = parts[0]
    lower_executable = executable.lower()
    if lower_executable in {"msiexec", "msiexec.exe"}:
        parts[0] = "msiexec.exe"
    elif executable.lower().endswith(".msi") and os.path.isfile(executable):
        parts = ["msiexec.exe", "/i", executable, *parts[1:]]

    return parts, subprocess.list2cmdline(parts), False


def cleanup_uninstall_registry_key(app: InstalledApp) -> str:
    if not app.registry_key:
        return ""

    try:
        hive_name, sub_path = app.registry_key.split("\\", 1)
    except ValueError:
        return ""

    hive = registry_hive_from_name(hive_name)
    if hive is None:
        return ""

    try:
        winreg.DeleteKey(hive, sub_path)
        log_event("registry_cleanup", name=app.name, registry_key=app.registry_key)
        return f"Removed uninstall registry key: {app.registry_key}"
    except PermissionError:
        message = "Skipped uninstall key cleanup: administrator rights are required."
        log_event("registry_cleanup_skipped", name=app.name, registry_key=app.registry_key, reason=message)
        return message
    except OSError as exc:
        log_event("registry_cleanup_skipped", name=app.name, registry_key=app.registry_key, reason=str(exc))
        return ""


def run_prepared_command(plan: ActionPlan, raw_command: str) -> subprocess.CompletedProcess:
    if plan.use_shell:
        log_event("run_command", command=raw_command, shell=True)
        return subprocess.run(raw_command, shell=True, timeout=600, creationflags=WINDOWS_NO_WINDOW)

    log_event("run_command", command=plan.command_text, shell=False)
    return subprocess.run(plan.command, shell=False, timeout=600, creationflags=WINDOWS_NO_WINDOW)


def handle_launcher_uninstall(app: InstalledApp, plan: ActionPlan) -> tuple[str, str]:
    os.startfile(plan.protocol_uri)
    message = (
        f"Opened {app.source} uninstall flow for {app.name}.\n"
        f"Finish the uninstall in the {app.source} launcher.\n"
        "This app has not yet confirmed removal."
    )
    log_event("launcher_uninstall_opened", name=app.name, source=app.source, uri=plan.protocol_uri)
    return "pending", message


def handle_store_uninstall(app: InstalledApp, plan: ActionPlan) -> tuple[str, str]:
    result = run_prepared_command(plan, "")
    if result.returncode == 0:
        return "success", f"Store app uninstall command completed for {app.name}."
    return "failed", f"Store uninstall exited with code {result.returncode}."


def handle_service_delete(app: InstalledApp, plan: ActionPlan) -> tuple[str, str]:
    result = subprocess.run(["sc", "delete", app.service_name], shell=False, timeout=120, creationflags=WINDOWS_NO_WINDOW)
    log_event("service_delete", name=app.name, service_name=app.service_name, returncode=result.returncode)
    if result.returncode == 0:
        return "success", f"Deleted service entry for {app.name}. Program files may remain on disk."
    return "failed", f"Service deletion exited with code {result.returncode}."


def handle_startup_remove(app: InstalledApp, plan: ActionPlan) -> tuple[str, str]:
    hive = registry_hive_from_name(app.startup_hive_name)
    if hive is None:
        return "failed", "Startup entry metadata is incomplete."

    try:
        reg_key = winreg.OpenKey(hive, app.startup_reg_path, 0, winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(reg_key, app.startup_name)
        finally:
            winreg.CloseKey(reg_key)
        log_event("startup_remove", name=app.name, registry=f"{app.startup_hive_name}\\{app.startup_reg_path}", value=app.startup_name)
        return "success", f"Removed startup entry {app.startup_name}. Program files remain on disk."
    except PermissionError:
        return "failed", "Removing this startup entry requires administrator rights."
    except OSError as exc:
        log_event("startup_remove_failed", name=app.name, error=str(exc))
        return "failed", str(exc)


def handle_filesystem_delete(app: InstalledApp, plan: ActionPlan) -> tuple[str, str]:
    try:
        shutil.rmtree(plan.delete_path)
    except FileNotFoundError:
        return "failed", "Folder is no longer present."
    except PermissionError:
        return "failed", "Folder deletion failed because access was denied."
    except OSError as exc:
        log_event("folder_delete_failed", name=app.name, path=plan.delete_path, error=str(exc))
        return "failed", str(exc)

    if os.path.exists(plan.delete_path):
        return "failed", "Folder could not be removed completely. Some files may still be in use."

    log_event("folder_deleted", name=app.name, path=plan.delete_path)
    return "success", f"Deleted folder: {plan.delete_path}"


def handle_registry_uninstall(app: InstalledApp, plan: ActionPlan) -> tuple[str, str]:
    raw_command = app.uninstall_string or app.quiet_uninstall_string
    result = run_prepared_command(plan, raw_command)
    if result.returncode == 0:
        message = "Uninstall command completed successfully."
        cleanup_message = cleanup_uninstall_registry_key(app)
        if cleanup_message:
            message = f"{message}\n{cleanup_message}"
        return "success", message
    return "failed", f"Uninstall command exited with code {result.returncode}."


def build_action_plan(app: InstalledApp, is_admin: bool) -> ActionPlan:
    if app.item_type == "Service":
        plan = ActionPlan(action_label="Delete Service Entry", requires_admin=True)
        plan.command = ["sc", "delete", app.service_name]
        plan.command_text = subprocess.list2cmdline(plan.command) if app.service_name else ""
        plan.details.append(f"Service: {app.service_name or app.name}")
        plan.warnings.append("This only removes the Windows service entry. Program files may remain on disk.")
        plan.can_execute = bool(app.service_name)
        if not plan.can_execute:
            plan.warnings.append("Service metadata is incomplete.")
        return plan

    if app.item_type == "Startup Item":
        plan = ActionPlan(action_label="Remove Startup Entry", requires_admin=app.startup_hive_name == "HKLM")
        plan.details.append(f"Registry value: {app.startup_hive_name}\\{app.startup_reg_path}\\{app.startup_name}")
        plan.warnings.append("This removes only the startup registration. Program files remain on disk.")
        plan.can_execute = bool(app.startup_name and app.startup_hive_name and app.startup_reg_path)
        if not plan.can_execute:
            plan.warnings.append("Startup entry metadata is incomplete.")
        return plan

    if app.item_type == "Store App":
        plan = ActionPlan(action_label="Uninstall Store App")
        if app.package_name:
            plan.command = ["powershell", "-NoProfile", "-Command", f'Get-AppxPackage -Name "{app.package_name}" | Remove-AppxPackage']
            plan.command_text = subprocess.list2cmdline(plan.command)
            plan.details.append(f"Package: {app.package_name}")
        else:
            plan.can_execute = False
            plan.warnings.append("Store package name was not captured for this item.")
        return plan

    launcher_uri = extract_protocol_uri(app.uninstall_string or app.quiet_uninstall_string)
    if app.source in LAUNCHER_SOURCES and launcher_uri:
        plan = ActionPlan(action_label="Open Launcher Uninstall", protocol_uri=launcher_uri, pending=True)
        plan.details.append(f"Launcher URI: {launcher_uri}")
        plan.warnings.append(f"{app.source} will handle the uninstall. Removal is not confirmed by this app.")
        return plan

    uninstall_command = app.uninstall_string or app.quiet_uninstall_string
    if uninstall_command:
        command, preview, use_shell = prepare_command(uninstall_command)
        action_label = "Run Uninstaller" if app.source == "Filesystem" else "Uninstall Selected"
        plan = ActionPlan(action_label=action_label, command=command, command_text=preview or uninstall_command, use_shell=use_shell)
        plan.can_execute = bool(command) or bool(uninstall_command)
        if use_shell:
            plan.warnings.append("Command parsing failed, so this action would fall back to shell execution.")
        if app.registry_key:
            plan.registry_key_to_remove = app.registry_key
            if registry_key_requires_admin(app.registry_key) and not is_admin:
                plan.warnings.append("HKLM uninstall key cleanup will be skipped unless you run this app as administrator.")
            else:
                plan.details.append(f"Cleanup after success: {app.registry_key}")
        return plan

    plan = ActionPlan(action_label="Delete Folder")
    if app.source != "Filesystem" or app.item_type != "Orphaned Folder":
        plan.can_execute = False
        plan.warnings.append("Direct folder deletion is allowed only for safe filesystem targets.")
        if app.install_path:
            plan.details.append(f"Path: {app.install_path}")
        return plan

    safe_path, reasons, warnings = assess_delete_target(app.install_path)
    plan.delete_path = safe_path
    plan.requires_admin = requires_admin_for_path(safe_path)
    plan.details.append(f"Folder: {safe_path or app.install_path}")
    plan.warnings.extend(warnings)
    plan.warnings.append("This permanently deletes the folder without running an uninstaller.")

    if reasons:
        plan.can_execute = False
        plan.warnings.extend(reasons)

    return plan


def perform_action(app: InstalledApp, is_admin: bool) -> tuple[str, str]:
    plan = build_action_plan(app, is_admin)
    if not plan.can_execute:
        message = "\n".join(plan.warnings) or "This item cannot be acted on safely."
        log_event("action_blocked", name=app.name, item_type=app.item_type, reason=message)
        return "failed", message

    if plan.requires_admin and not is_admin:
        message = "This action requires administrator rights."
        log_event("action_blocked", name=app.name, item_type=app.item_type, reason=message)
        return "failed", message

    log_event(
        "action_started",
        name=app.name,
        item_type=app.item_type,
        source=app.source,
        action=plan.action_label,
        command=plan.command_text,
        delete_path=plan.delete_path,
        registry_key=plan.registry_key_to_remove,
    )

    try:
        if app.item_type == "Service":
            status, message = handle_service_delete(app, plan)
        elif app.item_type == "Startup Item":
            status, message = handle_startup_remove(app, plan)
        elif app.item_type == "Store App":
            status, message = handle_store_uninstall(app, plan)
        elif plan.protocol_uri:
            status, message = handle_launcher_uninstall(app, plan)
        elif plan.delete_path:
            status, message = handle_filesystem_delete(app, plan)
        else:
            status, message = handle_registry_uninstall(app, plan)
    except subprocess.TimeoutExpired:
        status, message = "failed", "Action timed out after 10 minutes."
    except Exception as exc:
        status, message = "failed", str(exc)

    log_event("action_finished", name=app.name, item_type=app.item_type, status=status, message=message)
    return status, message


_DRIVE_BAR_CATEGORIES = [
    ("Games",    "#94e2d5"),
    ("Software", "#89b4fa"),
    ("Store",    "#cba6f7"),
    ("Services", "#f9e2af"),
    ("Startup",  "#fab387"),
]
_DRIVE_BAR_OTHER_COLOR = "#585b70"
_DRIVE_BAR_FREE_COLOR  = "#2a2a3e"


def _drive_bar_category(app: InstalledApp) -> str:
    if app.source in LAUNCHER_SOURCES:
        return "Games"
    if app.item_type == "Store App":
        return "Store"
    if app.item_type == "Service":
        return "Services"
    if app.item_type == "Startup Item":
        return "Startup"
    return "Software"


class DriveUsageWidget(QWidget):
    """Stacked bar showing installed-app categories + unaccounted space per drive."""

    _BAR_H  = 16
    _ROW_H  = 28
    _LABEL_W = 36
    _TEXT_W  = 100

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data: list[tuple[str, int, int, dict[str, int]]] = []
        self.setMinimumHeight(0)

    def update_drives(self, apps: list[InstalledApp]) -> None:
        drive_cats: dict[str, dict[str, int]] = {}
        for app in apps:
            if not app.drive:
                continue
            cats = drive_cats.setdefault(app.drive, {})
            cat = _drive_bar_category(app)
            cats[cat] = cats.get(cat, 0) + parse_size_to_bytes(app.size)

        self._data = []
        for drive in sorted(drive_cats):
            try:
                usage = shutil.disk_usage(drive + "\\")
            except OSError:
                continue
            self._data.append((drive, usage.total, usage.used, drive_cats[drive]))

        rows = len(self._data)
        legend_h = 20
        self.setFixedHeight(rows * self._ROW_H + 8 + legend_h if rows else 0)
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update()

    def paintEvent(self, event):
        if not self._data:
            return

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        font = self.font()
        font.setPointSize(10)
        p.setFont(font)
        fm = QFontMetrics(font)

        cat_colors = dict(_DRIVE_BAR_CATEGORIES)
        cat_order  = [name for name, _ in _DRIVE_BAR_CATEGORIES]

        bar_x = self._LABEL_W
        bar_w = self.width() - self._LABEL_W - self._TEXT_W - 8
        bar_top_off = (self._ROW_H - self._BAR_H) // 2

        y = 4
        for drive, total, used, cats in self._data:
            # Drive label
            p.setPen(QColor("#cdd6f4"))
            p.drawText(0, y, self._LABEL_W, self._ROW_H,
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, drive)

            if bar_w > 10 and total > 0:
                byt = y + bar_top_off

                # Free background
                p.fillRect(bar_x, byt, bar_w, self._BAR_H, QColor(_DRIVE_BAR_FREE_COLOR))

                # Category segments (left to right)
                x = bar_x
                attributed = 0
                for cat in cat_order:
                    cat_bytes = cats.get(cat, 0)
                    if cat_bytes <= 0:
                        continue
                    sw = int(bar_w * cat_bytes / total)
                    if sw > 0:
                        p.fillRect(x, byt, sw, self._BAR_H, QColor(cat_colors[cat]))
                        x += sw
                    attributed += cat_bytes

                # "Other used" — OS, documents, music, pictures, videos …
                other_used = max(0, used - attributed)
                if other_used > 0:
                    sw = int(bar_w * other_used / total)
                    if sw > 0:
                        p.fillRect(x, byt, sw, self._BAR_H, QColor(_DRIVE_BAR_OTHER_COLOR))

                # Border
                p.setPen(QColor("#45475a"))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(bar_x, byt, bar_w, self._BAR_H)

            # Used / total text
            used_gb  = used  / 1_073_741_824
            total_gb = total / 1_073_741_824
            p.setPen(QColor("#a6adc8"))
            p.drawText(bar_x + bar_w + 6, y, self._TEXT_W, self._ROW_H,
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       f"{used_gb:.0f} / {total_gb:.0f} GB")

            y += self._ROW_H

        # Legend
        lx = 0
        ly = y + 2
        swatch = 10
        legend_items = list(_DRIVE_BAR_CATEGORIES) + [
            ("Other",  _DRIVE_BAR_OTHER_COLOR),
            ("Free",   _DRIVE_BAR_FREE_COLOR),
        ]
        for label, color in legend_items:
            p.fillRect(lx, ly + 4, swatch, swatch, QColor(color))
            p.setPen(QColor("#45475a"))
            p.drawRect(lx, ly + 4, swatch, swatch)
            p.setPen(QColor("#a6adc8"))
            label_w = fm.horizontalAdvance(label)
            p.drawText(lx + swatch + 3, ly, label_w + 2, 18,
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)
            lx += swatch + 3 + label_w + 10

        p.end()


class AppFilterProxyModel(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.search_text = ""
        self.drive_filter = "All Drives"
        self.type_filter = "All Types"
        self.setDynamicSortFilter(True)

    def set_search_text(self, text: str):
        self.search_text = (text or "").casefold().strip()
        self.invalidate()

    def set_drive_filter(self, text: str):
        self.drive_filter = text or "All Drives"
        self.invalidate()

    def set_type_filter(self, text: str):
        self.type_filter = text or "All Types"
        self.invalidate()

    def filterAcceptsRow(self, source_row: int, source_parent) -> bool:
        model = self.sourceModel()
        item = model.item(source_row, 0)
        app = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not app:
            return False

        if self.drive_filter != "All Drives" and app.drive != self.drive_filter:
            return False

        if self.type_filter != "All Types" and app.item_type != self.type_filter:
            return False

        if not self.search_text:
            return True

        for column in range(model.columnCount()):
            value = model.data(model.index(source_row, column, source_parent), Qt.ItemDataRole.DisplayRole)
            if self.search_text in str(value or "").casefold():
                return True
        return False


class SizeScanThread(QThread):
    size_ready = Signal(int, str)

    def __init__(self, apps: list[InstalledApp]):
        super().__init__()
        self.apps = apps
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        for index, app in enumerate(self.apps):
            if self._stop:
                return
            if app.size or not is_real_directory_path(app.install_path):
                continue
            total = _calc_folder_size(app.install_path)
            if total and not self._stop:
                app.size = format_size(total // 1024)
                self.size_ready.emit(index, app.size)


class ScanThread(QThread):
    finished = Signal(list)

    def run(self):
        self.finished.emit(get_installed_apps())


class ActionThread(QThread):
    finished = Signal(str, str)

    def __init__(self, app: InstalledApp, is_admin: bool):
        super().__init__()
        self.app = app
        self.is_admin = is_admin

    def run(self):
        self.finished.emit(*perform_action(self.app, self.is_admin))


class MultiActionThread(QThread):
    finished = Signal(list, list)
    progress = Signal(str)

    def __init__(self, apps: list[InstalledApp], is_admin: bool):
        super().__init__()
        self.apps = apps
        self.is_admin = is_admin

    def run(self):
        statuses = []
        messages = []
        for index, app in enumerate(self.apps, start=1):
            self.progress.emit(f"Running {index} of {len(self.apps)}: {app.name}")
            status, message = perform_action(app, self.is_admin)
            statuses.append(status)
            messages.append(f"{app.name}: {message}")
        self.finished.emit(statuses, messages)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Utopia Uninstaller")
        self.setMinimumSize(980, 640)
        self.resize(1120, 740)
        self.apps: list[InstalledApp] = []
        self.action_thread = None
        self.scan_thread = None
        self.size_thread = None
        self.is_admin = is_running_as_admin()

        self._build_ui()
        self._apply_style()
        self._update_admin_label()
        self._load_apps()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        header_row = QHBoxLayout()
        header = QLabel("Utopia Uninstaller")
        header.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        header_row.addWidget(header)
        header_row.addStretch()

        self.admin_label = QLabel()
        self.admin_label.setObjectName("adminStatus")
        self.admin_label.setTextFormat(Qt.TextFormat.RichText)
        header_row.addWidget(self.admin_label)
        layout.addLayout(header_row)

        search_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search items...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._on_filter_changed)
        search_row.addWidget(self.search_input)

        self.type_combo = QComboBox()
        self.type_combo.setFixedWidth(170)
        self.type_combo.addItem("All Types")
        for item_type in ITEM_TYPES:
            self.type_combo.addItem(item_type)
        self.type_combo.currentTextChanged.connect(self._on_filter_changed)
        search_row.addWidget(self.type_combo)

        self.drive_combo = QComboBox()
        self.drive_combo.setFixedWidth(110)
        self.drive_combo.addItem("All Drives")
        self.drive_combo.currentTextChanged.connect(self._on_filter_changed)
        search_row.addWidget(self.drive_combo)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setFixedWidth(96)
        self.refresh_btn.clicked.connect(self._load_apps)
        search_row.addWidget(self.refresh_btn)
        layout.addLayout(search_row)

        self.count_label = QLabel()
        layout.addWidget(self.count_label)

        self.drive_bar = DriveUsageWidget()
        layout.addWidget(self.drive_bar)

        self.model = QStandardItemModel()
        self.model.setHorizontalHeaderLabels(COLUMNS)

        self.proxy = AppFilterProxyModel()
        self.proxy.setSourceModel(self.model)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setIconSize(QSize(20, 20))
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(COLUMNS)):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setColumnWidth(1, 140)
        self.table.setColumnWidth(5, 90)
        self.table.setColumnWidth(6, 56)
        self.table.doubleClicked.connect(self._on_preview_selected)
        self.table.selectionModel().selectionChanged.connect(self._update_action_controls)
        layout.addWidget(self.table)

        bottom = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setRange(0, 0)
        bottom.addWidget(self.progress)

        self.preview_btn = QPushButton("Preview Selected")
        self.preview_btn.setFixedWidth(150)
        self.preview_btn.clicked.connect(self._on_preview_selected)
        bottom.addWidget(self.preview_btn)

        self.action_btn = QPushButton("Run Selected Action")
        self.action_btn.setFixedWidth(170)
        self.action_btn.clicked.connect(self._on_execute_selected)
        bottom.addWidget(self.action_btn)
        layout.addLayout(bottom)

        self._update_action_controls()

    def _update_admin_label(self):
        if self.is_admin:
            self.admin_label.setText('<b>Admin:</b> <span style="color:#a6e3a1;">Yes</span>')
        else:
            self.admin_label.setText('<b>Admin:</b> <span style="color:#f9e2af;">No</span> <span style="color:#a6adc8;">- elevated actions are disabled</span>')

    def _apply_style(self):
        self.setStyleSheet(
            """
            QMainWindow { background: #1e1e2e; }
            QLabel { color: #cdd6f4; }
            QLabel#adminStatus { font-size: 12px; }
            QLineEdit {
                background: #313244; color: #cdd6f4; border: 1px solid #45475a;
                border-radius: 6px; padding: 8px 12px; font-size: 14px;
            }
            QLineEdit:focus { border-color: #89b4fa; }
            QComboBox {
                background: #313244; color: #cdd6f4; border: 1px solid #45475a;
                border-radius: 6px; padding: 6px 10px; font-size: 13px;
            }
            QComboBox:hover { border-color: #89b4fa; }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background: #313244; color: #cdd6f4; selection-background-color: #45475a;
                border: 1px solid #45475a;
            }
            QPushButton {
                background: #89b4fa; color: #1e1e2e; border: none;
                border-radius: 6px; padding: 8px 16px; font-weight: bold; font-size: 13px;
            }
            QPushButton:hover { background: #74c7ec; }
            QPushButton:pressed { background: #89dceb; }
            QPushButton:disabled { background: #45475a; color: #6c7086; }
            QTableView {
                background: #181825; color: #cdd6f4; border: 1px solid #313244;
                border-radius: 6px; gridline-color: #313244; font-size: 13px;
            }
            QTableView::item:selected { background: #45475a; }
            QTableView::item:alternate { background: #1e1e2e; }
            QHeaderView::section {
                background: #313244; color: #a6adc8; border: none;
                padding: 6px 8px; font-weight: bold; font-size: 12px;
            }
            QProgressBar {
                background: #313244; border: none; border-radius: 4px; height: 6px;
            }
            QProgressBar::chunk { background: #89b4fa; border-radius: 4px; }
            QMessageBox { background: #1e1e2e; }
            QMessageBox QLabel { color: #cdd6f4; }
            QMessageBox QPushButton { min-width: 80px; }
            """
        )

    def _load_apps(self):
        if self.size_thread and self.size_thread.isRunning():
            self.size_thread.stop()
            self.size_thread.wait()
        self._set_busy(True)
        self.count_label.setText("Deep scanning items...")
        log_event("scan_started")
        self.scan_thread = ScanThread()
        self.scan_thread.finished.connect(self._on_scan_done)
        self.scan_thread.start()

    def _on_scan_done(self, apps: list[InstalledApp]):
        self.model.removeRows(0, self.model.rowCount())
        self.apps = apps
        icon_provider = QFileIconProvider()

        self.drive_bar.update_drives(apps)

        drives = sorted({app.drive for app in apps if app.drive})
        self.drive_combo.blockSignals(True)
        current_drive = self.drive_combo.currentText() or "All Drives"
        self.drive_combo.clear()
        self.drive_combo.addItem("All Drives")
        for drive in drives:
            self.drive_combo.addItem(drive)
        drive_index = self.drive_combo.findText(current_drive)
        self.drive_combo.setCurrentIndex(drive_index if drive_index >= 0 else 0)
        self.drive_combo.blockSignals(False)

        source_colors = {
            "Registry": "#a6e3a1",
            "Store": "#89b4fa",
            "Filesystem": "#fab387",
            "Service": "#cba6f7",
            "Startup": "#f9e2af",
            "Steam": "#94e2d5",
            "Epic": "#f5c2e7",
            "GOG": "#eba0ac",
            "Ubisoft": "#74c7ec",
            "EA": "#f38ba8",
        }
        type_colors = {
            "Installed Program": "#a6e3a1",
            "Store App": "#89b4fa",
            "Startup Item": "#f9e2af",
            "Service": "#cba6f7",
            "Orphaned Folder": "#fab387",
        }

        for app in apps:
            row = [
                QStandardItem(app.name),
                QStandardItem(app.item_type),
                QStandardItem(app.publisher),
                QStandardItem(app.version),
                QStandardItem(app.size),
                QStandardItem(app.source),
                QStandardItem(app.drive),
                QStandardItem(app.install_date),
            ]

            icon_file = _resolve_icon_path(app.icon_path)
            if not icon_file:
                icon_file = _find_exe_icon(app.install_path)
            if icon_file:
                icon = icon_provider.icon(QFileInfo(icon_file))
                if not icon.isNull():
                    row[0].setIcon(icon)

            row[1].setForeground(QColor(type_colors.get(app.item_type, "#cdd6f4")))
            row[5].setForeground(QColor(source_colors.get(app.source, "#cdd6f4")))
            for item in row:
                item.setData(app, Qt.ItemDataRole.UserRole)
            self.model.appendRow(row)

        counts = Counter(app.item_type for app in apps)
        log_event("scan_completed", total=len(apps), counts=counts)

        self._set_busy(False)
        self._on_filter_changed()

        self.size_thread = SizeScanThread(self.apps)
        self.size_thread.size_ready.connect(self._on_size_ready)
        self.size_thread.start()

    def _on_size_ready(self, row_index: int, size_str: str):
        item = self.model.item(row_index, 4)
        if item:
            item.setText(size_str)

    def _on_filter_changed(self, _=None):
        self.proxy.set_search_text(self.search_input.text())
        self.proxy.set_type_filter(self.type_combo.currentText())
        self.proxy.set_drive_filter(self.drive_combo.currentText())

        visible = self.proxy.rowCount()
        total = len(self.apps)
        filters_active = bool(self.search_input.text().strip()) or self.type_combo.currentText() != "All Types" or self.drive_combo.currentText() != "All Drives"
        if filters_active:
            self.count_label.setText(f"Showing {visible} of {total} items")
        else:
            self.count_label.setText(f"{total} items found")

    def _get_selected_apps(self, show_message: bool = True) -> list[InstalledApp]:
        indexes = self.table.selectionModel().selectedRows()
        apps = []
        for index in indexes:
            source_index = self.proxy.mapToSource(index)
            item = self.model.item(source_index.row(), 0)
            app = item.data(Qt.ItemDataRole.UserRole) if item else None
            if app:
                apps.append(app)
        if not apps and show_message:
            QMessageBox.information(self, "No Selection", "Please select one or more items first.")
        return apps

    def _build_preview_html(self, apps: list[InstalledApp]) -> str:
        blocks = []
        for app in apps:
            plan = build_action_plan(app, self.is_admin)
            lines = [
                f"<b>{html.escape(app.name)}</b> <span style='color:#a6adc8'>({html.escape(app.item_type)})</span>",
                f"Action: <b>{html.escape(plan.action_label)}</b>",
            ]

            for detail in plan.details:
                lines.append(html.escape(detail))

            if plan.command_text:
                lines.append(f"Command:<br><code>{html.escape(plan.command_text)}</code>")
            if plan.protocol_uri:
                lines.append(f"Launcher URI:<br><code>{html.escape(plan.protocol_uri)}</code>")
            if plan.delete_path:
                lines.append(f"Folder:<br><code>{html.escape(plan.delete_path)}</code>")
            if plan.registry_key_to_remove:
                lines.append(f"Registry cleanup after success:<br><code>{html.escape(plan.registry_key_to_remove)}</code>")

            leftovers = find_possible_leftover_folders(app)
            if leftovers:
                paths = "".join(f"<br><code>{html.escape(path)}</code>" for path in leftovers)
                lines.append(f"Possible leftover folders:{paths}")

            if plan.requires_admin and not self.is_admin:
                lines.append("<b>Status:</b> Requires administrator rights.")
            elif not plan.can_execute:
                lines.append("<b>Status:</b> Blocked until the safety checks pass.")

            if plan.warnings:
                warnings = "".join(f"<br>• {html.escape(warning)}" for warning in plan.warnings)
                lines.append(f"<b>Warnings:</b>{warnings}")

            blocks.append("<br>".join(lines))

        return "<br><br>".join(blocks)

    def _build_confirmation_html(self, apps: list[InstalledApp]) -> str:
        plans = [build_action_plan(app, self.is_admin) for app in apps]
        if len(apps) == 1:
            header = f"Run <b>{html.escape(plans[0].action_label)}</b> for <b>{html.escape(apps[0].name)}</b>?"
        else:
            header = f"Run <b>{len(apps)}</b> selected actions?"

        if any(plan.delete_path for plan in plans):
            header += "<br><br><span style='color:#f38ba8'><b>Folder deletion is permanent.</b></span>"
        if any(plan.pending for plan in plans):
            header += "<br><span style='color:#f9e2af'>Launcher actions only open the external uninstall flow.</span>"

        return f"{header}<br><br>{self._build_preview_html(apps)}"

    def _update_action_controls(self, *_args):
        busy = self.progress.isVisible()
        selected = self._get_selected_apps(show_message=False)

        self.preview_btn.setEnabled(bool(selected) and not busy)
        self.preview_btn.setToolTip("Show planned commands and cleanup without making changes.")

        if not selected:
            self.action_btn.setText("Run Selected Action")
            self.action_btn.setEnabled(False)
            self.action_btn.setToolTip("")
            return

        plans = [build_action_plan(app, self.is_admin) for app in selected]
        self.action_btn.setText(plans[0].action_label if len(plans) == 1 else "Run Selected Actions")

        blocking_messages = []
        for app, plan in zip(selected, plans):
            if not plan.can_execute:
                detail = "; ".join(plan.warnings[:2]) or "This item cannot be acted on safely."
                blocking_messages.append(f"{app.name}: {detail}")
            elif plan.requires_admin and not self.is_admin:
                blocking_messages.append(f"{app.name}: requires administrator rights.")

        self.action_btn.setEnabled(not busy and not blocking_messages)
        self.action_btn.setToolTip("\n".join(blocking_messages))

    def _on_preview_selected(self):
        apps = self._get_selected_apps()
        if not apps:
            return

        box = QMessageBox(self)
        box.setWindowTitle("Action Preview")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(self._build_preview_html(apps))
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()

    def _on_execute_selected(self):
        apps = self._get_selected_apps()
        if not apps:
            return

        plans = [build_action_plan(app, self.is_admin) for app in apps]
        blocked = []
        for app, plan in zip(apps, plans):
            if not plan.can_execute:
                blocked.append(f"{app.name}: " + "; ".join(plan.warnings[:2]))
            elif plan.requires_admin and not self.is_admin:
                blocked.append(f"{app.name}: requires administrator rights.")

        if blocked:
            QMessageBox.warning(self, "Action Blocked", "The selected items cannot be processed safely:\n\n" + "\n".join(blocked))
            return

        reply = QMessageBox.question(
            self,
            "Confirm Action",
            self._build_confirmation_html(apps),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        log_event(
            "selection_confirmed",
            count=len(apps),
            items=[app.name for app in apps],
            actions=[plan.action_label for plan in plans],
        )

        self._set_busy(True)
        if len(apps) == 1:
            self.action_thread = ActionThread(apps[0], self.is_admin)
            self.action_thread.finished.connect(self._on_action_done)
            self.action_thread.start()
        else:
            self.action_thread = MultiActionThread(apps, self.is_admin)
            self.action_thread.progress.connect(self._on_action_progress)
            self.action_thread.finished.connect(self._on_multi_action_done)
            self.action_thread.start()

    def _on_action_progress(self, text: str):
        self.count_label.setText(text)

    def _on_action_done(self, status: str, message: str):
        self._set_busy(False)
        if status == "success":
            QMessageBox.information(self, "Done", message)
            self._load_apps()
        elif status == "pending":
            QMessageBox.information(self, "Action Started", message)
            self._on_filter_changed()
        else:
            QMessageBox.warning(self, "Action Issue", message)

    def _on_multi_action_done(self, statuses: list[str], messages: list[str]):
        self._set_busy(False)
        success_count = sum(1 for status in statuses if status == "success")
        pending_count = sum(1 for status in statuses if status == "pending")
        failed_count = sum(1 for status in statuses if status == "failed")
        full_message = "\n\n".join(messages)

        if failed_count:
            QMessageBox.warning(
                self,
                "Actions Complete (with issues)",
                f"Successful: {success_count}\nPending: {pending_count}\nFailed: {failed_count}\n\n{full_message}",
            )
        elif pending_count:
            QMessageBox.information(
                self,
                "Actions Started",
                f"Successful: {success_count}\nPending: {pending_count}\n\n{full_message}",
            )
        else:
            QMessageBox.information(
                self,
                "Done",
                f"Successful: {success_count}\n\n{full_message}",
            )

        if success_count:
            self._load_apps()
        else:
            self._on_filter_changed()

    def _set_busy(self, busy: bool):
        self.progress.setVisible(busy)
        self.refresh_btn.setEnabled(not busy)
        self.search_input.setEnabled(not busy)
        self.type_combo.setEnabled(not busy)
        self.drive_combo.setEnabled(not busy)
        self.table.setEnabled(not busy)
        if busy:
            self.preview_btn.setEnabled(False)
            self.action_btn.setEnabled(False)
        else:
            self._update_action_controls()


def main():
    app = QApplication(sys.argv)
    app.setStyle(QStyleFactory.create("Fusion"))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    if not ctypes.windll.shell32.IsUserAnAdmin():
        # Re-launch with UAC elevation prompt
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, " ".join(f'"{a}"' for a in sys.argv), None, 1
        )
        sys.exit(0)
    main()
