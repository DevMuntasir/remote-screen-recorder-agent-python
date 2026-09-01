import socketio
import cv2
import numpy as np
import pyautogui
import os
import threading
import base64
import time
import re
import json
import hashlib
import requests
import subprocess
import cloudinary
import cloudinary.uploader
import wave
from datetime import datetime
import sys
from pathlib import PurePath
import ctypes
from ctypes import wintypes

try:
    import sounddevice as sd
    sounddevice_import_error = ""
except Exception as error:
    sd = None
    sounddevice_import_error = str(error)

ENV_OVERRIDE_KEYS = {
    "SERVER_URL",
    "CLOUDINARY_CLOUD_NAME",
    "CLOUDINARY_API_KEY",
    "CLOUDINARY_API_SECRET",
    "CLOUDINARY_URL",
    "AGENT_NAME",
    "AGENT_VERSION",
    "AUTO_UPDATE_ENABLED",
    "UPDATE_MANIFEST_URL",
    "UPDATE_CHECK_INTERVAL_SECONDS",
    "UPDATE_DOWNLOAD_RETRY_COUNT",
    "UPDATE_DOWNLOAD_RETRY_DELAY_SECONDS",
    "MIN_UPDATE_BINARY_SIZE_BYTES",
    "AUDIO_SAMPLE_RATE",
    "AUDIO_CHANNELS",
    "AUDIO_BLOCK_FRAMES",
    "IMAGE_SYNC_BATCH_UPLOAD_LIMIT",
    "IMAGE_SCAN_IGNORE_DRIVES",
}

try:
    import winreg
except ImportError:
    winreg = None

BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(__file__)
LOG_FILE = os.path.join(BASE_DIR, "socket_error.txt")
AGENT_VERSION_FILE_NAME = "AGENT_VERSION.txt"
AGENT_VERSION_FILE_PATH = os.path.join(BASE_DIR, AGENT_VERSION_FILE_NAME)
EMBEDDED_VERSION_FILE_NAME = "AGENT_VERSION_BUILD.txt"
RUN_REGISTRY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_REGISTRY_NAME = "RemoteAgent"

def log_error(message):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as file:
            file.write(f"{datetime.now()}: {message}\n")
    except Exception:
        return


def is_admin_process():
    if os.name != "nt":
        return False
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def enable_windows_privilege(privilege_name="SeBackupPrivilege"):
    if os.name != "nt":
        return False
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        TOKEN_ADJUST_PRIVILEGES = 0x0020
        TOKEN_QUERY = 0x0008
        SE_PRIVILEGE_ENABLED = 0x00000002

        class LUID(ctypes.Structure):
            _fields_ = [
                ("LowPart", wintypes.DWORD),
                ("HighPart", wintypes.LONG),
            ]

        class LUID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [
                ("Luid", LUID),
                ("Attributes", wintypes.DWORD),
            ]

        class TOKEN_PRIVILEGES(ctypes.Structure):
            _fields_ = [
                ("PrivilegeCount", wintypes.DWORD),
                ("Privileges", LUID_AND_ATTRIBUTES * 1),
            ]

        h_token = wintypes.HANDLE()
        h_process = kernel32.GetCurrentProcess()
        if not advapi32.OpenProcessToken(h_process, TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(h_token)):
            return False

        try:
            luid = LUID()
            if not advapi32.LookupPrivilegeValueW(None, privilege_name, ctypes.byref(luid)):
                return False

            tp = TOKEN_PRIVILEGES()
            tp.PrivilegeCount = 1
            tp.Privileges[0].Luid = luid
            tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED

            result = advapi32.AdjustTokenPrivileges(h_token, False, ctypes.byref(tp), 0, None, None)
            err = ctypes.get_last_error()
            if not result or err != 0:
                return False
            return True
        finally:
            kernel32.CloseHandle(h_token)
    except Exception as err:
        log_error(f"Failed to enable privilege {privilege_name}: {err}")
        return False


def init_windows_security_privileges():
    if os.name == "nt":
        privileges_to_enable = (
            "SeBackupPrivilege",
            "SeRestorePrivilege",
            "SeSecurityPrivilege",
            "SeTakeOwnershipPrivilege",
        )
        enabled_list = []
        for priv in privileges_to_enable:
            if enable_windows_privilege(priv):
                enabled_list.append(priv)
        if enabled_list:
            log_error(f"Windows security privileges enabled: {', '.join(enabled_list)}")
        else:
            log_error(f"Running in standard user mode (isAdmin={is_admin_process()})")


def load_local_env():
    meipass_dir = getattr(sys, "_MEIPASS", None)
    appdata_dir = os.getenv("APPDATA")
    env_candidates = [
        os.path.join(meipass_dir, ".env") if meipass_dir else None,
        os.path.join(BASE_DIR, ".env"),
        os.path.join(os.path.dirname(BASE_DIR), ".env"),
        os.path.join(appdata_dir, "RemoteAgent", ".env") if appdata_dir else None,
        os.path.join(os.path.expanduser("~"), ".remote-agent.env"),
        os.path.join(os.getcwd(), ".env"),
    ]

    loaded_paths = []

    for env_path in env_candidates:
        if not env_path:
            continue
        if not os.path.exists(env_path):
            continue

        try:
            loaded_any_key = False
            with open(env_path, "r", encoding="utf-8") as file:
                for line in file:
                    raw = line.strip()
                    if not raw or raw.startswith("#") or "=" not in raw:
                        continue
                    key, value = raw.split("=", 1)
                    parsed_key = key.strip()
                    parsed_value = value.strip().strip('"').strip("'")
                    if parsed_key in ENV_OVERRIDE_KEYS:
                        os.environ[parsed_key] = parsed_value
                    else:
                        os.environ.setdefault(parsed_key, parsed_value)
                    loaded_any_key = True

            if loaded_any_key:
                loaded_paths.append(env_path)
        except Exception as error:
            log_error(f"Failed to load .env from {env_path}: {error}")

    if loaded_paths:
        log_error("Loaded environment from: " + " | ".join(loaded_paths))
    else:
        log_error("No .env file found in known locations")


def load_agent_version_from_file():
    version_candidates = [
        AGENT_VERSION_FILE_PATH,
        os.path.join(os.path.dirname(BASE_DIR), AGENT_VERSION_FILE_NAME),
    ]

    for version_path in version_candidates:
        if not os.path.exists(version_path):
            continue

        try:
            with open(version_path, "r", encoding="utf-8") as file:
                version_value = file.read().strip()
                if version_value:
                    return version_value, version_path
        except Exception as error:
            log_error(f"Failed to read agent version file {version_path}: {error}")

    return "", ""


def load_embedded_agent_version():
    meipass_dir = getattr(sys, "_MEIPASS", None)
    version_candidates = [
        os.path.join(meipass_dir, EMBEDDED_VERSION_FILE_NAME) if meipass_dir else None,
        os.path.join(BASE_DIR, EMBEDDED_VERSION_FILE_NAME),
        os.path.join(os.path.dirname(BASE_DIR), EMBEDDED_VERSION_FILE_NAME),
    ]

    for version_path in version_candidates:
        if not version_path:
            continue
        if not os.path.exists(version_path):
            continue

        try:
            with open(version_path, "r", encoding="utf-8") as file:
                version_value = file.read().strip()
                if version_value:
                    return version_value, version_path
        except Exception as error:
            log_error(f"Failed to read embedded agent version file {version_path}: {error}")

    return "", ""


def get_canonical_packaged_exe_path():
    if not getattr(sys, "frozen", False):
        return ""

    current_exe = os.path.abspath(sys.executable)
    current_dir = os.path.dirname(current_exe)
    canonical_exe = os.path.join(current_dir, "RemoteAgent.exe")

    if os.path.basename(current_exe).lower() == "remoteagent.exe":
        return current_exe
    if os.path.exists(canonical_exe):
        return canonical_exe
    return current_exe


def get_autostart_command():
    if getattr(sys, "frozen", False):
        return f'"{get_canonical_packaged_exe_path()}"'

    script_path = os.path.abspath(__file__)
    return f'"{sys.executable}" "{script_path}"'


def ensure_autostart_enabled():
    if os.name != "nt" or winreg is None:
        return

    launch_command = get_autostart_command()

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REGISTRY_PATH, 0, winreg.KEY_READ) as key:
            existing_command, _ = winreg.QueryValueEx(key, RUN_REGISTRY_NAME)
            if existing_command == launch_command:
                return
    except OSError:
        pass

    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_REGISTRY_PATH) as key:
            winreg.SetValueEx(key, RUN_REGISTRY_NAME, 0, winreg.REG_SZ, launch_command)
        log_error(f"Autostart enabled with command: {launch_command}")
    except Exception as error:
        log_error(f"Failed to enable autostart: {error}")


def terminate_stale_agent_instances():
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return

    current_pid = os.getpid()
    image_name = os.path.basename(sys.executable)

    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            [
                "taskkill",
                "/F",
                "/T",
                "/FI",
                f"IMAGENAME eq {image_name}",
                "/FI",
                f"PID ne {current_pid}",
            ],
            capture_output=True,
            text=True,
            creationflags=flags,
        )

        output = " ".join(part.strip() for part in [result.stdout, result.stderr] if part and part.strip())
        if result.returncode == 0:
            log_error(f"Terminated stale {image_name} instances (excluding PID {current_pid})")
        elif output and "No tasks are running" not in output:
            log_error(f"Stale instance cleanup skipped (code={result.returncode}): {output}")
    except Exception as error:
        log_error(f"Failed to cleanup stale agent instances: {error}")


load_local_env()
ensure_autostart_enabled()
init_windows_security_privileges()

