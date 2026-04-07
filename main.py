import sys
import os
import json
import subprocess
import winreg
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import Qt, QSortFilterProxyModel, QThread, Signal
from PySide6.QtGui import QStandardItemModel, QStandardItem, QIcon, QFont, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QTableView, QPushButton, QHeaderView, QMessageBox,
    QLabel, QProgressBar, QAbstractItemView, QStyleFactory, QComboBox,
)

REGISTRY_PATHS = [
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
]

COLUMNS = ["Name", "Publisher", "Version", "Size", "Source", "Drive", "Install Date"]

# ── Game platform detection ──
STEAM_LIBRARY_VDF = r"libraryfolders.vdf"
GAME_PLATFORM_REGISTRY = [
    # Steam
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
    # GOG Galaxy
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\GOG.com\GalaxyClient\paths", "client"),
    # Epic Games
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Epic Games\EpicGamesLauncher", "AppDataPath"),
]

# Directories to scan for filesystem-only installs
PROGRAM_DIRS = [
    os.environ.get("ProgramFiles", r"C:\Program Files"),
    os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
    os.path.join(os.environ.get("APPDATA", ""), ""),
]

# Known non-app folders to skip when scanning filesystem
FS_SKIP = {
    "common files", "windows nt", "windows kits", "windowsapps",
    "microsoft", "microsoft office", "internet explorer", "windows defender",
    "windows mail", "windows media player", "windows multimedia platform",
    "windows portable devices", "windows photo viewer", "windows sidebar",
    "desktop", "documents", "downloads", "music", "pictures", "videos",
    "contacts", "favorites", "links", "saved games", "searches",
    "appdata", "local", "roaming", "locallow",
}


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
    registry_key: str = ""  # full path for cleanup, e.g. HKLM\SOFTWARE\...\AppName


def get_drive(path: str) -> str:
    """Extract drive letter from a path, e.g. 'C:'"""
    if not path:
        return ""
    p = os.path.normpath(path)
    if len(p) >= 2 and p[1] == ":":
        return p[:2].upper()
    return ""


