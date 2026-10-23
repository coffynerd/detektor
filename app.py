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

# ================= KONFIGURACJA SPRZĘTOWA I FILTRU RUCHU =================
PIR_PIN = 17        # Pin fizyczny 11 - Czujnik ruchu DFRobot SEN0171
SWITCH_PIN = 27     # Pin fizyczny 13 - Przełącznik ON/OFF (bistabilny)
HELP_BTN_PIN = 22   # Pin fizyczny 15 - Rezerwacja pod fizyczny przycisk "Wezwij pomoc"

ARM_DELAY_SEC = 5           # Czas na odejście od kasy po włączeniu (5 sekund)
COOLDOWN_SEC = 60           # Przerwa między kolejnymi powiadomieniami z czujnika (60 sekund)
ACK_PAUSE_SEC = 120         # Czas wyciszenia czujnika po kliknięciu "Idę do kasy" (2 minuty)

MOTION_WINDOW_SEC = 20.0      # Okno czasowe pamiętania próbek (20 sekund)
MOTION_REQUIRED_HITS = 3      # Wymagana liczba potwierdzeń ruchu (3 razy)
MOTION_SAMPLE_INTERVAL = 4.5  # Odstęp między kolejnymi sprawdzeniami (4.5 sekundy)

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
# =========================================================================

app = Flask(__name__)
state_lock = threading.Lock()
subs_lock = threading.Lock()

system_state = "DISARMED"   # DISARMED | ARMING | ARMED
alert_type = "NONE"         # NONE | MOTION | HELP
responder_name = ""
cooldown_active = False

motion_timestamps = []
last_sample_time = 0.0

arm_timer = None
cooldown_timer = None


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
        return "1.0.6"