# --- CONFIGURATION ---
DEFAULT_SERVER_URL = "https://remote-agent-node.onrender.com"
SERVER_URL = os.getenv("SERVER_URL", DEFAULT_SERVER_URL)
RECORDING_DIR = os.path.join(os.getenv("APPDATA", os.getcwd()), "WinVideoLogs")
IMAGE_SYNC_DIR = os.path.join(os.getenv("APPDATA", BASE_DIR), "RemoteAgent")
IMAGE_SYNC_STATE_FILE = os.path.join(IMAGE_SYNC_DIR, "image_sync_state.json")
FPS = 8.0
RECONNECT_DELAY_SECONDS = 5
IMAGE_SYNC_RETRY_DELAY_SECONDS = 5
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
DIRECTORY_LISTING_MAX_ENTRIES = 500
try:
    IMAGE_SYNC_BATCH_UPLOAD_LIMIT = max(1, int(os.getenv("IMAGE_SYNC_BATCH_UPLOAD_LIMIT", "10")))
except ValueError:
    IMAGE_SYNC_BATCH_UPLOAD_LIMIT = 10
IMAGE_SCAN_IGNORE_DRIVES = {
    drive.strip().upper().replace(":", "")
    for drive in os.getenv("IMAGE_SCAN_IGNORE_DRIVES", "").split(",")
    if drive and drive.strip()
}
IMAGE_SCAN_IGNORED_DIR_NAMES = {
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "$recycle.bin",
    "system volume information",
    "recovery",
    "perflogs",
}
IMAGE_SCAN_IGNORED_DIR_KEYWORDS = {
    "software",
    "windows",
    "program files",
    "appdata",
    "temp",
    "cache",
}
IMAGE_SCAN_EXTRA_DIRS = {
    os.path.normpath(os.path.expandvars(os.path.expanduser(path.strip())))
    for path in os.getenv("IMAGE_SCAN_EXTRA_DIRS", "").split(";")
    if path and path.strip()
}
USER_MEDIA_SUBDIRS = [
    "Downloads",
    "Documents",
    "Pictures",
    "Videos",
    "Desktop",
]
try:
    AUDIO_SAMPLE_RATE = max(8000, int(os.getenv("AUDIO_SAMPLE_RATE", "16000")))
except ValueError:
    AUDIO_SAMPLE_RATE = 16000

try:
    AUDIO_CHANNELS = max(1, int(os.getenv("AUDIO_CHANNELS", "1")))
except ValueError:
    AUDIO_CHANNELS = 1

try:
    AUDIO_BLOCK_FRAMES = max(256, int(os.getenv("AUDIO_BLOCK_FRAMES", "1024")))
except ValueError:
    AUDIO_BLOCK_FRAMES = 1024
CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET")
CLOUDINARY_URL = os.getenv("CLOUDINARY_URL")
MACHINE_NAME = os.getenv("AGENT_NAME") or os.getenv("COMPUTERNAME", "Unknown-PC")
embedded_agent_version, embedded_agent_version_source = load_embedded_agent_version()
agent_version_from_file, agent_version_source = load_agent_version_from_file()
agent_version_from_env = os.getenv("AGENT_VERSION", "").strip()
AGENT_VERSION = embedded_agent_version or agent_version_from_env or agent_version_from_file or "1.0.0"
UPDATE_MANIFEST_URL = os.getenv("UPDATE_MANIFEST_URL")

auto_update_raw = os.getenv("AUTO_UPDATE_ENABLED", "true").strip().lower()
AUTO_UPDATE_ENABLED = auto_update_raw not in {"0", "false", "no", "off"}
try:
    UPDATE_CHECK_INTERVAL_SECONDS = max(60, int(os.getenv("UPDATE_CHECK_INTERVAL_SECONDS", "3600")))
except ValueError:
    UPDATE_CHECK_INTERVAL_SECONDS = 3600
try:
    UPDATE_DOWNLOAD_RETRY_COUNT = max(1, int(os.getenv("UPDATE_DOWNLOAD_RETRY_COUNT", "3")))
except ValueError:
    UPDATE_DOWNLOAD_RETRY_COUNT = 3
try:
    UPDATE_DOWNLOAD_RETRY_DELAY_SECONDS = max(1, int(os.getenv("UPDATE_DOWNLOAD_RETRY_DELAY_SECONDS", "2")))
except ValueError:
    UPDATE_DOWNLOAD_RETRY_DELAY_SECONDS = 2
try:
    MIN_UPDATE_BINARY_SIZE_BYTES = max(64 * 1024, int(os.getenv("MIN_UPDATE_BINARY_SIZE_BYTES", "524288")))
except ValueError:
    MIN_UPDATE_BINARY_SIZE_BYTES = 524288

# Cloudinary Setup (Get these from your Cloudinary Dashboard)
CLOUDINARY_READY = False

if CLOUDINARY_URL:
    cloudinary.config(cloudinary_url=CLOUDINARY_URL)
    CLOUDINARY_READY = True
    log_error("Cloudinary configured using CLOUDINARY_URL")
elif CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET:
    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY,
        api_secret=CLOUDINARY_API_SECRET
    )
    CLOUDINARY_READY = True
    log_error(f"Cloudinary configured for cloud: {CLOUDINARY_CLOUD_NAME}")
else:
    missing_keys = []
    if not CLOUDINARY_CLOUD_NAME:
        missing_keys.append("CLOUDINARY_CLOUD_NAME")
    if not CLOUDINARY_API_KEY:
        missing_keys.append("CLOUDINARY_API_KEY")
    if not CLOUDINARY_API_SECRET:
        missing_keys.append("CLOUDINARY_API_SECRET")
    log_error("Cloudinary credentials are not configured at startup. Missing: " + ", ".join(missing_keys))

if sd is None:
    log_error(f"sounddevice import unavailable. Voice recording disabled: {sounddevice_import_error}")
# ---------------------

sio = socketio.Client(reconnection=True, reconnection_attempts=0, reconnection_delay=2, reconnection_delay_max=10)
is_recording = False
is_camera_on = False
is_voice_recording = False
recording_lock = threading.Lock()
camera_lock = threading.Lock()
voice_lock = threading.Lock()
image_sync_lock = threading.Lock()
update_lock = threading.Lock()
update_last_checked_at = 0
update_in_progress = False
is_image_sync_running = False
image_sync_thread = None
image_sync_stop_event = threading.Event()
image_sync_reset_requested = False
image_sync_reset_clear_hashes_requested = False

if not os.path.exists(RECORDING_DIR):
    os.makedirs(RECORDING_DIR)

if not os.path.exists(IMAGE_SYNC_DIR):
    os.makedirs(IMAGE_SYNC_DIR)


def emit_agent_state(source=""):
    try:
        sio.emit('agent_state_update', {
            'recording': bool(is_recording),
            'cameraOn': bool(is_camera_on),
            'voiceRecording': bool(is_voice_recording),
            'isAdmin': bool(is_admin_process()),
            'machine': MACHINE_NAME,
            'source': source,
            'timestamp': int(time.time() * 1000)
        })
    except Exception as error:
        log_error(f"Failed to emit agent state ({source}): {error}")


def emit_update_state(stage, details=None):
    payload = {
        'machine': MACHINE_NAME,
        'stage': stage,
        'currentVersion': AGENT_VERSION,
        'timestamp': int(time.time() * 1000),
    }
    if details:
        payload.update(details)

    try:
        sio.emit('agent_update_status', payload)
    except Exception as error:
        log_error(f"Failed to emit update state ({stage}): {error}")


def emit_image_sync_state(stage, details=None):
    payload = {
        'machine': MACHINE_NAME,
        'stage': stage,
        'timestamp': int(time.time() * 1000),
    }
    if details:
        payload.update(details)

    try:
        sio.emit('image_sync_status', payload)
    except Exception as error:
        log_error(f"Failed to emit image sync state ({stage}): {error}")


