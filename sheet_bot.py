#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VK-бот: читает приватную Google-таблицу через сервис-аккаунт
и выдает статистику по запросу.
"""

import logging
import os
import re
import time
import threading
from datetime import datetime, timedelta, date
from urllib.parse import quote
import pytz

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account
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
SPREADSHEET_ID = (os.getenv("SPREADSHEET_ID") or "").strip()
WORKSHEET_NAME = (os.getenv("WORKSHEET_NAME") or "").strip()

WARNING_PEER_ID = int(os.getenv("WARNING_PEER_ID") or 0)
REPORT_USER_ID = int(os.getenv("REPORT_USER_ID") or 0)

COOLDOWN_SECONDS = 5
user_cooldowns = {}
last_warning_date = None
last_report_date = None

if not TOKEN or not GROUP_ID:
    raise SystemExit("Не заданы VK_TOKEN / VK_GROUP_ID")
if not SPREADSHEET_ID:
    raise SystemExit("Не задан SPREADSHEET_ID")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"

# ---------- Google auth ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDS_FILE = os.path.join(BASE_DIR, "service_account.json")

if not os.path.exists(CREDS_FILE):
    raise FileNotFoundError(
        f"Критическая ошибка: Файл service_account.json не найден по пути {CREDS_FILE}"
    )

_creds = service_account.Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)

def get_access_token() -> str:
    global _creds
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
                _creds = service_account.Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
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

# ---------- вспомогательные функции ----------
def get_agents_for_date(target_date: date) -> list[str]:
    try:
        rows = fetch_rows()
    except Exception:
        return []

    if not rows:
        return []

    keys = list(rows[0].keys())
    key_nick = _find_key(keys, 'nickname', 'никнейм', 'ник')
    key_pos = _find_key(keys, 'должность', 'долж')
    key_date = _find_key(keys, 'назначен', 'дата')

    if not key_pos or not key_date or not key_nick:
        return []

    agents = []
    for row in rows:
        position = str(row.get(key_pos, '')).strip().lower()
        if 'агент поддержки' not in position:
            continue
        date_str = str(row.get(key_date, '')).strip()
        app_date = parse_date_to_dt(date_str)
        if app_date == target_date:
            agents.append(str(row.get(key_nick, '')).strip())
    return agents

def get_total_agents() -> int:
    try:
        rows = fetch_rows()
    except Exception:
        return 0
    if not rows:
        return 0
    keys = list(rows[0].keys())
    key_pos = _find_key(keys, 'должность', 'долж')
    if not key_pos:
        return 0
    count = 0
    for r in rows:
        if 'агент поддержки' in str(r.get(key_pos, '')).lower():
            count += 1
    return count

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
    today = datetime.now().date()
    agents = get_agents_for_date(today)
    return f"🔔 Апов за сегодня: {len(agents)}"

def get_full_stats() -> str:
    try:
        rows = fetch_rows()
    except Exception as e:
        return f"❌ Ошибка чтения таблицы: {e}"

    if not rows:
        return "❌ Данные не найдены."

    keys = list(rows[0].keys())
    key_pos = _find_key(keys, 'должность', 'долж')
    key_date = _find_key(keys, 'назначен', 'дата')
    key_nick = _find_key(keys, 'nickname', 'никнейм', 'ник')

    if not key_pos or not key_date or not key_nick:
        return "❌ Не найдены нужные колонки для статистики."

    today = datetime.now().date()
    
    total_agents = 0
    total_candidates = 0
    agents_today = []
    ap_this_week = 0
    ap_last_week = 0

    start_of_this_week = today - timedelta(days=today.weekday())
    end_of_this_week = start_of_this_week + timedelta(days=6)
    start_of_last_week = start_of_this_week - timedelta(days=7)
    end_of_last_week = start_of_this_week - timedelta(days=1)

    for row in rows:
        position = str(row.get(key_pos, '')).strip().lower()
        
        if 'агент поддержки' in position:
            total_agents += 1
            
            date_str = str(row.get(key_date, '')).strip()
            app_date = parse_date_to_dt(date_str)
            
            if app_date:
                if app_date == today:
                    agents_today.append(str(row.get(key_nick, '')).strip())
                
                if start_of_this_week <= app_date <= end_of_this_week:
                    ap_this_week += 1
                elif start_of_last_week <= app_date <= end_of_last_week:
                    ap_last_week += 1
                    
        elif 'кандидат' in position:
            total_candidates += 1

    report = f"📊 Статистика на {today.strftime('%d.%m.%Y')}\n\n"
    report += f"👥 Всего агентов: {total_agents}\n"
    if total_candidates > 0:
        report += f"📝 Кандидатов: {total_candidates}\n"
    report += f"🔔 Апов сегодня: {len(agents_today)}\n"
    report += f"📈 За текущую неделю: {ap_this_week}\n"
    report += f"📉 За прошлую неделю: {ap_last_week}\n"
    
    if agents_today:
        report += "\n📌 Назначены сегодня:\n"
        for nick in agents_today:
            report += f"• {nick}\n"
            
    return report

HELP_TEXT = (
    "📖 Команды:\n"
    "/агентометр — количество агентов и кандидатов\n"
    "/агентосчет — сколько назначено за эту и прошлую неделю\n"
    "/звоносчет — сколько ап поставлено сегодня\n"
    "/стата — полная статистика на сегодня"
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

    if text == "/стата":
        if not check_cooldown("stats"): return
        response = get_full_stats()
        vk.messages.send(peer_id=peer_id, message=response, random_id=get_random_id())
        return

# ---------- ПЛАНИРОВЩИК ----------
def scheduler_loop(vk) -> None:
    global last_warning_date, last_report_date
    moscow_tz = pytz.timezone('Europe/Moscow')
    
    while True:
        try:
            now = datetime.now(moscow_tz)
            today_date = now.date()

            if WARNING_PEER_ID and now.hour == 0 and now.minute == 0 and last_warning_date != today_date:
                yesterday = today_date - timedelta(days=1)
                ap_yesterday = len(get_agents_for_date(yesterday))
                
                if ap_yesterday == 0:
                    total_agents = get_total_agents()
                    warning_text = f"⚠️ [АП] За вчерашний день было назначено 0 Агентов Поддержки!\nТекущее их число: {total_agents}"
                    try:
                        vk.messages.send(peer_id=WARNING_PEER_ID, message=warning_text, random_id=get_random_id())
                        log.info("Варнинг в беседу отправлен.")
                    except Exception as e:
                        log.error(f"Не удалось отправить варнинг: {e}")
                
                last_warning_date = today_date

            if REPORT_USER_ID and now.hour == 23 and now.minute == 55 and last_report_date != today_date:
                agents_today = get_agents_for_date(today_date)
                count_today = len(agents_today)
                total_agents = get_total_agents()
                
                report = f"📊 Отчет за {today_date.strftime('%d.%m.%Y')}\n\n"
                report += f"🔔 Апов за сегодня: {count_today}\n"
                report += f"👥 Всего агентов поддержки: {total_agents}\n"
                
                if agents_today:
                    report += "\n📝 Назначены сегодня:\n"
                    for nick in agents_today:
                        report += f"• {nick}\n"
                else:
                    report += "\n📝 Сегодня никто не назначен."
                
                try:
                    vk.messages.send(user_id=REPORT_USER_ID, message=report, random_id=get_random_id())
                    log.info("Отчет в ЛС отправлен.")
                except Exception as e:
                    log.error(f"Не удалось отправить отчет в ЛС: {e}")
                
                last_report_date = today_date

            time.sleep(30)
                
        except Exception as e:
            log.error(f"Ошибка в планировщике: {e}")
            time.sleep(60)

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

    threading.Thread(target=scheduler_loop, args=(vk,), daemon=True).start()
    log.info("Планировщик уведомлений запущен.")
    if WARNING_PEER_ID:
        log.info("  Варнинги в беседу: %s", WARNING_PEER_ID)
    if REPORT_USER_ID:
        log.info("  Отчеты в ЛС: %s", REPORT_USER_ID)

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
