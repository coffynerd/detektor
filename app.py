#!/usr/bin/env python3
import os
import time
import json
import base64
import threading
import subprocess
import urllib.request
from flask import Flask, jsonify, request
from gpiozero import MotionSensor, Button
from pywebpush import webpush, WebPushException
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization

# ================= KONFIGURACJA SPRZĘTOWA I DETEKCJI (v1.0.12) =================
PIR_PIN = 17        # Pin fizyczny 11 - Czujnik ruchu DFRobot SEN0171
SWITCH_PIN = 27     # Pin fizyczny 13 - Przełącznik ON/OFF (bistabilny)
HELP_BTN_PIN = 22   # Pin fizyczny 15 - Rezerwacja pod fizyczny przycisk "Wezwij pomoc"

ARM_DELAY_SEC = 8           # Czas na odejście od kasy po włączeniu przełącznika (8 sekund)
COOLDOWN_SEC = 60           # Przerwa między kolejnymi powiadomieniami z czujnika (60 sekund)
ACK_PAUSE_SEC = 120         # Czas wyciszenia czujnika po kliknięciu "Idę do kasy" (2 minuty)

# Nowe podejście v1.0.12: Detekcja zbocza (0 -> 1) + Cisza Radiowa Wi-Fi
SAMPLE_TICK_SEC = 0.25          # Próbkowanie co 0.25s
QUIET_CONFIRM_SEC = 1.5         # Wymagane 1.5s ciszy (LOW) przed uzbrojeniem detekcji zbocza
TRIGGER_HOLD_SEC = 1.5          # 1.5s ciągłego sygnału po zboczu wyzwala alarm (łapie pojedyncze podejście!)
STUCK_HIGH_TIMEOUT_SEC = 12.0   # Jeśli czujnik wisi na HIGH > 12s, traktuj jako zakłócenie tła
MOTION_REQUIRED_HITS = 3        # Skala dla paska postępu w UI (0/3 .. 3/3)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(BASE_DIR, "version.txt")
CHANGELOG_FILE = os.path.join(BASE_DIR, "changelog.txt")

GITHUB_REPO_URL = "https://github.com/coffynerd/detektor.git"
GITHUB_RAW_VERSION_URL = "https://raw.githubusercontent.com/coffynerd/detektor/main/version.txt"
GITHUB_RAW_CHANGELOG_URL = "https://raw.githubusercontent.com/coffynerd/detektor/main/changelog.txt"
SERVICE_NAME = "kasa-alarm.service"
TUNNEL_SERVICE_NAME = "kasa-tunnel.service"

DATA_DIR = "/var/lib/kasa-alarm"
SUBSCRIPTIONS_FILE = os.path.join(DATA_DIR, "push_subscriptions.json")
VAPID_KEYS_FILE = os.path.join(DATA_DIR, "vapid_keys.json")
PUBLIC_URL_FILE = os.path.join(DATA_DIR, "public_url.txt")
CLOUD_CONFIG_FILE = os.path.join(DATA_DIR, "cloud_config.json")
VAPID_CLAIMS = {"sub": "mailto:admin@twojadomena.pl"}
# ===============================================================================

app = Flask(__name__)
state_lock = threading.Lock()
subs_lock = threading.Lock()
relay_wake_event = threading.Event()

system_state = "DISARMED"   # DISARMED | ARMING | ARMED
alert_type = "NONE"         # NONE | MOTION | HELP
responder_name = ""
cooldown_active = False

sensor_ready = False        # True dopiero gdy czujnik miał min. 1.5s ciszy (stanu LOW)
current_pulse_sec = 0.0     # Czas trwania bieżącego impulsu po zboczu 0 -> 1
ui_motion_hits = 0

arm_timer = None
cooldown_timer = None

cached_remote_version = ""
cached_remote_changelog = ""
last_github_check_ts = 0.0


@app.after_request
def add_cors_and_pna_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, Authorization, ngrok-skip-browser-warning, Bypass-Tunnel-Reminder"
    )
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


def parse_version(v_str):
    try:
        return tuple(int(x) for x in v_str.strip().split("."))
    except Exception:
        return (0, 0, 0)