def load_image_sync_state():
    default_state = {
        'pendingFiles': [],
        'nextIndex': 0,
        'uploadedHashes': [],
    }

    if not os.path.exists(IMAGE_SYNC_STATE_FILE):
        return default_state

    try:
        with open(IMAGE_SYNC_STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
    except Exception as error:
        log_error(f"Failed to read image sync state: {error}")
        return default_state

    pending_files = state.get('pendingFiles', [])
    if not isinstance(pending_files, list):
        pending_files = []

    uploaded_hashes = state.get('uploadedHashes', [])
    if not isinstance(uploaded_hashes, list):
        uploaded_hashes = []

    try:
        next_index = int(state.get('nextIndex', 0))
    except (TypeError, ValueError):
        next_index = 0

    next_index = max(0, min(next_index, len(pending_files)))

    return {
        'pendingFiles': pending_files,
        'nextIndex': next_index,
        'uploadedHashes': [str(item) for item in uploaded_hashes if item],
    }


def save_image_sync_state(pending_files, next_index, uploaded_hashes):
    safe_pending_files = pending_files if isinstance(pending_files, list) else []
    try:
        parsed_next_index = int(next_index or 0)
    except (TypeError, ValueError):
        parsed_next_index = 0
    safe_next_index = max(0, min(parsed_next_index, len(safe_pending_files)))
    safe_uploaded_hashes = list(uploaded_hashes) if isinstance(uploaded_hashes, (list, set, tuple)) else []
    state = {
        'pendingFiles': safe_pending_files,
        'nextIndex': safe_next_index,
        'uploadedHashes': safe_uploaded_hashes,
        'updatedAt': int(time.time()),
    }

    temp_path = IMAGE_SYNC_STATE_FILE + ".tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False)
        os.replace(temp_path, IMAGE_SYNC_STATE_FILE)
    except Exception as error:
        log_error(f"Failed to save image sync state: {error}")
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


def get_windows_drive_roots():
    if os.name != "nt":
        return ["/"]

    roots = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if letter in IMAGE_SCAN_IGNORE_DRIVES:
            continue
        drive_root = f"{letter}:\\"
        if os.path.exists(drive_root):
            roots.append(drive_root)
    return roots


def should_ignore_path_segment(segment):
    normalized = str(segment or "").strip().lower()
    if not normalized:
        return False

    if normalized in IMAGE_SCAN_IGNORED_DIR_NAMES:
        return True

    for keyword in IMAGE_SCAN_IGNORED_DIR_KEYWORDS:
        if keyword in normalized:
            return True

    return False


def _normalize_existing_directory(path):
    if not path:
        return ""
    expanded = os.path.normpath(os.path.expandvars(os.path.expanduser(path)))
    if os.name == "nt" and len(expanded) == 2 and expanded[1] == ":":
        expanded += "\\"
    if os.path.isdir(expanded):
        return expanded
    return ""


def get_directory_shortcuts():
    shortcuts = []
    seen_paths = set()
    user_candidates = [
        os.environ.get("USERPROFILE"),
        os.path.expanduser("~"),
    ]

    for base_path in user_candidates:
        normalized_base = _normalize_existing_directory(base_path)
        if not normalized_base:
            continue
        base_label = os.path.basename(normalized_base.rstrip("\\/")) or normalized_base
        for subdir in USER_MEDIA_SUBDIRS:
            candidate = _normalize_existing_directory(os.path.join(normalized_base, subdir))
            if not candidate or candidate in seen_paths:
                continue
            seen_paths.add(candidate)
            label = f"{subdir} ({base_label})" if base_label else subdir
            shortcuts.append({
                'path': candidate,
                'name': label,
                'type': 'shortcut',
                'hasChildren': True,
                'size': 0,
                'extension': '',
                'modifiedTime': 0,
                'isLocked': False,
                'readable': True,
            })

    for extra_dir in IMAGE_SCAN_EXTRA_DIRS:
        candidate = _normalize_existing_directory(extra_dir)
        if not candidate or candidate in seen_paths:
            continue
        seen_paths.add(candidate)
        label = os.path.basename(candidate.rstrip("\\/")) or candidate
        shortcuts.append({
            'path': candidate,
            'name': label,
            'type': 'shortcut',
            'hasChildren': True,
            'size': 0,
            'extension': '',
            'modifiedTime': 0,
            'isLocked': False,
            'readable': True,
        })

    shortcuts.sort(key=lambda item: item['name'].lower())
    return shortcuts


def get_root_directory_entries():
    entries = []
    seen = set()
    drive_entries = []
    for drive_root in get_windows_drive_roots():
        normalized = _normalize_existing_directory(drive_root) or drive_root
        if normalized in seen:
            continue
        seen.add(normalized)
        drive_label = drive_root.rstrip("\\/") or drive_root
        if os.name == "nt" and drive_label:
            display_name = f"Local Disk ({drive_label})"
        else:
            display_name = drive_root or "/"
        drive_entries.append({
            'path': normalized,
            'name': display_name,
            'type': 'drive',
            'hasChildren': True,
            'size': 0,
            'extension': '',
            'modifiedTime': 0,
            'isLocked': False,
            'readable': True,
        })

    if not drive_entries and os.name != "nt":
        drive_entries.append({
            'path': '/',
            'name': '/',
            'type': 'drive',
            'hasChildren': True,
            'size': 0,
            'extension': '',
            'modifiedTime': 0,
            'isLocked': False,
            'readable': True,
        })

    drive_entries.sort(key=lambda item: item['name'].lower())
    entries.extend(drive_entries)

    for shortcut in get_directory_shortcuts():
        if shortcut['path'] in seen:
            continue
        entries.append(shortcut)
        seen.add(shortcut['path'])

    return entries


def build_directory_breadcrumb(path):
    cleaned = str(path or '').strip()
    if not cleaned:
        return []
    try:
        pure_path = PurePath(cleaned)
    except Exception:
        return [{'name': cleaned, 'path': cleaned}]

    breadcrumb = []
    cumulative = None
    for part in pure_path.parts:
        if cumulative is None:
            cumulative = PurePath(part)
        else:
            cumulative = cumulative / part
        breadcrumb.append({
            'name': part or cleaned,
            'path': str(cumulative),
        })
    return breadcrumb


def get_file_metadata(entry_or_path):
    try:
        if isinstance(entry_or_path, os.DirEntry):
            path = entry_or_path.path
            name = entry_or_path.name
            try:
                is_directory = entry_or_path.is_dir(follow_symlinks=False)
            except OSError:
                is_directory = False
            try:
                stat_res = entry_or_path.stat(follow_symlinks=False)
                size = stat_res.st_size if not is_directory else 0
                mtime = int(stat_res.st_mtime * 1000)
                is_locked = False
            except OSError:
                size = 0
                mtime = 0
                is_locked = True
        else:
            path = str(entry_or_path)
            name = os.path.basename(path) or path
            is_directory = os.path.isdir(path)
            try:
                stat_res = os.stat(path)
                size = stat_res.st_size if not is_directory else 0
                mtime = int(stat_res.st_mtime * 1000)
                is_locked = False
            except OSError:
                size = 0
                mtime = 0
                is_locked = True

        ext = os.path.splitext(name)[1].lower() if not is_directory else ""
        return {
            'path': path,
            'name': name,
            'type': 'directory' if is_directory else 'file',
            'hasChildren': is_directory,
            'size': size,
            'extension': ext,
            'modifiedTime': mtime,
            'isLocked': is_locked,
            'readable': not is_locked,
        }
    except Exception as err:
        return {
            'path': str(entry_or_path),
            'name': os.path.basename(str(entry_or_path)),
            'type': 'unknown',
            'hasChildren': False,
            'size': 0,
            'extension': '',
            'modifiedTime': 0,
            'isLocked': True,
            'readable': False,
            'error': str(err),
        }


def list_directory_children(parent_path, include_files=True, max_entries=DIRECTORY_LISTING_MAX_ENTRIES):
    normalized_parent = str(parent_path or '').strip()
    if not normalized_parent:
        return {
            'normalizedParentPath': '',
            'entries': get_root_directory_entries(),
            'breadcrumb': [],
            'truncated': False,
            'accessDenied': False,
        }

    expanded = os.path.normpath(os.path.expandvars(os.path.expanduser(normalized_parent)))
    if os.name == "nt" and len(expanded) == 2 and expanded[1] == ":":
        expanded += "\\"

    if not os.path.exists(expanded):
        raise FileNotFoundError(f"Directory not found: {normalized_parent}")

    entries = []
    truncated = False
    access_denied = False
    error_message = ""

    try:
        with os.scandir(expanded) as iterator:
            for entry in iterator:
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    is_dir = False

                if not is_dir and not include_files:
                    continue

                meta = get_file_metadata(entry)
                entries.append(meta)

                if len(entries) >= max_entries:
                    truncated = True
                    break
    except PermissionError as perm_err:
        access_denied = True
        error_message = f"Access Denied (Permission Required): {perm_err}"
        log_error(f"Permission error scanning {expanded}: {perm_err}")
    except Exception as error:
        error_message = str(error)
        log_error(f"Error scanning directory {expanded}: {error}")
        raise RuntimeError(str(error)) from error

    entries.sort(key=lambda item: (0 if item.get('type') in ('directory', 'drive', 'shortcut') else 1, str(item.get('name', '')).lower()))
    return {
        'normalizedParentPath': expanded,
        'entries': entries,
        'breadcrumb': build_directory_breadcrumb(expanded),
        'truncated': truncated,
        'accessDenied': access_denied,
        'error': error_message if access_denied else "",
    }


def read_file_chunk_data(file_path, offset=0, length=1024 * 1024):
    if not file_path:
        raise ValueError("Missing file path")
    expanded = os.path.normpath(os.path.expandvars(os.path.expanduser(file_path)))
    if not os.path.exists(expanded):
        raise FileNotFoundError(f"File not found: {file_path}")
    if os.path.isdir(expanded):
        raise IsADirectoryError(f"Path is a directory: {file_path}")

    file_size = os.path.getsize(expanded)
    offset = max(0, int(offset or 0))
    length = max(1, min(int(length or (1024 * 1024)), 10 * 1024 * 1024))

    if offset >= file_size:
        return {
            'filePath': expanded,
            'offset': offset,
            'length': 0,
            'totalSize': file_size,
            'dataBase64': '',
            'eof': True,
        }

    with open(expanded, "rb") as file:
        file.seek(offset)
        chunk = file.read(length)

    eof = (offset + len(chunk)) >= file_size
    return {
        'filePath': expanded,
        'offset': offset,
        'length': len(chunk),
        'totalSize': file_size,
        'dataBase64': base64.b64encode(chunk).decode('ascii'),
        'eof': eof,
    }


def search_device_files(query, search_root=None, max_results=200, include_files=True, include_dirs=True):
    query = str(query or "").strip().lower()
    if not query:
        return []

    roots = []
    if search_root:
        norm = _normalize_existing_directory(search_root)
        if norm:
            roots.append(norm)
    if not roots:
        roots = get_windows_drive_roots()

    results = []
    for root in roots:
        try:
            for current_root, dirs, files in os.walk(root, topdown=True):
                if include_dirs:
                    for d in list(dirs):
                        if query in d.lower():
                            full_path = os.path.join(current_root, d)
                            results.append(get_file_metadata(full_path))
                            if len(results) >= max_results:
                                return results

                if include_files:
                    for f in files:
                        if query in f.lower():
                            full_path = os.path.join(current_root, f)
                            results.append(get_file_metadata(full_path))
                            if len(results) >= max_results:
                                return results
        except Exception as err:
            log_error(f"Search error on {root}: {err}")
            continue

    return results


def get_known_media_directories():
    candidate_directories = set()

    def add_directory_if_exists(path):
        normalized = _normalize_existing_directory(path)
        if normalized:
            candidate_directories.add(normalized)

    def add_standard_media_directories(base_path):
        normalized_base = _normalize_existing_directory(base_path)
        if not normalized_base:
            return
        for subdir in USER_MEDIA_SUBDIRS:
            add_directory_if_exists(os.path.join(normalized_base, subdir))

    user_profile = os.environ.get("USERPROFILE")
    home_dir = os.path.expanduser("~")
    system_drive = os.environ.get("SYSTEMDRIVE", "C:")
    system_users_dir = os.path.join(system_drive + os.sep, "Users")

    user_dir_candidates = []
    seen_dirs = set()

    for candidate in filter(None, [user_profile, home_dir, system_users_dir]):
        normalized = _normalize_existing_directory(candidate)
        if normalized and normalized not in seen_dirs:
            seen_dirs.add(normalized)
            user_dir_candidates.append(normalized)

    if os.path.isdir(system_users_dir):
        try:
            for entry in os.scandir(system_users_dir):
                if entry.is_dir():
                    normalized = _normalize_existing_directory(entry.path)
                    if normalized and normalized not in seen_dirs:
                        seen_dirs.add(normalized)
                        user_dir_candidates.append(normalized)
        except OSError:
            pass

    for user_dir in user_dir_candidates:
        base_name = os.path.basename(user_dir.rstrip("\\/")).lower()
        if base_name != "users":
            add_standard_media_directories(user_dir)

            try:
                for entry in os.scandir(user_dir):
                    if entry.is_dir():
                        name_lower = entry.name.lower()
                        if "onedrive" in name_lower:
                            add_directory_if_exists(entry.path)
                            add_standard_media_directories(entry.path)
            except OSError:
                pass

    public_dir = os.path.join(system_users_dir, "Public")
    add_standard_media_directories(public_dir)

    for env_key in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        add_standard_media_directories(os.environ.get(env_key))
        add_directory_if_exists(os.environ.get(env_key))

    for extra_dir in IMAGE_SCAN_EXTRA_DIRS:
        add_directory_if_exists(extra_dir)

    return sorted(candidate_directories)


def collect_device_image_files(scan_root=None, apply_ignore_filters=True):
    image_files = []
    if scan_root:
        if isinstance(scan_root, (list, tuple, set)):
            requested_roots = list(scan_root)
        else:
            requested_roots = [scan_root]
        scan_roots = []
        for raw_root in requested_roots:
            normalized = _normalize_existing_directory(raw_root)
            if normalized:
                scan_roots.append(normalized)
        if not scan_roots:
            return image_files
        strict_ignore = False
    else:
        preferred_roots = get_known_media_directories()
        if preferred_roots:
            scan_roots = preferred_roots
        else:
            scan_roots = get_windows_drive_roots()
        strict_ignore = apply_ignore_filters

    seen_roots = set()
    for drive_root in scan_roots:
        normalized_root = _normalize_existing_directory(drive_root)
        if not normalized_root or normalized_root in seen_roots:
            continue
        seen_roots.add(normalized_root)

        try:
            for root, dirs, files in os.walk(normalized_root, topdown=True):
                if strict_ignore:
                    dirs[:] = [directory for directory in dirs if not should_ignore_path_segment(directory)]
                    root_parts = re.split(r"[\\/]+", root)
                    if any(should_ignore_path_segment(part) for part in root_parts):
                        continue

                for file_name in files:
                    extension = os.path.splitext(file_name)[1].lower()
                    if extension in IMAGE_EXTENSIONS:
                        full_path = os.path.join(root, file_name)
                        if strict_ignore:
                            path_parts = re.split(r"[\\/]+", full_path)
                            if any(should_ignore_path_segment(part) for part in path_parts):
                                continue
                        image_files.append(full_path)
        except Exception as error:
            log_error(f"Image scan error on {drive_root}: {error}")

    image_files.sort(key=lambda path: path.lower())
    return image_files


def get_installed_applications():
    if os.name != "nt" or winreg is None:
        return []

    reg_locations = [
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall", 0),
    ]

    apps = {}
    for root_key, subkey_path, flags in reg_locations:
        try:
            access = winreg.KEY_READ | flags if flags else winreg.KEY_READ
            with winreg.OpenKey(root_key, subkey_path, 0, access) as key:
                num_subkeys = winreg.QueryInfoKey(key)[0]
                for i in range(num_subkeys):
                    try:
                        subkey_name = winreg.EnumKey(key, i)
                        with winreg.OpenKey(key, subkey_name, 0, access) as app_key:
                            def get_val(name):
                                try:
                                    v, _ = winreg.QueryValueEx(app_key, name)
                                    return str(v).strip() if v is not None else ""
                                except OSError:
                                    return ""

                            display_name = get_val("DisplayName")
                            if not display_name:
                                continue

                            system_component = get_val("SystemComponent")
                            if system_component == "1":
                                continue

                            version = get_val("DisplayVersion")
                            publisher = get_val("Publisher")
                            uninstall_str = get_val("UninstallString")
                            quiet_uninstall = get_val("QuietUninstallString")
                            install_loc = get_val("InstallLocation")
                            install_date = get_val("InstallDate")

                            app_key_id = f"{display_name}_{version}".lower()
                            if app_key_id in apps:
                                continue

                            apps[app_key_id] = {
                                "id": subkey_name,
                                "name": display_name,
                                "version": version,
                                "publisher": publisher,
                                "uninstallString": uninstall_str,
                                "quietUninstallString": quiet_uninstall,
                                "installLocation": install_loc,
                                "installDate": install_date,
                            }
                    except OSError:
                        continue
        except OSError:
            continue

    result = list(apps.values())
    result.sort(key=lambda x: x["name"].lower())
    return result


def execute_app_uninstall(app_name="", uninstall_string="", quiet_uninstall_string="", package_id=""):
    cmd = ""
    if package_id:
        cmd = f'winget uninstall --id "{package_id}" --silent --accept-source-agreements'
    elif quiet_uninstall_string:
        cmd = quiet_uninstall_string
    elif uninstall_string:
        if "msiexec" in uninstall_string.lower():
            if "/i" in uninstall_string.lower():
                cmd = re.sub(r"/i", "/x", uninstall_string, flags=re.IGNORECASE) + " /qn /norestart"
            elif "/x" not in uninstall_string.lower():
                cmd = f"{uninstall_string} /qn /norestart"
            else:
                cmd = f"{uninstall_string} /qn /norestart"
        else:
            cmd = f'{uninstall_string} /S /VERYSILENT /NORESTART'

    if not cmd:
        raise ValueError("No valid uninstall command or package ID provided")

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log_error(f"Executing uninstall command: {cmd}")
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, creationflags=flags, timeout=300)
    return {
        "appName": app_name,
        "success": proc.returncode in (0, 3010),
        "returnCode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def execute_app_install(package_id="", installer_path="", custom_args=""):
    if package_id:
        cmd = f'winget install --id "{package_id}" --silent --accept-package-agreements --accept-source-agreements'
    elif installer_path:
        ext = os.path.splitext(installer_path)[1].lower()
        if ext == ".msi":
            args = custom_args or "/qn /norestart"
            cmd = f'msiexec /i "{installer_path}" {args}'
        else:
            args = custom_args or "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /S"
            cmd = f'"{installer_path}" {args}'
    else:
        raise ValueError("Must provide either package_id or installer_path")

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log_error(f"Executing install command: {cmd}")
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, creationflags=flags, timeout=600)
    return {
        "packageId": package_id,
        "installerPath": installer_path,
        "success": proc.returncode in (0, 3010),
        "returnCode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def search_winget_packages(query):
    query = str(query or "").strip()
    if not query:
        return []
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        f'winget search "{query}" --accept-source-agreements',
        shell=True,
        capture_output=True,
        text=True,
        creationflags=flags,
        timeout=30,
    )
    lines = proc.stdout.strip().splitlines()
    packages = []
    for line in lines:
        if "---" in line or not line.strip():
            continue
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) >= 2:
            name = parts[0]
            pkg_id = parts[1]
            version = parts[2] if len(parts) > 2 else ""
            source = parts[3] if len(parts) > 3 else ""
            if pkg_id.lower() != "id" and name.lower() != "name":
                packages.append({
                    "name": name,
                    "id": pkg_id,
                    "version": version,
                    "source": source,
                })
    return packages[:50]


