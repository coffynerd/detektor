#!/usr/bin/env python3
import os
import re
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

FALLBACK_ADMIN_VERSION = "1.0.3"
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


def option_1_install():
    print("\n=== [1] INSTALACJA OPROGRAMOWANIA, VENV I USŁUGI SYSTEMD ===")
    run_cmd("apt-get update")
    run_cmd("apt-get install -y git openssh-client python3-venv python3-pip python3-flask python3-gpiozero python3-requests python3-lgpio python3-cryptography network-manager")

    os.makedirs(DATA_DIR, exist_ok=True)

    if not os.path.exists(os.path.join(APP_DIR, ".git")):
        print(f"Klonowanie repozytorium {GITHUB_REPO_URL} do {APP_DIR}...")
        shutil.rmtree(APP_DIR, ignore_errors=True)
        res = subprocess.run(f"git clone {GITHUB_REPO_URL} {APP_DIR}", shell=True)
        if res.returncode != 0:
            os.makedirs(APP_DIR, exist_ok=True)
            for fname in ["app.py", "admin.py", "version.txt", "admin_version.txt", "changelog.txt"]:
                if os.path.exists(fname):
                    shutil.copy2(fname, os.path.join(APP_DIR, fname))
    else:
        run_cmd(f"cd {APP_DIR} && git fetch --all && git reset --hard origin/main")

    print("\nTworzenie środowiska wirtualnego venv (--system-site-packages)...")
    shutil.rmtree(VENV_DIR, ignore_errors=True)
    run_cmd(f"python3 -m venv --system-site-packages {VENV_DIR}")
    run_cmd(f"{VENV_DIR}/bin/pip install pywebpush py-vapid requests")

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

    if parse_version(remote_ver) > parse_version(local_ver):
        print(f"\n[!] Dostępna jest NOWA wersja programu czujnika ({remote_ver})!")
        choice = input("Czy chcesz zaktualizować program główny teraz? (t/n): ").strip().lower()
    else:
        print("\n[i] Posiadasz aktualną wersję programu głównego.")
        choice = input("Czy mimo to wymusić ponowne pobranie z GitHuba? (t/n): ").strip().lower()

    if choice == "t":
        if os.path.exists(os.path.join(APP_DIR, ".git")):
            run_cmd(f"cd {APP_DIR} && git fetch --all && git reset --hard origin/main")
        else:
            run_cmd(f"rm -rf {APP_DIR} && git clone {GITHUB_REPO_URL} {APP_DIR}")
            run_cmd(f"python3 -m venv --system-site-packages {VENV_DIR}")
            run_cmd(f"{VENV_DIR}/bin/pip install pywebpush py-vapid requests")
        run_cmd(f"systemctl restart {SERVICE_NAME}")
        print(f"\n[OK] Zaktualizowano program czujnika do wersji {get_local_app_version()} i zrestartowano usługę!")


def option_3_update_admin():
    print("\n=== [3] AKTUALIZACJA PANELU ADMINISTRATORA (admin.py -> admin_version.txt) ===")
    local_admin_ver = get_local_admin_version()
    remote_admin_ver = fetch_github_text(GITHUB_ADMIN_VERSION_URL)

    print(f"Wersja Panelu Admina lokalnie    : {local_admin_ver}")
    print(f"Wersja Panelu Admina na GitHubie : {remote_admin_ver or 'Błąd połączenia'}")

    if not remote_admin_ver:
        return

    if parse_version(remote_admin_ver) > parse_version(local_admin_ver):
        print(f"\n[!] Dostępna jest NOWA wersja Panelu Administratora ({remote_admin_ver})!")
        choice = input("Czy pobrać i przeładować Panel Administratora teraz? (t/n): ").strip().lower()
    else:
        print("\n[i] Posiadasz aktualną wersję Panelu Administratora.")
        choice = input("Czy mimo to wymusić pobranie nowej kopii admin.py z GitHuba? (t/n): ").strip().lower()

    if choice == "t":
        new_admin_code = fetch_github_text(GITHUB_ADMIN_SCRIPT_URL)
        if not new_admin_code:
            print("[!] Nie udało się pobrać pliku admin.py z GitHuba.")
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

        print(f"\n[OK] Panel Administratora zaktualizowany do wersji {remote_admin_ver}! Przeładowuję skrypt...")
        time.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)


def option_4_wifi():
    print("\n=== [4] KONFIGURACJA SIECI WI-FI ===")
    ssid = input("Podaj nazwę sieci Wi-Fi (SSID): ").strip()
    password = input("Podaj hasło do Wi-Fi: ").strip()

    if not ssid:
        print("[!] Anulowano - nazwa sieci nie może być pusta.")
        return

    run_cmd("raspi-config nonint do_wifi_country PL")
    res = subprocess.run(f'raspi-config nonint do_wifi_ssid_passphrase "{ssid}" "{password}"', shell=True)
    if res.returncode == 0:
        print(f"\n[OK] Zapisano sieć Wi-Fi '{ssid}'. Łączenie...")
    else:
        run_cmd(f'nmcli dev wifi connect "{ssid}" password "{password}"')


