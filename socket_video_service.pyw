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
from datetime import datetime
import sys

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
FPS = 8.0
RECONNECT_DELAY_SECONDS = 5
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
# ---------------------

sio = socketio.Client(reconnection=True, reconnection_attempts=0, reconnection_delay=2, reconnection_delay_max=10)
is_recording = False
is_camera_on = False
recording_lock = threading.Lock()
camera_lock = threading.Lock()
update_lock = threading.Lock()
update_last_checked_at = 0
update_in_progress = False

if not os.path.exists(RECORDING_DIR):
    os.makedirs(RECORDING_DIR)


def emit_agent_state(source=""):
    try:
        sio.emit('agent_state_update', {
            'recording': bool(is_recording),
            'cameraOn': bool(is_camera_on),
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

    if not version:
        raise RuntimeError("Update manifest missing 'version'")
    if not download_url:
        raise RuntimeError("Update manifest missing 'url'")

    return {
        'version': version,
        'url': download_url,
        'sha256': sha256,
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


def download_update_binary(download_url, expected_sha256=""):
    updates_dir = os.path.join(os.getenv("APPDATA", BASE_DIR), "RemoteAgent", "updates")
    os.makedirs(updates_dir, exist_ok=True)

    temp_file = os.path.join(updates_dir, f"RemoteAgent_update_{int(time.time())}.exe")
    with requests.get(download_url, stream=True, timeout=(15, 180)) as response:
        response.raise_for_status()
        with open(temp_file, "wb") as file:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    file.write(chunk)

    if expected_sha256:
        actual_sha256 = compute_sha256(temp_file)
        if actual_sha256 != expected_sha256:
            try:
                os.remove(temp_file)
            except OSError:
                pass
            raise RuntimeError(f"SHA256 mismatch. expected={expected_sha256} actual={actual_sha256}")

    return temp_file


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

        downloaded_file = download_update_binary(manifest['url'], manifest.get('sha256', ""))
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

def upload_to_cloudinary(file_path):
    """Uploads the video and sends the URL back to the server."""
    if not CLOUDINARY_READY:
        message = "Cloudinary credentials missing. Skipping upload."
        print(message)
        log_error(message)
        return

    try:
        print(f"Uploading {file_path} to Cloudinary...")
        log_error(f"Uploading to Cloudinary: {file_path}")
        response = cloudinary.uploader.upload(file_path, resource_type="video")
        video_url = build_playable_video_url(response) or response.get("secure_url")
        print(f"Upload Success: {video_url}")
        log_error(f"Upload success: {video_url}")
        
        # Notify the backend that a new video is ready
        sio.emit('video_upload_complete', {'url': video_url, 'machine': MACHINE_NAME})
        
        # Cleanup local file to save space
        os.remove(file_path)
        log_error(f"Local file removed after upload: {file_path}")
    except Exception as e:
        print(f"Cloudinary Error: {e}")
        log_error(f"Cloudinary Error: {e}")

def record_loop():
    global is_recording
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    file_path = os.path.join(RECORDING_DIR, f"rec_{timestamp}.mp4")
    out = None
    should_upload = False

    try:
        screen_size = pyautogui.size()
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter(file_path, fourcc, FPS, screen_size)
        if not out.isOpened():
            log_error(f"VideoWriter failed to open: {file_path}")
            return

        should_upload = True
        log_error(f"Recording started: {file_path}")
        while is_recording:
            img = pyautogui.screenshot()
            frame = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
            out.write(frame)
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
        if should_upload and os.path.exists(file_path):
            threading.Thread(target=upload_to_cloudinary, args=(file_path,), daemon=True).start()
        else:
            log_error(f"Upload skipped, recording file missing: {file_path}")

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
    threading.Thread(target=check_for_agent_updates, kwargs={'force': True, 'source': 'connect'}, daemon=True).start()


@sio.event
def disconnect():
    log_error("Disconnected from server")

@sio.on('start_capture')
def on_start(data=None):
    global is_recording
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


@sio.on('force_update_check')
def on_force_update_check(data=None):
    log_error("force_update_check received")
    threading.Thread(target=check_for_agent_updates, kwargs={'force': True, 'source': 'admin'}, daemon=True).start()

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