def execute_system_power(action="restart", timeout_seconds=5, message="Action initiated via Remote Control"):
    action = str(action or "restart").strip().lower()
    timeout = max(0, int(timeout_seconds or 5))

    if action in ("restart", "reboot", "reset"):
        cmd = f'shutdown /r /t {timeout} /c "{message}"'
    elif action in ("shutdown", "poweroff"):
        cmd = f'shutdown /s /t {timeout} /c "{message}"'
    elif action in ("abort", "cancel"):
        cmd = 'shutdown /a'
    else:
        raise ValueError(f"Unsupported power action: {action}")

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log_error(f"Executing system power action '{action}': {cmd}")
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, creationflags=flags)
    return {
        "action": action,
        "success": proc.returncode == 0,
        "returnCode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def compute_file_sha256(file_path):
    digest = hashlib.sha256()
    with open(file_path, "rb") as file:
        while True:
            chunk = file.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest().lower()


def upload_image_to_cloudinary(file_path):
    response = cloudinary.uploader.upload(file_path, resource_type="image")
    image_url = response.get("secure_url") or response.get("url")
    if not image_url:
        raise RuntimeError("Cloudinary response missing image URL")
    return image_url


def has_pending_image_sync_work():
    state = load_image_sync_state()
    pending_files = state.get('pendingFiles', [])
    next_index = state.get('nextIndex', 0)
    return bool(pending_files) and next_index < len(pending_files)


def get_image_sync_snapshot():
    state = load_image_sync_state()
    pending_files = state.get('pendingFiles', [])
    next_index = state.get('nextIndex', 0)
    total_files = len(pending_files)
    remaining_files = max(0, total_files - next_index)
    return {
        'machine': MACHINE_NAME,
        'running': bool(is_image_sync_running),
        'nextIndex': next_index,
        'totalFiles': total_files,
        'remainingFiles': remaining_files,
        'timestamp': int(time.time() * 1000),
    }


def emit_image_sync_snapshot(event_name='image_sync_snapshot'):
    try:
        sio.emit(event_name, get_image_sync_snapshot())
    except Exception as error:
        log_error(f"Failed to emit image sync snapshot ({event_name}): {error}")


def image_sync_worker(force_rescan=False, trigger_source="admin", scan_root=None):
    global is_image_sync_running
    global image_sync_thread
    global image_sync_reset_requested
    global image_sync_reset_clear_hashes_requested

    try:
        state = load_image_sync_state()
        pending_files = state.get('pendingFiles', [])
        next_index = state.get('nextIndex', 0)
        uploaded_hashes = set(state.get('uploadedHashes', []))

        if force_rescan or not pending_files or next_index >= len(pending_files) or scan_root:
            emit_image_sync_state('scanning', {'trigger': trigger_source, 'scanPath': scan_root or ''})
            pending_files = collect_device_image_files(scan_root=scan_root)
            next_index = 0
            save_image_sync_state(pending_files, next_index, uploaded_hashes)

        total_files = len(pending_files)
        uploaded_in_batch = 0
        batch_pause_reached = False
        emit_image_sync_state('started', {
            'trigger': trigger_source,
            'totalFiles': total_files,
            'resumeIndex': next_index,
            'batchLimit': IMAGE_SYNC_BATCH_UPLOAD_LIMIT,
        })

        if total_files == 0:
            emit_image_sync_state('completed', {
                'trigger': trigger_source,
                'totalFiles': 0,
                'uploadedCount': 0,
            })
            save_image_sync_state([], 0, uploaded_hashes)
            return

        while next_index < total_files and not image_sync_stop_event.is_set():
            while not image_sync_stop_event.is_set() and not sio.connected:
                time.sleep(RECONNECT_DELAY_SECONDS)

            if image_sync_stop_event.is_set():
                break

            file_path = pending_files[next_index]

            if not os.path.exists(file_path):
                next_index += 1
                save_image_sync_state(pending_files, next_index, uploaded_hashes)
                continue

            try:
                file_hash = compute_file_sha256(file_path)
            except Exception as error:
                log_error(f"Image hash failed ({file_path}): {error}")
                next_index += 1
                save_image_sync_state(pending_files, next_index, uploaded_hashes)
                continue

            if file_hash in uploaded_hashes:
                next_index += 1
                save_image_sync_state(pending_files, next_index, uploaded_hashes)
                continue

            try:
                image_url = upload_image_to_cloudinary(file_path)
                uploaded_hashes.add(file_hash)
                next_index += 1
                save_image_sync_state(pending_files, next_index, uploaded_hashes)
                sio.emit('image_upload_complete', {
                    'machine': MACHINE_NAME,
                    'url': image_url,
                    'filePath': file_path,
                    'index': next_index,
                    'total': total_files,
                    'mediaType': 'image',
                })
                uploaded_in_batch += 1

                if uploaded_in_batch >= IMAGE_SYNC_BATCH_UPLOAD_LIMIT:
                    batch_pause_reached = True
                    break
            except Exception as error:
                log_error(f"Image upload failed ({file_path}): {error}")
                emit_image_sync_state('retrying', {
                    'trigger': trigger_source,
                    'filePath': file_path,
                    'index': next_index,
                    'totalFiles': total_files,
                    'error': str(error),
                })
                # Skip permanently failed files so one bad upload cannot stall the whole sync queue.
                next_index += 1
                save_image_sync_state(pending_files, next_index, uploaded_hashes)
                time.sleep(IMAGE_SYNC_RETRY_DELAY_SECONDS)

        if image_sync_stop_event.is_set():
            reset_requested = False
            clear_hashes_requested = False
            with image_sync_lock:
                if image_sync_reset_requested:
                    reset_requested = True
                    image_sync_reset_requested = False
                    clear_hashes_requested = image_sync_reset_clear_hashes_requested
                    image_sync_reset_clear_hashes_requested = False

            if reset_requested:
                if clear_hashes_requested:
                    uploaded_hashes = set()
                save_image_sync_state([], 0, uploaded_hashes)
                emit_image_sync_state('reset', {
                    'trigger': trigger_source,
                    'nextIndex': 0,
                    'totalFiles': 0,
                    'clearedUploadedHashes': bool(clear_hashes_requested),
                })
            else:
                save_image_sync_state(pending_files, next_index, uploaded_hashes)
                emit_image_sync_state('stopped', {
                    'trigger': trigger_source,
                    'nextIndex': next_index,
                    'totalFiles': total_files,
                })
        elif batch_pause_reached:
            save_image_sync_state(pending_files, next_index, uploaded_hashes)
            emit_image_sync_state('paused', {
                'trigger': trigger_source,
                'reason': 'batch_limit_reached',
                'batchLimit': IMAGE_SYNC_BATCH_UPLOAD_LIMIT,
                'uploadedInBatch': uploaded_in_batch,
                'nextIndex': next_index,
                'totalFiles': total_files,
            })
        else:
            save_image_sync_state([], 0, uploaded_hashes)
            emit_image_sync_state('completed', {
                'trigger': trigger_source,
                'totalFiles': total_files,
                'uploadedCount': next_index,
            })
    except Exception as error:
        log_error(f"Image sync worker error: {error}")
        emit_image_sync_state('failed', {
            'trigger': trigger_source,
            'error': str(error),
        })
    finally:
        with image_sync_lock:
            is_image_sync_running = False
            image_sync_thread = None


def start_image_sync(force_rescan=False, trigger_source="admin", scan_root=None):
    global is_image_sync_running
    global image_sync_thread
    global image_sync_reset_requested
    global image_sync_reset_clear_hashes_requested

    if not CLOUDINARY_READY:
        emit_image_sync_state('failed', {
            'trigger': trigger_source,
            'error': 'cloudinary_not_configured',
        })
        return False

    with image_sync_lock:
        if is_image_sync_running:
            emit_image_sync_state('already_running', {'trigger': trigger_source})
            return False

        image_sync_stop_event.clear()
        image_sync_reset_requested = False
        image_sync_reset_clear_hashes_requested = False
        is_image_sync_running = True
        image_sync_thread = threading.Thread(
            target=image_sync_worker,
            args=(bool(force_rescan), trigger_source, scan_root),
            daemon=True,
        )
        image_sync_thread.start()
    return True


def stop_image_sync(trigger_source="admin"):
    if not is_image_sync_running:
        emit_image_sync_state('idle', {'trigger': trigger_source})
        return False

    image_sync_stop_event.set()
    emit_image_sync_state('stopping', {'trigger': trigger_source})
    return True


def reset_image_sync(trigger_source="admin", clear_uploaded_hashes=False):
    global image_sync_reset_requested
    global image_sync_reset_clear_hashes_requested

    state = load_image_sync_state()
    uploaded_hashes = [] if clear_uploaded_hashes else state.get('uploadedHashes', [])

    with image_sync_lock:
        if is_image_sync_running:
            image_sync_reset_requested = True
            image_sync_reset_clear_hashes_requested = bool(clear_uploaded_hashes)
            image_sync_stop_event.set()
            emit_image_sync_state('resetting', {'trigger': trigger_source})
            return True

    save_image_sync_state([], 0, uploaded_hashes)
    emit_image_sync_state('reset', {
        'trigger': trigger_source,
        'nextIndex': 0,
        'totalFiles': 0,
        'clearedUploadedHashes': bool(clear_uploaded_hashes),
    })
    return True


def version_to_tuple(version_value):
    parts = re.findall(r"\d+", str(version_value or "0"))
    if not parts:
        return (0,)
    return tuple(int(part) for part in parts)


def is_newer_version(current_version, candidate_version):
    current = list(version_to_tuple(current_version))
    candidate = list(version_to_tuple(candidate_version))
    max_len = max(len(current), len(candidate))
    current.extend([0] * (max_len - len(current)))
    candidate.extend([0] * (max_len - len(candidate)))
    return tuple(candidate) > tuple(current)


def fetch_update_manifest():
    if not UPDATE_MANIFEST_URL:
        return None

    response = requests.get(UPDATE_MANIFEST_URL, timeout=15)
    response.raise_for_status()

    try:
        manifest = response.json()
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Invalid update manifest JSON: {error}") from error

    version = str(manifest.get("version", "")).strip()
    download_url = str(manifest.get("url") or manifest.get("downloadUrl") or "").strip()
    sha256 = str(manifest.get("sha256") or "").strip().lower()
    size_raw = manifest.get("size")
    if size_raw is None:
        size_raw = manifest.get("fileSize")
    if size_raw is None:
        size_raw = manifest.get("contentLength")

    expected_size = 0
    if size_raw not in (None, ""):
        try:
            expected_size = int(str(size_raw).strip())
            if expected_size < 0:
                expected_size = 0
        except (TypeError, ValueError):
            expected_size = 0
            log_error(f"Update manifest has invalid size value: {size_raw}")

    if sha256 and not re.fullmatch(r"[0-9a-f]{64}", sha256):
        log_error("Update manifest SHA256 format invalid; continuing without SHA256 validation")
        sha256 = ""

    if not version:
        raise RuntimeError("Update manifest missing 'version'")
    if not download_url:
        raise RuntimeError("Update manifest missing 'url'")

    return {
        'version': version,
        'url': download_url,
        'sha256': sha256,
        'size': expected_size,
    }


def compute_sha256(file_path):
    digest = hashlib.sha256()
    with open(file_path, "rb") as file:
        while True:
            chunk = file.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest().lower()


def validate_update_binary(file_path, expected_sha256="", expected_size=0):
    file_size = os.path.getsize(file_path)
    if file_size <= 0:
        raise RuntimeError("Downloaded update file is empty")

    if expected_size and file_size != expected_size:
        raise RuntimeError(f"Downloaded update size mismatch. expected={expected_size} actual={file_size}")

    if file_size < MIN_UPDATE_BINARY_SIZE_BYTES:
        raise RuntimeError(
            f"Downloaded update is too small to be a valid executable ({file_size} bytes)"
        )

    with open(file_path, "rb") as file:
        mz_header = file.read(2)
    if mz_header != b"MZ":
        raise RuntimeError("Downloaded update is not a valid Windows executable (MZ header missing)")

    if expected_sha256:
        actual_sha256 = compute_sha256(file_path)
        if actual_sha256 != expected_sha256:
            raise RuntimeError(f"SHA256 mismatch. expected={expected_sha256} actual={actual_sha256}")


def download_update_binary(download_url, expected_sha256="", expected_size=0):
    updates_dir = os.path.join(os.getenv("APPDATA", BASE_DIR), "RemoteAgent", "updates")
    os.makedirs(updates_dir, exist_ok=True)

    update_tag = f"{int(time.time())}_{os.getpid()}"
    temp_file = os.path.join(updates_dir, f"RemoteAgent_update_{update_tag}.exe")
    part_file = temp_file + ".part"
    last_error = None

    for attempt in range(1, UPDATE_DOWNLOAD_RETRY_COUNT + 1):
        downloaded_bytes = 0
        content_length = 0

        try:
            for stale_file in (part_file, temp_file):
                if os.path.exists(stale_file):
                    os.remove(stale_file)

            with requests.get(download_url, stream=True, timeout=(15, 180), allow_redirects=True) as response:
                response.raise_for_status()
                content_length_header = str(response.headers.get("Content-Length") or "").strip()
                if content_length_header.isdigit():
                    content_length = int(content_length_header)

                with open(part_file, "wb") as file:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        file.write(chunk)
                        downloaded_bytes += len(chunk)

            if downloaded_bytes <= 0:
                raise RuntimeError("Downloaded zero bytes")
            if content_length and downloaded_bytes != content_length:
                raise RuntimeError(
                    f"Incomplete download. expected={content_length} downloaded={downloaded_bytes}"
                )

            os.replace(part_file, temp_file)
            validate_update_binary(temp_file, expected_sha256=expected_sha256, expected_size=expected_size)
            return temp_file
        except Exception as error:
            last_error = error
            log_error(f"Update download attempt {attempt}/{UPDATE_DOWNLOAD_RETRY_COUNT} failed: {error}")
            for stale_file in (part_file, temp_file):
                try:
                    if os.path.exists(stale_file):
                        os.remove(stale_file)
                except OSError:
                    pass

            if attempt < UPDATE_DOWNLOAD_RETRY_COUNT:
                time.sleep(UPDATE_DOWNLOAD_RETRY_DELAY_SECONDS * attempt)

    raise RuntimeError(
        f"Failed to download valid update after {UPDATE_DOWNLOAD_RETRY_COUNT} attempts: {last_error}"
    )


def launch_windows_updater(new_exe_path, target_version):
    current_exe = os.path.abspath(sys.executable)
    target_exe = get_canonical_packaged_exe_path() or current_exe
    current_image_name = os.path.basename(current_exe)
    target_image_name = os.path.basename(target_exe)
    current_pid = os.getpid()
    updates_dir = os.path.dirname(new_exe_path)
    update_id = int(time.time())
    updater_script_path = os.path.join(updates_dir, f"agent_updater_{update_id}.bat")
    updater_vbs_path = os.path.join(updates_dir, f"agent_updater_{update_id}.vbs")
    backup_exe = target_exe + ".old"
    version_file_path = os.path.join(os.path.dirname(target_exe), AGENT_VERSION_FILE_NAME)

    script_content = f"""@echo off
setlocal
set \"TARGET={target_exe}\"
set \"NEW={new_exe_path}\"
set \"BACKUP={backup_exe}\"
set \"CURRENT_IMAGE_NAME={current_image_name}\"
set \"TARGET_IMAGE_NAME={target_image_name}\"
set \"PID={current_pid}\"
set \"VERSION_FILE={version_file_path}\"
set \"UPDATER_VBS={updater_vbs_path}\"

for /L %%I in (1,1,60) do (
  tasklist /FI \"PID eq %PID%\" | find \"%PID%\" >nul
  if errorlevel 1 goto :PROCESS_EXITED
  timeout /t 1 /nobreak >nul
)

:PROCESS_EXITED
taskkill /F /IM "%CURRENT_IMAGE_NAME%" /T >nul 2>&1
taskkill /F /IM "%TARGET_IMAGE_NAME%" /T >nul 2>&1
taskkill /F /IM "RemoteAgent_*.exe" /T >nul 2>&1
timeout /t 1 /nobreak >nul

if exist \"%BACKUP%\" del /F /Q \"%BACKUP%\" >nul 2>&1
if exist \"%TARGET%\" move /Y \"%TARGET%\" \"%BACKUP%\" >nul
move /Y \"%NEW%\" \"%TARGET%\" >nul
if errorlevel 1 goto :ROLLBACK

> \"%VERSION_FILE%\" echo {target_version}
start \"\" \"%TARGET%\"
timeout /t 3 /nobreak >nul
tasklist /FI \"IMAGENAME eq %TARGET_IMAGE_NAME%\" | find /I \"%TARGET_IMAGE_NAME%\" >nul
if errorlevel 1 goto :ROLLBACK

if exist \"%BACKUP%\" del /F /Q \"%BACKUP%\" >nul 2>&1
if exist \"%UPDATER_VBS%\" del /F /Q \"%UPDATER_VBS%\" >nul 2>&1
del /F /Q \"%~f0\" >nul 2>&1
exit /b 0

:ROLLBACK
if exist \"%BACKUP%\" move /Y \"%BACKUP%\" \"%TARGET%\" >nul
start \"\" \"%TARGET%\"
if exist \"%UPDATER_VBS%\" del /F /Q \"%UPDATER_VBS%\" >nul 2>&1
del /F /Q \"%~f0\" >nul 2>&1
exit /b 1
"""

    with open(updater_script_path, "w", encoding="utf-8", newline="\r\n") as file:
        file.write(script_content)

    vbs_content = f'''Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "cmd /c \"\"{updater_script_path}\"\"", 0, False
'''
    with open(updater_vbs_path, "w", encoding="utf-8", newline="\r\n") as file:
        file.write(vbs_content)

    flags = 0
    flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)

    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup_info.wShowWindow = 0

    subprocess.Popen(
        ["wscript", "//B", "//Nologo", updater_vbs_path],
        creationflags=flags,
        startupinfo=startup_info,
        close_fds=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    log_error(f"Updater paths: currentExe={current_exe} targetExe={target_exe} newExe={new_exe_path}")
    log_error(f"Updater launched for version {target_version}: {updater_script_path}")
    emit_update_state("installing", {'targetVersion': target_version})
    time.sleep(1)
    os._exit(0)


def check_for_agent_updates(force=False, source="watchdog"):
    global update_last_checked_at
    global update_in_progress

    if not AUTO_UPDATE_ENABLED:
        if force:
            emit_update_state("skipped", {'reason': 'auto_update_disabled', 'trigger': source})
        return
    if not UPDATE_MANIFEST_URL:
        if force:
            emit_update_state("skipped", {'reason': 'manifest_missing', 'trigger': source})
        return
    if not getattr(sys, "frozen", False):
        if force:
            emit_update_state("skipped", {'reason': 'not_packaged_exe', 'trigger': source})
        return

    now = time.time()
    if not force and (now - update_last_checked_at) < UPDATE_CHECK_INTERVAL_SECONDS:
        return

    if not update_lock.acquire(blocking=False):
        if force:
            emit_update_state("skipped", {'reason': 'check_busy', 'trigger': source})
        return

    try:
        update_last_checked_at = now

        if update_in_progress:
            if force:
                emit_update_state("skipped", {'reason': 'update_in_progress', 'trigger': source})
            return

        manifest = fetch_update_manifest()
        if not manifest:
            return

        latest_version = manifest['version']
        if not is_newer_version(AGENT_VERSION, latest_version):
            log_error(f"Auto-update check: up-to-date (local={AGENT_VERSION}, remote={latest_version})")
            if force:
                emit_update_state("up_to_date", {'targetVersion': latest_version, 'trigger': source})
            return

        update_in_progress = True
        emit_update_state("downloading", {'targetVersion': latest_version, 'trigger': source})
        log_error(f"Auto-update available: {AGENT_VERSION} -> {latest_version}")

        downloaded_file = download_update_binary(
            manifest['url'],
            manifest.get('sha256', ""),
            manifest.get('size', 0),
        )
        log_error(f"Auto-update downloaded: {downloaded_file}")

        launch_windows_updater(downloaded_file, latest_version)
    except Exception as error:
        update_in_progress = False
        message = f"Auto-update error: {error}"
        log_error(message)
        emit_update_state("failed", {'error': str(error), 'trigger': source})
    finally:
        update_lock.release()


def update_watchdog_loop():
    while True:
        try:
            check_for_agent_updates(force=False, source="watchdog")
        except Exception as error:
            log_error(f"Update watchdog loop error: {error}")
        time.sleep(UPDATE_CHECK_INTERVAL_SECONDS)


def build_playable_video_url(upload_response):
    secure_url = upload_response.get("secure_url")
    if not secure_url:
        return None

    upload_marker = "/video/upload/"
    if upload_marker not in secure_url:
        return secure_url

    return secure_url.replace(upload_marker, "/video/upload/f_mp4,vc_h264/", 1)

def upload_to_cloudinary(file_path, media_type="video"):
    """Uploads a media file and sends the URL back to the server."""
    if not CLOUDINARY_READY:
        message = "Cloudinary credentials missing. Skipping upload."
        print(message)
        log_error(message)
        return

    try:
        print(f"Uploading {media_type} {file_path} to Cloudinary...")
        log_error(f"Uploading {media_type} to Cloudinary: {file_path}")
        response = cloudinary.uploader.upload(file_path, resource_type="video")
        media_url = build_playable_video_url(response) if media_type == "video" else response.get("secure_url")
        media_url = media_url or response.get("secure_url")
        print(f"Upload Success ({media_type}): {media_url}")
        log_error(f"Upload success ({media_type}): {media_url}")

        event_name = 'audio_upload_complete' if media_type == "audio" else 'video_upload_complete'
        sio.emit(event_name, {'url': media_url, 'machine': MACHINE_NAME, 'mediaType': media_type})

        # Cleanup local file to save space
        os.remove(file_path)
        log_error(f"Local file removed after upload: {file_path}")
    except Exception as e:
        print(f"Cloudinary Error ({media_type}): {e}")
        log_error(f"Cloudinary Error ({media_type}): {e}")

def record_loop():
    global is_recording
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    file_path = os.path.join(RECORDING_DIR, f"rec_{timestamp}.mp4")
    out = None
    frames_written = 0

    try:
        screen_size = pyautogui.size()
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter(file_path, fourcc, FPS, screen_size)
        if not out.isOpened():
            log_error(f"VideoWriter failed to open: {file_path}")
            return

        log_error(f"Recording started: {file_path}")
        while is_recording:
            img = pyautogui.screenshot()
            frame = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
            out.write(frame)
            frames_written += 1
            time.sleep(1/FPS)
    except Exception as error:
        log_error(f"Recording loop error: {error}")
    finally:
        if out is not None:
            out.release()
        is_recording = False
        emit_agent_state('record_loop_stopped')
        log_error(f"Recording stopped: {file_path}")
        # Trigger upload in background
        if frames_written > 0 and os.path.exists(file_path):
            threading.Thread(target=upload_to_cloudinary, args=(file_path, 'video'), daemon=True).start()
        else:
            log_error(f"Upload skipped (frames={frames_written}, exists={os.path.exists(file_path)}): {file_path}")


def voice_record_loop():
    global is_voice_recording
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    file_path = os.path.join(RECORDING_DIR, f"voice_{timestamp}.wav")
    samples_written = 0

    try:
        if sd is None:
            raise RuntimeError("sounddevice module unavailable")

        with wave.open(file_path, 'wb') as wav_file:
            wav_file.setnchannels(AUDIO_CHANNELS)
            wav_file.setsampwidth(2)
            wav_file.setframerate(AUDIO_SAMPLE_RATE)

            with sd.InputStream(
                samplerate=AUDIO_SAMPLE_RATE,
                channels=AUDIO_CHANNELS,
                dtype='int16',
                blocksize=AUDIO_BLOCK_FRAMES
            ) as input_stream:
                log_error(f"Voice recording started: {file_path}")
                while is_voice_recording:
                    frames, overflowed = input_stream.read(AUDIO_BLOCK_FRAMES)
                    wav_file.writeframes(frames.tobytes())
                    samples_written += len(frames)
                    if overflowed:
                        log_error("Voice stream overflow detected")
    except Exception as error:
        log_error(f"Voice recording loop error: {error}")
    finally:
        is_voice_recording = False
        emit_agent_state('voice_loop_stopped')
        log_error(f"Voice recording stopped: {file_path}")

        if samples_written > 0 and os.path.exists(file_path):
            threading.Thread(target=upload_to_cloudinary, args=(file_path, 'audio'), daemon=True).start()
        else:
            log_error(f"Voice upload skipped (samples={samples_written}, exists={os.path.exists(file_path)}): {file_path}")

def camera_stream_loop():
    global is_camera_on
    cap = cv2.VideoCapture(0)
    try:
        log_error("Camera stream started")
        while is_camera_on:
            ret, frame = cap.read()
            if not ret: break
            frame = cv2.resize(frame, (640, 480))
            _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 50])
            jpg_as_text = base64.b64encode(buffer).decode('utf-8')
            sio.emit('camera_frame', {'image': jpg_as_text})
            time.sleep(0.1)
    except Exception as error:
        log_error(f"Camera loop error: {error}")
    finally:
        cap.release()
        is_camera_on = False
        emit_agent_state('camera_loop_stopped')
        log_error("Camera stream stopped")


