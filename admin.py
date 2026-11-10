#!/usr/bin/env python3
import os
import sys
import time
import json
import subprocess
import urllib.request

# ================= KONFIGURACJA PANELU ADMINA (v1.1.0) =================
INSTALL_DIR = "/opt/kasa-alarm"
DATA_DIR = "/var/lib/kasa-alarm"
VENV_DIR = os.path.join(INSTALL_DIR, "venv")
SERVICE_NAME = "kasa-alarm.service"
SERVICE_FILE = f"/etc/systemd/system/{SERVICE_NAME}"
CLOUD_CONFIG_FILE = os.path.join(DATA_DIR, "cloud_config.json")
SENSOR_CONFIG_FILE = os.path.join(DATA_DIR, "sensor_config.json")

GITHUB_REPO_URL = "https://github.com/coffynerd/detektor.git"
LOCAL_API_URL = "http://127.0.0.1:5000"
# =======================================================================


def run_cmd(cmd, check=False):
    return subprocess.run(cmd, shell=True, check=check)


def capture_cmd(cmd):
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return res.returncode, res.stdout.strip(), res.stderr.strip()


def ensure_root():
    if os.geteuid() != 0:
        print("\n[!] Ten skrypt wymaga uprawnień administratora (root).")
        print("    Uruchom go poleceniem: sudo python3 admin.py\n")
        sys.exit(1)


def api_request(method, path, payload=None, timeout=3.0):
    url = f"{LOCAL_API_URL}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8").strip()
        return json.loads(raw) if raw else {}


def enable_i2c_hardware():
    print("\n[*] Włączanie sprzętowej magistrali I2C w systemie Raspberry Pi...")
    run_cmd("raspi-config nonint do_i2c 0")
    run_cmd("modprobe i2c-dev")
    if os.path.exists("/etc/modules"):
        with open("/etc/modules", "r", encoding="utf-8") as f:
            mods = f.read()
        if "i2c-dev" not in mods:
            with open("/etc/modules", "a", encoding="utf-8") as f:
                f.write("\ni2c-dev\n")
    print("[OK] Magistrala I2C została aktywowana.")


def detect_i2c_sensor():
    code, out, _ = capture_cmd("i2cdetect -y 1")
    if code != 0:
        return False, "Brak dostępu do szyny /dev/i2c-1"
    found = (" 29 " in out) or out.endswith(" 29") or ("\n20: -- -- -- -- -- -- -- -- -- 29" in out)
    return found, out


def create_systemd_service():
    service_content = f"""[Unit]
Description=Detektor Kasa API & VL53L1X ToF Monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory={INSTALL_DIR}
ExecStart={VENV_DIR}/bin/python -u {INSTALL_DIR}/app.py
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
"""
    with open(SERVICE_FILE, "w", encoding="utf-8") as f:
        f.write(service_content)
    run_cmd("systemctl daemon-reload")
    run_cmd(f"systemctl enable {SERVICE_NAME}")


