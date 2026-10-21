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
DATA_DIR = "/var/lib/kasa-alarm"
SERVICE_NAME = "kasa-alarm.service"

# Wersja awaryjna, jeśli plik admin_version.txt jeszcze nie istnieje na dysku
FALLBACK_ADMIN_VERSION = "1.0.0"
# =============================================================


def run_cmd(cmd):
    print(f"\n>>> Wykonywanie: {cmd}")
    subprocess.run(cmd, shell=True, check=False)


def parse_version(v_str):
    """Zamienia tekst np. '1.1.4' na krotkę (1, 1, 4) do porównania."""
    try:
        return tuple(int(x) for x in v_str.strip().split("."))
    except Exception:
        return (0, 0, 0)


def fetch_github_text(raw_url):
    """Pobiera plik tekstowy z GitHuba z pominięciem pamięci podręcznej (cache)."""
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
    """Odczytuje lokalną wersję programu głównego (version.txt)."""
    for path in [os.path.join(APP_DIR, "version.txt"), "version.txt"]:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read().strip()
            except Exception:
                pass
    return "Brak (niezainstalowano)"


def get_local_admin_version():
    """Odczytuje lokalną wersję panelu admina (admin_version.txt)."""
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
    print("\n=== [1] INSTALACJA OPROGRAMOWANIA, WEB PUSH I USŁUGI SYSTEMD ===")
    run_cmd("apt-get update")
    run_cmd("apt-get install -y git python3-pip python3-flask python3-gpiozero python3-requests python3-lgpio python3-cryptography network-manager")
    run_cmd("pip3 install pywebpush py-vapid --break-system-packages")

    os.makedirs(DATA_DIR, exist_ok=True)

    if not os.path.exists(os.path.join(APP_DIR, ".git")):
        print(f"Klonowanie repozytorium {GITHUB_REPO_URL} do {APP_DIR}...")
        shutil.rmtree(APP_DIR, ignore_errors=True)
        res = subprocess.run(f"git clone {GITHUB_REPO_URL} {APP_DIR}", shell=True)
        if res.returncode != 0:
            print("Klonowanie nie powiodło się - kopiuję pliki lokalne do katalogu docelowego...")
            os.makedirs(APP_DIR, exist_ok=True)
            for fname in ["app.py", "admin.py", "version.txt", "admin_version.txt"]:
                if os.path.exists(fname):
                    shutil.copy2(fname, os.path.join(APP_DIR, fname))

    service_content = f"""[Unit]
Description=System Czujnika Ruchu i Przycisku Pomocy przy Kasie (detektor)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory={APP_DIR}
ExecStart=/usr/bin/python3 {APP_DIR}/app.py
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
    time.sleep(2)
    option_7_show_vapid()
    print("\n[OK] Instalacja zakończona! Czujnik działa w tle i włączy się sam po każdym zaniku prądu.")


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

        # Zapis nowego admin.py i admin_version.txt w bieżącej lokalizacji
        with open(current_script_path, "w", encoding="utf-8") as f:
            f.write(new_admin_code + "\n")
        with open(os.path.join(current_script_dir, "admin_version.txt"), "w", encoding="utf-8") as f:
            f.write(remote_admin_ver + "\n")

        # Jeśli istnieje katalog /opt/kasa-alarm, aktualizujemy również tamtą kopię
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
        print("[!] Klucze VAPID nie zostały jeszcze wygenerowane (uruchom opcję 1).")

    if os.path.exists(subs_path):
        with open(subs_path, "r", encoding="utf-8") as f:
            subs = json.load(f)
        print(f"Zarejestrowane telefony pracowników: {len(subs)}")
    else:
        print("Zarejestrowane telefony pracowników: 0")


def main():
    if os.geteuid() != 0:
        print("[!] Uruchom ten skrypt z uprawnieniami administratora:")
        print("    sudo python3 admin.py")
        sys.exit(1)

    while True:
        print("\n" + "=" * 64)
        print(f" PANEL ADMINISTRATORA v{get_local_admin_version()} | Program czujnika: v{get_local_app_version()}")
        print("=" * 64)
        print("1. Zainstaluj oprogramowanie i Web Push (pierwszy start / po awarii)")
        print("2. Zaktualizuj program czujnika z GitHuba (sprawdź version.txt)")
        print("3. Zaktualizuj ten Panel Administratora (sprawdź admin_version.txt)")
        print("4. Konfiguracja Wi-Fi")
        print("5. Aktualizuj system operacyjny (apt update & upgrade)")
        print("6. Uruchom ponownie czujnik / malinkę (reboot)")
        print("7. Pokaż klucz VAPID i liczbę zapisanych telefonów")
        print("0. Wyjdź")
        print("-" * 64)

        choice = input("Wybierz opcję [0-7]: ").strip()
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
        elif choice == "0":
            print("Do zobaczenia!")
            break
        else:
            print("[!] Nieprawidłowy wybór.")


if __name__ == "__main__":
    main()