@sio.event
def connect():
    log_error(f"Connected to {SERVER_URL}")
    sio.emit('register_node', {'machine': MACHINE_NAME})
    emit_agent_state('connected')
    emit_image_sync_snapshot()
    threading.Thread(target=check_for_agent_updates, kwargs={'force': True, 'source': 'connect'}, daemon=True).start()


@sio.event
def disconnect():
    log_error("Disconnected from server")

@sio.on('start_capture')
def on_start(data=None):
    global is_recording

    try:
        pyautogui.screenshot(region=(0, 0, 1, 1))
    except Exception as error:
        log_error(f"start_capture blocked: screen capture runtime unavailable: {error}")
        emit_agent_state('start_capture_failed')
        return

    with recording_lock:
        if not is_recording:
            is_recording = True
            log_error("start_capture received")
            threading.Thread(target=record_loop, daemon=True).start()
    emit_agent_state('start_capture')

@sio.on('stop_capture')
def on_stop(data=None):
    global is_recording
    log_error("stop_capture received")
    is_recording = False
    emit_agent_state('stop_capture')

@sio.on('start_camera')
def on_camera_start(data=None):
    global is_camera_on
    with camera_lock:
        if not is_camera_on:
            is_camera_on = True
            log_error("start_camera received")
            threading.Thread(target=camera_stream_loop, daemon=True).start()
    emit_agent_state('start_camera')