def install_or_repair_system():
    print("\n========================================================")
    print("  INSTALACJA / NAPRAWA SYSTEMU DETEKTOR KASA (v1.1.0)")
    print("========================================================")

    enable_i2c_hardware()

    print("\n[*] Instalowanie pakietów systemowych (git, python3-venv, i2c-tools, lgpio)...")
    run_cmd("apt-get update")
    run_cmd("apt-get install -y git python3-venv python3-pip python3-lgpio python3-gpiozero i2c-tools python3-smbus")

    os.makedirs(DATA_DIR, exist_ok=True)

    if not os.path.exists( os.path.join(INSTALL_DIR, ".git") ):
        print(f"\n[*] Pobieranie repozytorium z {GITHUB_REPO_URL} do {INSTALL_DIR}...")
        run_cmd(f"rm -rf {INSTALL_DIR}")
        run_cmd(f"git clone {GITHUB_REPO_URL} {INSTALL_DIR}", check=True)
    else:
        print(f"\n[*] Aktualizowanie kodu w {INSTALL_DIR}...")
        run_cmd(f"cd {INSTALL_DIR} && git fetch --all && git reset --hard origin/main", check=True)

    print("\n[*] Tworzenie środowiska Python (z dostępem do pakietów systemowych GPIO)...")
    if not os.path.exists(os.path.join(VENV_DIR, "bin", "pip")):
        run_cmd(f"python3 -m venv --system-site-packages {VENV_DIR}", check=True)

    print("\n[*] Instalowanie bibliotek Python (Flask, WebPush, Adafruit VL53L1X)...")
    pip_bin = os.path.join(VENV_DIR, "bin", "pip")
    run_cmd(f"{pip_bin} install --upgrade pip")
    run_cmd(
        f"{pip_bin} install flask pywebpush cryptography requests "
        f"adafruit-blinka adafruit-circuitpython-vl53l1x",
        check=True
    )

    print("\n[*] Konfigurowanie usługi systemowej systemd...")
    create_systemd_service()
    run_cmd(f"systemctl stop kasa-tunnel.service 2>/dev/null")
    run_cmd(f"systemctl disable kasa-tunnel.service 2>/dev/null")
    run_cmd(f"systemctl restart {SERVICE_NAME}")

    time.sleep(2)
    found, _ = detect_i2c_sensor()
    print("\n========================================================")
    print(" [SUKCES] System v1.1.0 został zainstalowany i uruchomiony!")
    if found:
        print(" [LASER OK] Wykryto czujnik VL53L1X na magistrali I2C (adres 0x29).")
    else:
        print(" [UWAGA] Nie wykryto jeszcze czujnika VL53L1X na I2C (0x29).")
        print("         Sprawdź przewody (Pin 1=VCC, Pin 3=SDA, Pin 5=SCL, Pin 6/9=GND).")
    print("========================================================")
    input("\nNaciśnij Enter, aby wrócić do menu...")


def update_from_github():
    print("\n[*] Pobieranie najnowszej wersji z GitHub (coffynerd/detektor)...")
    if not os.path.exists(os.path.join(INSTALL_DIR, ".git")):
        print("[!] Katalog /opt/kasa-alarm nie istnieje. Uruchamiam pełną instalację...")
        install_or_repair_system()
        return

    enable_i2c_hardware()
    run_cmd(f"cd {INSTALL_DIR} && git fetch --all && git reset --hard origin/main")

    pip_bin = os.path.join(VENV_DIR, "bin", "pip")
    run_cmd(f"{pip_bin} install adafruit-blinka adafruit-circuitpython-vl53l1x")

    create_systemd_service()
    run_cmd(f"systemctl restart {SERVICE_NAME}")
    time.sleep(2)
    print("[OK] Zaktualizowano i zrestartowano usługę kasa-alarm.service!")
    input("\nNaciśnij Enter, aby wrócić do menu...")


def show_status_and_diagnostics():
    print("\n================ STATUS SYSTEMU I CZUJNIKA ================")
    code, _, _ = capture_cmd(f"systemctl is-active --quiet {SERVICE_NAME}")
    print(f"Usługa {SERVICE_NAME}: {'AKTYWNA (RUNNING)' if code == 0 else 'ZATRZYMANA'}")

    found_i2c, i2c_table = detect_i2c_sensor()
    print(f"Magistrala I2C (0x29):   {'WYKRYTO VL53L1X (0x29)' if found_i2c else 'BRAK UKŁADU 0x29'}")

    try:
        st = api_request("GET", "/api/status")
        print(f"Wersja oprogramowania:   {st.get('version')}")
        print(f"Aktywny tryb czujnika:   {st.get('sensor_mode')}")
        print(f"Stan przełącznika kasy:  {st.get('system_state')}")
        print(f"Aktualny alarm:          {st.get('alert_type')}")
        dist = st.get("distance_mm")
        thresh = st.get("trigger_threshold_mm")
        bg = st.get("background_distance_mm")
        print(f"Zmierzony dystans:       {f'{dist} mm ({dist/10:.1f} cm)' if dist is not None else 'Brak odczytu'}")
        print(f"Próg wywołania alarmu:   {thresh} mm ({thresh/10:.1f} cm)")
        print(f"Skalibrowane tło (regał):{f'{bg} mm ({bg/10:.1f} cm)' if bg else 'Brak / Ręczny próg'}")
        print(f"Zarejestrowane telefony: {st.get('subscribed_devices')}")
        print(f"Synchronizacja Firebase: {'WŁĄCZONA' if st.get('lte_enabled') else 'WYŁĄCZONA'}")
    except Exception as e:
        print(f"[!] Nie udało się pobrać statusu z API ({e}).")
        print("\nTablica I2C (i2cdetect -y 1):")
        print(i2c_table)

    print("===========================================================")
    input("\nNaciśnij Enter, aby wrócić do menu...")


