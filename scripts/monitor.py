#!/usr/bin/env python3
"""
Monitoring opinii produktowych Allegro (TORATEX).
Tier 1: delta tracking przez Allegro API (darmowe).
Tier 2: targeted scraping przez Apify — tylko oferty ze zmianą ratingu.
Alerty e-mail: negatywne opinie, awarie tokenów, kredyt Apify, digest poniedziałkowy.
"""
import datetime as dt
import hashlib
import json
import os
import re
import smtplib
import subprocess
import sys
import time
from email.mime.text import MIMEText
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter, Retry

SESSION = requests.Session()
SESSION.mount("https://", HTTPAdapter(max_retries=Retry(
    total=4, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=("GET", "POST"))))

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"                # stan wewnętrzny
PUB = ROOT / "docs" / "data"        # dane publiczne dla dashboardu

ENV = os.environ.get

SHOPS = [
    {"key": "tora_official", "secret_name": "ALLEGRO_REFRESH_TOKEN_TORA"},
    {"key": "toratex_pl", "secret_name": "ALLEGRO_REFRESH_TOKEN_TORATEX"},
]

NEGATIVE_THRESHOLD = 3
_RATING_DEBUGGED = False
_OFFER_DEBUGGED = False
ROTATION_WARN_DAYS = 75   # ostrzeżenie, gdy rotacja refresh tokena nie działa tyle dni
APIFY_CREDIT_WARN = 0.80  # alert przy 80% zużycia miesięcznego kredytu


# ---------------------------------------------------------------- narzędzia

def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t=None):
    return (t or now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def log(msg):
    print(f"[{iso()}] {msg}", flush=True)


# ---------------------------------------------------------------- e-mail

def send_email(subject, html):
    user, pwd, to = ENV("SMTP_USER"), ENV("SMTP_PASS"), ENV("ALERT_EMAIL")
    if not (user and pwd and to):
        log(f"MAIL POMINIĘTY (brak konfiguracji SMTP): {subject}")
        return False
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pwd)
            s.sendmail(user, [to], msg.as_string())
        log(f"MAIL WYSŁANY: {subject}")
        return True
    except Exception as e:
        log(f"BŁĄD WYSYŁKI MAILA ({subject}): {e}")
        return False


def dashboard_link():
    url = ENV("DASHBOARD_URL")
    if url:
        return f'<p><a href="{url}">Otwórz dashboard</a></p>'
    return ""


# ---------------------------------------------------------------- Allegro

def allegro_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.allegro.public.v1+json",
    }


def rotate_secret(name, value):
    """Zapisuje nowy refresh token jako sekret repo przez gh CLI (wymaga GH_PAT)."""
    repo = ENV("GH_REPO") or ENV("GITHUB_REPOSITORY") or ""
    if not ENV("GH_TOKEN"):
        log(f"UWAGA: brak GH_PAT — nowy refresh token {name} NIE został zapisany. "
            f"Rotacja nie działa, token umrze po ~3 miesiącach.")
        return False
    try:
        subprocess.run(
            ["gh", "secret", "set", name, "--repo", repo, "--body", value],
            check=True, capture_output=True, text=True, timeout=60,
        )
        log(f"Sekret zaktualizowany: {name}")
        return True
    except Exception as e:
        log(f"UWAGA: aktualizacja sekretu {name} nie powiodła się: {e}")
        return False


