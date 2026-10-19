#!/usr/bin/env python3
import os
import sys
import time
import shutil
import subprocess
import urllib.request

# ================= KONFIGURACJA REPOZYTORIUM =================
GITHUB_REPO_URL = "https://github.com/coffynerd/detektor.git"
GITHUB_RAW_VERSION_URL = "https://raw.githubusercontent.com/coffynerd/detektor/main/version.txt"

APP_DIR = "/opt/kasa-alarm"
SERVICE_NAME = "kasa-alarm.service"
# =============================================================


def run_cmd(cmd):
    print(f"\n>>> Wykonywanie: {cmd}")
    subprocess.run(cmd, shell=True, check=False)


def parse_version(v_str):
    """Zamienia tekst np. '1.1.4' na krotkę liczb (1, 1, 4) do porównania."""
    try:
        return tuple(int(x) for x in v_str.strip().split("."))
    except Exception:
        return (0, 0, 0)


def get_local_version():
    ver_path = os.path.join(APP_DIR, "version.txt")
    if not os.path.exists(ver_path):
        ver_path = "version.txt"
    try:
        with open(ver_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "Brak (niezainstalowano)"


def get_remote_version():
    try:
        # Dodanie parametru czasu omija 5-minutowy cache serwerów GitHub Raw
        url = f"{GITHUB_RAW_VERSION_URL}?t={int(time.time())}"
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
        print(f"[!] Nie udało się pobrać wersji z GitHuba: {e}")
        return None


def option_1_install():
    print("\n=== [1] INSTALACJA OPROGRAMOWANIA I USŁUGI SYSTEMD ===")
    run_cmd("apt-get update")
    run_cmd("apt-get install -y git python3-flask python3-gpiozero python3-requests python3-lgpio network-manager")

    if not os.path.exists(os.path.join(APP_DIR, ".git")):
        print(f"Klonowanie repozytorium {GITHUB_REPO_URL} do {APP_DIR}...")
        shutil.rmtree(APP_DIR, ignore_errors=True)
        res = subprocess.run(f"git clone {GITHUB_REPO_URL} {APP_DIR}", shell=True)
        if res.returncode != 0:
            print("Klonowanie nie powiodło się - kopiuję pliki lokalne do katalogu docelowego...")
            os.makedirs(APP_DIR, exist_ok=True)
            for fname in ["app.py", "admin.py", "version.txt"]:
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
    print("\n[OK] Instalacja zakończona! Czujnik działa w tle i włączy się sam po każdym zaniku prądu.")


def option_2_update_program():
    print("\n=== [2] SPRAWDZANIE AKTUALIZACJI NA GITHUBIE (coffynerd/detektor) ===")
    local_ver = get_local_version()
    remote_ver = get_remote_version()

    print(f"Wersja zainstalowana lokalnie : {local_ver}")
    print(f"Wersja dostępna na GitHubie   : {remote_ver or 'Błąd połączenia'}")

    if not remote_ver:
        return

    if parse_version(remote_ver) > parse_version(local_ver):
        print(f"\n[!] Dostępna jest NOWA wersja ({remote_ver})!")
        choice = input("Czy chcesz zaktualizować program teraz? (t/n): ").strip().lower()
    else:
        print("\n[i] Posiadasz aktualną wersję programu.")
        choice = input("Czy mimo to wymusić ponowne pobranie plików z GitHuba? (t/n): ").strip().lower()

    if choice == "t":
        if os.path.exists(os.path.join(APP_DIR, ".git")):
            run_cmd(f"cd {APP_DIR} && git fetch --all && git reset --hard origin/main")
        else:
            run_cmd(f"rm -rf {APP_DIR} && git clone {GITHUB_REPO_URL} {APP_DIR}")
        run_cmd(f"systemctl restart {SERVICE_NAME}")
        print(f"\n[OK] Zaktualizowano do wersji {get_local_version()} i zrestartowano usługę czujnika!")


def option_3_wifi():
    print("\n=== [3] KONFIGURACJA SIECI WI-FI ===")
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
        print("\nPróba połączenia przez NetworkManager (nmcli)...")
        run_cmd(f'nmcli dev wifi connect "{ssid}" password "{password}"')


def option_4_system_update():
    print("\n=== [4] AKTUALIZACJA SYSTEMU LINUX (APT) ===")
    run_cmd("apt-get update && apt-get upgrade -y && apt-get autoremove -y")
    print("\n[OK] System operacyjny został zaktualizowany.")


def option_5_reboot():
    print("\n=== [5] PONOWNE URUCHOMIENIE ===")
    print("a) Szybki restart samego programu czujnika (trwa 2 sekundy)")
    print("b) Pełny restart całej malinki (sudo reboot)")
    print("c) Anuluj")
    sub = input("Wybierz (a/b/c): ").strip().lower()

    if sub == "a":
        run_cmd(f"systemctl restart {SERVICE_NAME}")
        print("[OK] Usługa czujnika została zrestartowana.")
    elif sub == "b":
        print("Restartowanie Raspberry Pi... Połączenie SSH zostanie przerwane.")
        run_cmd("reboot")


def main():
    if os.geteuid() != 0:
        print("[!] Uruchom ten skrypt z uprawnieniami administratora:")
        print("    sudo python3 admin.py")
        sys.exit(1)

    while True:
        print("\n" + "=" * 55)
        print(f" PANEL ADMINISTRATORA - DETEKTOR (Wersja: {get_local_version()})")
        print("=" * 55)
        print("1. Zainstaluj oprogramowanie (pierwszy start / po awarii)")
        print("2. Zaktualizuj program z GitHuba (sprawdź version.txt)")
        print("3. Konfiguracja Wi-Fi")
        print("4. Aktualizuj system operacyjny (apt update & upgrade)")
        print("5. Uruchom ponownie czujnik / malinkę (reboot)")
        print("0. Wyjdź")
        print("-" * 55)

        choice = input("Wybierz opcję [0-5]: ").strip()
        if choice == "1":
            option_1_install()
        elif choice == "2":
            option_2_update_program()
        elif choice == "3":
            option_3_wifi()
        elif choice == "4":
            option_4_system_update()
        elif choice == "5":
            option_5_reboot()
        elif choice == "0":
            print("Do zobaczenia!")
            break
        else:
            print("[!] Nieprawidłowy wybór.")


if __name__ == "__main__":
    main()