def option_5_system_update():
    print("\n=== [5] AKTUALIZACJA SYSTEMU LINUX (APT) ===")
    run_cmd("apt-get update && apt-get upgrade -y && apt-get autoremove -y")
    print("\n[OK] System operacyjny został zaktualizowany.")


def option_6_reboot():
    print("\n=== [6] PONOWNE URUCHOMIENIE ===")
    print("a) Szybki restart samego programu czujnika (trwa 2 sekundy)")
    print("b) Pełny restart całej malinki (sudo reboot)")
    print("c) Anuluj")
    sub = input("Wybierz (a/b/c): ").strip().lower()

    if sub == "a":
        run_cmd(f"systemctl restart {SERVICE_NAME}")
        print("[OK] Usługa czujnika została zrestartowana.")
    elif sub == "b":
        run_cmd("reboot")


def option_7_show_vapid():
    print("\n=== [7] KLUCZ PUBLICZNY VAPID I STATYSTYKI TELEFONÓW ===")
    keys_path = os.path.join(DATA_DIR, "vapid_keys.json")
    subs_path = os.path.join(DATA_DIR, "push_subscriptions.json")

    if os.path.exists(keys_path):
        with open(keys_path, "r", encoding="utf-8") as f:
            keys = json.load(f)
        print(f"Application Server Key (Public Key):\n\n{keys.get('publicKey')}\n")
    else:
        print("[!] Klucze VAPID nie zostały jeszcze wygenerowane.")

    if os.path.exists(subs_path):
        with open(subs_path, "r", encoding="utf-8") as f:
            subs = json.load(f)
        print(f"Zarejestrowane telefony pracowników: {len(subs)}")
    else:
        print("Zarejestrowane telefony pracowników: 0")


def option_8_diagnostics():
    print("\n=== [8] DIAGNOSTYKA SERWERA I LOGI BŁĘDÓW ===")
    run_cmd("hostname -I")
    if os.path.exists(PUBLIC_URL_FILE):
        with open(PUBLIC_URL_FILE, "r", encoding="utf-8") as f:
            pub = f.read().strip()
        if pub:
            print(f"Aktywny adres publiczny HTTPS (Tunel): {pub}")
    run_cmd("curl -i http://localhost:5000/api/status")
    print("\n--- Ostatnie 15 linii logów usługi kasa-alarm.service ---")
    run_cmd(f"journalctl -u {SERVICE_NAME} -n 15 --no-pager")