def _parse_steam_libraries() -> list[str]:
    """Find all Steam library folders from libraryfolders.vdf."""
    steam_path = ""
    for hive, key_path, val_name in GAME_PLATFORM_REGISTRY:
        if "Steam" not in key_path:
            continue
        try:
            k = winreg.OpenKey(hive, key_path)
            steam_path = winreg.QueryValueEx(k, val_name)[0]
            winreg.CloseKey(k)
            if steam_path:
                break
        except OSError:
            continue
    if not steam_path:
        return []
    vdf = os.path.join(steam_path, "steamapps", STEAM_LIBRARY_VDF)
    libs = [os.path.join(steam_path, "steamapps")]
    if os.path.isfile(vdf):
        try:
            with open(vdf, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if '"path"' in line:
                        parts = line.split('"')
                        if len(parts) >= 4:
                            libs.append(os.path.join(parts[3], "steamapps"))
        except OSError:
            pass
    return libs


def _scan_steam_games(seen_names: set, seen_paths: set, lock: threading.Lock) -> list[InstalledApp]:
    apps = []
    for lib_dir in _parse_steam_libraries():
        common = os.path.join(lib_dir, "common")
        if not os.path.isdir(common):
            continue
        manifests = {}
        try:
            for f in os.scandir(lib_dir):
                if f.name.startswith("appmanifest_") and f.name.endswith(".acf"):
                    data = {}
                    with open(f.path, "r", encoding="utf-8", errors="ignore") as fh:
                        for line in fh:
                            line = line.strip()
                            parts = line.split('"')
                            if len(parts) >= 4:
                                data[parts[1].lower()] = parts[3]
                    if data.get("installdir"):
                        manifests[data["installdir"].lower()] = data
        except OSError:
            pass
        try:
            for entry in os.scandir(common):
                if not entry.is_dir():
                    continue
                norm = os.path.normpath(entry.path).lower()
                with lock:
                    if norm in seen_paths or entry.name in seen_names:
                        continue
                    seen_names.add(entry.name)
                    seen_paths.add(norm)
                # Try exact match first, then search all manifests
                manifest = manifests.get(entry.name.lower())
                if not manifest:
                    # Fuzzy: find any manifest whose installdir matches this folder
                    for m in manifests.values():
                        m_dir = m.get("installdir", "")
                        if m_dir and os.path.normpath(os.path.join(common, m_dir)).lower() == norm:
                            manifest = m
                            break
                if not manifest:
                    manifest = {}
                size_str = format_size(int(manifest.get("sizeondisk", 0)) // 1024) if manifest.get("sizeondisk") else ""
                app_id = manifest.get("appid", "")
                apps.append(InstalledApp(
                    name=entry.name,
                    publisher="Steam",
                    version=manifest.get("buildid", ""),
                    size=size_str,
                    install_date="",
                    uninstall_string=f'steam://uninstall/{app_id}' if app_id else "",
                    quiet_uninstall_string="",
                    source="Steam",
                    install_path=entry.path,
                    drive=get_drive(entry.path),
                ))
        except (PermissionError, OSError):
            continue
    return apps


def _scan_epic_games(seen_names: set, seen_paths: set, lock: threading.Lock) -> list[InstalledApp]:
    apps = []
    manifests_dir = os.path.join(
        os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
        "Epic", "EpicGamesLauncher", "Data", "Manifests"
    )
    if not os.path.isdir(manifests_dir):
        return apps
    try:
        for f in os.scandir(manifests_dir):
            if not f.name.endswith(".item"):
                continue
            try:
                with open(f.path, "r", encoding="utf-8", errors="ignore") as fh:
                    data = json.load(fh)
            except (json.JSONDecodeError, OSError):
                continue
            name = data.get("DisplayName", "")
            install_loc = data.get("InstallLocation", "")
            if not name:
                continue
            norm = os.path.normpath(install_loc).lower() if install_loc else ""
            with lock:
                if name in seen_names or (norm and norm in seen_paths):
                    continue
                seen_names.add(name)
                if norm:
                    seen_paths.add(norm)
            # Use InstallSize from manifest (bytes) if available
            install_size = data.get("InstallSize", 0)
            size_str = format_size(int(install_size) // 1024) if install_size else ""
            app_name = data.get("AppName", "")
            apps.append(InstalledApp(
                name=name,
                publisher="Epic Games",
                version=data.get("AppVersionString", ""),
                size=size_str,
                install_date="",
                uninstall_string=f'com.epicgames.launcher://apps/{app_name}?action=uninstall' if app_name else "",
                quiet_uninstall_string="",
                source="Epic",
                install_path=install_loc,
                drive=get_drive(install_loc),
            ))
    except (PermissionError, OSError):
        pass
    return apps


def _scan_gog_games(seen_names: set, seen_paths: set, lock: threading.Lock) -> list[InstalledApp]:
    apps = []
    try:
        reg_key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\GOG.com\Games")
    except OSError:
        return apps
    try:
        i = 0
        while True:
            try:
                subkey_name = winreg.EnumKey(reg_key, i)
                i += 1
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
            if not name:
                continue
            norm = os.path.normpath(path).lower() if path else ""
            with lock:
                if name in seen_names or (norm and norm in seen_paths):
                    continue
                seen_names.add(name)
                if norm:
                    seen_paths.add(norm)
            apps.append(InstalledApp(
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
            ))
    finally:
        winreg.CloseKey(reg_key)
    return apps


def read_registry_value(key, value_name):
    try:
        return winreg.QueryValueEx(key, value_name)[0]
    except OSError:
        return ""


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


def _scan_powershell(seen_names: set, seen_paths: set, lock: threading.Lock) -> list[InstalledApp]:
    """Run Store + Services scan in a single PowerShell process."""
    apps = []
    ps_script = """
$store = Get-AppxPackage | Select-Object Name, Publisher, Version, InstallLocation, PackageFamilyName | ConvertTo-Json -Compress
$svc = Get-CimInstance Win32_Service | Where-Object { $_.PathName -and $_.PathName -notmatch 'Windows|System32|svchost|Microsoft' } | Select-Object Name, DisplayName, PathName | ConvertTo-Json -Compress
'{"store":' + $store + ',"services":' + $svc + '}'
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return apps
        blob = json.loads(result.stdout)
    except Exception:
        return apps

    # ── Store apps ──
    store_apps = blob.get("store", [])
    if isinstance(store_apps, dict):
        store_apps = [store_apps]
    skip_keywords = {
        "framework", ".net", "vclibs", "microsoft.ui", "microsoft.services",
        "microsoft.windows", "inputapp", "microsoft.desktopappinstaller",
        "microsoft.screensketch", "microsoft.webp", "microsoft.heif",
        "microsoft.vp9", "microsoft.hevc", "microsoft.webmedia",
        "microsoft.raw", "microsoft.av1",
    }
    for sa in store_apps:
        raw_name = sa.get("Name", "")
        if not raw_name:
            continue
        raw_lower = raw_name.lower()
        if any(s in raw_lower for s in skip_keywords):
            continue
        display_name = raw_name.split(".")[-1] if "." in raw_name else raw_name
        pretty = ""
        for ch in display_name:
            if ch.isupper() and pretty and pretty[-1] != " ":
                pretty += " "
            pretty += ch
        install_loc = sa.get("InstallLocation", "")
        with lock:
            if pretty in seen_names:
                continue
            seen_names.add(pretty)
            if install_loc:
                seen_paths.add(os.path.normpath(install_loc).lower())
        publisher_raw = sa.get("Publisher", "")
        if publisher_raw.startswith("CN="):
            publisher_raw = publisher_raw[3:].split(",")[0]
        apps.append(InstalledApp(
            name=pretty,
            publisher=publisher_raw,
            version=sa.get("Version", ""),
            size="",
            install_date="",
            uninstall_string=f'powershell -Command "Get-AppxPackage *{raw_name}* | Remove-AppxPackage"'
                if raw_name else "",
            quiet_uninstall_string="",
            source="Store",
            install_path=install_loc,
            drive=get_drive(install_loc),
        ))

    # ── Services ──
    services = blob.get("services", [])
    if isinstance(services, dict):
        services = [services]
    for svc in services:
        display = svc.get("DisplayName", "")
        svc_path = svc.get("PathName", "").strip('"').strip()
        if not display or not svc_path:
            continue
        with lock:
            if display in seen_names:
                continue
            seen_names.add(display)
        svc_name = svc.get("Name", "")
        apps.append(InstalledApp(
            name=display,
            publisher="",
            version="",
            size="",
            install_date="",
            uninstall_string=f'sc delete "{svc_name}"' if svc_name else "",
            quiet_uninstall_string="",
            source="Service",
            install_path=svc_path,
            drive=get_drive(svc_path),
        ))

    return apps


def _scan_filesystem(seen_names: set, seen_paths: set, lock: threading.Lock) -> list[InstalledApp]:
    """Scan Program Files etc. for orphaned installs (no rglob for size)."""
    apps = []
    for base_dir in PROGRAM_DIRS:
        if not base_dir or not os.path.isdir(base_dir):
            continue
        try:
            for entry in os.scandir(base_dir):
                if not entry.is_dir():
                    continue
                folder_lower = entry.name.lower()
                if folder_lower in FS_SKIP or folder_lower.startswith("."):
                    continue
                norm_path = os.path.normpath(entry.path).lower()
                with lock:
                    if norm_path in seen_paths or entry.name in seen_names:
                        continue
                # Check for .exe (shallow — top-level only)
                has_exe = False
                try:
                    for sub in os.scandir(entry.path):
                        if sub.is_file() and sub.name.lower().endswith(".exe"):
                            has_exe = True
                            break
                except (PermissionError, OSError):
                    continue
                if not has_exe:
                    continue
                with lock:
                    if entry.name in seen_names:
                        continue
                    seen_names.add(entry.name)
                    seen_paths.add(norm_path)
                apps.append(InstalledApp(
                    name=entry.name,
                    publisher="",
                    version="",
                    size="",
                    install_date="",
                    uninstall_string="",
                    quiet_uninstall_string="",
                    source="Filesystem",
                    install_path=entry.path,
                    drive=get_drive(entry.path),
                ))
        except (PermissionError, OSError):
            continue
    return apps


def _scan_registry(seen_names: set, seen_paths: set) -> list[InstalledApp]:
    """Registry uninstall scan — fast, no threading needed."""
    apps = []
    for hive, path in REGISTRY_PATHS:
        try:
            reg_key = winreg.OpenKey(hive, path)
        except OSError:
            continue
        try:
            i = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(reg_key, i)
                    i += 1
                except OSError:
                    break
                try:
                    subkey = winreg.OpenKey(reg_key, subkey_name)
                except OSError:
                    continue
                name = read_registry_value(subkey, "DisplayName")
                if not name or name in seen_names:
                    winreg.CloseKey(subkey)
                    continue
                system_component = read_registry_value(subkey, "SystemComponent")
                if system_component == 1:
                    winreg.CloseKey(subkey)
                    continue
                install_location = read_registry_value(subkey, "InstallLocation") or ""
                if install_location:
                    seen_paths.add(os.path.normpath(install_location).lower())
                hive_name = "HKLM" if hive == winreg.HKEY_LOCAL_MACHINE else "HKCU"
                reg_key_path = f"{hive_name}\\{path}\\{subkey_name}"

                uninstall_str = read_registry_value(subkey, "UninstallString") or ""
                quiet_uninstall_str = read_registry_value(subkey, "QuietUninstallString") or ""
                publisher = read_registry_value(subkey, "Publisher") or ""

                # Detect game platform from uninstall string or publisher
                uninstall_lower = uninstall_str.lower()
                source = "Registry"
                if "steam" in uninstall_lower or "steam://uninstall" in uninstall_lower:
                    source = "Steam"
                    # Ensure it uses the steam:// protocol
                    if "steam://uninstall" not in uninstall_str:
                        # Try to extract app ID from subkey name (Steam_appXXXXXX)
                        if subkey_name.startswith("Steam App "):
                            app_id = subkey_name.replace("Steam App ", "")
                            uninstall_str = f"steam://uninstall/{app_id}"
                elif "epicgames" in uninstall_lower or "epic games" in publisher.lower():
                    source = "Epic"
                elif "gog" in uninstall_lower or "gog.com" in publisher.lower():
                    source = "GOG"
                elif "ubisoft" in uninstall_lower or "ubisoft" in publisher.lower():
                    source = "Ubisoft"
                elif "ea app" in uninstall_lower or "origin" in uninstall_lower or \
                        "electronic arts" in publisher.lower():
                    source = "EA"

                seen_names.add(name)
                apps.append(InstalledApp(
                    name=name,
                    publisher=publisher,
                    version=read_registry_value(subkey, "DisplayVersion") or "",
                    size=format_size(read_registry_value(subkey, "EstimatedSize")),
                    install_date=format_date(read_registry_value(subkey, "InstallDate")),
                    uninstall_string=uninstall_str,
                    quiet_uninstall_string=quiet_uninstall_str,
                    source=source,
                    install_path=install_location,
                    drive=get_drive(install_location),
                    registry_key=reg_key_path,
                ))
                winreg.CloseKey(subkey)
        finally:
            winreg.CloseKey(reg_key)
    return apps


def _scan_startup(seen_names: set, lock: threading.Lock) -> list[InstalledApp]:
    apps = []
    startup_reg_paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
    ]
    for hive, path in startup_reg_paths:
        try:
            reg_key = winreg.OpenKey(hive, path)
            i = 0
            while True:
                try:
                    val_name, val_data, _ = winreg.EnumValue(reg_key, i)
                    i += 1
                except OSError:
                    break
                if not val_name:
                    continue
                with lock:
                    if val_name in seen_names:
                        continue
                    seen_names.add(val_name)
                apps.append(InstalledApp(
                    name=f"{val_name} (Startup)",
                    publisher="",
                    version="",
                    size="",
                    install_date="",
                    uninstall_string="",
                    quiet_uninstall_string="",
                    source="Startup",
                    install_path=str(val_data),
                    drive=get_drive(str(val_data)),
                ))
            winreg.CloseKey(reg_key)
        except OSError:
            pass
    return apps


def _calc_folder_size(path: str) -> int:
    """Fast folder size using os.scandir recursion (no Path.rglob overhead)."""
    total = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
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
    """Parallel deep scan — registry first, then everything else concurrently."""
    seen_names: set[str] = set()
    seen_paths: set[str] = set()
    lock = threading.Lock()

    # Phase 1: Registry scan (fast, populates seen_names/seen_paths for dedup)
    apps = _scan_registry(seen_names, seen_paths)

    # Phase 2: Everything else in parallel
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [
            pool.submit(_scan_powershell, seen_names, seen_paths, lock),
            pool.submit(_scan_steam_games, seen_names, seen_paths, lock),
            pool.submit(_scan_epic_games, seen_names, seen_paths, lock),
            pool.submit(_scan_gog_games, seen_names, seen_paths, lock),
            pool.submit(_scan_filesystem, seen_names, seen_paths, lock),
            pool.submit(_scan_startup, seen_names, lock),
        ]
        for fut in as_completed(futures):
            try:
                apps.extend(fut.result())
            except Exception:
                pass

    apps.sort(key=lambda a: a.name.lower())
    return apps


class SizeScanThread(QThread):
    """Background thread that calculates folder sizes after the main scan."""
    size_ready = Signal(int, str)  # row index, size string

    def __init__(self, apps: list[InstalledApp]):
        super().__init__()
        self.apps = apps
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        for i, app in enumerate(self.apps):
            if self._stop:
                return
            if app.size or not app.install_path:
                continue
            if not os.path.isdir(app.install_path):
                continue
            total = _calc_folder_size(app.install_path)
            if total and not self._stop:
                app.size = format_size(total // 1024)
                self.size_ready.emit(i, app.size)


class ScanThread(QThread):
    finished = Signal(list)

    def run(self):
        self.finished.emit(get_installed_apps())


class UninstallThread(QThread):
    finished = Signal(bool, str)

    def __init__(self, app: InstalledApp):
        super().__init__()
        self.app = app

    def run(self):
        app = self.app
        messages = []
        success = True
        try:
            if not (app.uninstall_string or app.quiet_uninstall_string) and app.install_path:
                # No uninstaller — delete the folder directly
                shutil.rmtree(app.install_path, ignore_errors=True)
                if os.path.exists(app.install_path):
                    success = False
                    messages.append("Could not fully remove the folder (some files may be in use).")
                else:
                    messages.append(f"Deleted folder: {app.install_path}")
            elif app.source == "Filesystem" and app.install_path:
                shutil.rmtree(app.install_path, ignore_errors=True)
                if os.path.exists(app.install_path):
                    success = False
                    messages.append("Could not fully remove the folder (some files may be in use).")
                else:
                    messages.append(f"Deleted folder: {app.install_path}")
            else:
                cmd = app.uninstall_string or app.quiet_uninstall_string
                if not cmd:
                    self.finished.emit(False, "No uninstall command available.")
                    return

                # Extract protocol URI from commands like:
                #   "C:\...\steam.exe" steam://uninstall/12345
                protocol_uri = None
                if "steam://uninstall/" in cmd:
                    idx = cmd.find("steam://uninstall/")
                    protocol_uri = cmd[idx:].strip().strip('"')
                elif "com.epicgames.launcher://" in cmd:
                    idx = cmd.find("com.epicgames.launcher://")
                    protocol_uri = cmd[idx:].strip().strip('"')
                elif "goggalaxy://" in cmd:
                    idx = cmd.find("goggalaxy://")
                    protocol_uri = cmd[idx:].strip().strip('"')

                if protocol_uri:
                    os.startfile(protocol_uri)
                    messages.append(
                        f"Launched {app.source} uninstaller for {app.name}.\n"
                        "Complete the uninstall in the platform client."
                    )
                else:
                    result = subprocess.run(cmd, shell=True, timeout=600)
                    if result.returncode == 0:
                        messages.append("Uninstall completed successfully.")
                    else:
                        success = False
                        messages.append(f"Uninstaller exited with code {result.returncode}.")

            # ── Registry cleanup ──
            cleanup = self._cleanup_registry(app)
            if cleanup:
                messages.append(cleanup)

            self.finished.emit(success, "\n".join(messages))
        except subprocess.TimeoutExpired:
            self.finished.emit(False, "Uninstall timed out after 10 minutes.")
        except Exception as e:
            self.finished.emit(False, str(e))

    @staticmethod
    def _cleanup_registry(app: InstalledApp) -> str:
        """Remove leftover registry entries after uninstall."""
        cleaned = []

        # 1. Remove the app's own Uninstall registry key
        if app.registry_key:
            try:
                if app.registry_key.startswith("HKLM\\"):
                    hive = winreg.HKEY_LOCAL_MACHINE
                    sub_path = app.registry_key[5:]
                elif app.registry_key.startswith("HKCU\\"):
                    hive = winreg.HKEY_CURRENT_USER
                    sub_path = app.registry_key[5:]
                else:
                    hive = None
                    sub_path = None
                if hive is not None and sub_path:
                    winreg.DeleteKey(hive, sub_path)
                    cleaned.append("Uninstall registry key")
            except OSError:
                pass

        # 2. Clean up App Paths
        app_paths_locations = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
        ]
        for hive, base in app_paths_locations:
            try:
                reg_key = winreg.OpenKey(hive, base)
                i = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(reg_key, i)
                        i += 1
                    except OSError:
                        break
                    try:
                        subkey = winreg.OpenKey(reg_key, subkey_name)
                        path_val = read_registry_value(subkey, "") or read_registry_value(subkey, "Path")
                        winreg.CloseKey(subkey)
                        if path_val and app.install_path and \
                                os.path.normpath(app.install_path).lower() in os.path.normpath(str(path_val)).lower():
                            winreg.DeleteKey(reg_key, subkey_name)
                            cleaned.append(f"App Paths: {subkey_name}")
                    except OSError:
                        pass
                winreg.CloseKey(reg_key)
            except OSError:
                pass

        # 3. Clean up SharedDLLs references
        if app.install_path:
            try:
                dll_key = winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\SharedDLLs",
                    0, winreg.KEY_ALL_ACCESS,
                )
                norm_install = os.path.normpath(app.install_path).lower()
                to_delete = []
                i = 0
                while True:
                    try:
                        val_name, val_data, _ = winreg.EnumValue(dll_key, i)
                        i += 1
                        if norm_install in os.path.normpath(val_name).lower():
                            to_delete.append(val_name)
                    except OSError:
                        break
                for vn in to_delete:
                    try:
                        winreg.DeleteValue(dll_key, vn)
                    except OSError:
                        pass
                winreg.CloseKey(dll_key)
                if to_delete:
                    cleaned.append(f"SharedDLLs: {len(to_delete)} entries")
            except OSError:
                pass

        # 4. Clean startup entries referencing this app
        if app.install_path:
            startup_locs = [
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
            ]
            norm_install = os.path.normpath(app.install_path).lower()
            for hive, path in startup_locs:
                try:
                    reg_key = winreg.OpenKey(hive, path, 0, winreg.KEY_ALL_ACCESS)
                    to_delete = []
                    i = 0
                    while True:
                        try:
                            val_name, val_data, _ = winreg.EnumValue(reg_key, i)
                            i += 1
                            if norm_install in os.path.normpath(str(val_data)).lower():
                                to_delete.append(val_name)
                        except OSError:
                            break
                    for vn in to_delete:
                        try:
                            winreg.DeleteValue(reg_key, vn)
                        except OSError:
                            pass
                    winreg.CloseKey(reg_key)
                    if to_delete:
                        cleaned.append(f"Startup: {', '.join(to_delete)}")
                except OSError:
                    pass

        if cleaned:
            return "Registry cleaned: " + "; ".join(cleaned)
        return ""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Utopia Uninstaller")
        self.setMinimumSize(900, 600)
        self.resize(1050, 700)
        self.apps: list[InstalledApp] = []
        self.uninstall_thread = None
        self.scan_thread = None
        self.size_thread = None

        self._build_ui()
        self._apply_style()
        self._load_apps()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # Header
        header = QLabel("Utopia Uninstaller")
        header.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        layout.addWidget(header)

        # Search bar + drive filter + refresh
        search_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search installed programs...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._on_filter_changed)
        search_row.addWidget(self.search_input)

        self.drive_combo = QComboBox()
        self.drive_combo.setFixedWidth(100)
        self.drive_combo.addItem("All Drives")
        self.drive_combo.currentTextChanged.connect(self._on_filter_changed)
        search_row.addWidget(self.drive_combo)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setFixedWidth(90)
        self.refresh_btn.clicked.connect(self._load_apps)
        search_row.addWidget(self.refresh_btn)
        layout.addLayout(search_row)

        # App count
        self.count_label = QLabel()
        layout.addWidget(self.count_label)

        # Table
        self.model = QStandardItemModel()
        self.model.setHorizontalHeaderLabels(COLUMNS)

        self.proxy = QSortFilterProxyModel()
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy.setFilterKeyColumn(-1)  # search all columns

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, len(COLUMNS)):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setColumnWidth(4, 80)  # Source column
        self.table.setColumnWidth(5, 50)  # Drive column
        self.table.doubleClicked.connect(self._on_uninstall)
        layout.addWidget(self.table)

        # Bottom bar
        bottom = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setRange(0, 0)  # indeterminate
        bottom.addWidget(self.progress)

        self.uninstall_btn = QPushButton("Uninstall Selected")
        self.uninstall_btn.setFixedWidth(160)
        self.uninstall_btn.clicked.connect(self._on_uninstall)
        bottom.addWidget(self.uninstall_btn)
        layout.addLayout(bottom)

    def _apply_style(self):
        self.setStyleSheet("""
            QMainWindow { background: #1e1e2e; }
            QLabel { color: #cdd6f4; }
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
        """)

    def _load_apps(self):
        if self.size_thread and self.size_thread.isRunning():
            self.size_thread.stop()
            self.size_thread.wait()
        self._set_busy(True)
        self.count_label.setText("Deep scanning...")
        self.scan_thread = ScanThread()
        self.scan_thread.finished.connect(self._on_scan_done)
        self.scan_thread.start()

    def _on_scan_done(self, apps: list[InstalledApp]):
        self.model.removeRows(0, self.model.rowCount())
        self.apps = apps

        # Collect drives for dropdown
        drives = set()
        for app in self.apps:
            if app.drive:
                drives.add(app.drive)

        self.drive_combo.blockSignals(True)
        current = self.drive_combo.currentText()
        self.drive_combo.clear()
        self.drive_combo.addItem("All Drives")
        for d in sorted(drives):
            self.drive_combo.addItem(d)
        idx = self.drive_combo.findText(current)
        self.drive_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.drive_combo.blockSignals(False)

        for app in self.apps:
            row = [
                QStandardItem(app.name),
                QStandardItem(app.publisher),
                QStandardItem(app.version),
                QStandardItem(app.size),
                QStandardItem(app.source),
                QStandardItem(app.drive),
                QStandardItem(app.install_date),
            ]
            # Color-code the source tag
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
            row[4].setForeground(QColor(source_colors.get(app.source, "#cdd6f4")))
            for item in row:
                item.setData(app, Qt.ItemDataRole.UserRole)
            self.model.appendRow(row)

        self.count_label.setText(f"{len(self.apps)} programs found (deep scan)")
        self._set_busy(False)
        self._on_filter_changed()

        # Start background size calculation for apps missing size
        self.size_thread = SizeScanThread(self.apps)
        self.size_thread.size_ready.connect(self._on_size_ready)
        self.size_thread.start()

    def _on_size_ready(self, row_idx: int, size_str: str):
        """Update the size cell as sizes are calculated in background."""
        item = self.model.item(row_idx, 3)  # Size column
        if item:
            item.setText(size_str)

    def _on_filter_changed(self, _=None):
        search_text = self.search_input.text()
        drive_filter = self.drive_combo.currentText()

        self.proxy.setFilterFixedString(search_text)

        # Apply drive filter by hiding rows that don't match
        for row in range(self.proxy.rowCount()):
            source_idx = self.proxy.mapToSource(self.proxy.index(row, 0))
            item = self.model.item(source_idx.row(), 0)
            app = item.data(Qt.ItemDataRole.UserRole)
            if drive_filter != "All Drives" and app.drive != drive_filter:
                self.table.setRowHidden(row, True)
            else:
                self.table.setRowHidden(row, False)

        visible = sum(1 for r in range(self.proxy.rowCount()) if not self.table.isRowHidden(r))
        total = len(self.apps)
        if search_text or drive_filter != "All Drives":
            self.count_label.setText(f"Showing {visible} of {total} programs")
        else:
            self.count_label.setText(f"{total} programs found (deep scan)")

    def _get_selected_app(self) -> InstalledApp | None:
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            QMessageBox.information(self, "No Selection", "Please select a program to uninstall.")
            return None
        source_index = self.proxy.mapToSource(indexes[0])
        item = self.model.item(source_index.row(), 0)
        return item.data(Qt.ItemDataRole.UserRole)

    def _on_uninstall(self):
        app = self._get_selected_app()
        if not app:
            return

        can_uninstall = app.uninstall_string or app.quiet_uninstall_string
        can_delete = app.install_path and os.path.isdir(app.install_path)

        if not can_uninstall and not can_delete:
            QMessageBox.warning(self, "Cannot Uninstall",
                                f'"{ app.name}" does not have an uninstall command registered.')
            return

        msg = f"Are you sure you want to uninstall <b>{app.name}</b>?"
        if not can_uninstall and can_delete:
            msg += f"<br><br>No uninstaller found. This will <b>delete the folder</b>:<br><code>{app.install_path}</code>"
        elif app.source == "Filesystem":
            msg += f"<br><br>This will <b>delete the folder</b>:<br><code>{app.install_path}</code>"
        elif app.source == "Service":
            msg += "<br><br>This will <b>delete the Windows service</b>. Requires admin."
        elif app.source in ("Steam", "Epic", "GOG", "Ubisoft", "EA"):
            msg += f"<br><br>This will use the <b>{app.source}</b> client to handle the uninstall."

        reply = QMessageBox.question(
            self, "Confirm Uninstall", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._set_busy(True)
        self.uninstall_thread = UninstallThread(app)
        self.uninstall_thread.finished.connect(self._on_uninstall_done)
        self.uninstall_thread.start()

    def _on_uninstall_done(self, success: bool, message: str):
        self._set_busy(False)
        if success:
            QMessageBox.information(self, "Done", message)
            self._load_apps()
        else:
            QMessageBox.warning(self, "Uninstall Issue", message)

    def _set_busy(self, busy: bool):
        self.progress.setVisible(busy)
        self.uninstall_btn.setEnabled(not busy)
        self.refresh_btn.setEnabled(not busy)
        self.table.setEnabled(not busy)


def main():
    app = QApplication(sys.argv)
    app.setStyle(QStyleFactory.create("Fusion"))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