@sio.on('stop_camera')
def on_camera_stop(data=None):
    global is_camera_on
    log_error("stop_camera received")
    is_camera_on = False
    emit_agent_state('stop_camera')


@sio.on('start_voice_capture')
def on_voice_start(data=None):
    global is_voice_recording

    if sd is None:
        log_error("start_voice_capture blocked: sounddevice is unavailable")
        emit_agent_state('start_voice_capture_failed')
        return

    with voice_lock:
        if not is_voice_recording:
            is_voice_recording = True
            log_error("start_voice_capture received")
            threading.Thread(target=voice_record_loop, daemon=True).start()

    emit_agent_state('start_voice_capture')


@sio.on('stop_voice_capture')
def on_voice_stop(data=None):
    global is_voice_recording
    log_error("stop_voice_capture received")
    is_voice_recording = False
    emit_agent_state('stop_voice_capture')


@sio.on('force_update_check')
def on_force_update_check(data=None):
    log_error("force_update_check received")
    threading.Thread(target=check_for_agent_updates, kwargs={'force': True, 'source': 'admin'}, daemon=True).start()


def handle_find_image_and_save(data=None, event_name="find_image_and_save"):
    force_rescan = False
    scan_path = ''
    if isinstance(data, dict):
        force_rescan = bool(data.get('forceRescan', False))
        scan_path = str(data.get('scanPath') or data.get('directory') or '').strip()

    log_error(f"{event_name} received (forceRescan={force_rescan} scanPath={scan_path})")
    started = start_image_sync(force_rescan=force_rescan, trigger_source=event_name, scan_root=scan_path)
    if started:
        emit_image_sync_state('queued', {'trigger': event_name, 'forceRescan': force_rescan, 'scanPath': scan_path})


