#!/usr/bin/env python3
import os
import threading
import requests
from flask import Flask, render_template_string, jsonify, request
from gpiozero import MotionSensor, Button

# ================= KONFIGURACJA =================
PIR_PIN = 17        # Pin fizyczny 11 - Czujnik ruchu DFRobot SEN0171
SWITCH_PIN = 27     # Pin fizyczny 13 - Przełącznik ON/OFF (bistabilny)
HELP_BTN_PIN = 22   # Pin fizyczny 15 - Rezerwacja pod fizyczny przycisk "Wezwij pomoc"

ARM_DELAY_SEC = 5       # Czas na odejście od kasy po włączeniu (5 sekund)
COOLDOWN_SEC = 60       # Przerwa między kolejnymi powiadomieniami z czujnika (60 sekund)
ACK_PAUSE_SEC = 120     # Czas wyciszenia czujnika po kliknięciu "Idę do kasy" (2 minuty)

# Nazwa kanału w darmowej aplikacji ntfy (Android / iOS)
NTFY_TOPIC = "coffynerd-detektor-kasa"
VERSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "version.txt")
# ================================================

app = Flask(__name__)
@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response
state_lock = threading.Lock()

system_state = "DISARMED"   # DISARMED | ARMING | ARMED
alert_type = "NONE"         # NONE | MOTION | HELP
responder_name = ""
cooldown_active = False

arm_timer = None
cooldown_timer = None

pir = MotionSensor(PIR_PIN)
switch = Button(SWITCH_PIN, pull_up=True, bounce_time=0.1)
help_button = Button(HELP_BTN_PIN, pull_up=True, bounce_time=0.1)


def get_local_version():
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "1.0.0"


def send_push_notification(title, message, priority="high"):
    """Wysyła powiadomienie Push na telefony pracowników przez ntfy.sh."""
    def _send():
        try:
            requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers={"Title": title, "Priority": priority, "Tags": "rotating_light"},
                timeout=5
            )
        except Exception as e:
            print(f"[BŁĄD POWIADOMIENIA] {e}")
    threading.Thread(target=_send, daemon=True).start()


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
        send_push_notification(
            "Ktoś czeka przy kasie!",
            "Czujnik wykrył ruch przy pustej kasie. Wejdź w panel i kliknij 'Idę do kasy'.",
            priority="urgent"
        )
        cooldown_timer = threading.Timer(COOLDOWN_SEC, reset_cooldown)
        cooldown_timer.start()


def trigger_help_request():
    global alert_type, responder_name
    with state_lock:
        alert_type = "HELP"
        responder_name = ""
        print("[POMOC] Kasjer wzywa drugiego pracownika do pomocy!")
        send_push_notification(
            "POMOC PRZY KASIE!",
            "Kasjer prosi o wsparcie przy kasie! Kliknij w panelu 'Idę do kasy'.",
            priority="urgent"
        )


switch.when_pressed = on_switch_on
switch.when_released = on_switch_off
pir.when_motion = on_motion_detected
help_button.when_pressed = trigger_help_request

