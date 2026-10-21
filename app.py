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

# ================= KONFIGURACJA SPRZĘTOWA I GITHUB =================
PIR_PIN = 17        # Pin fizyczny 11 - Czujnik ruchu DFRobot SEN0171
SWITCH_PIN = 27     # Pin fizyczny 13 - Przełącznik ON/OFF (bistabilny)
HELP_BTN_PIN = 22   # Pin fizyczny 15 - Rezerwacja pod fizyczny przycisk "Wezwij pomoc"

ARM_DELAY_SEC = 5       # Czas na odejście od kasy po włączeniu (5 sekund)
COOLDOWN_SEC = 60       # Przerwa między kolejnymi powiadomieniami z czujnika (60 sekund)
ACK_PAUSE_SEC = 120     # Czas wyciszenia czujnika po kliknięciu "Idę do kasy" (2 minuty)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(BASE_DIR, "version.txt")
CHANGELOG_FILE = os.path.join(BASE_DIR, "changelog.txt")

GITHUB_REPO_URL = "https://github.com/coffynerd/detektor.git"
GITHUB_RAW_VERSION_URL = "https://raw.githubusercontent.com/coffynerd/detektor/main/version.txt"
GITHUB_RAW_CHANGELOG_URL = "https://raw.githubusercontent.com/coffynerd/detektor/main/changelog.txt"
SERVICE_NAME = "kasa-alarm.service"

# Stały katalog danych (nie kasuje się podczas aktualizacji z GitHuba)
DATA_DIR = "/var/lib/kasa-alarm"
SUBSCRIPTIONS_FILE = os.path.join(DATA_DIR, "push_subscriptions.json")
VAPID_KEYS_FILE = os.path.join(DATA_DIR, "vapid_keys.json")
VAPID_CLAIMS = {"sub": "mailto:admin@twojadomena.pl"}
# ===================================================================

app = Flask(__name__)
state_lock = threading.Lock()
subs_lock = threading.Lock()

system_state = "DISARMED"   # DISARMED | ARMING | ARMED
alert_type = "NONE"         # NONE | MOTION | HELP
responder_name = ""
cooldown_active = False

arm_timer = None
cooldown_timer = None


# --- Nagłówki CORS oraz Private Network Access (dla strony HTTPS / Astro) ---
@app.after_request
def add_cors_and_pna_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


# --- Pomocnicze funkcje wersji i aktualizacji ---
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
        return "1.0.0"


def get_local_changelog():
    try:
        with open(CHANGELOG_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "Brak opisu zmian."


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


# --- Automatyczne generowanie i odczyt kluczy VAPID ---
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


# --- Zarządzanie subskrypcjami Web Push ---
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


# --- Inicjalizacja GPIO ---
pir = MotionSensor(PIR_PIN)
switch = Button(SWITCH_PIN, pull_up=True, bounce_time=0.1)
help_button = Button(HELP_BTN_PIN, pull_up=True, bounce_time=0.1)


def cancel_timers():
    global arm_timer, cooldown_timer, cooldown_active
    if arm_timer is not None:
        arm_timer.cancel()
        arm_timer = None
    if cooldown_timer is not None:
        cooldown_timer.cancel()
        cooldown_timer = None
    cooldown_active = False


def finish_arming():
    global system_state
    with state_lock:
        if system_state == "ARMING":
            system_state = "ARMED"
            print("[SYSTEM] Uzbrojono - aktywne monitorowanie ruchu.")


def reset_cooldown():
    global cooldown_active, alert_type, responder_name
    with state_lock:
        cooldown_active = False
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


def on_motion_detected():
    global alert_type, responder_name, cooldown_active, cooldown_timer
    with state_lock:
        if system_state != "ARMED" or cooldown_active:
            return
        alert_type = "MOTION"
        responder_name = ""
        cooldown_active = True
        print("[ALARM] Wykryto ruch przy kasie!")
        send_push_to_all(
            "🚨 Ruch przy kasie!",
            "Klient czeka przy pustej kasie! Kliknij, aby przyjąć.",
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


switch.when_pressed = on_switch_on
switch.when_released = on_switch_off
pir.when_motion = on_motion_detected
help_button.when_pressed = trigger_help_request

if switch.is_pressed:
    on_switch_on()


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

    with subs_lock:
        subs = load_subscriptions()
        if not any(s.get("endpoint") == sub["endpoint"] for s in subs):
            subs.append(sub)
            save_subscriptions(subs)

    return jsonify({"status": "subscribed"}), 201


@app.route("/api/status", methods=["GET"])
def api_status():
    with state_lock:
        return jsonify({
            "system_state": system_state,
            "alert_type": alert_type,
            "responder_name": responder_name,
            "version": get_local_version(),
            "subscribed_devices": len(load_subscriptions())
        })


@app.route("/api/ack", methods=["POST", "OPTIONS"])
def api_ack():
    if request.method == "OPTIONS":
        return jsonify({"ok": True}), 200

    global responder_name, cooldown_active, cooldown_timer
    data = request.get_json(silent=True) or {}
    worker = data.get("name", "Pracownik").strip() or "Pracownik"

    with state_lock:
        if alert_type != "NONE" and not responder_name:
            responder_name = worker
            if cooldown_timer is not None:
                cooldown_timer.cancel()
            cooldown_active = True
            cooldown_timer = threading.Timer(ACK_PAUSE_SEC, reset_cooldown)
            cooldown_timer.start()
            send_push_to_all(
                "✅ Kasa obsłużona",
                f"{worker} już idzie do kasy!",
                "ACK"
            )
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

    global alert_type, responder_name
    with state_lock:
        alert_type = "NONE"
        responder_name = ""
    return jsonify({"ok": True})


# --- ENDPOINTY AKTUALIZACJI DLA PANELU ADMINA ---

@app.route("/api/update-check", methods=["GET"])
def api_update_check():
    """Sprawdza na GitHubie (version.txt oraz changelog.txt), czy jest nowa wersja."""
    local_ver = get_local_version()
    remote_ver = fetch_github_text(GITHUB_RAW_VERSION_URL)
    remote_changelog = fetch_github_text(GITHUB_RAW_CHANGELOG_URL)

    if not remote_ver:
        return jsonify({
            "ok": False,
            "error": "Brak połączenia z GitHubem lub brak pliku version.txt w repozytorium.",
            "local_version": local_ver
        }), 502

    is_newer = parse_version(remote_ver) > parse_version(local_ver)
    return jsonify({
        "ok": True,
        "local_version": local_ver,
        "remote_version": remote_ver,
        "update_available": is_newer,
        "changelog": remote_changelog or "Brak pliku changelog.txt w repozytorium."
    })


@app.route("/api/update-run", methods=["POST", "OPTIONS"])
def api_update_run():
    """Pobiera nową wersję z GitHuba i restartuje usługę systemd."""
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
    return jsonify({
        "service": "Detektor Kasa API (coffynerd/detektor)",
        "version": get_local_version(),
        "status": "online"
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)