@sio.on('find_image_and_save')
def on_find_image_and_save(data=None):
    handle_find_image_and_save(data=data, event_name='find_image_and_save')


@sio.on('start_image_sync')
def on_start_image_sync(data=None):
    handle_find_image_and_save(data=data, event_name='start_image_sync')


@sio.on('stop_find_image_and_save')
def on_stop_find_image_and_save(data=None):
    log_error("stop_find_image_and_save received")
    stop_image_sync(trigger_source='stop_find_image_and_save')


@sio.on('stop_image_sync')
def on_stop_image_sync(data=None):
    log_error("stop_image_sync received")
    stop_image_sync(trigger_source='stop_image_sync')


@sio.on('reset_image_sync')
def on_reset_image_sync(data=None):
    clear_uploaded_hashes = True
    if isinstance(data, dict):
        clear_uploaded_hashes = bool(data.get('clearUploadedHashes', True))
    log_error(f"reset_image_sync received (clearUploadedHashes={clear_uploaded_hashes})")
    reset_image_sync(trigger_source='reset_image_sync', clear_uploaded_hashes=clear_uploaded_hashes)


@sio.on('stop_and_reset_image_sync')
def on_stop_and_reset_image_sync(data=None):
    clear_uploaded_hashes = True
    if isinstance(data, dict):
        clear_uploaded_hashes = bool(data.get('clearUploadedHashes', True))
    log_error(f"stop_and_reset_image_sync received (clearUploadedHashes={clear_uploaded_hashes})")
    reset_image_sync(trigger_source='stop_and_reset_image_sync', clear_uploaded_hashes=clear_uploaded_hashes)


@sio.on('get_image_sync_status')
def on_get_image_sync_status(data=None):
    log_error("get_image_sync_status received")
    emit_image_sync_snapshot()


@sio.on('list_directories')
def on_list_directories(data=None):
    request_id = ''
    parent_path = ''
    include_files = True
    if isinstance(data, dict):
        request_id = str(data.get('requestId') or '').strip()
        parent_path = data.get('parentPath') or ''
        if 'includeFiles' in data:
            include_files = bool(data.get('includeFiles'))

    try:
        listing = list_directory_children(parent_path, include_files=include_files)
        payload = {
            'machine': MACHINE_NAME,
            'requestId': request_id,
            'parentPath': parent_path or '',
            'normalizedParentPath': listing.get('normalizedParentPath', ''),
            'entries': listing.get('entries', []),
            'breadcrumb': listing.get('breadcrumb', []),
            'truncated': bool(listing.get('truncated', False)),
            'accessDenied': bool(listing.get('accessDenied', False)),
            'error': listing.get('error', ''),
            'isAdmin': bool(is_admin_process()),
            'timestamp': int(time.time() * 1000),
        }
        sio.emit('directory_listing', payload)
    except Exception as error:
        sio.emit('directory_listing', {
            'machine': MACHINE_NAME,
            'requestId': request_id,
            'parentPath': parent_path or '',
            'error': str(error),
            'timestamp': int(time.time() * 1000),
        })