if switch.is_pressed:
    on_switch_on()


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Detektor Kasa</title>
    <style>
        body { font-family: Arial, sans-serif; background: #121212; color: #fff; text-align: center; padding: 15px; margin: 0; }
        .card { max-width: 460px; margin: 15px auto; padding: 20px; border-radius: 14px; background: #1e1e1e; box-shadow: 0 4px 15px rgba(0,0,0,0.5); }
        .status-badge { display: inline-block; padding: 10px 20px; border-radius: 8px; font-weight: bold; font-size: 1.2rem; margin: 10px 0; width: 85%; }
        .DISARMED { background: #2e7d32; }
        .ARMING { background: #f57f17; }
        .ARMED { background: #1565c0; }
        .alert-box { padding: 20px; border-radius: 12px; margin: 15px 0; font-size: 1.2rem; font-weight: bold; }
        .alert-danger { background: #c62828; animation: pulse 1.2s infinite; }
        .alert-ack { background: #00695c; }
        @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.65; } 100% { opacity: 1; } }
        input { width: 85%; padding: 12px; font-size: 1rem; border-radius: 8px; border: 1px solid #444; background: #2c2c2c; color: #fff; margin-bottom: 12px; text-align: center; }
        button { width: 92%; padding: 16px; font-size: 1.15rem; font-weight: bold; border: none; border-radius: 10px; cursor: pointer; margin: 8px 0; color: #fff; }
        .btn-ack { background: #00c853; color: #000; }
        .btn-help { background: #ff6d00; }
        .btn-clear { background: #555; font-size: 0.95rem; padding: 10px; }
        .footer { color: #777; font-size: 0.85rem; margin-top: 20px; }
    </style>
</head>
<body>
    <div class="card">
        <h2>Stan Czujnika przy Kasie</h2>
        <div id="state-badge" class="status-badge DISARMED">Ładowanie...</div>

        <div id="alert-section" style="display:none;">
            <div id="alert-box" class="alert-box alert-danger"></div>
            <input type="text" id="worker-name" placeholder="Wpisz swoje imię (np. Tomek)">
            <button id="btn-ack" class="btn-ack" onclick="acknowledgeAlert()">🏃 PRZYJMUJĘ – IDĘ DO KASY!</button>
            <button class="btn-clear" onclick="clearAlert()">Odwołaj / Już obsłużone</button>
        </div>
    </div>

    <div class="card">
        <h3>Panel Kasjera</h3>
        <p style="color:#bbb; font-size:0.9rem;">Jesteś przy kasie i potrzebujesz drugiego pracownika?</p>
        <button class="btn-help" onclick="callHelp()">🆘 WEZWIJ POMOC DO KASY</button>
    </div>

    <div class="footer">Repozytorium: coffynerd/detektor | Wersja: <span id="ver">...</span></div>

    <script>
        const nameInput = document.getElementById('worker-name');
        nameInput.value = localStorage.getItem('worker_name') || '';
        nameInput.addEventListener('input', () => localStorage.setItem('worker_name', nameInput.value));

        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                document.getElementById('ver').innerText = data.version;

                const badge = document.getElementById('state-badge');
                badge.className = 'status-badge ' + data.system_state;
                if (data.system_state === 'DISARMED') badge.innerText = '🟢 WYŁĄCZONY (Kasjer na miejscu)';
                if (data.system_state === 'ARMING') badge.innerText = '⏳ ZAŁĄCZANIE (5 sekund na odejście)';
                if (data.system_state === 'ARMED') badge.innerText = '🛡️ CZUWANIE (Kasa pusta)';

                const alertSec = document.getElementById('alert-section');
                const alertBox = document.getElementById('alert-box');
                const btnAck = document.getElementById('btn-ack');

                if (data.alert_type === 'NONE') {
                    alertSec.style.display = 'none';
                } else {
                    alertSec.style.display = 'block';
                    if (data.responder_name) {
                        alertBox.className = 'alert-box alert-ack';
                        alertBox.innerText = '✅ ' + data.responder_name + ' już idzie do kasy!';
                        btnAck.style.display = 'none';
                    } else {
                        alertBox.className = 'alert-box alert-danger';
                        btnAck.style.display = 'inline-block';
                        alertBox.innerText = (data.alert_type === 'MOTION')
                            ? '🚨 RUCH PRZY KASIE! Klient czeka!'
                            : '🆘 KASJER PROSI O POMOC PRZY KASIE!';
                    }
                }
            } catch (e) {}
        }

        async function acknowledgeAlert() {
            const name = nameInput.value.trim() || 'Pracownik';
            localStorage.setItem('worker_name', name);
            await fetch('/api/ack', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: name})
            });
            fetchStatus();
        }

        async function callHelp() {
            await fetch('/api/help', {method: 'POST'});
            fetchStatus();
        }

        async function clearAlert() {
            await fetch('/api/clear', {method: 'POST'});
            fetchStatus();
        }

        setInterval(fetchStatus, 1000);
        fetchStatus();
    </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/status")
def api_status():
    with state_lock:
        return jsonify({
            "system_state": system_state,
            "alert_type": alert_type,
            "responder_name": responder_name,
            "version": get_local_version()
        })


@app.route("/api/ack", methods=["POST"])
def api_ack():
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
            send_push_notification(
                "Zgłoszenie przyjęte",
                f"{worker} już idzie do kasy!",
                priority="default"
            )
    return jsonify({"ok": True})


@app.route("/api/help", methods=["POST", "GET"])
def api_help():
    trigger_help_request()
    return jsonify({"ok": True})


@app.route("/api/clear", methods=["POST"])
def api_clear():
    global alert_type, responder_name
    with state_lock:
        alert_type = "NONE"
        responder_name = ""
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