def live_laser_monitor():
    print("\n===========================================================")
    print("   PODGLĄD POMIARU LASERA VL53L1X NA ŻYWO (Co 0.3 sekundy)")
    print("   Podejdź do kasy i sprawdź odczyt. Wciśnij Ctrl+C aby wyjść.")
    print("===========================================================\n")

    found_i2c, i2c_table = detect_i2c_sensor()
    if not found_i2c:
        print("[!] UWAGA: Układ VL53L1X (adres 0x29) nie jest widoczny na szynie I2C!")
        print("    Wynik polecenia 'i2cdetect -y 1':\n")
        print(i2c_table)
        print("\nSprawdź podłączenie 4 przewodów:")
        print("  VCC -> Pin 1 (3.3V) | SDA -> Pin 3 | SCL -> Pin 5 | GND -> Pin 6 lub 9")
        input("\nNaciśnij Enter, aby wrócić do menu...")
        return

    try:
        while True:
            st = api_request("GET", "/api/status", timeout=1.5)
            dist = st.get("distance_mm")
            thresh = st.get("trigger_threshold_mm", 1000)
            state = st.get("system_state", "?")
            hits = st.get("motion_hits", 0)
            req_hits = st.get("motion_required", 3)

            if dist is None:
                bar = "[ Brak odbicia / Poza zasięgiem ]"
                dist_str = "---- mm"
                zone = "TŁO"
            else:
                dist_str = f"{dist:4d} mm ({dist/10:5.1f} cm)"
                filled = min(30, max(1, int(dist / 100)))
                bar = "#" * filled + "." * (30 - filled)
                zone = "🔥 KLIENT W STREFIE!" if (80 <= dist <= thresh) else "Czysto (Tło)"

            sys.stdout.write(
                f"\r[{state:8s}] Dystans: {dist_str} | Próg: {thresh}mm | [{bar}] {zone} ({hits}/{req_hits})   "
            )
            sys.stdout.flush()
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("\n\n[i] Zakończono podgląd na żywo.")
    except Exception as e:
        print(f"\n\n[!] Błąd odczytu z API: {e}")

    input("\nNaciśnij Enter, aby wrócić do menu...")