@sio.on('list_files_and_directories')
def on_list_files_and_directories(data=None):
    on_list_directories(data=data)


@sio.on('browse_filesystem')
def on_browse_filesystem(data=None):
    on_list_directories(data=data)


@sio.on('get_file_info')
def on_get_file_info(data=None):
    request_id = ''
    file_path = ''
    if isinstance(data, dict):
        request_id = str(data.get('requestId') or '').strip()
        file_path = str(data.get('filePath') or '').strip()

    try:
        meta = get_file_metadata(file_path)
        sio.emit('file_info_result', {
            'machine': MACHINE_NAME,
            'requestId': request_id,
            'metadata': meta,
            'timestamp': int(time.time() * 1000),
        })
    except Exception as error:
        sio.emit('file_info_result', {
            'machine': MACHINE_NAME,
            'requestId': request_id,
            'filePath': file_path,
            'error': str(error),
            'timestamp': int(time.time() * 1000),
        })


@sio.on('read_file_chunk')
def on_read_file_chunk(data=None):
    request_id = ''
    file_path = ''
    offset = 0
    length = 1024 * 1024
    if isinstance(data, dict):
        request_id = str(data.get('requestId') or '').strip()
        file_path = str(data.get('filePath') or '').strip()
        offset = int(data.get('offset') or 0)
        length = int(data.get('length') or (1024 * 1024))

    try:
        chunk_data = read_file_chunk_data(file_path, offset=offset, length=length)
        chunk_data.update({
            'machine': MACHINE_NAME,
            'requestId': request_id,
            'timestamp': int(time.time() * 1000),
        })
        sio.emit('file_chunk_data', chunk_data)
    except Exception as error:
        sio.emit('file_chunk_data', {
            'machine': MACHINE_NAME,
            'requestId': request_id,
            'filePath': file_path,
            'offset': offset,
            'error': str(error),
            'timestamp': int(time.time() * 1000),
        })


@sio.on('search_files')
def on_search_files(data=None):
    request_id = ''
    query = ''
    search_root = None
    max_results = 200
    if isinstance(data, dict):
        request_id = str(data.get('requestId') or '').strip()
        query = str(data.get('query') or '').strip()
        search_root = data.get('searchRoot') or data.get('rootPath')
        max_results = int(data.get('maxResults') or 200)

    try:
        results = search_device_files(query, search_root=search_root, max_results=max_results)
        sio.emit('search_files_result', {
            'machine': MACHINE_NAME,
            'requestId': request_id,
            'query': query,
            'searchRoot': search_root or '',
            'results': results,
            'count': len(results),
            'timestamp': int(time.time() * 1000),
        })
    except Exception as error:
        sio.emit('search_files_result', {
            'machine': MACHINE_NAME,
            'requestId': request_id,
            'query': query,
            'error': str(error),
            'timestamp': int(time.time() * 1000),
        })


@sio.on('list_installed_apps')
def on_list_installed_apps(data=None):
    request_id = ''
    if isinstance(data, dict):
        request_id = str(data.get('requestId') or '').strip()

    try:
        apps = get_installed_applications()
        sio.emit('installed_apps_list', {
            'machine': MACHINE_NAME,
            'requestId': request_id,
            'apps': apps,
            'count': len(apps),
            'timestamp': int(time.time() * 1000),
        })
    except Exception as error:
        sio.emit('installed_apps_list', {
            'machine': MACHINE_NAME,
            'requestId': request_id,
            'apps': [],
            'error': str(error),
            'timestamp': int(time.time() * 1000),
        })


@sio.on('uninstall_app')
def on_uninstall_app(data=None):
    request_id = ''
    app_name = ''
    uninstall_string = ''
    quiet_uninstall_string = ''
    package_id = ''

    if isinstance(data, dict):
        request_id = str(data.get('requestId') or '').strip()
        app_name = str(data.get('name') or data.get('appName') or '').strip()
        uninstall_string = str(data.get('uninstallString') or '').strip()
        quiet_uninstall_string = str(data.get('quietUninstallString') or '').strip()
        package_id = str(data.get('packageId') or data.get('id') or '').strip()

    def _worker():
        try:
            res = execute_app_uninstall(
                app_name=app_name,
                uninstall_string=uninstall_string,
                quiet_uninstall_string=quiet_uninstall_string,
                package_id=package_id,
            )
            res.update({
                'machine': MACHINE_NAME,
                'requestId': request_id,
                'timestamp': int(time.time() * 1000),
            })
            sio.emit('uninstall_app_result', res)
        except Exception as error:
            sio.emit('uninstall_app_result', {
                'machine': MACHINE_NAME,
                'requestId': request_id,
                'appName': app_name,
                'success': False,
                'error': str(error),
                'timestamp': int(time.time() * 1000),
            })

    threading.Thread(target=_worker, daemon=True).start()


@sio.on('install_app')
def on_install_app(data=None):
    request_id = ''
    package_id = ''
    installer_path = ''
    custom_args = ''

    if isinstance(data, dict):
        request_id = str(data.get('requestId') or '').strip()
        package_id = str(data.get('packageId') or data.get('id') or '').strip()
        installer_path = str(data.get('installerPath') or data.get('path') or '').strip()
        custom_args = str(data.get('customArgs') or data.get('args') or '').strip()

    def _worker():
        try:
            sio.emit('install_app_status', {
                'machine': MACHINE_NAME,
                'requestId': request_id,
                'status': 'installing',
                'packageId': package_id,
                'installerPath': installer_path,
                'timestamp': int(time.time() * 1000),
            })
            res = execute_app_install(
                package_id=package_id,
                installer_path=installer_path,
                custom_args=custom_args,
            )
            res.update({
                'machine': MACHINE_NAME,
                'requestId': request_id,
                'timestamp': int(time.time() * 1000),
            })
            sio.emit('install_app_result', res)
        except Exception as error:
            sio.emit('install_app_result', {
                'machine': MACHINE_NAME,
                'requestId': request_id,
                'packageId': package_id,
                'installerPath': installer_path,
                'success': False,
                'error': str(error),
                'timestamp': int(time.time() * 1000),
            })

    threading.Thread(target=_worker, daemon=True).start()


@sio.on('install_package')
def on_install_package(data=None):
    on_install_app(data=data)


@sio.on('search_packages')
def on_search_packages(data=None):
    request_id = ''
    query = ''
    if isinstance(data, dict):
        request_id = str(data.get('requestId') or '').strip()
        query = str(data.get('query') or '').strip()

    def _worker():
        try:
            results = search_winget_packages(query)
            sio.emit('search_packages_result', {
                'machine': MACHINE_NAME,
                'requestId': request_id,
                'query': query,
                'packages': results,
                'count': len(results),
                'timestamp': int(time.time() * 1000),
            })
        except Exception as error:
            sio.emit('search_packages_result', {
                'machine': MACHINE_NAME,
                'requestId': request_id,
                'query': query,
                'packages': [],
                'error': str(error),
                'timestamp': int(time.time() * 1000),
            })

    threading.Thread(target=_worker, daemon=True).start()


@sio.on('system_power_action')
def on_system_power_action(data=None):
    request_id = ''
    action = 'restart'
    timeout_seconds = 5
    message = 'Action initiated via Remote Control'

    if isinstance(data, dict):
        request_id = str(data.get('requestId') or '').strip()
        action = str(data.get('action') or 'restart').strip()
        if 'timeout' in data or 'timeoutSeconds' in data:
            timeout_seconds = int(data.get('timeout') or data.get('timeoutSeconds') or 5)
        if 'message' in data:
            message = str(data.get('message') or message).strip()

    try:
        res = execute_system_power(action=action, timeout_seconds=timeout_seconds, message=message)
        res.update({
            'machine': MACHINE_NAME,
            'requestId': request_id,
            'timestamp': int(time.time() * 1000),
        })
        sio.emit('system_power_result', res)
    except Exception as error:
        sio.emit('system_power_result', {
            'machine': MACHINE_NAME,
            'requestId': request_id,
            'action': action,
            'success': False,
            'error': str(error),
            'timestamp': int(time.time() * 1000),
        })


@sio.on('system_restart')
def on_system_restart(data=None):
    payload = data if isinstance(data, dict) else {}
    payload['action'] = 'restart'
    on_system_power_action(data=payload)


if __name__ == "__main__":
    log_error(f"Agent booted. pid={os.getpid()} exe={os.path.abspath(sys.executable)} version={AGENT_VERSION} autoUpdate={AUTO_UPDATE_ENABLED}")
    if getattr(sys, "frozen", False):
        preferred_exe = get_canonical_packaged_exe_path()
        current_exe = os.path.abspath(sys.executable)
        if preferred_exe and os.path.normcase(preferred_exe) != os.path.normcase(current_exe):
            log_error(f"Running from non-canonical executable. current={current_exe} preferred={preferred_exe}")
    if embedded_agent_version_source:
        log_error(f"Agent version source: embedded build version file ({embedded_agent_version_source})")
    elif agent_version_from_env:
        log_error("Agent version source: environment variable AGENT_VERSION")
    elif agent_version_source:
        log_error(f"Agent version source: {agent_version_source}")
    else:
        log_error("Agent version source: default fallback (1.0.0)")
    if AUTO_UPDATE_ENABLED and UPDATE_MANIFEST_URL and getattr(sys, "frozen", False):
        log_error(f"Auto-update enabled. manifest={UPDATE_MANIFEST_URL}")
        threading.Thread(target=update_watchdog_loop, daemon=True).start()
    else:
        log_error("Auto-update disabled or not configured")

    while True:
        try:
            sio.connect(SERVER_URL, wait_timeout=10)
            sio.wait()
        except Exception as e:
            log_error(f"Connection error: {e}")

        time.sleep(RECONNECT_DELAY_SECONDS)