def get_local_version():
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "1.0.12"


def get_local_changelog():
    try:
        with open(CHANGELOG_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "Brak opisu zmian."


def load_cloud_config():
    default_cfg = {"lte_enabled": False, "firebase_db_url": ""}
    try:
        if os.path.exists(CLOUD_CONFIG_FILE):
            with open(CLOUD_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {
                    "lte_enabled": bool(data.get("lte_enabled", False)),
                    "firebase_db_url": str(data.get("firebase_db_url", "")).strip()
                }
    except Exception:
        pass
    return default_cfg


def save_cloud_config(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CLOUD_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def get_public_tunnel_url():
    cfg = load_cloud_config()
    if not cfg.get("lte_enabled"):
        return ""
    try:
        if os.path.exists(PUBLIC_URL_FILE):
            with open(PUBLIC_URL_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""


def fetch_github_text(raw_url):
    try:
        url = f"{raw_url}?t={int(time.time())}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache"
            }
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.read().decode("utf-8").strip()
    except Exception as e:
        print(f"[GITHUB FETCH ERROR] {raw_url}: {e}")
        return None


def refresh_github_info(force=False):
    global cached_remote_version, cached_remote_changelog, last_github_check_ts
    now = time.time()
    if not force and cached_remote_version and (now - last_github_check_ts < 600):
        return cached_remote_version, cached_remote_changelog

    rem_ver = fetch_github_text(GITHUB_RAW_VERSION_URL)
    rem_log = fetch_github_text(GITHUB_RAW_CHANGELOG_URL)
    if rem_ver:
        cached_remote_version = rem_ver
    if rem_log:
        cached_remote_changelog = rem_log
    last_github_check_ts = now
    return cached_remote_version, cached_remote_changelog


def perform_self_update():
    if os.path.exists(os.path.join(BASE_DIR, ".git")):
        cmd = f"cd {BASE_DIR} && git fetch --all && git reset --hard origin/main"
    else:
        cmd = f"rm -rf {BASE_DIR} && git clone {GITHUB_REPO_URL} {BASE_DIR}"

    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        return False, res.stderr

    def _restart_service():
        time.sleep(1.5)
        subprocess.run(f"systemctl restart {SERVICE_NAME}", shell=True)

    threading.Thread(target=_restart_service, daemon=True).start()
    return True, get_local_version()


def http_json_request(method, url, payload=None, timeout=3.0):
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8").strip()
        return resp.status, (json.loads(raw) if raw else None)


def init_vapid_keys():
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(VAPID_KEYS_FILE):
        try:
            with open(VAPID_KEYS_FILE, "r", encoding="utf-8") as f:
                keys = json.load(f)
                if "publicKey" in keys and "privateKey" in keys:
                    return keys["publicKey"], keys["privateKey"]
        except Exception as e:
            print(f"[BŁĄD ODCZYTU KLUCZY VAPID] {e}")

    private_key = ec.generate_private_key(ec.SECP256R1())
    raw_priv = private_key.private_numbers().private_value.to_bytes(32, "big")
    raw_pub = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint
    )

    priv_b64 = base64.urlsafe_b64encode(raw_priv).rstrip(b"=").decode("utf-8")
    pub_b64 = base64.urlsafe_b64encode(raw_pub).rstrip(b"=").decode("utf-8")

    with open(VAPID_KEYS_FILE, "w", encoding="utf-8") as f:
        json.dump({"publicKey": pub_b64, "privateKey": priv_b64}, f, indent=2)

    return pub_b64, priv_b64


VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY = init_vapid_keys()


def load_subscriptions():
    if not os.path.exists(SUBSCRIPTIONS_FILE):
        return []
    try:
        with open(SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_subscriptions(subs):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SUBSCRIPTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(subs, f, indent=2)


def add_subscription_entry(sub):
    if not sub or "endpoint" not in sub:
        return len(load_subscriptions())
    with subs_lock:
        subs = load_subscriptions()
        if not any(s.get("endpoint") == sub["endpoint"] for s in subs):
            subs.append(sub)
            save_subscriptions(subs)
        return len(subs)


def remove_subscription_entry(endpoint):
    if not endpoint:
        return len(load_subscriptions())
    with subs_lock:
        subs = load_subscriptions()
        new_subs = [s for s in subs if s.get("endpoint") != endpoint]
        if len(new_subs) != len(subs):
            save_subscriptions(new_subs)
        return len(new_subs)


def send_push_to_all(title, body, push_alert_type="MOTION"):
    def _send_worker():
        with subs_lock:
            subs = load_subscriptions()
            if not subs:
                return
            valid_subs = []
            payload = json.dumps({
                "title": title,
                "body": body,
                "alert_type": push_alert_type,
                "url": "/user/kasa"
            })

            for sub in subs:
                try:
                    webpush(
                        subscription_info=sub,
                        data=payload,
                        vapid_private_key=VAPID_PRIVATE_KEY,
                        vapid_claims=VAPID_CLAIMS,
                        ttl=60
                    )
                    valid_subs.append(sub)
                except WebPushException as ex:
                    if ex.response is not None and ex.response.status_code in [404, 410]:
                        continue
                    valid_subs.append(sub)
                except Exception as e:
                    print(f"[BŁĄD WYSYŁKI WEB PUSH] {e}")
                    valid_subs.append(sub)

            if len(valid_subs) != len(subs):
                save_subscriptions(valid_subs)

    threading.Thread(target=_send_worker, daemon=True).start()


pir = MotionSensor(PIR_PIN)
switch = Button(SWITCH_PIN, pull_up=True, bounce_time=0.1)
help_button = Button(HELP_BTN_PIN, pull_up=True, bounce_time=0.1)


def cancel_timers():
    global arm_timer, cooldown_timer, cooldown_active, sensor_ready, current_pulse_sec, ui_motion_hits
    if arm_timer is not None:
        arm_timer.cancel()
        arm_timer = None
    if cooldown_timer is not None:
        cooldown_timer.cancel()
        cooldown_timer = None
    cooldown_active = False
    sensor_ready = False
    current_pulse_sec = 0.0
    ui_motion_hits = 0


def finish_arming():
    global system_state, sensor_ready, current_pulse_sec, ui_motion_hits
    with state_lock:
        if system_state == "ARMING":
            sensor_ready = not pir.motion_detected
            current_pulse_sec = 0.0
            ui_motion_hits = 0
            system_state = "ARMED"
            print(f"[SYSTEM] Uzbrojono (v1.0.12) | Stan początkowy PIR: {'HIGH (czekam na opadnięcie)' if not sensor_ready else 'LOW (gotowy)'}")
    relay_wake_event.set()


def reset_cooldown():
    global cooldown_active, alert_type, responder_name, sensor_ready, current_pulse_sec, ui_motion_hits
    with state_lock:
        cooldown_active = False
        sensor_ready = not pir.motion_detected
        current_pulse_sec = 0.0
        ui_motion_hits = 0
        if alert_type == "MOTION":
            alert_type = "NONE"
            responder_name = ""
    relay_wake_event.set()


def on_switch_on():
    global system_state, arm_timer
    with state_lock:
        cancel_timers()
        system_state = "ARMING"
        print(f"[SYSTEM] Włączono przełącznik - odliczanie {ARM_DELAY_SEC}s...")
        arm_timer = threading.Timer(ARM_DELAY_SEC, finish_arming)
        arm_timer.start()
    relay_wake_event.set()


def on_switch_off():
    global system_state, alert_type, responder_name
    with state_lock:
        cancel_timers()
        system_state = "DISARMED"
        if alert_type == "MOTION":
            alert_type = "NONE"
            responder_name = ""
        print("[SYSTEM] Wyłączono przełącznik - system rozbrojony natychmiast.")
    relay_wake_event.set()


def trigger_help_request():
    global alert_type, responder_name
    with state_lock:
        alert_type = "HELP"
        responder_name = ""
        print("[POMOC] Kasjer wzywa drugiego pracownika do pomocy!")
        send_push_to_all(
            "🆘 Wezwanie do kasy!",
            "Kasjer prosi o wsparcie przy kasie!",
            "HELP"
        )
    relay_wake_event.set()


def handle_ack_action(worker_name):
    global responder_name, cooldown_active, cooldown_timer, ui_motion_hits
    worker = (worker_name or "Pracownik").strip() or "Pracownik"
    with state_lock:
        if alert_type != "NONE" and not responder_name:
            responder_name = worker
            if cooldown_timer is not None:
                cooldown_timer.cancel()
            cooldown_active = True
            ui_motion_hits = 0
            cooldown_timer = threading.Timer(ACK_PAUSE_SEC, reset_cooldown)
            cooldown_timer.start()
            send_push_to_all(
                "✅ Kasa obsłużona",
                f"{worker} już idzie do kasy!",
                "ACK"
            )
    relay_wake_event.set()


def handle_clear_action():
    global alert_type, responder_name, ui_motion_hits
    with state_lock:
        alert_type = "NONE"
        responder_name = ""
        ui_motion_hits = 0
    relay_wake_event.set()


def motion_monitor_loop():
    """
    Detekcja Zbocza 0 -> 1 (v1.0.12):
    1. Wymaga, aby czujnik najpierw był w stanie LOW (sensor_ready = True).
       Dzięki temu zawieszony stan HIGH przy starcie nigdy nie wywoła fałszywego alarmu.
    2. Gdy czujnik przejdzie z LOW na HIGH i utrzyma sygnał przez TRIGGER_HOLD_SEC (1.5s),
       natychmiast uruchamia alarm (wystarczy samo podejście klienta i zatrzymanie się).
    """
    global alert_type, responder_name, cooldown_active, cooldown_timer
    global sensor_ready, current_pulse_sec, ui_motion_hits

    low_sec = 0.0
    high_sec = 0.0

    while True:
        time.sleep(SAMPLE_TICK_SEC)
        is_high = pir.motion_detected

        with state_lock:
            if system_state != "ARMED" or cooldown_active:
                low_sec = 0.0
                high_sec = 0.0
                current_pulse_sec = 0.0
                ui_motion_hits = 0
                continue

            if not sensor_ready:
                # Czekamy, aż czujnik zejdzie na LOW przez min. QUIET_CONFIRM_SEC (1.5s)
                if not is_high:
                    low_sec += SAMPLE_TICK_SEC
                    if low_sec >= QUIET_CONFIRM_SEC:
                        sensor_ready = True
                        high_sec = 0.0
                        print("[SYSTEM] Czujnik PIR osiągnął stan spoczynku (LOW) - gotowy na wejście klienta.")
                        relay_wake_event.set()
                else:
                    low_sec = 0.0
                continue

            # Czujnik był w stanie spoczynku i jest gotowy na zbocze 0 -> 1
            if is_high:
                high_sec += SAMPLE_TICK_SEC
                current_pulse_sec = high_sec

                # Zabezpieczenie: gdyby czujnik zablokował się sprzętowo powyżej 12s
                if high_sec > STUCK_HIGH_TIMEOUT_SEC:
                    sensor_ready = False
                    low_sec = 0.0
                    high_sec = 0.0
                    ui_motion_hits = 0
                    continue

                ratio = min(1.0, high_sec / TRIGGER_HOLD_SEC)
                new_hits = int(round(ratio * MOTION_REQUIRED_HITS))
                if new_hits != ui_motion_hits:
                    ui_motion_hits = new_hits
                    print(f"[DETEKTOR] Impuls wejścia klienta: {high_sec:.2f}s / {TRIGGER_HOLD_SEC:.2f}s ({ui_motion_hits}/{MOTION_REQUIRED_HITS})")

                if high_sec >= TRIGGER_HOLD_SEC:
                    high_sec = 0.0
                    current_pulse_sec = 0.0
                    ui_motion_hits = 0
                    sensor_ready = False
                    alert_type = "MOTION"
                    responder_name = ""
                    cooldown_active = True
                    print("[ALARM] Wykryto podejście klienta do kasy! Wysyłam powiadomienia.")
                    send_push_to_all(
                        "🚨 Klient czeka przy kasie!",
                        "Wykryto podejście klienta do pustej kasy. Kliknij, aby przyjąć.",
                        "MOTION"
                    )
                    cooldown_timer = threading.Timer(COOLDOWN_SEC, reset_cooldown)
                    cooldown_timer.start()
                    relay_wake_event.set()
            else:
                if high_sec > 0:
                    high_sec = 0.0
                    current_pulse_sec = 0.0
                    ui_motion_hits = 0


def firebase_relay_loop():
    """
    Tryb Ciszy Radiowej podczas czuwania (v1.0.12):
    Gdy system czuwa (ARMED i brak alarmu), synchronizuje się z Firebase rzadko (co 20s)
    lub natychmiast po wykryciu zdarzenia (relay_wake_event).
    Eliminuje to skoki napięcia od Wi-Fi/SSL co 2.5s, które wzbudzały czujnik PIR!
    """
    while True:
        with state_lock:
            is_idle_armed = (system_state == "ARMED" and alert_type == "NONE")

        wait_timeout = 20.0 if is_idle_armed else 3.0
        relay_wake_event.wait(timeout=wait_timeout)
        relay_wake_event.clear()

        cfg = load_cloud_config()
        if not cfg.get("lte_enabled"):
            continue
        db_url = cfg.get("firebase_db_url", "").rstrip("/")
        if not db_url:
            continue

        try:
            rem_ver, rem_log = refresh_github_info(force=False)
            loc_ver = get_local_version()
            now = time.time()

            with state_lock:
                status_payload = {
                    "system_state": system_state,
                    "alert_type": alert_type,
                    "responder_name": responder_name,
                    "motion_hits": ui_motion_hits,
                    "motion_required": MOTION_REQUIRED_HITS,
                    "pir_raw": pir.motion_detected,
                    "sensor_ready": sensor_ready,
                    "version": loc_ver,
                    "remote_version": rem_ver or loc_ver,
                    "update_available": parse_version(rem_ver) > parse_version(loc_ver) if rem_ver else False,
                    "changelog": rem_log or get_local_changelog(),
                    "subscribed_devices": len(load_subscriptions()),
                    "vapid_public_key": VAPID_PUBLIC_KEY,
                    "lte_enabled": True,
                    "public_url": get_public_tunnel_url(),
                    "last_updated": int(now * 1000)
                }

            http_json_request("PUT", f"{db_url}/kasa_relay/status.json", status_payload, timeout=3.0)

            code, cmd_data = http_json_request("GET", f"{db_url}/kasa_relay/command.json", timeout=3.0)
            if code == 200 and isinstance(cmd_data, dict) and cmd_data.get("action"):
                action = cmd_data.get("action")
                http_json_request("DELETE", f"{db_url}/kasa_relay/command.json", timeout=3.0)

                if action == "ACK":
                    handle_ack_action(cmd_data.get("name", "Pracownik"))
                elif action == "HELP":
                    trigger_help_request()
                elif action == "CLEAR":
                    handle_clear_action()
                elif action == "SUBSCRIBE":
                    add_subscription_entry(cmd_data.get("subscription"))
                elif action == "UNSUBSCRIBE":
                    remove_subscription_entry(cmd_data.get("endpoint"))
                elif action == "UPDATE_CHECK":
                    refresh_github_info(force=True)
                elif action == "UPDATE_RUN":
                    perform_self_update()
        except Exception as e:
            print(f"[FIREBASE RELAY WARN] {e}")


switch.when_pressed = on_switch_on
switch.when_released = on_switch_off
help_button.when_pressed = trigger_help_request

if switch.is_pressed:
    on_switch_on()

threading.Thread(target=motion_monitor_loop, daemon=True).start()
threading.Thread(target=firebase_relay_loop, daemon=True).start()


# ================= ENDPOINTY REST API =================

@app.route("/api/vapid-public-key", methods=["GET"])
def get_vapid_public_key():
    return jsonify({"publicKey": VAPID_PUBLIC_KEY})


@app.route("/api/subscribe", methods=["POST", "OPTIONS"])
def subscribe_push():
    if request.method == "OPTIONS":
        return jsonify({"ok": True}), 200

    sub = request.get_json(silent=True)
    if not sub or "endpoint" not in sub:
        return jsonify({"error": "Invalid subscription"}), 400

    total = add_subscription_entry(sub)
    return jsonify({"status": "subscribed", "subscribed_devices": total}), 201


@app.route("/api/unsubscribe", methods=["POST", "OPTIONS"])
def unsubscribe_push():
    if request.method == "OPTIONS":
        return jsonify({"ok": True}), 200

    data = request.get_json(silent=True) or {}
    endpoint = data.get("endpoint")
    if not endpoint:
        return jsonify({"error": "Missing endpoint"}), 400

    total = remove_subscription_entry(endpoint)
    return jsonify({"status": "unsubscribed", "subscribed_devices": total}), 200


@app.route("/api/status", methods=["GET"])
def api_status():
    cfg = load_cloud_config()
    with state_lock:
        return jsonify({
            "system_state": system_state,
            "alert_type": alert_type,
            "responder_name": responder_name,
            "motion_hits": ui_motion_hits,
            "motion_required": MOTION_REQUIRED_HITS,
            "pir_raw": pir.motion_detected,
            "sensor_ready": sensor_ready,
            "version": get_local_version(),
            "subscribed_devices": len(load_subscriptions()),
            "vapid_public_key": VAPID_PUBLIC_KEY,
            "lte_enabled": cfg.get("lte_enabled", False),
            "public_url": get_public_tunnel_url()
        })


@app.route("/api/lte-mode", methods=["GET", "POST", "OPTIONS"])
def api_lte_mode():
    if request.method == "OPTIONS":
        return jsonify({"ok": True}), 200

    cfg = load_cloud_config()
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        enabled = bool(data.get("lte_enabled", False))
        cfg["lte_enabled"] = enabled
        if "firebase_db_url" in data:
            cfg["firebase_db_url"] = str(data["firebase_db_url"]).strip()
        save_cloud_config(cfg)

        if not enabled:
            subprocess.run(f"systemctl stop {TUNNEL_SERVICE_NAME} 2>/dev/null", shell=True)
            subprocess.run(f"systemctl disable {TUNNEL_SERVICE_NAME} 2>/dev/null", shell=True)

    relay_wake_event.set()
    return jsonify({"ok": True, "lte_enabled": cfg.get("lte_enabled", False)})


@app.route("/api/ack", methods=["POST", "OPTIONS"])
def api_ack():
    if request.method == "OPTIONS":
        return jsonify({"ok": True}), 200

    data = request.get_json(silent=True) or {}
    handle_ack_action(data.get("name", "Pracownik"))
    return jsonify({"ok": True})


@app.route("/api/help", methods=["POST", "GET", "OPTIONS"])
def api_help():
    if request.method == "OPTIONS":
        return jsonify({"ok": True}), 200
    trigger_help_request()
    return jsonify({"ok": True})


@app.route("/api/clear", methods=["POST", "OPTIONS"])
def api_clear():
    if request.method == "OPTIONS":
        return jsonify({"ok": True}), 200
    handle_clear_action()
    return jsonify({"ok": True})


@app.route("/api/update-check", methods=["GET"])
def api_update_check():
    local_ver = get_local_version()
    remote_ver, remote_changelog = refresh_github_info(force=True)

    if not remote_ver:
        return jsonify({
            "ok": False,
            "error": "Brak połączenia z GitHubem lub brak pliku version.txt.",
            "local_version": local_ver
        }), 502

    is_newer = parse_version(remote_ver) > parse_version(local_ver)
    return jsonify({
        "ok": True,
        "local_version": local_ver,
        "remote_version": remote_ver,
        "update_available": is_newer,
        "changelog": remote_changelog or "Brak pliku changelog.txt."
    })


@app.route("/api/update-run", methods=["POST", "OPTIONS"])
def api_update_run():
    if request.method == "OPTIONS":
        return jsonify({"ok": True}), 200

    try:
        ok, info = perform_self_update()
        if not ok:
            return jsonify({"ok": False, "error": info}), 500

        return jsonify({
            "ok": True,
            "status": "restarting",
            "new_version": info,
            "changelog": get_local_changelog()
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/")
def index():
    cfg = load_cloud_config()
    return jsonify({
        "service": "Detektor Kasa API (coffynerd/detektor)",
        "version": get_local_version(),
        "status": "online",
        "lte_enabled": cfg.get("lte_enabled", False)
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
