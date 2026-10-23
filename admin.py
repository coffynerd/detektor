#!/usr/bin/env python3
import os
import sys
import json
import time
import shutil
import subprocess
import urllib.request

# ================= KONFIGURACJA REPOZYTORIUM =================
GITHUB_REPO_URL = "https://github.com/coffynerd/detektor.git"
RAW_BASE_URL = "https://raw.githubusercontent.com/coffynerd/detektor/main"

GITHUB_APP_VERSION_URL = f"{RAW_BASE_URL}/version.txt"
GITHUB_ADMIN_VERSION_URL = f"{RAW_BASE_URL}/admin_version.txt"
GITHUB_ADMIN_SCRIPT_URL = f"{RAW_BASE_URL}/admin.py"

APP_DIR = "/opt/kasa-alarm"
VENV_DIR = "/opt/kasa-alarm/venv"
DATA_DIR = "/var/lib/kasa-alarm"
PUBLIC_URL_FILE = os.path.join(DATA_DIR, "public_url.txt")
CLOUD_CONFIG_FILE = os.path.join(DATA_DIR, "cloud_config.json")

SERVICE_NAME = "kasa-alarm.service"
TUNNEL_SERVICE_NAME = "kasa-tunnel.service"

FALLBACK_ADMIN_VERSION = "1.0.4"
# =============================================================


def run_cmd(cmd):
    print(f"\n>>> Wykonywanie: {cmd}")
    subprocess.run(cmd, shell=True, check=False)


def parse_version(v_str):
    try:
        return tuple(int(x) for x in v_str.strip().split("."))
    except Exception:
        return (0, 0, 0)


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
        print(f"[!] Błąd połączenia z GitHubem: {e}")
        return None


def get_local_app_version():
    for path in [os.path.join(APP_DIR, "version.txt"), "version.txt"]:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read().strip()
            except Exception:
                pass
    return "Brak (niezainstalowano)"


def get_local_admin_version():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for path in [
        os.path.join(script_dir, "admin_version.txt"),
        os.path.join(APP_DIR, "admin_version.txt"),
        "admin_version.txt"
    ]:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read().strip()
            except Exception:
                pass
    return FALLBACK_ADMIN_VERSION


