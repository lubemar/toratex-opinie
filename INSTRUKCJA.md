# Instrukcja wdrożenia — monitoring opinii produktowych Allegro

Czas wdrożenia: ~45–60 min. Koszt stały: 0 zł. Jedyny potencjalny koszt: przekroczenie
darmowego kredytu $5/mies. w Apify (dostaniesz alert przy 80%).

## Czego potrzebujesz

| Konto | Po co | Koszt |
|---|---|---|
| GitHub | hosting kodu, cron, dashboard | 0 zł |
| Allegro Developer | darmowe API (delta tracking) | 0 zł |
| Apify | scraping treści opinii | 0 zł (kredyt $5/mies.) |
| healthchecks.io | watchdog "czy system żyje" | 0 zł |
| Gmail (istniejące) | wysyłka alertów | 0 zł |

---

## Krok 1. Repozytorium na GitHubie

1. Załóż konto na github.com (jeśli nie masz).
2. Utwórz nowe repozytorium, np. `toratex-opinie`. Musi być **publiczne**
   (darmowy GitHub Pages działa tylko z publicznym repo; opinie i tak są publiczne
   na Allegro — w repo NIE ma żadnych haseł, wszystkie sekrety trzymane są osobno).
3. Wgraj całą zawartość tego folderu do repo (przeciągnij pliki w interfejsie
   webowym GitHuba: Add file → Upload files; albo przez git).

**Ważne:** folder `.github/workflows/monitor.yml` musi trafić do repo z dokładnie
taką ścieżką — to definicja automatu.

## Krok 2. Aplikacja w Allegro Developer

1. Wejdź na https://apps.developer.allegro.pl (zaloguj się na DOWOLNE swoje konto Allegro).
2. Utwórz nową aplikację:
   - typ: zaznacz opcję dla **urządzeń / aplikacji bez przeglądarki** (device flow),
   - uprawnienia (scope): odczyt ofert — `allegro:api:sale:offers:read`.
3. Zapisz **Client ID** i **Client Secret**.

Jedna aplikacja obsłuży oba konta sprzedażowe — autoryzujesz ją dwa razy.

## Krok 3. Autoryzacja obu kont Allegro (jednorazowa, lokalnie)

Na swoim komputerze (wymagany Python: https://python.org, przy instalacji zaznacz
"Add to PATH"):

```
pip install requests
python scripts/authorize.py
```

Skrypt poprosi o Client ID/Secret i wyświetli link. **Zanim klikniesz Zezwól,
upewnij się, że w przeglądarce jesteś zalogowany na właściwe konto** (najpierw
tora_official, potem powtórz całość dla toratex_pl). Skrypt wypisze REFRESH TOKEN —
skopiuj go do sekretów (krok 6).

## Krok 4. Apify

1. Załóż konto na apify.com (plan Free — $5 kredytu/mies.).
2. Skopiuj API token: Settings → Integrations → API tokens.
3. (Opcjonalnie) Drugi token z konta innego użytkownika → sekret `APIFY_TOKEN_2`;
   system przełączy się na niego automatycznie, gdy pierwszemu skończy się kredyt.

**Weryfikacja aktora (ważne):** domyślny aktor to `tri_angle/allegro-reviews-scraper`.
Przed pierwszym backfillem odpal go RĘCZNIE w konsoli Apify na 1–2 ofertach
i sprawdź: (a) jego cennik per start/per result, (b) format wejścia. Jeśli wejście
nie nazywa się `startUrls`, zmień klucz w pliku `apify_input.json` (pole `__urlsKey`;
`__urlsFormat`: `objects` = lista `{"url": ...}`, `strings` = lista samych URL-i).
Jeśli używasz innego aktora, ustaw zmienną repo `APIFY_ACTOR_ID` (krok 6).

## Krok 5. Personal Access Token GitHuba (do rotacji tokenów Allegro)

Refresh tokeny Allegro żyją ~3 miesiące, ale przy codziennym użyciu Allegro wydaje
nowe — system zapisuje je automatycznie w sekretach repo. Do tego potrzebuje:

1. GitHub → Settings (Twojego profilu) → Developer settings → Personal access tokens
   → Tokens (classic) → Generate new token.
2. Zakres (scope): **repo**. Ważność: 1 rok (wpisz przypomnienie w kalendarz —
   GitHub i tak wyśle mail przed wygaśnięciem).
3. Zapisz jako sekret `GH_PAT` (krok 6).

Bez GH_PAT system działa, ale rotacja tokenów Allegro nie — dostaniesz wtedy
ostrzeżenie mailowe po 75 dniach i będziesz musiał ręcznie powtórzyć krok 3
co ~3 miesiące.

## Krok 6. Sekrety i zmienne repo

Repo → Settings → Secrets and variables → Actions → **New repository secret**:

| Sekret | Wartość |
|---|---|
| `ALLEGRO_CLIENT_ID` | z kroku 2 |
| `ALLEGRO_CLIENT_SECRET` | z kroku 2 |
| `ALLEGRO_REFRESH_TOKEN_TORA` | refresh token konta tora_official (krok 3) |
| `ALLEGRO_REFRESH_TOKEN_TORATEX` | refresh token konta toratex_pl (krok 3) |
| `APIFY_TOKEN` | token Apify (krok 4) |
| `APIFY_TOKEN_2` | (opcjonalnie) drugi token Apify |
| `GH_PAT` | personal access token (krok 5) |
| `SMTP_USER` | Twój adres Gmail |
| `SMTP_PASS` | hasło aplikacji Gmail (patrz niżej) |
| `ALERT_EMAIL` | adres, na który mają przychodzić alerty |
| `HEALTHCHECK_URL` | ping URL z healthchecks.io (krok 7) |

**Hasło aplikacji Gmail:** myaccount.google.com → Bezpieczeństwo → włącz weryfikację
dwuetapową → wyszukaj "Hasła aplikacji" → utwórz nowe. To NIE jest Twoje zwykłe hasło.

W zakładce **Variables** (nie Secrets) możesz dodać opcjonalnie:

| Zmienna | Wartość |
|---|---|
| `APIFY_ACTOR_ID` | inny aktor niż domyślny, np. `uzytkownik/nazwa-aktora` |
| `DASHBOARD_URL` | adres dashboardu (do linków w mailach), np. `https://TWOJLOGIN.github.io/toratex-opinie/` |

## Krok 7. Watchdog — healthchecks.io

1. Załóż darmowe konto na healthchecks.io.
2. Utwórz check "opinie-allegro": Period = 1 dzień, Grace = 3 godziny.
3. Skopiuj **ping URL** → sekret `HEALTHCHECK_URL`.
4. W Integrations ustaw powiadomienie na Twój e-mail.

Od teraz: jeśli system nie zamelduje się przez ~27 h (np. GitHub w ogóle nie odpalił
crona), dostaniesz mail z healthchecks.io. To łapie awarie, o których GitHub sam
nie informuje. Dodatkowo workflow pinguje `/fail` przy błędzie — alert przychodzi
natychmiast, nie po 27 h.

## Krok 8. Dashboard — GitHub Pages

Repo → Settings → Pages → Source: **Deploy from a branch** → Branch: `main`,
folder: `/docs` → Save. Po 1–2 min dashboard będzie pod adresem
`https://TWOJLOGIN.github.io/toratex-opinie/`.

## Krok 9. Pierwsze uruchomienie

1. Repo → Actions → "Monitor opinii Allegro" → **Run workflow** (bez backfill).
   To przebieg bazowy: zapisze stan ratingów, nic nie scrapuje.
2. (Zalecane) Drugi raz: Run workflow z zaznaczonym **backfill** — pobierze
   WSZYSTKIE istniejące opinie (jednorazowo; przy ~380 opiniach koszt rzędu
   $1–3 kredytu, zweryfikuj cennik aktora — krok 4).
3. Sprawdź dashboard i to, czy przyszedł ping na healthchecks.io.

Od tej pory wszystko dzieje się samo, codziennie ok. 06:00.

---

## Jakie maile będziesz dostawać

| Mail | Kiedy |
|---|---|
| [ALERT] Negatywna opinia | nowa opinia ≤3★ — z treścią i linkiem |
| [OK] Tygodniowy digest | KAŻDY poniedziałek — dowód, że system żyje; jego brak = system padł |
| [AWARIA] Token Allegro / Apify | natychmiast przy błędzie |
| [UWAGA] Rotacja tokena / kredyt Apify 80% | prewencyjnie, zanim coś padnie |
| healthchecks.io | system nie zameldował się ~27 h |
| GitHub | czerwony run workflow |

## Najczęstsze problemy

- **Workflow czerwony przy pierwszym runie** — najpewniej literówka w nazwie sekretu.
  Zajrzyj w logi: Actions → kliknij run → krok "Monitoring".
- **Aktor Apify zwraca 0 itemów** — sprawdź format wejścia (krok 4, `apify_input.json`).
- **Endpoint ratingu zwraca błąd** — w logach zobaczysz odpowiedź API; daj znać,
  poprawimy parsowanie (plik `scripts/monitor.py`, funkcja `fetch_rating`).
- **Re-autoryzacja Allegro** (gdy rotacja padła): powtórz krok 3, podmień sekret.
- **Cron nie odpala się od 60+ dni nieaktywności** — nie dotyczy: codzienny commit
  danych utrzymuje repo jako aktywne.

## Pliki w repo

```
.github/workflows/monitor.yml   automat (cron + ręczne uruchomienie)
scripts/monitor.py              cała logika: Tier 1, Tier 2, alerty, digest
scripts/authorize.py            jednorazowa autoryzacja kont Allegro
apify_input.json                konfiguracja wejścia aktora Apify
data/state.json                 ostatni znany stan ratingów (Tier 1)
data/history.jsonl              dziennik przebiegów (do digestu)
data/config.json                wykluczenia ofert
docs/index.html                 dashboard (GitHub Pages)
docs/data/reviews.json          wszystkie opinie (czyta dashboard)
docs/data/meta.json             status ostatniego synca (czyta dashboard)
```
