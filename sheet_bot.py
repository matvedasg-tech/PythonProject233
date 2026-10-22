#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VK-бот: читает приватную Google-таблицу через сервис-аккаунт
и выдает статистику по запросу. С защитой от спама.
"""

import logging
import os
import re
import time
from datetime import datetime, timedelta, date
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.service_account import Credentials
from vk_api import VkApi
from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll
from vk_api.utils import get_random_id

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("sheetbot")

# ---------- конфиг ----------
TOKEN = (os.getenv("VK_TOKEN") or "").strip()
GROUP_ID = int(os.getenv("VK_GROUP_ID") or 0)
GOOGLE_CREDENTIALS = (os.getenv("GOOGLE_CREDENTIALS") or "service_account.json").strip()
SPREADSHEET_ID = (os.getenv("SPREADSHEET_ID") or "").strip()
WORKSHEET_NAME = (os.getenv("WORKSHEET_NAME") or "").strip()

# Защита от спама (в секундах)
COOLDOWN_SECONDS = 5

if not TOKEN or not GROUP_ID:
    raise SystemExit("Не заданы VK_TOKEN / VK_GROUP_ID")
if not SPREADSHEET_ID:
    raise SystemExit("Не задан SPREADSHEET_ID")
if not os.path.isfile(GOOGLE_CREDENTIALS):
    raise SystemExit(f"Не найден файл ключа: {GOOGLE_CREDENTIALS}")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"

# Хранилище для кулдауна: {user_id: {command: last_time}}
user_cooldowns = {}

# ---------- Google auth ----------
_creds: Credentials | None = None


def get_access_token() -> str:
    global _creds
    if _creds is None:
        _creds = Credentials.from_service_account_file(
            GOOGLE_CREDENTIALS, scopes=SCOPES
        )
    if not _creds.valid or _creds.expired:
        _creds.refresh(GoogleAuthRequest())
    return _creds.token


# ---------- чтение таблицы ----------
def _sheet_range() -> str:
    name = WORKSHEET_NAME or _resolve_first_sheet_name()
    safe_name = name.replace("'", "''")
    encoded_name = quote(safe_name, safe="")
    return f"{encoded_name}!A1:Z100000"


def _resolve_first_sheet_name() -> str:
    url = f"{SHEETS_API}/{SPREADSHEET_ID}"
    params = {"fields": "sheets.properties.title"}
    r = _google_get(url, params=params)
    data = r.json()
    sheets = data.get("sheets") or []
    if not sheets:
        raise RuntimeError("В таблице нет ни одного листа")
    return sheets[0]["properties"]["title"]


def _google_get(url: str, params: dict | None = None, retries: int = 3) -> requests.Response:
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            headers = {"Authorization": f"Bearer {get_access_token()}"}
            r = requests.get(url, params=params, headers=headers, timeout=30)
            if r.status_code == 401:
                global _creds
                _creds = None
                continue
            if r.status_code == 400:
                raise RuntimeError(f"Bad Request: {r.text}")
            r.raise_for_status()
            return r
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            log.warning("Сетевая ошибка (%s/%s): %s", attempt, retries, e)
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"Google API недоступен после {retries} попыток: {last_exc}")


def fetch_rows() -> list[dict]:
    url = f"{SHEETS_API}/{SPREADSHEET_ID}/values/{_sheet_range()}"
    params = {"majorDimension": "ROWS"}
    r = _google_get(url, params=params)
    values = r.json().get("values", [])
    if not values:
        return []

    header_idx = 0
    for i, row in enumerate(values[:5]):
        row_str = " ".join(str(c) for c in row).lower()
        if 'nickname' in row_str or 'должность' in row_str or 'id' in row_str:
            header_idx = i
            break

    headers = [str(h).strip() for h in values[header_idx]]
    rows = []
    for raw in values[header_idx + 1:]:
        raw = list(raw) + [""] * (len(headers) - len(raw))
        row = {
            (headers[i] or f"col{i + 1}"): str(raw[i]).strip()
            for i in range(len(headers))
        }
        if any(row.values()):
            rows.append(row)
    return rows


def _find_key(keys: list[str], *variants: str) -> str | None:
    for k in keys:
        clean_k = k.replace('\xa0', ' ').strip().lower()
        for v in variants:
            if v in clean_k:
                return k
    return None


# ---------- команды ----------
def get_agentometer() -> str:
    try:
        rows = fetch_rows()
    except Exception as e:
        return f"❌ Ошибка чтения таблицы: {e}"

    if not rows:
        return "🤖 Всего агентов поддержки: 0"

    keys = list(rows[0].keys())
    key_pos = _find_key(keys, 'должность', 'долж')

    if not key_pos:
        return "❌ Не найдена колонка с должностью."

    count_agents = 0
    count_candidates = 0

    for row in rows:
        position = str(row.get(key_pos, '')).strip().lower()
        if 'агент поддержки' in position:
            count_agents += 1
        elif 'кандидат' in position:
            count_candidates += 1

    response = f"🤖 Всего агентов поддержки: {count_agents}"

    if count_candidates > 0:
        response += f"\n📝 Всего кандидатов на администратора: {count_candidates}"

    return response


def parse_date_to_dt(date_str: str) -> date | None:
    if not date_str:
        return None

    match = re.search(r'(\d{2})\.(\d{2})\.(\d{4})', date_str)
    if match:
        try:
            return date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        except ValueError:
            pass

    match = re.search(r'(\d{4})-(\d{2})-(\d{2})', date_str)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            pass

    return None


def get_agent_count() -> str:
    try:
        rows = fetch_rows()
    except Exception as e:
        return f"❌ Ошибка чтения таблицы: {e}"

    if not rows:
        return "📊 Назначено за текущую неделю: 0\nНазначено за прошлую неделю: 0"

    keys = list(rows[0].keys())
    key_pos = _find_key(keys, 'должность', 'долж')
    key_date = _find_key(keys, 'назначен', 'дата')

    if not key_pos or not key_date:
        return "❌ Не найдены колонки с должностью или датой назначения."

    today = datetime.now().date()
    start_of_this_week = today - timedelta(days=today.weekday())
    end_of_this_week = start_of_this_week + timedelta(days=6)
    start_of_last_week = start_of_this_week - timedelta(days=7)
    end_of_last_week = start_of_this_week - timedelta(days=1)

    count_this_week = 0
    count_last_week = 0

    for row in rows:
        position = str(row.get(key_pos, '')).strip().lower()
        if 'агент поддержки' not in position:
            continue

        date_str = str(row.get(key_date, '')).strip()
        app_date = parse_date_to_dt(date_str)

        if not app_date:
            continue

        if start_of_this_week <= app_date <= end_of_this_week:
            count_this_week += 1
        elif start_of_last_week <= app_date <= end_of_last_week:
            count_last_week += 1

    return (
        f"📊 Назначено за текущую неделю: {count_this_week}\n"
        f"Назначено за прошлую неделю: {count_last_week}"
    )


def get_zvon_count() -> str:
    try:
        rows = fetch_rows()
    except Exception as e:
        return f"❌ Ошибка чтения таблицы: {e}"

    if not rows:
        return "🔔 Апов за сегодня: 0"

    keys = list(rows[0].keys())
    key_pos = _find_key(keys, 'должность', 'долж')
    key_date = _find_key(keys, 'назначен', 'дата')

    if not key_pos or not key_date:
        return "❌ Не найдены колонки с должностью или датой назначения."

    today = datetime.now().date()
    count = 0

    for row in rows:
        position = str(row.get(key_pos, '')).strip().lower()
        if 'агент поддержки' not in position:
            continue

        date_str = str(row.get(key_date, '')).strip()
        app_date = parse_date_to_dt(date_str)

        if app_date == today:
            count += 1

    return f"🔔 Апов за сегодня: {count}"


HELP_TEXT = (
    "📖 Команды:\n"
    "/агентометр — количество агентов и кандидатов\n"
    "/агентосчет — сколько назначено за эту и прошлую неделю\n"
    "/звоносчет — сколько ап поставлено сегодня"
)


def handle_message(event, vk) -> None:
    msg = event.object.message
    text = (msg.get("text") or "").strip().lower()
    peer_id = msg["peer_id"]
    from_id = msg["from_id"]

    if not text:
        return

    user_cmds = user_cooldowns.setdefault(from_id, {})
    now = time.time()

    def check_cooldown(cmd_name: str) -> bool:
        last_time = user_cmds.get(cmd_name, 0)
        if now - last_time < COOLDOWN_SECONDS:
            return False
        user_cmds[cmd_name] = now
        return True

    if text in ("/help", "/start", "/помощь"):
        if not check_cooldown("help"): return
        vk.messages.send(peer_id=peer_id, message=HELP_TEXT, random_id=get_random_id())
        return

    if text == "/агентометр":
        if not check_cooldown("agentometer"): return
        response = get_agentometer()
        vk.messages.send(peer_id=peer_id, message=response, random_id=get_random_id())
        return

    if text == "/агентосчет":
        if not check_cooldown("agentcount"): return
        response = get_agent_count()
        vk.messages.send(peer_id=peer_id, message=response, random_id=get_random_id())
        return

    if text == "/звоносчет":
        if not check_cooldown("zvoncount"): return
        response = get_zvon_count()
        vk.messages.send(peer_id=peer_id, message=response, random_id=get_random_id())
        return


# ---------- main ----------
def main() -> None:
    try:
        name = _resolve_first_sheet_name()
        log.info("Google Sheets доступен. Первый лист: %s", name)
    except Exception as e:
        raise SystemExit(
            "Не удалось открыть таблицу.\n"
            f"SPREADSHEET_ID={SPREADSHEET_ID}\n"
            f"Ошибка: {e}"
        )

    vk_session = VkApi(token=TOKEN)
    vk = vk_session.get_api()

    longpoll = VkBotLongPoll(vk_session, GROUP_ID)
    log.info("Бот запущен. Группа %s", GROUP_ID)

    for event in longpoll.listen():
        try:
            if event.type == VkBotEventType.MESSAGE_NEW:
                handle_message(event, vk)
        except Exception:
            log.exception("Ошибка обработки события")


if __name__ == "__main__":
    main()