def option_9_tunnel():
    print("\n=== [9] DARMOWY TUNEL HTTPS (DOSTĘP SPOZA WI-FI / NA LTE) ===")
    print("Tunel wystawia malinkę pod bezpiecznym adresem https://... bez publicznego IP.")
    print("a) Włącz / Zrestartuj darmowy tunel HTTPS (localhost.run przez SSH - bez konta)")
    print("b) Pokaż aktualny adres publiczny HTTPS tunelu")
    print("c) Wyłącz tunel HTTPS")
    print("0) Powrót")
    sub = input("Wybierz (a/b/c/0): ").strip().lower()

    if sub == "a":
        os.makedirs(DATA_DIR, exist_ok=True)
        ssh_key_path = "/root/.ssh/id_ed25519"
        if not os.path.exists(ssh_key_path):
            os.makedirs("/root/.ssh", exist_ok=True)
            run_cmd(f'ssh-keygen -t ed25519 -f {ssh_key_path} -N "" -q')

        tunnel_runner = os.path.join(DATA_DIR, "run_tunnel.py")
        runner_code = f'''#!/usr/bin/env python3
import re
import time
import subprocess

PUBLIC_URL_FILE = "{PUBLIC_URL_FILE}"

while True:
    proc = subprocess.Popen(
        [
            "ssh", "-o", "StrictHostKeyChecking=no",
            "-o", "ServerAliveInterval=30",
            "-o", "ExitOnForwardFailure=yes",
            "-R", "80:localhost:5000", "nokey@localhost.run"
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )
    for line in proc.stdout:
        print(line, end="", flush=True)
        match = re.search(r"(https://[a-zA-Z0-9.-]+\\.lhr\\.life)", line)
        if match:
            url = match.group(1)
            with open(PUBLIC_URL_FILE, "w", encoding="utf-8") as f:
                f.write(url)
    proc.wait()
    time.sleep(5)
'''
        with open(tunnel_runner, "w", encoding="utf-8") as f:
            f.write(runner_code)
        os.chmod(tunnel_runner, 0o755)

        tunnel_service = f"""[Unit]
Description=Publiczny Tunel HTTPS dla Czujnika Kasy (localhost.run)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 -u {tunnel_runner}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
        with open(f"/etc/systemd/system/{TUNNEL_SERVICE_NAME}", "w", encoding="utf-8") as f:
            f.write(tunnel_service)

        run_cmd("systemctl daemon-reload")
        run_cmd(f"systemctl enable {TUNNEL_SERVICE_NAME}")
        run_cmd(f"systemctl restart {TUNNEL_SERVICE_NAME}")
        print("\nCzekam 5 sekund na wygenerowanie adresu HTTPS...")
        time.sleep(5)
        if os.path.exists(PUBLIC_URL_FILE):
            with open(PUBLIC_URL_FILE, "r", encoding="utf-8") as f:
                print(f"\n[OK] Twój publiczny adres HTTPS:\n>>> {f.read().strip()} <<<\n")
        else:
            print("[i] Tunel startuje w tle. Wybierz za chwilę opcję 9 -> b, aby odczytać adres.")

    elif sub == "b":
        if os.path.exists(PUBLIC_URL_FILE):
            with open(PUBLIC_URL_FILE, "r", encoding="utf-8") as f:
                print(f"\nAktualny adres publiczny HTTPS:\n>>> {f.read().strip()} <<<\n")
        else:
            print("\n[!] Brak zapisanego adresu. Uruchom najpierw opcję 9 -> a.")

    elif sub == "c":
        run_cmd(f"systemctl stop {TUNNEL_SERVICE_NAME}")
        run_cmd(f"systemctl disable {TUNNEL_SERVICE_NAME}")
        if os.path.exists(PUBLIC_URL_FILE):
            os.remove(PUBLIC_URL_FILE)
        print("[OK] Tunel HTTPS został wyłączony.")


def option_10_firebase_relay():
    print("\n=== [10] KONFIGURACJA FIREBASE RELAY (SERWER POŚREDNICZĄCY) ===")
    print("Jeśli podasz URL swojej bazy Firebase Realtime Database (np. https://twoj-projekt.firebaseio.com),")
    print("malinka będzie co 2s synchronizować stan z chmurą bez potrzeby używania tuneli i przekierowania portów.")
    current_url = ""
    if os.path.exists(CLOUD_CONFIG_FILE):
        try:
            with open(CLOUD_CONFIG_FILE, "r", encoding="utf-8") as f:
                current_url = json.load(f).get("firebase_db_url", "")
        except Exception:
            pass
    print(f"Aktualny adres Firebase DB: {current_url or 'Brak (wyłączone)'}")
    new_url = input("Podaj nowy URL bazy Firebase (lub wciśnij Enter, aby wyłączyć): ").strip()
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CLOUD_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"firebase_db_url": new_url}, f, indent=2)
    run_cmd(f"systemctl restart {SERVICE_NAME}")
    print("[OK] Zapisano konfigurację Firebase Relay i zrestartowano usługę.")


def main():
    if os.geteuid() != 0:
        print("[!] Uruchom ten skrypt z uprawnieniami administratora:")
        print("    sudo python3 admin.py")
        sys.exit(1)

    while True:
        print("\n" + "=" * 68)
        print(f" PANEL ADMINISTRATORA v{get_local_admin_version()} | Program czujnika: v{get_local_app_version()}")
        print("=" * 68)
        print("1. Zainstaluj oprogramowanie i Web Push (pierwszy start / po awarii)")
        print("2. Zaktualizuj program czujnika z GitHuba (sprawdź version.txt)")
        print("3. Zaktualizuj ten Panel Administratora (sprawdź admin_version.txt)")
        print("4. Konfiguracja Wi-Fi")
        print("5. Aktualizuj system operacyjny (apt update & upgrade)")
        print("6. Uruchom ponownie czujnik / malinkę (reboot)")
        print("7. Pokaż klucz VAPID i liczbę zapisanych telefonów")
        print("8. Diagnostyka serwera i podgląd logów błędów")
        print("9. Darmowy Tunel HTTPS (dostęp spoza Wi-Fi / na LTE)")
        print("10. Konfiguracja Firebase Relay (chmurowy serwer pośredniczący)")
        print("0. Wyjdź")
        print("-" * 68)

        choice = input("Wybierz opcję [0-10]: ").strip()
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
            option_9_tunnel()
        elif choice == "10":
            option_10_firebase_relay()
        elif choice == "0":
            print("Do zobaczenia!")
            break
        else:
            print("[!] Nieprawidłowy wybór.")


if __name__ == "__main__":
    main()