def configure_threshold():
    print("\n================ KONFIGURACJA PROGU ODLEGŁOŚCI ================")
    try:
        st = api_request("GET", "/api/status")
        cur_dist = st.get("distance_mm")
        cur_thresh = st.get("trigger_threshold_mm", 1000)
        print(f"Aktualny odczyt z lasera:  {f'{cur_dist} mm ({cur_dist/10:.1f} cm)' if cur_dist else 'Brak'}")
        print(f"Aktualny próg wyzwalania:  {cur_thresh} mm ({cur_thresh/10:.1f} cm)")
    except Exception:
        pass

    print("\nPodaj nowy próg w MILIMETRACH (np. 950 dla 95 cm):")
    print(" - Jeśli wpiszesz 'auto', system sam dobierze próg na podstawie odległości do regału.")
    val = input("Nowy próg [mm lub 'auto']: ").strip().lower()

    if not val:
        return

    if val == "auto":
        if os.path.exists(SENSOR_CONFIG_FILE):
            os.remove(SENSOR_CONFIG_FILE)
        run_cmd(f"systemctl restart {SERVICE_NAME}")
        print("[OK] Przywrócono automatyczną kalibrację tła i zrestartowano usługę.")
    else:
        try:
            mm = int(val)
            if not (200 <= mm <= 3500):
                print("[!] Podaj wartość z zakresu od 200 do 3500 mm (20 cm - 350 cm).")
            else:
                os.makedirs(DATA_DIR, exist_ok=True)
                with open(SENSOR_CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump({"trigger_threshold_mm": mm}, f, indent=2)
                try:
                    api_request("POST", "/api/sensor-config", {"trigger_threshold_mm": mm})
                except Exception:
                    run_cmd(f"systemctl restart {SERVICE_NAME}")
                print(f"[OK] Zapisano nowy próg alarmu: {mm} mm ({mm/10:.1f} cm)!")
        except ValueError:
            print("[!] Nieprawidłowa liczba.")

    input("\nNaciśnij Enter, aby wrócić do menu...")


def configure_firebase():
    print("\n================ KONFIGURACJA FIREBASE RELAY (LTE) ================")
    cfg = {"lte_enabled": False, "firebase_db_url": ""}
    if os.path.exists(CLOUD_CONFIG_FILE):
        try:
            with open(CLOUD_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            pass

    print(f"Status przekaźnika chmurowego: {'WŁĄCZONY' if cfg.get('lte_enabled') else 'WYŁĄCZONY'}")
    print(f"Adres bazy Firebase RTDB:      {cfg.get('firebase_db_url') or '(brak)'}")
    print("\n1. Włącz i ustaw adres bazy Firebase")
    print("2. Wyłącz synchronizację z Firebase")
    print("0. Powrót")

    choice = input("\nWybór: ").strip()
    if choice == "1":
        url = input("Podaj URL bazy Firebase (lub Enter aby zostawić obecny): ").strip()
        if url:
            cfg["firebase_db_url"] = url.rstrip("/")
        cfg["lte_enabled"] = True
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CLOUD_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        run_cmd(f"systemctl restart {SERVICE_NAME}")
        print("[OK] Włączono synchronizację Firebase Relay.")
        input("\nNaciśnij Enter...")
    elif choice == "2":
        cfg["lte_enabled"] = False
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CLOUD_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        run_cmd(f"systemctl restart {SERVICE_NAME}")
        print("[OK] Wyłączono synchronizację Firebase Relay.")
        input("\nNaciśnij Enter...")


def send_test_push():
    print("\n[*] Wysyłanie testowego powiadomienia Web Push (wezwanie do kasy)...")
    try:
        api_request("POST", "/api/help")
        print("[OK] Wysłano sygnał testowy! Sprawdź telefon.")
    except Exception as e:
        print(f"[!] Błąd wysyłania powiadomienia: {e}")
    input("\nNaciśnij Enter, aby wrócić do menu...")


def show_live_logs():
    print("\n[*] Wyświetlanie logów usługi (Wciśnij Ctrl+C, aby zakończyć)...\n")
    try:
        run_cmd(f"journalctl -u {SERVICE_NAME} -n 30 -f")
    except KeyboardInterrupt:
        pass


def main_menu():
    ensure_root()
    while True:
        os.system("clear")
        print("==============================================================")
        print("   DETEKTOR KASA - PANEL ADMINISTRATORA v1.1.0 (Laser ToF)    ")
        print("==============================================================")
        print(" 1. Zainstaluj / Napraw system (Auto-konfiguracja I2C + VL53L1X)")
        print(" 2. Status systemu, czujnika I2C (0x29) i zmierzonej odległości")
        print(" 3. Aktualizuj kod z GitHub (coffynerd/detektor)")
        print(" 4. Miernik laserowy NA ŻYWO (podgląd odległości w mm i cm)")
        print(" 5. Ustaw próg wykrycia klienta (w milimetrach lub AUTO)")
        print(" 6. Konfiguracja chmury Firebase Relay (praca przez LTE)")
        print(" 7. Wyślij testowe powiadomienie Web Push na telefony")
        print(" 8. Podgląd logów systemowych na żywo (journalctl)")
        print(" 9. Restart usługi kasa-alarm.service")
        print(" 0. Wyjście")
        print("==============================================================")

        choice = input("Wybierz opcję [0-9]: ").strip()

        if choice == "1":
            install_or_repair_system()
        elif choice == "2":
            show_status_and_diagnostics()
        elif choice == "3":
            update_from_github()
        elif choice == "4":
            live_laser_monitor()
        elif choice == "5":
            configure_threshold()
        elif choice == "6":
            configure_firebase()
        elif choice == "7":
            send_test_push()
        elif choice == "8":
            show_live_logs()
        elif choice == "9":
            run_cmd(f"systemctl restart {SERVICE_NAME}")
            print("\n[OK] Usługa została zrestartowana.")
            time.sleep(1.5)
        elif choice == "0":
            print("\nDo widzenia!\n")
            break


if __name__ == "__main__":
    main_menu()
