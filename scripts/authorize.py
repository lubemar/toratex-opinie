#!/usr/bin/env python3
"""
Jednorazowa autoryzacja konta Allegro (device flow).
Uruchom lokalnie: python scripts/authorize.py
Wykonaj OSOBNO dla każdego konta (tora_official, toratex_pl) —
przed kliknięciem "Zezwól" zaloguj się w przeglądarce na WŁAŚCIWE konto.
Wymaga: pip install requests
"""
import sys
import time

import requests

AUTH_DEVICE = "https://allegro.pl/auth/oauth/device"
AUTH_TOKEN = "https://allegro.pl/auth/oauth/token"


def main():
    print("=== Autoryzacja Allegro (device flow) ===")
    cid = input("Client ID aplikacji: ").strip()
    csec = input("Client Secret: ").strip()

    r = requests.post(AUTH_DEVICE, auth=(cid, csec), data={"client_id": cid}, timeout=30)
    if r.status_code != 200:
        sys.exit(f"Błąd ({r.status_code}): {r.text}\n"
                 "Upewnij się, że aplikacja w Allegro Developer ma włączony "
                 "typ DEVICE (urządzenie/aplikacja bez przeglądarki).")
    d = r.json()
    print("\n1. Zaloguj się w przeglądarce na WŁAŚCIWE konto sprzedawcy.")
    print(f"2. Otwórz ten link i kliknij Zezwól:\n\n   {d['verification_uri_complete']}\n")
    print("Czekam na potwierdzenie...")

    interval = int(d.get("interval", 5))
    while True:
        time.sleep(interval)
        rr = requests.post(
            AUTH_TOKEN, auth=(cid, csec),
            data={"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                  "device_code": d["device_code"]},
            timeout=30,
        )
        j = rr.json()
        if "refresh_token" in j:
            break
        err = j.get("error", "")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        sys.exit(f"Błąd autoryzacji: {j}")

    print("\n=== SUKCES ===")
    print("Skopiuj poniższy REFRESH TOKEN do sekretów repo na GitHubie")
    print("(Settings -> Secrets and variables -> Actions):")
    print("  - konto tora_official  -> sekret ALLEGRO_REFRESH_TOKEN_TORA")
    print("  - konto toratex_pl     -> sekret ALLEGRO_REFRESH_TOKEN_TORATEX\n")
    print(j["refresh_token"])
    print("\nUwaga: token jest poufny — nie wklejaj go nigdzie poza sekretami GitHuba.")


if __name__ == "__main__":
    main()
