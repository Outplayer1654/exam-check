"""
Проверка свободных дат экзамена по вождению (Женева, DispoWeb) + уведомление в Telegram.

Только ЧИТАЕТ свободные даты. Ничего не бронирует и не переносит.

Переменные окружения (в GitHub — Settings → Secrets and variables → Actions):
  DISPO_NO_REGISTRE   — номер регистра (N° de registre), как при входе на сайт
  DISPO_BIRTHDAY      — дата рождения в формате ДД.ММ.ГГГГ
  DISPO_RDV_ID        — номер записи из ссылки (например 20261115161803480)
  TELEGRAM_BOT_TOKEN  — токен бота от @BotFather
  TELEGRAM_CHAT_ID    — ваш chat id
Необязательные:
  END_DATE            — до какой даты искать, ГГГГ-ММ-ДД (по умолчанию 2027-03-31)
  LIEU                — место экзамена (по умолчанию SANCAROUGE = OCV Carouge)
  STATE_FILE          — файл, где запоминается, о чём уже сообщали (по умолчанию state.json)
  DRY_RUN=1           — не отправлять в Telegram, только печатать
"""

import datetime as dt
import hashlib
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://ge.ch/tradispoweb_public"
PAGE_URL = BASE + "/ui/app/init/conduite/prive/rendez-vous/{rdv}"
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Zurich")  # Женева, с учётом летнего времени
except Exception:
    TZ = dt.timezone(dt.timedelta(hours=1))


def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        sys.exit(f"Не задана переменная {name}")
    return v


cookies = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36",
}


def request(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=HEADERS)
    try:
        with opener.open(req, timeout=30) as r:
            text = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(text) if text.strip() else None)
    except urllib.error.HTTPError as e:
        return e.code, None


def login(no_registre, birthday):
    # Открыть страницу, чтобы получить cookie сайта
    request("GET", "/ui/app/init/conduite/prive/login")
    digits = birthday.replace(".", "").replace("/", "").replace("-", "")
    # Поле на сайте с маской 99.99.9999 — пробуем без точек и с точками
    for date_value in (digits, f"{digits[:2]}.{digits[2:4]}.{digits[4:]}"):
        status, _ = request("POST", "/login/loginConduitePrive/fr",
                            {"noRegistre": no_registre, "dateNaissance": date_value, "captcha": None})
        if status == 200:
            s, who = request("GET", "/login/restoreUserSession/CONDUITE_PRIVE")
            if s == 200 and who and who.get("personneID"):
                return True
    return False


def fetch_slots(rdv_id, lieu, end_date):
    slots = []
    base = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(60):  # не больше 60 недель
        body = {
            "typeCategorieLieuDispo": {
                "dateBase": base.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "idLieuExamen": lieu,
                "idGroupeExamen": "EXAMENCONDUITE",
                "idCategorie": "B",
                "idTypeExamen": "PRATIQUEAUTO",
                "idGenreExamen": "STANDARD",
            },
            "rendezVous": rdv_id,
        }
        status, data = request("POST", "/lieu-examen/listeJourHeureLibre", body)
        if status == 401:
            raise RuntimeError("Сессия не принята (401) — вход не удался или истёк")
        if status != 200 or data is None:
            raise RuntimeError(f"Ответ сайта: HTTP {status}")
        if data.get("retCode") not in (None, "0000"):
            raise RuntimeError(f"Сайт вернул ошибку: {data.get('retCode')} {data.get('retMessage')}")
        for s in data.get("listeJourHeureLibreDispo") or []:
            day = dt.datetime.fromtimestamp(s["jour"] / 1000, TZ).date()
            if day <= end_date:
                slots.append((day, int(s.get("heure", 0)), int(s.get("minute", 0))))
        week_start = dt.datetime.fromtimestamp((data.get("weekDay") or base.timestamp() * 1000) / 1000, dt.timezone.utc)
        base = max(base, week_start) + dt.timedelta(days=7)
        if not data.get("nextWeek") or base.date() > end_date:
            break
        time.sleep(1)  # бережно к сайту
    return sorted(set(slots))


def telegram(text):
    if env("DRY_RUN") == "1":
        print("[DRY_RUN] Telegram:\n" + text)
        return
    token = env("TELEGRAM_BOT_TOKEN", required=True)
    chat = env("TELEGRAM_CHAT_ID", required=True)
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=30).read()


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(path, state):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)


def main():
    no_registre = env("DISPO_NO_REGISTRE", required=True)
    birthday = env("DISPO_BIRTHDAY", required=True)
    rdv_id = env("DISPO_RDV_ID", required=True)
    lieu = env("LIEU", "SANCAROUGE")
    end_date = dt.date.fromisoformat(env("END_DATE", "2027-03-31"))
    state_path = env("STATE_FILE", "state.json")
    state = load_state(state_path)
    link = PAGE_URL.format(rdv=rdv_id)

    try:
        if not login(no_registre, birthday):
            raise RuntimeError("Не удалось войти: проверьте номер регистра и дату рождения "
                               "(или на сайте включили капчу)")
        slots = fetch_slots(rdv_id, lieu, end_date)
    except Exception as e:
        msg = f"⚠️ Проверка экзамена не сработала: {e}"
        print(msg)
        if state.get("last_error") != str(e):  # про одну и ту же ошибку сообщаем один раз
            telegram(msg + f"\n{link}")
            state["last_error"] = str(e)
            save_state(state_path, state)
        sys.exit(1)

    state.pop("last_error", None)
    print(f"Проверено до {end_date:%d.%m.%Y}: свободных слотов {len(slots)}")

    if slots:
        lines = [f"• {d:%d.%m.%Y} ({['пн','вт','ср','чт','пт','сб','вс'][d.weekday()]}) {h:02d}:{m:02d}"
                 for d, h, m in slots]
        fingerprint = hashlib.sha1("\n".join(lines).encode()).hexdigest()
        if state.get("last_slots") != fingerprint:  # сообщаем только если список изменился
            text = (f"🚗 Найдены свободные даты экзамена ({lieu}):\n" + "\n".join(lines[:30]) +
                    (f"\n…и ещё {len(lines) - 30}" if len(lines) > 30 else "") +
                    f"\n\nЗаписаться: {link}")
            telegram(text)
            state["last_slots"] = fingerprint
        for line in lines:
            print(line)
    else:
        state.pop("last_slots", None)

    save_state(state_path, state)


if __name__ == "__main__":
    main()