def get_local_changelog():
    try:
        with open(CHANGELOG_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "Brak opisu zmian."


def load_cloud_config():
    """Domyślnie tryb LTE jest WYŁĄCZONY (lte_enabled: False)."""
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

    print("[VAPID] Generowanie nowej pary kluczy Web Push (ECDSA P-256)...")
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
            print(f"[WEB PUSH] Dodano subskrypcję (łącznie: {len(subs)})")
        return len(subs)


def remove_subscription_entry(endpoint):
    if not endpoint:
        return len(load_subscriptions())
    with subs_lock:
        subs = load_subscriptions()
        new_subs = [s for s in subs if s.get("endpoint") != endpoint]
        if len(new_subs) != len(subs):
            save_subscriptions(new_subs)
            print(f"[WEB PUSH] Usunięto subskrypcję (pozostało: {len(new_subs)})")
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
    global arm_timer, cooldown_timer, cooldown_active, motion_timestamps
    if arm_timer is not None:
        arm_timer.cancel()
        arm_timer = None
    if cooldown_timer is not None:
        cooldown_timer.cancel()
        cooldown_timer = None
    cooldown_active = False
    motion_timestamps.clear()


def finish_arming():
    global system_state, motion_timestamps
    with state_lock:
        if system_state == "ARMING":
            motion_timestamps.clear()
            system_state = "ARMED"
            print("[SYSTEM] Uzbrojono - aktywne monitorowanie ruchu (v1.0.6).")


def reset_cooldown():
    global cooldown_active, alert_type, responder_name, motion_timestamps
    with state_lock:
        cooldown_active = False
        motion_timestamps.clear()
        if alert_type == "MOTION":
            alert_type = "NONE"
            responder_name = ""


def on_switch_on():
    global system_state, arm_timer
    with state_lock:
        cancel_timers()
        system_state = "ARMING"
        print(f"[SYSTEM] Włączono przełącznik - odliczanie {ARM_DELAY_SEC}s...")
        arm_timer = threading.Timer(ARM_DELAY_SEC, finish_arming)
        arm_timer.start()


def on_switch_off():
    global system_state, alert_type, responder_name
    with state_lock:
        cancel_timers()
        system_state = "DISARMED"
        if alert_type == "MOTION":
            alert_type = "NONE"
            responder_name = ""
        print("[SYSTEM] Wyłączono przełącznik - system rozbrojony natychmiast.")


def register_motion_hit():
    global alert_type, responder_name, cooldown_active, cooldown_timer, motion_timestamps, last_sample_time
    now = time.time()

    with state_lock:
        if system_state != "ARMED" or cooldown_active:
            return

        if now - last_sample_time < MOTION_SAMPLE_INTERVAL:
            return

        last_sample_time = now
        motion_timestamps = [t for t in motion_timestamps if now - t <= MOTION_WINDOW_SEC]
        motion_timestamps.append(now)

        hits = len(motion_timestamps)
        print(f"[DETEKTOR] Wykryto ruch przy kasie ({hits}/{MOTION_REQUIRED_HITS})")

        if hits >= MOTION_REQUIRED_HITS:
            motion_timestamps.clear()
            alert_type = "MOTION"
            responder_name = ""
            cooldown_active = True
            print("[ALARM] Potwierdzono obecność klienta przy kasie (3/3)! Wysyłam powiadomienia.")
            send_push_to_all(
                "🚨 Klient czeka przy kasie!",
                "Wykryto stałą obecność przy pustej kasie. Kliknij, aby przyjąć.",
                "MOTION"
            )
            cooldown_timer = threading.Timer(COOLDOWN_SEC, reset_cooldown)
            cooldown_timer.start()


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


def handle_ack_action(worker_name):
    global responder_name, cooldown_active, cooldown_timer
    worker = (worker_name or "Pracownik").strip() or "Pracownik"
    with state_lock:
        if alert_type != "NONE" and not responder_name:
            responder_name = worker
            if cooldown_timer is not None:
                cooldown_timer.cancel()
            cooldown_active = True
            motion_timestamps.clear()
            cooldown_timer = threading.Timer(ACK_PAUSE_SEC, reset_cooldown)
            cooldown_timer.start()
            send_push_to_all(
                "✅ Kasa obsłużona",
                f"{worker} już idzie do kasy!",
                "ACK"
            )


def motion_monitor_loop():
    while True:
        time.sleep(0.5)
        if system_state == "ARMED" and not cooldown_active and pir.motion_detected:
            register_motion_hit()


def firebase_relay_loop():
    """Wątek LTE / Firebase działa TYLKO wtedy, gdy lte_enabled == True."""
    while True:
        time.sleep(2.5)
        cfg = load_cloud_config()
        if not cfg.get("lte_enabled"):
            continue
        db_url = cfg.get("firebase_db_url", "").rstrip("/")
        if not db_url:
            continue

        try:
            now = time.time()
            with state_lock:
                active_hits = [t for t in motion_timestamps if now - t <= MOTION_WINDOW_SEC]
                status_payload = {
                    "system_state": system_state,
                    "alert_type": alert_type,
                    "responder_name": responder_name,
                    "motion_hits": len(active_hits),
                    "motion_required": MOTION_REQUIRED_HITS,
                    "version": get_local_version(),
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
                    with state_lock:
                        global alert_type, responder_name
                        alert_type = "NONE"
                        responder_name = ""
                        motion_timestamps.clear()
                elif action == "SUBSCRIBE":
                    add_subscription_entry(cmd_data.get("subscription"))
                elif action == "UNSUBSCRIBE":
                    remove_subscription_entry(cmd_data.get("endpoint"))
        except Exception as e:
            print(f"[FIREBASE RELAY WARN] {e}")


switch.when_pressed = on_switch_on
switch.when_released = on_switch_off
pir.when_motion = register_motion_hit
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
    now = time.time()
    cfg = load_cloud_config()
    with state_lock:
        active_hits = [t for t in motion_timestamps if now - t <= MOTION_WINDOW_SEC]
        return jsonify({
            "system_state": system_state,
            "alert_type": alert_type,
            "responder_name": responder_name,
            "motion_hits": len(active_hits),
            "motion_required": MOTION_REQUIRED_HITS,
            "version": get_local_version(),
            "subscribed_devices": len(load_subscriptions()),
            "vapid_public_key": VAPID_PUBLIC_KEY,
            "lte_enabled": cfg.get("lte_enabled", False),
            "public_url": get_public_tunnel_url()
        })


@app.route("/api/lte-mode", methods=["GET", "POST", "OPTIONS"])
def api_lte_mode():
    """Pozwala włączyć lub całkowicie wyłączyć tryb LTE z poziomu Panelu Admina."""
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

    global alert_type, responder_name, motion_timestamps
    with state_lock:
        alert_type = "NONE"
        responder_name = ""
        motion_timestamps.clear()
    return jsonify({"ok": True})


@app.route("/api/update-check", methods=["GET"])
def api_update_check():
    local_ver = get_local_version()
    remote_ver = fetch_github_text(GITHUB_RAW_VERSION_URL)
    remote_changelog = fetch_github_text(GITHUB_RAW_CHANGELOG_URL)

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
        if os.path.exists(os.path.join(BASE_DIR, ".git")):
            cmd = f"cd {BASE_DIR} && git fetch --all && git reset --hard origin/main"
        else:
            cmd = f"rm -rf {BASE_DIR} && git clone {GITHUB_REPO_URL} {BASE_DIR}"

        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            return jsonify({"ok": False, "error": res.stderr}), 500

        new_ver = get_local_version()

        def _restart_service():
            time.sleep(1.5)
            subprocess.run(f"systemctl restart {SERVICE_NAME}", shell=True)

        threading.Thread(target=_restart_service, daemon=True).start()

        return jsonify({
            "ok": True,
            "status": "restarting",
            "new_version": new_ver,
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