def get_lte_status_label():
    try:
        if os.path.exists(CLOUD_CONFIG_FILE):
            with open(CLOUD_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                if cfg.get("lte_enabled"):
                    return "WŁĄCZONY"
    except Exception:
        pass
    return "WYŁĄCZONY (Tylko Wi-Fi LAN)"


def option_1_install():
    print("\n=== [1] INSTALACJA OPROGRAMOWANIA, VENV I USŁUGI SYSTEMD ===")
    run_cmd("apt-get update")
    run_cmd("apt-get install -y git python3-venv python3-pip python3-flask python3-gpiozero python3-requests python3-lgpio python3-cryptography network-manager")

    os.makedirs(DATA_DIR, exist_ok=True)

    if not os.path.exists(os.path.join(APP_DIR, ".git")):
        shutil.rmtree(APP_DIR, ignore_errors=True)
        res = subprocess.run(f"git clone {GITHUB_REPO_URL} {APP_DIR}", shell=True)
        if res.returncode != 0:
            os.makedirs(APP_DIR, exist_ok=True)
            for fname in ["app.py", "admin.py", "version.txt", "admin_version.txt", "changelog.txt"]:
                if os.path.exists(fname):
                    shutil.copy2(fname, os.path.join(APP_DIR, fname))
    else:
        run_cmd(f"cd {APP_DIR} && git fetch --all && git reset --hard origin/main")

    shutil.rmtree(VENV_DIR, ignore_errors=True)
    run_cmd(f"python3 -m venv --system-site-packages {VENV_DIR}")
    run_cmd(f"{VENV_DIR}/bin/pip install pywebpush py-vapid")

    service_content = f"""[Unit]
Description=System Czujnika Ruchu i Przycisku Pomocy przy Kasie (detektor)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory={APP_DIR}
ExecStart={VENV_DIR}/bin/python -u {APP_DIR}/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
    with open(f"/etc/systemd/system/{SERVICE_NAME}", "w", encoding="utf-8") as f:
        f.write(service_content)

    run_cmd("systemctl daemon-reload")
    run_cmd(f"systemctl enable {SERVICE_NAME}")
    run_cmd(f"systemctl restart {SERVICE_NAME}")
    time.sleep(3)
    option_8_diagnostics()


def option_2_update_app():
    print("\n=== [2] AKTUALIZACJA PROGRAMU CZUJNIKA (app.py -> version.txt) ===")
    local_ver = get_local_app_version()
    remote_ver = fetch_github_text(GITHUB_APP_VERSION_URL)

    print(f"Wersja app.py zainstalowana lokalnie : {local_ver}")
    print(f"Wersja app.py dostępna na GitHubie   : {remote_ver or 'Błąd połączenia'}")

    if not remote_ver:
        return

    choice = input("Czy chcesz pobrać najnowszą wersję i zrestartować czujnik? (t/n): ").strip().lower()
    if choice == "t":
        if os.path.exists(os.path.join(APP_DIR, ".git")):
            run_cmd(f"cd {APP_DIR} && git fetch --all && git reset --hard origin/main")
        else:
            run_cmd(f"rm -rf {APP_DIR} && git clone {GITHUB_REPO_URL} {APP_DIR}")
            run_cmd(f"python3 -m venv --system-site-packages {VENV_DIR}")
            run_cmd(f"{VENV_DIR}/bin/pip install pywebpush py-vapid")
        run_cmd(f"systemctl restart {SERVICE_NAME}")
        print(f"\n[OK] Zaktualizowano program czujnika do wersji {get_local_app_version()}!")


def option_3_update_admin():
    print("\n=== [3] AKTUALIZACJA PANELU ADMINISTRATORA (admin.py -> admin_version.txt) ===")
    local_admin_ver = get_local_admin_version()
    remote_admin_ver = fetch_github_text(GITHUB_ADMIN_VERSION_URL)

    print(f"Wersja Panelu Admina lokalnie    : {local_admin_ver}")
    print(f"Wersja Panelu Admina na GitHubie : {remote_admin_ver or 'Błąd połączenia'}")

    if not remote_admin_ver:
        return

    choice = input("Czy pobrać i przeładować Panel Administratora teraz? (t/n): ").strip().lower()
    if choice == "t":
        new_admin_code = fetch_github_text(GITHUB_ADMIN_SCRIPT_URL)
        if not new_admin_code:
            return

        current_script_path = os.path.abspath(__file__)
        current_script_dir = os.path.dirname(current_script_path)

        with open(current_script_path, "w", encoding="utf-8") as f:
            f.write(new_admin_code + "\n")
        with open(os.path.join(current_script_dir, "admin_version.txt"), "w", encoding="utf-8") as f:
            f.write(remote_admin_ver + "\n")

        if os.path.exists(APP_DIR):
            with open(os.path.join(APP_DIR, "admin.py"), "w", encoding="utf-8") as f:
                f.write(new_admin_code + "\n")
            with open(os.path.join(APP_DIR, "admin_version.txt"), "w", encoding="utf-8") as f:
                f.write(remote_admin_ver + "\n")

        print(f"\n[OK] Panel Administratora zaktualizowany do wersji {remote_admin_ver}!")
        time.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)


def option_4_wifi():
    print("\n=== [4] KONFIGURACJA SIECI WI-FI ===")
    ssid = input("Podaj nazwę sieci Wi-Fi (SSID): ").strip()
    password = input("Podaj hasło do Wi-Fi: ").strip()
    if not ssid:
        return
    run_cmd("raspi-config nonint do_wifi_country PL")
    res = subprocess.run(f'raspi-config nonint do_wifi_ssid_passphrase "{ssid}" "{password}"', shell=True)
    if res.returncode != 0:
        run_cmd(f'nmcli dev wifi connect "{ssid}" password "{password}"')


def option_5_system_update():
    print("\n=== [5] AKTUALIZACJA SYSTEMU LINUX (APT) ===")
    run_cmd("apt-get update && apt-get upgrade -y && apt-get autoremove -y")


def option_6_reboot():
    print("\n=== [6] PONOWNE URUCHOMIENIE ===")
    print("a) Szybki restart samego programu czujnika")
    print("b) Pełny restart całej malinki (sudo reboot)")
    sub = input("Wybierz (a/b): ").strip().lower()
    if sub == "a":
        run_cmd(f"systemctl restart {SERVICE_NAME}")
    elif sub == "b":
        run_cmd("reboot")


def option_7_show_vapid():
    print("\n=== [7] KLUCZ PUBLICZNY VAPID I STATYSTYKI TELEFONÓW ===")
    keys_path = os.path.join(DATA_DIR, "vapid_keys.json")
    subs_path = os.path.join(DATA_DIR, "push_subscriptions.json")
    if os.path.exists(keys_path):
        with open(keys_path, "r", encoding="utf-8") as f:
            print(f"Public Key:\n{json.load(f).get('publicKey')}\n")
    if os.path.exists(subs_path):
        with open(subs_path, "r", encoding="utf-8") as f:
            print(f"Zarejestrowane telefony: {len(json.load(f))}")


def option_8_diagnostics():
    print("\n=== [8] DIAGNOSTYKA SERWERA I LOGI BŁĘDÓW ===")
    print(f"Tryb LTE / Chmura: {get_lte_status_label()}")
    run_cmd("hostname -I")
    run_cmd("curl -i http://localhost:5000/api/status")
    print("\n--- Ostatnie 15 linii logów usługi kasa-alarm.service ---")
    run_cmd(f"journalctl -u {SERVICE_NAME} -n 15 --no-pager")


def option_9_lte_toggle():
    print("\n=== [9] TRYB LTE / DOSTĘP SPOZA WI-FI (WŁĄCZ / WYŁĄCZ) ===")
    print(f"Aktualny status: {get_lte_status_label()}")
    print("a) WYŁĄCZ całkowicie sieć LTE / Tunel / Firebase (Czysty, niezawodny tryb lokalny Wi-Fi)")
    print("b) WŁĄCZ tryb LTE przez Firebase Relay")
    print("0) Powrót")
    sub = input("Wybierz (a/b/0): ").strip().lower()

    os.makedirs(DATA_DIR, exist_ok=True)
    if sub == "a":
        run_cmd(f"systemctl stop {TUNNEL_SERVICE_NAME} 2>/dev/null")
        run_cmd(f"systemctl disable {TUNNEL_SERVICE_NAME} 2>/dev/null")
        if os.path.exists(PUBLIC_URL_FILE):
            os.remove(PUBLIC_URL_FILE)
        with open(CLOUD_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"lte_enabled": False, "firebase_db_url": ""}, f, indent=2)
        run_cmd(f"systemctl restart {SERVICE_NAME}")
        print("\n[OK] Sieć LTE i tunele zostały CAŁKOWICIE WYŁĄCZONE. System działa wyłącznie lokalnie!")

    elif sub == "b":
        db_url = input("Podaj URL bazy Firebase Realtime DB (https://...): ").strip()
        with open(CLOUD_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"lte_enabled": bool(db_url), "firebase_db_url": db_url}, f, indent=2)
        run_cmd(f"systemctl restart {SERVICE_NAME}")
        print("\n[OK] Zapisano ustawienia trybu LTE.")


def main():
    if os.geteuid() != 0:
        print("Uruchom jako root: sudo python3 admin.py")
        sys.exit(1)

    while True:
        print("\n" + "=" * 70)
        print(f" PANEL ADMINA v{get_local_admin_version()} | Czujnik: v{get_local_app_version()} | LTE: {get_lte_status_label()}")
        print("=" * 70)
        print("1. Zainstaluj oprogramowanie i Web Push (pierwszy start / po awarii)")
        print("2. Zaktualizuj program czujnika z GitHuba (sprawdź version.txt)")
        print("3. Zaktualizuj ten Panel Administratora (sprawdź admin_version.txt)")
        print("4. Konfiguracja Wi-Fi")
        print("5. Aktualizuj system operacyjny (apt update & upgrade)")
        print("6. Uruchom ponownie czujnik / malinkę (reboot)")
        print("7. Pokaż klucz VAPID i liczbę zapisanych telefonów")
        print("8. Diagnostyka serwera i podgląd logów błędów")
        print("9. Włącz / Wyłącz obsługę sieci LTE (powrót do czystego Wi-Fi LAN)")
        print("0. Wyjdź")
        print("-" * 70)

        choice = input("Wybierz opcję [0-9]: ").strip()
        if choice == "1":
            option_1_install()
        elif choice == "2":
            option_2_update_app()
        elif choice == "3":
            option_3_update_admin()
        elif choice == "4":
            option_4_wifi()
        elif choice == "5":
            option_5_system_update()
        elif choice == "6":
            option_6_reboot()
        elif choice == "7":
            option_7_show_vapid()
        elif choice == "8":
            option_8_diagnostics()
        elif choice == "9":
            option_9_lte_toggle()
        elif choice == "0":
            break


if __name__ == "__main__":
    main()