def refresh_allegro_token(shop, meta):
    """Odświeża access token; rotuje refresh token w sekretach repo."""
    cid, csec = ENV("ALLEGRO_CLIENT_ID"), ENV("ALLEGRO_CLIENT_SECRET")
    rt = ENV(shop["secret_name"])
    if not (cid and csec and rt):
        raise RuntimeError(f"Brak sekretów Allegro dla {shop['key']}")
    r = requests.post(
        "https://allegro.pl/auth/oauth/token",
        auth=(cid, csec),
        data={"grant_type": "refresh_token", "refresh_token": rt},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Refresh tokena {shop['key']}: HTTP {r.status_code} {r.text[:300]}")
    tok = r.json()
    auth_meta = meta.setdefault("auth", {}).setdefault(shop["key"], {})
    new_rt = tok.get("refresh_token")
    if new_rt and new_rt != rt:
        if rotate_secret(shop["secret_name"], new_rt):
            auth_meta["lastRotation"] = iso()
    else:
        auth_meta.setdefault("lastRotation", iso())
    auth_meta["lastRefreshOk"] = iso()
    return tok["access_token"]


def fetch_offers(token):
    """Wszystkie aktywne oferty konta (paginacja)."""
    out, offset, limit = [], 0, 1000
    while True:
        r = SESSION.get(
            "https://api.allegro.pl/sale/offers",
            headers=allegro_headers(token),
            params={"publication.status": "ACTIVE", "limit": limit, "offset": offset},
            timeout=60,
        )
        r.raise_for_status()
        chunk = r.json().get("offers", [])
        out.extend(chunk)
        if len(chunk) < limit:
            return out
        offset += limit


def fetch_rating(token, offer_id):
    """Rating oferty: liczba opinii, średnia, rozkład 1-5. Parsowanie defensywne."""
    r = SESSION.get(
        f"https://api.allegro.pl/sale/offers/{offer_id}/rating",
        headers=allegro_headers(token), timeout=30,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    j = r.json()
    global _RATING_DEBUGGED
    if not _RATING_DEBUGGED:
        _RATING_DEBUGGED = True
        log(f"RATING RAW (oferta {offer_id}): {json.dumps(j, ensure_ascii=False)[:500]}")

    # niektore API zagniezdzaja dane pod kluczem "rating"
    if isinstance(j.get("rating"), dict):
        j = {**j, **j["rating"]}

    def _num(v):
        try:
            return float(str(v).replace(",", "."))
        except (ValueError, TypeError):
            return None

    avg = None
    for k in ("averageScore", "averageRating", "average", "avg", "value", "score", "rating"):
        avg = _num(j.get(k))
        if avg is not None:
            break

    dist = {str(k): 0 for k in range(1, 6)}
    raw_dist = (j.get("scoreDistribution") or j.get("ratingCountDistribution")
                or j.get("distribution") or j.get("ratings") or j.get("stars") or [])
    if isinstance(raw_dist, dict):
        for star, cnt in raw_dist.items():
            s = str(star)
            if s in dist:
                dist[s] = int(_num(cnt) or 0)
    elif isinstance(raw_dist, list):
        for d in raw_dist:
            if not isinstance(d, dict):
                continue
            star = str(d.get("name") or d.get("rating") or d.get("value") or d.get("star") or "")
            cnt = d.get("count")
            if cnt is None:
                cnt = d.get("quantity")
            if cnt is None:
                cnt = d.get("amount")
            if star in dist:
                dist[star] = int(_num(cnt) or 0)

    total = None
    for k in ("totalResponses", "ratingCount", "ratingsCount", "totalCount", "count", "total", "opinionsCount", "reviewsCount", "size"):
        total = _num(j.get(k))
        if total is not None:
            break
    if total is None:
        total = sum(dist.values())
    return {"avg": round(avg or 0, 2), "total": int(total), "dist": dist}


# ---------------------------------------------------------------- Tier 1: delta

def collect_current_state(meta, alerts):
    """Zbiera aktualny stan ratingów ze wszystkich kont. Zwraca dict offerId -> stan."""
    config = load_json(DATA / "config.json", {})
    exclude = set(str(x) for x in config.get("excludeOfferIds", []))
    current, ok_shops = {}, 0
    for shop in SHOPS:
        try:
            token = refresh_allegro_token(shop, meta)
            offers = fetch_offers(token)
            log(f"{shop['key']}: {len(offers)} aktywnych ofert")
            global _OFFER_DEBUGGED
            for off in offers:
                oid = str(off.get("id"))
                if oid in exclude:
                    continue
                if not _OFFER_DEBUGGED:
                    _OFFER_DEBUGGED = True
                    log(f"OFFER KEYS: {sorted(off.keys())}")
                    log(f"OFFER external: {json.dumps(off.get('external'), ensure_ascii=False)}")
                    log(f"OFFER stock: {json.dumps(off.get('stock'), ensure_ascii=False)} "
                        f"stats: {json.dumps(off.get('stats'), ensure_ascii=False)}")
                rating = fetch_rating(token, oid)
                if rating is None:
                    continue
                ext = off.get("external") or {}
                sku = (ext.get("id") if isinstance(ext, dict) else "") or ""
                ean = (off.get("ean") or off.get("gtin") or off.get("eanCode") or "")
                stock = off.get("stock") or {}
                ostats = off.get("stats") or {}
                current[oid] = {
                    "name": off.get("name", ""),
                    "shop": shop["key"],
                    "url": f"https://allegro.pl/oferta/{oid}",
                    "sku": sku,
                    "ean": ean,
                    "sales": stock.get("sold"),
                    "stockAvailable": stock.get("available"),
                    "visits": ostats.get("visitsCount"),
                    "watchers": ostats.get("watchersCount"),
                    **rating,
                }
                time.sleep(0.05)  # grzecznościowy odstęp; limit API to 9000/min
            ok_shops += 1
        except Exception as e:
            log(f"BŁĄD konta {shop['key']}: {e}")
            alerts.append(("token", shop["key"], str(e)))
    if ok_shops == 0:
        raise RuntimeError("Żadne konto Allegro nie działa — przerywam bez nadpisania stanu.")
    failed = [a[1] for a in alerts if a[0] == "token"]
    return current, failed


def diff_state(prev, curr):
    """Porównuje stany. Zwraca listę ofert ze zmianą liczby/rozkładu opinii."""
    deltas = []
    for oid, c in curr.items():
        p = prev.get(oid)
        if p is None:
            if c["total"] > 0:
                deltas.append(_delta_entry(oid, c, {"avg": 0, "total": 0, "dist": {}}, new_offer=True))
            continue
        if c["total"] != p["total"] or c["dist"] != p.get("dist", {}):
            deltas.append(_delta_entry(oid, c, p))
    return deltas


def _delta_entry(oid, c, p, new_offer=False):
    per_star = {}
    for s in ("1", "2", "3", "4", "5"):
        d = c["dist"].get(s, 0) - p.get("dist", {}).get(s, 0)
        if d:
            per_star[s] = d
    return {
        "offerId": oid, "name": c["name"], "shop": c["shop"], "url": c["url"],
        "before": {"avg": p.get("avg", 0), "total": p.get("total", 0)},
        "after": {"avg": c["avg"], "total": c["total"]},
        "perStar": per_star, "newOffer": new_offer,
    }


# ---------------------------------------------------------------- Tier 2: Apify

class ApifyCreditError(Exception):
    pass


def apify_tokens():
    return [t for t in (ENV("APIFY_TOKEN"), ENV("APIFY_TOKEN_2")) if t]


def apify_usage(token):
    """Zwraca (zużycie USD, limit USD) albo None."""
    try:
        r = requests.get("https://api.apify.com/v2/users/me/limits",
                         params={"token": token}, timeout=30)
        if r.status_code != 200:
            return None
        d = r.json().get("data", {})
        cur = d.get("current", {}).get("monthlyUsageUsd")
        lim = d.get("limits", {}).get("maxMonthlyUsageUsd")
        if cur is None or not lim:
            return None
        return float(cur), float(lim)
    except Exception:
        return None


def run_actor(actor, token, payload, timeout_s=1500, actor_timeout=600):
    """Odpala aktora Apify i zwraca itemy z datasetu.
    actor_timeout = limit czasu pojedynczego runu po stronie Apify (sekundy)."""
    r = requests.post(
        f"https://api.apify.com/v2/acts/{actor}/runs",
        params={"token": token, "timeout": actor_timeout}, json=payload, timeout=60,
    )
    if r.status_code == 402:
        raise ApifyCreditError(f"Brak kredytu Apify (HTTP 402): {r.text[:200]}")
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Start aktora: HTTP {r.status_code} {r.text[:300]}")
    run = r.json()["data"]
    run_id = run["id"]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(10)
        rr = requests.get(f"https://api.apify.com/v2/actor-runs/{run_id}",
                          params={"token": token}, timeout=30)
        rr.raise_for_status()
        rd = rr.json()["data"]
        status = rd["status"]
        if status == "SUCCEEDED":
            ds = rd["defaultDatasetId"]
            items = requests.get(
                f"https://api.apify.com/v2/datasets/{ds}/items",
                params={"token": token, "format": "json", "clean": "true"},
                timeout=120,
            )
            items.raise_for_status()
            return items.json()
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            msg = rd.get("statusMessage", "")
            if "usage" in msg.lower() or "credit" in msg.lower() or "payment" in msg.lower():
                raise ApifyCreditError(f"Run {status}: {msg}")
            raise RuntimeError(f"Run Apify {status}: {msg}")
    raise RuntimeError("Run Apify: przekroczono limit czasu oczekiwania")


CHUNK_SIZE = 25  # ofert na jeden run aktora — Allegro blokuje masowe wejscia, wiec male paczki

def apify_scrape(urls, alerts, full=False):
    """Scrapuje opinie partiami (CHUNK_SIZE ofert/run). Tolerancja czesciowych bledow:
    jedna paczka padnie -> reszta leci dalej, zebrane opinie sie zapisuja.
    Fallback na drugi token przy braku kredytu. Zwraca (items, liczba_nieudanych_paczek).

    full=True (backfill): bez limitu opinii na produkt - komplet.
    full=False (codziennie): limit maxReviewsPerProduct z apify_input.json.
    """
    actor = (ENV("APIFY_ACTOR_ID") or "e-commerce/allegro-reviews-scraper").replace("/", "~")
    template = load_json(ROOT / "apify_input.json", {})
    urls_key = template.pop("__urlsKey", "startUrls")
    urls_format = template.pop("__urlsFormat", "objects")
    base = dict(template)
    if full:
        base.pop("maxReviewsPerProduct", None)

    tokens = apify_tokens()
    if not tokens:
        raise RuntimeError("Brak APIFY_TOKEN w sekretach")

    urls = list(urls)
    chunks = [urls[i:i + CHUNK_SIZE] for i in range(0, len(urls), CHUNK_SIZE)]
    all_items, failed = [], 0
    credit_dead = set()  # tokeny bez kredytu — nie probujemy ich ponownie

    for ci, chunk in enumerate(chunks, start=1):
        payload = dict(base)
        payload[urls_key] = ([{"url": u} for u in chunk] if urls_format == "objects"
                             else list(chunk))
        ok, last = False, None
        for ti, tok in enumerate(tokens, start=1):
            if ti in credit_dead:
                continue
            try:
                items = run_actor(actor, tok, payload)
                all_items.extend(items)
                log(f"Apify paczka {ci}/{len(chunks)} OK (token #{ti}): "
                    f"{len(items)} itemow [{len(chunk)} ofert]")
                ok = True
                break
            except ApifyCreditError as e:
                log(f"Token Apify #{ti}: brak kredytu — {e}")
                alerts.append(("apify_credit_exhausted", f"token #{ti}", str(e)))
                credit_dead.add(ti)
                last = e
            except Exception as e:
                log(f"Apify paczka {ci}/{len(chunks)} BLAD (token #{ti}): {e}")
                last = e
                break  # blad nie-kredytowy: nie probuj innego tokena dla tej paczki
        if not ok:
            failed += 1
        if len(credit_dead) == len(tokens):
            log("Wszystkie tokeny Apify bez kredytu — przerywam dalsze paczki")
            failed += len(chunks) - ci
            break

    if not all_items and failed:
        raise RuntimeError(f"Apify: wszystkie {failed} paczek nieudane (ostatni blad: {last})")
    if failed:
        log(f"Apify: {failed}/{len(chunks)} paczek nieudanych, "
            f"{len(all_items)} itemow zebranych mimo to")
    return all_items, failed


# ----------------------------------------------------- normalizacja opinii

def _pick(item, keys):
    for k in keys:
        v = item.get(k)
        if v not in (None, "", []):
            return v
    return None


def extract_offer_id(item):
    v = _pick(item, ["offerId", "offerID", "itemId", "offer_id"])
    if v:
        return str(v)
    for key in ("url", "offerUrl", "itemUrl", "link"):
        u = item.get(key) or ""
        m = re.search(r"/oferta/(?:[\w-]*-)?(\d{8,})", str(u)) or re.search(r"(\d{10,})", str(u))
        if m:
            return m.group(1)
    return None


def normalize_review(item, offer_lookup):
    """Schemat aktora e-commerce/allegro-reviews-scraper (zweryfikowany 06.2026):
    id, author{name}, opinion, rating{label,percentage}, pros, cons, createdAt,
    productTitle, url, seller{login}. Parsowanie pozostaje defensywne na wypadek zmian."""
    oid = extract_offer_id(item)

    rating_raw = _pick(item, ["rating", "score", "stars", "ratingValue"])
    if isinstance(rating_raw, dict):
        rating_raw = (rating_raw.get("label") or rating_raw.get("value")
                      or rating_raw.get("rating") or rating_raw.get("score"))
    try:
        rating = float(str(rating_raw).replace(",", ".")) if rating_raw is not None else None
    except (ValueError, TypeError):
        rating = None

    author_raw = _pick(item, ["author", "user", "username", "login", "reviewer", "buyer"]) or ""
    if isinstance(author_raw, dict):
        author_raw = author_raw.get("name") or author_raw.get("login") or ""

    content = _pick(item, ["opinion", "content", "text", "comment", "review", "description"]) or ""
    date = _pick(item, ["createdAt", "date", "publishedAt", "creationDate", "reviewDate"]) or ""
    pros = _pick(item, ["pros", "advantages", "positives"]) or ""
    cons = _pick(item, ["cons", "disadvantages", "negatives"]) or ""

    info = offer_lookup.get(oid, {})
    name = info.get("name") or str(item.get("productTitle") or "")[:90]

    rid = item.get("id")
    if not rid:
        rid = hashlib.sha1(f"{oid}|{author_raw}|{date}|{content}".encode()).hexdigest()[:16]

    return {
        "id": str(rid), "offerId": oid,
        "offerName": name, "shop": info.get("shop", ""),
        "url": info.get("url", f"https://allegro.pl/oferta/{oid}" if oid else ""),
        "rating": rating, "content": str(content).strip(),
        "author": str(author_raw), "date": str(date),
        "pros": str(pros), "cons": str(cons),
        "sku": info.get("sku", ""), "ean": info.get("ean", ""),
    }


def merge_reviews(stored, scraped_items, offer_lookup, requested_ids):
    """
    Dopisuje nowe opinie do istniejacych (dedupe po id). Nie usuwa starych -
    codzienny scrape jest przyciety limitem maxReviewsPerProduct, wiec nie jest
    kompletem. Usuniete na Allegro opinie zostaja w archiwum; pelne wyrownanie
    robi backfill (full=True, bez limitu).
    Zwraca (nowa_lista, nowe_opinie).
    """
    normalized = [normalize_review(it, offer_lookup) for it in scraped_items]
    normalized = [n for n in normalized if n["offerId"]]

    known_ids = {rv["id"] for rv in stored}
    scraped_offers = {n["offerId"] for n in normalized}
    ts = iso()
    new_reviews = []
    result = list(stored)
    for n in normalized:
        if n["id"] in known_ids:
            continue
        known_ids.add(n["id"])
        n["firstSeen"] = ts
        new_reviews.append(n)
        result.append(n)

    missing = requested_ids - scraped_offers
    if missing:
        log(f"UWAGA: aktor nie zwrocil opinii dla ofert: {sorted(missing)}")

    result.sort(key=lambda r: (r.get("date") or "", r.get("firstSeen") or ""), reverse=True)
    return result, new_reviews


# ---------------------------------------------------------------- alerty

def alert_negative_reviews(new_reviews):
    neg = [r for r in new_reviews if r.get("rating") is not None and r["rating"] <= NEGATIVE_THRESHOLD]
    if not neg:
        return
    rows = ""
    for r in neg:
        rows += (
            f'<li><b>{r["offerName"] or r["offerId"]}</b> ({r["shop"]}) — '
            f'<b>{r["rating"]:.0f}★</b><br>'
            f'„{r["content"] or "(bez treści)"}”<br>'
            f'{("Wady: " + r["cons"] + "<br>") if r["cons"] else ""}'
            f'<a href="{r["url"]}">Otwórz ofertę</a></li>'
        )
    send_email(
        f"[ALERT] Negatywna opinia na Allegro ({len(neg)})",
        f"<p>Nowe opinie z oceną ≤ {NEGATIVE_THRESHOLD}★:</p><ul>{rows}</ul>{dashboard_link()}",
    )


def alert_token_failures(alerts):
    fails = [a for a in alerts if a[0] == "token"]
    for _, shop, err in fails:
        send_email(
            f"[AWARIA] Konto Allegro: {shop}",
            f"<p>Pobieranie danych konta <b>{shop}</b> nie powiodło się:</p>"
            f"<pre>{err}</pre>"
            f"<p>Jeśli błąd się powtórzy, wykonaj ponowną autoryzację: "
            f"uruchom lokalnie <code>python scripts/authorize.py</code>, zaloguj się na konto "
            f"<b>{shop}</b> i wklej nowy refresh token do sekretów repo (Settings → Secrets).</p>",
        )


def check_rotation_age(meta):
    """Ostrzega, gdy rotacja refresh tokena nie zadziałała od ROTATION_WARN_DAYS dni."""
    for shop in SHOPS:
        a = meta.get("auth", {}).get(shop["key"], {})
        last = a.get("lastRotation")
        if not last:
            continue
        age = (now() - dt.datetime.fromisoformat(last.replace("Z", "+00:00"))).days
        if age >= ROTATION_WARN_DAYS:
            warned = a.get("rotationWarnedAt")
            if warned and (now() - dt.datetime.fromisoformat(warned.replace("Z", "+00:00"))).days < 7:
                continue
            send_email(
                f"[UWAGA] Token Allegro ({shop['key']}): rotacja nie działa od {age} dni",
                f"<p>Refresh token konta <b>{shop['key']}</b> nie został zrotowany od <b>{age} dni</b>. "
                f"Tokeny Allegro żyją ~3 miesiące — jeśli rotacja nie ruszy, system straci dostęp.</p>"
                f"<p>Sprawdź: czy sekret GH_PAT jest ustawiony i ważny? "
                f"W razie wątpliwości wykonaj ponowną autoryzację (scripts/authorize.py).</p>",
            )
            a["rotationWarnedAt"] = iso()


def check_apify_credit(meta):
    """Alert przy >= 80% zużycia kredytu (raz na miesiąc kalendarzowy, per token)."""
    month = now().strftime("%Y-%m")
    usage_summary = []
    for i, tok in enumerate(apify_tokens(), start=1):
        u = apify_usage(tok)
        if not u:
            continue
        cur, lim = u
        pct = cur / lim if lim else 0
        usage_summary.append({"token": i, "usedUsd": round(cur, 2), "limitUsd": lim,
                              "pct": round(pct * 100, 1)})
        key = f"apifyWarn_{i}"
        if pct >= APIFY_CREDIT_WARN and meta.get(key) != month:
            send_email(
                f"[UWAGA] Apify token #{i}: zużyto {pct * 100:.0f}% miesięcznego kredytu",
                f"<p>Zużycie: <b>${cur:.2f} / ${lim:.2f}</b>. Po wyczerpaniu kredytu "
                f"system przełączy się na drugi token (jeśli ustawiony) albo Tier 2 stanie. "
                f"Tier 1 (wykrywanie zmian) działa dalej niezależnie.</p>",
            )
            meta[key] = month
    meta["apifyUsage"] = usage_summary


def monday_digest(meta, history):
    """Poniedziałkowy dowód życia + statystyki tygodnia. Wysyłany ZAWSZE w poniedziałek."""
    if now().weekday() != 0:
        return
    if meta.get("digestSent") == now().strftime("%Y-%m-%d"):
        return
    week = history[-7:]
    new_total = sum(h.get("newReviews", 0) for h in week)
    neg_total = sum(h.get("negativeNew", 0) for h in week)
    runs = len(week)
    rows = "".join(
        f"<tr><td>{h['date']}</td><td>{h.get('offersChecked', '-')}</td>"
        f"<td>{h.get('deltas', 0)}</td><td>{h.get('newReviews', 0)}</td>"
        f"<td>{h.get('negativeNew', 0)}</td></tr>"
        for h in week
    )
    send_email(
        f"[OK] Tygodniowy digest opinii — system działa ({new_total} nowych, {neg_total} negatywnych)",
        f"<p>System monitoringu opinii <b>działa</b>. Ostatni udany przebieg: {iso()}.</p>"
        f"<p>Ostatnie {runs} przebiegów:</p>"
        f"<table border='1' cellpadding='4' cellspacing='0'>"
        f"<tr><th>Data</th><th>Ofert</th><th>Delty</th><th>Nowe opinie</th><th>Negatywne</th></tr>"
        f"{rows}</table>{dashboard_link()}"
        f"<p style='color:#888'>Ten mail przychodzi w każdy poniedziałek — również przy zerze zmian. "
        f"Jego brak oznacza, że system nie działa.</p>",
    )
    meta["digestSent"] = now().strftime("%Y-%m-%d")


def sales_band(n):
    """Anonimizacja sprzedazy do przedzialow (na publiczny dashboard)."""
    n = n or 0
    if n <= 0:
        return "0"
    if n < 10:
        return "1-9"
    if n < 50:
        return "10-49"
    if n < 100:
        return "50-99"
    return "100+"


def weekly_call_list(meta, current):
    """Poniedzialkowa lista do obdzwonki. DANE WRAZLIWE (sprzedaz, LN) — tylko mailem, nie na public."""
    if now().weekday() != 0:
        return
    if meta.get("callListSent") == now().strftime("%Y-%m-%d"):
        return
    opps = [c for c in current.values() if (c.get("sales") or 0) > 0 and c.get("total", 0) <= 2]
    opps.sort(key=lambda c: -(c.get("sales") or 0))
    low = [c for c in current.values()
           if c.get("total", 0) >= 3 and c.get("avg") and c["avg"] < 4.7]
    low.sort(key=lambda c: c.get("avg") or 0)
    if not opps and not low:
        return
    head = ("<tr><th>LN</th><th>Produkt</th><th>Sklep</th><th>Sprzedaz</th>"
            "<th>Opinie</th><th>Ocena</th><th></th></tr>")

    def rows(items):
        out = ""
        for c in items[:30]:
            out += (f"<tr><td>{c.get('sku') or '-'}</td>"
                    f"<td>{(c.get('name') or '')[:60]}</td>"
                    f"<td>{c.get('shop', '')}</td>"
                    f"<td align='right'>{c.get('sales') or 0}</td>"
                    f"<td align='right'>{c.get('total', 0)}</td>"
                    f"<td align='right'>{c.get('avg') or '-'}</td>"
                    f"<td><a href=\"{c.get('url', '')}\">oferta</a></td></tr>")
        return out

    html = "<p>Lista do obdzwonki (dane wrazliwe — wysylane tylko mailem, nie na dashboard).</p>"
    if opps:
        html += (f"<h3>Okazje: sprzedaje sie, ale &le;2 opinie ({len(opps)})</h3>"
                 f"<table border='1' cellpadding='4' cellspacing='0'>{head}{rows(opps)}</table>")
    if low:
        html += (f"<h3>Niska ocena (&lt;4,7 przy &ge;3 opiniach) ({len(low)})</h3>"
                 f"<table border='1' cellpadding='4' cellspacing='0'>{head}{rows(low)}</table>")
    send_email(f"[LISTA] Produkty do obdzwonki — {len(opps)} okazji, {len(low)} niskich ocen", html)
    meta["callListSent"] = now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------- main

def main():
    alerts = []
    meta = load_json(DATA / "meta.json", {})
    prev_state = load_json(DATA / "state.json", {})
    stored = load_json(PUB / "reviews.json", {"reviews": []}).get("reviews", [])
    history = [json.loads(l) for l in
               (DATA / "history.jsonl").read_text(encoding="utf-8").splitlines()
               ] if (DATA / "history.jsonl").exists() else []
    backfill = (ENV("BACKFILL") or "").lower() == "true"
    baseline = not prev_state

    meta["lastRun"] = iso()

    # --- Tier 1
    current, failed_shops = collect_current_state(meta, alerts)
    if failed_shops:
        kept = 0
        for oid, s in prev_state.items():
            if s.get("shop") in failed_shops and oid not in current:
                current[oid] = s
                kept += 1
        log(f"Awaria sklepow {failed_shops}: zachowano {kept} ofert z poprzedniego stanu "
            f"(unikamy falszywych delt i utraty danych)")
    deltas = diff_state(prev_state, current)
    log(f"Tier 1: {len(current)} ofert, {len(deltas)} z deltą"
        + (" (baseline — pierwszy przebieg)" if baseline else ""))

    # --- wybór ofert do scrape'u
    if backfill:
        to_scrape = {oid: c for oid, c in current.items() if c["total"] > 0}
        log(f"BACKFILL: scrape wszystkich {len(to_scrape)} ofert z opiniami")
    elif baseline:
        to_scrape = {}
        log("Baseline: zapisuję stan początkowy, bez scrape'u (użyj backfill, by pobrać historię)")
    else:
        to_scrape = {d["offerId"]: current[d["offerId"]] for d in deltas}

    new_reviews = []
    scrape_failed = False
    if to_scrape:
        urls = [c["url"] for c in to_scrape.values()]
        try:
            items, failed_chunks = apify_scrape(urls, alerts, full=backfill)
            stored, new_reviews = merge_reviews(stored, items, current, set(to_scrape.keys()))
            log(f"Tier 2: {len(new_reviews)} nowych opinii"
                + (f" ({failed_chunks} paczek nieudanych — sprobuje ponownie nastepnym razem)"
                   if failed_chunks else ""))
        except Exception as e:
            scrape_failed = True
            log(f"BŁĄD Tier 2: {e}")
            send_email(
                "[AWARIA] Scraping opinii (Apify) nie powiódł się",
                f"<p>Wykryto zmiany w {len(to_scrape)} ofertach, ale scrape nie zadziałał:</p>"
                f"<pre>{e}</pre><p>Stan NIE został nadpisany — następny przebieg spróbuje ponownie. "
                f"Sprawdź kredyt/token Apify oraz czy aktor "
                f"<code>{ENV('APIFY_ACTOR_ID') or 'e-commerce/allegro-reviews-scraper'}</code> działa.</p>",
            )

    # --- zapis stanu (przy porażce scrape'u zostawiamy stary stan => retry jutro)
    if not scrape_failed:
        slim_state = {oid: {"avg": c.get("avg", 0), "total": c.get("total", 0),
                            "dist": c.get("dist", {}), "shop": c.get("shop", "")}
                      for oid, c in current.items()}
        save_json(DATA / "state.json", slim_state)
    # LN zostaje (niskoryzykowne); wzbogacamy istniejace opinie o LN z aktualnych ofert (za darmo)
    for rv in stored:
        info = current.get(rv.get("offerId"))
        if info and not rv.get("sku"):
            rv["sku"] = info.get("sku", "")
    save_json(PUB / "reviews.json", {"updated": iso(), "reviews": stored})

    # --- alerty
    alert_negative_reviews(new_reviews)
    alert_token_failures(alerts)
    check_rotation_age(meta)
    check_apify_credit(meta)

    # --- historia + digest
    neg_new = len([r for r in new_reviews
                   if r.get("rating") is not None and r["rating"] <= NEGATIVE_THRESHOLD])
    entry = {"date": now().strftime("%Y-%m-%d"), "offersChecked": len(current),
             "deltas": len(deltas), "newReviews": len(new_reviews), "negativeNew": neg_new,
             "scrapeFailed": scrape_failed}
    history.append(entry)
    (DATA / "history.jsonl").parent.mkdir(parents=True, exist_ok=True)
    (DATA / "history.jsonl").write_text(
        "\n".join(json.dumps(h, ensure_ascii=False) for h in history[-400:]) + "\n",
        encoding="utf-8")
    monday_digest(meta, history)

    # --- meta dla dashboardu
    if not scrape_failed:
        meta["lastSuccess"] = iso()
    meta["offersChecked"] = len(current)
    meta["lastDeltas"] = len(deltas)
    meta["lastNewReviews"] = len(new_reviews)
    save_json(DATA / "meta.json", meta)

    # --- publiczna lista ofert (widok Produkty): ocena + liczba opinii + SKU/EAN per oferta
    offers_pub = []
    for oid, c in current.items():
        offers_pub.append({
            "offerId": oid, "name": c.get("name", ""), "shop": c.get("shop", ""),
            "url": c.get("url", ""), "sku": c.get("sku", ""),
            "avg": c.get("avg", 0), "reviews": c.get("total", 0), "dist": c.get("dist", {}),
            "salesBand": sales_band(c.get("sales")),
        })
    offers_pub.sort(key=lambda o: (o["reviews"], o["avg"]))  # najpierw braki opinii / niskie oceny
    save_json(PUB / "offers.json", {"updated": iso(), "offers": offers_pub})

    save_json(PUB / "meta.json", {
        "lastSuccess": meta.get("lastSuccess"), "lastRun": meta["lastRun"],
        "offersChecked": len(current), "lastDeltas": len(deltas),
        "lastNewReviews": len(new_reviews), "apifyUsage": meta.get("apifyUsage", []),
    })

    if scrape_failed:
        sys.exit(1)
    log("OK — przebieg zakończony")


if __name__ == "__main__":
    main()
