# Monitoring opinii produktowych Allegro — TORATEX

Darmowy system monitoringu pełnych recenzji produktowych z Allegro dla kont
`tora_official` i `toratex_pl`. Zastępuje płatny dashboard zewnętrzny.

## Architektura: hybrid two-tier

- **Tier 1 (darmowy, codziennie ~06:00):** GitHub Actions odpytuje Allegro API
  o rating każdej aktywnej oferty i porównuje ze stanem z wczoraj (delta per gwiazdka).
- **Tier 2 (płatny, tylko przy zmianach):** oferty z deltą trafiają w JEDNYM runie
  do aktora Apify, który pobiera treści opinii. Brak zmian = zero kosztu.
- **Dashboard:** statyczna strona na GitHub Pages czyta `docs/data/*.json` —
  statystyki, filtry, baner statusu z datą ostatniego udanego synca.
- **Alerty e-mail:** negatywne opinie (≤3★) natychmiast; digest w każdy poniedziałek
  (dowód życia); awarie tokenów i kredytu Apify prewencyjnie.
- **Watchdog:** healthchecks.io dostaje ping po każdym udanym przebiegu —
  brak pingu >27 h = mail. Cisza nigdy nie oznacza niepewności.

Szczegóły wdrożenia: **INSTRUKCJA.md**.

## Zasady projektowe

1. Cisza domyślnie — mail tylko, gdy coś się zmieniło albo coś padło.
2. Cisza nie usypia — poniedziałkowy digest + zewnętrzny watchdog + baner na dashboardzie.
3. Awaria scrape'u nie nadpisuje stanu — delta zostanie wykryta ponownie następnego dnia.
4. Usunięte opinie też są wykrywane (delta ujemna => pełny re-scrape oferty).
