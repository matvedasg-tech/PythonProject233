#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import os
import re
import time
import threading
import json
import sqlite3
from datetime import datetime, timedelta, date
from urllib.parse import quote
import pytz

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account
from vk_api import VkApi
from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
from vk_api.utils import get_random_id

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sheetbot")

# ---------- Конфиг ----------
TOKEN = (os.getenv("VK_TOKEN") or "").strip()
GROUP_ID = int(os.getenv("VK_GROUP_ID") or 0)
SPREADSHEET_ID = (os.getenv("SPREADSHEET_ID") or "").strip()
WORKSHEET_NAME = (os.getenv("WORKSHEET_NAME") or "").strip()
WARNING_PEER_ID = int(os.getenv("WARNING_PEER_ID") or 0)
ADMIN_ID = int(os.getenv("ADMIN_ID") or 0)

COOLDOWN_SECONDS = 5
user_cooldowns = {}
last_lesson_check = None
last_unread_report = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDS_FILE = os.path.join(BASE_DIR, "service_account.json")
DB_PATH = os.path.join(BASE_DIR, "learning.db")

if not TOKEN or not GROUP_ID:
    raise SystemExit("Не заданы VK_TOKEN / VK_GROUP_ID")
if not SPREADSHEET_ID:
    raise SystemExit("Не задан SPREADSHEET_ID")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"

# ---------- База данных ----------
def db_conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db_conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                vk_id INTEGER PRIMARY KEY,
                nickname TEXT UNIQUE
            );
            CREATE TABLE IF NOT EXISTS lesson_status (
                vk_id INTEGER,
                lesson_num INTEGER,
                status TEXT,
                sent_at TEXT,
                read_at TEXT,
                PRIMARY KEY (vk_id, lesson_num)
            );
        """)

def get_user_nickname(vk_id: int) -> str | None:
    with db_conn() as c:
        row = c.execute("SELECT nickname FROM users WHERE vk_id=?", (vk_id,)).fetchone()
        return row['nickname'] if row else None

def save_user_nickname(vk_id: int, nickname: str):
    with db_conn() as c:
        c.execute("INSERT OR REPLACE INTO users (vk_id, nickname) VALUES (?, ?)", (vk_id, nickname))

def mark_lesson_sent(vk_id: int, lesson_num: int):
    with db_conn() as c:
        c.execute("INSERT OR IGNORE INTO lesson_status (vk_id, lesson_num, status, sent_at) VALUES (?, ?, 'sent', ?)",
                  (vk_id, lesson_num, datetime.now().isoformat()))

def mark_lesson_read(vk_id: int, lesson_num: int):
    with db_conn() as c:
        c.execute("UPDATE lesson_status SET status='read', read_at=? WHERE vk_id=? AND lesson_num=?",
                  (datetime.now().isoformat(), vk_id, lesson_num))

# ---------- Google auth ----------
_creds = service_account.Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)

def get_access_token() -> str:
    global _creds
    if not _creds.valid or _creds.expired:
        _creds.refresh(GoogleAuthRequest())
    return _creds.token

def _sheet_range() -> str:
    name = WORKSHEET_NAME or _resolve_first_sheet_name()
    safe_name = name.replace("'", "''")
    return f"{quote(safe_name, safe='')}!A1:Z100000"

def _resolve_first_sheet_name() -> str:
    url = f"{SHEETS_API}/{SPREADSHEET_ID}"
    r = _google_get(url, params={"fields": "sheets.properties.title"})
    return r.json()["sheets"][0]["properties"]["title"]

def _google_get(url: str, params: dict | None = None, retries: int = 3) -> requests.Response:
    for attempt in range(1, retries + 1):
        try:
            headers = {"Authorization": f"Bearer {get_access_token()}"}
            r = requests.get(url, params=params, headers=headers, timeout=30)
            if r.status_code == 401:
                global _creds
                _creds = service_account.Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
                continue
            r.raise_for_status()
            return r
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt == retries: raise
            time.sleep(1.5 * attempt)

def fetch_rows() -> list[dict]:
    url = f"{SHEETS_API}/{SPREADSHEET_ID}/values/{_sheet_range()}"
    r = _google_get(url, params={"majorDimension": "ROWS"})
    values = r.json().get("values", [])
    if not values: return []
    header_idx = 0
    for i, row in enumerate(values[:5]):
        if any(x in " ".join(row).lower() for x in ['nickname', 'должность']):
            header_idx = i
            break
    headers = [str(h).strip() for h in values[header_idx]]
    rows = []
    for raw in values[header_idx + 1:]:
        raw = list(raw) + [""] * (len(headers) - len(raw))
        row = {(headers[i] or f"col{i}"): str(raw[i]).strip() for i in range(len(headers))}
        if any(row.values()): rows.append(row)
    return rows

def _find_key(keys: list[str], *variants: str) -> str | None:
    for k in keys:
        for v in variants:
            if v in k.replace('\xa0', ' ').strip().lower(): return k
    return None

def parse_date_to_dt(date_str: str) -> date | None:
    for fmt in [r'(\d{2})\.(\d{2})\.(\d{4})', r'(\d{4})-(\d{2})-(\d{2})']:
        m = re.search(fmt, date_str or "")
        if m:
            try:
                if '.' in date_str: return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                else: return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError: pass
    return None

# ---------- СТАТИСТИКА ----------
def get_agentometer() -> str:
    try: rows = fetch_rows()
    except Exception as e: return f"❌ Ошибка: {e}"
    keys = list(rows[0].keys()) if rows else []
    key_pos = _find_key(keys, 'должность')
    if not key_pos: return "❌ Нет колонки Должность"
    agents = sum(1 for r in rows if 'агент поддержки' in str(r.get(key_pos, '')).lower())
    cands = sum(1 for r in rows if 'кандидат' in str(r.get(key_pos, '')).lower())
    res = f"🤖 Всего агентов поддержки: {agents}"
    if cands > 0: res += f"\n📝 Всего кандидатов: {cands}"
    return res

def get_agent_count() -> str:
    try: rows = fetch_rows()
    except: return "❌ Ошибка таблицы"
    keys = list(rows[0].keys()) if rows else []
    key_pos = _find_key(keys, 'должность')
    key_date = _find_key(keys, 'назначен')
    if not key_pos or not key_date: return "❌ Нет колонок"
    
    today = datetime.now().date()
    start_this = today - timedelta(days=today.weekday())
    start_last = start_this - timedelta(days=7)
    end_last = start_this - timedelta(days=1)
    cnt_this, cnt_last = 0, 0
    for r in rows:
        if 'агент поддержки' not in str(r.get(key_pos, '')).lower(): continue
        d = parse_date_to_dt(str(r.get(key_date, '')).strip())
        if not d: continue
        if start_this <= d <= today: cnt_this += 1
        elif start_last <= d <= end_last: cnt_last += 1
    return f"📊 Назначено за текущую неделю: {cnt_this}\nНазначено за прошлую неделю: {cnt_last}"

def get_zvon_count() -> str:
    try: rows = fetch_rows()
    except: return "❌ Ошибка таблицы"
    keys = list(rows[0].keys()) if rows else []
    key_pos = _find_key(keys, 'должность')
    key_date = _find_key(keys, 'назначен')
    if not key_pos or not key_date: return "❌ Нет колонок"
    today = datetime.now().date()
    cnt = 0
    for r in rows:
        if 'агент поддержки' not in str(r.get(key_pos, '')).lower(): continue
        if parse_date_to_dt(str(r.get(key_date, '')).strip()) == today:
            cnt += 1
    return f"🔔 Апов за сегодня: {cnt}"

def get_full_stats() -> str:
    try: rows = fetch_rows()
    except: return "❌ Ошибка таблицы"
    keys = list(rows[0].keys()) if rows else []
    key_pos = _find_key(keys, 'должность')
    key_date = _find_key(keys, 'назначен')
    key_nick = _find_key(keys, 'nickname')
    if not key_pos or not key_date or not key_nick: return "❌ Нет колонок"
    
    today = datetime.now().date()
    start_this = today - timedelta(days=today.weekday())
    start_last = start_this - timedelta(days=7)
    end_last = start_this - timedelta(days=1)
    
    total_a, total_c, ap_today, ap_this, ap_last = 0, 0, 0, 0, 0
    today_names = []
    for r in rows:
        pos = str(r.get(key_pos, '')).lower()
        if 'агент поддержки' in pos:
            total_a += 1
            d = parse_date_to_dt(str(r.get(key_date, '')).strip())
            if d:
                if d == today: 
                    ap_today += 1
                    today_names.append(r.get(key_nick, '?'))
                if start_this <= d <= today: ap_this += 1
                elif start_last <= d <= end_last: ap_last += 1
        elif 'кандидат' in pos: total_c += 1
        
    res = f"📊 Статистика на {today.strftime('%d.%m.%Y')}\n\n"
    res += f"👥 Всего агентов: {total_a}\n"
    if total_c > 0: res += f"📝 Кандидатов: {total_c}\n"
    res += f"🔔 Апов сегодня: {ap_today}\n📈 За текущую неделю: {ap_this}\n📉 За прошлую неделю: {ap_last}\n"
    if today_names:
        res += "\n📌 Назначены сегодня:\n" + "\n".join(f"• {n}" for n in today_names)
    return res

# ---------- Логика уроков ----------
def load_lessons() -> dict:
    try:
        with open(os.path.join(BASE_DIR, "lessons.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except: return {}

def send_lesson_announcement(vk, vk_id: int, lesson_num: int, nickname: str):
    lessons = load_lessons()
    if str(lesson_num) not in lessons: return False
    kb = VkKeyboard(inline=True)
    kb.add_button("📖 Получить урок", color=VkKeyboardColor.PRIMARY, payload={"cmd": "get_lesson", "num": lesson_num})
    try:
        vk.messages.send(user_id=vk_id, message=f"🔔 Привет, {nickname}!\nДоступен новый учебный материал (Урок {lesson_num}). Нажми на кнопку ниже.", keyboard=kb.get_keyboard(), random_id=get_random_id())
        mark_lesson_sent(vk_id, lesson_num)
        return True
    except: return False

def daily_lesson_check(vk):
    today = datetime.now().date()
    lessons = load_lessons()
    if not lessons: return
    try: rows = fetch_rows()
    except: return
    
    keys = list(rows[0].keys()) if rows else []
    key_nick = _find_key(keys, 'nickname')
    key_pos = _find_key(keys, 'должность')
    key_date = _find_key(keys, 'назначен')
    
    if not key_pos or not key_date or not key_nick: return

    with db_conn() as c:
        users = c.execute("SELECT vk_id, nickname FROM users").fetchall()

    for user in users:
        vk_id = user['vk_id']
        nick = user['nickname']
        
        for row in rows:
            if str(row.get(key_nick, '')).strip().lower() == nick.strip().lower():
                if 'агент поддержки' not in str(row.get(key_pos, '')).lower():
                    break
                
                d = parse_date_to_dt(str(row.get(key_date, '')).strip())
                if not d: break
                
                days = (today - d).days
                if 1 <= days <= 10 and str(days) in lessons:
                    send_lesson_announcement(vk, vk_id, days, nick)
                break

def send_unread_report(vk):
    if not WARNING_PEER_ID: return
    today = datetime.now().date()
    lessons = load_lessons()
    if not lessons: return
    try: rows = fetch_rows()
    except: return
    
    keys = list(rows[0].keys()) if rows else []
    key_nick = _find_key(keys, 'nickname')
    key_pos = _find_key(keys, 'должность')
    key_date = _find_key(keys, 'назначен')
    if not key_pos or not key_date or not key_nick: return

    with db_conn() as c:
        users = c.execute("SELECT vk_id, nickname FROM users").fetchall()

    unread = []
    for user in users:
        vk_id = user['vk_id']
        nick = user['nickname']
        
        for row in rows:
            if str(row.get(key_nick, '')).strip().lower() == nick.strip().lower():
                if 'агент поддержки' not in str(row.get(key_pos, '')).lower(): break
                d = parse_date_to_dt(str(row.get(key_date, '')).strip())
                if not d: break
                
                days = (today - d).days
                if 1 <= days <= 10 and str(days) in lessons:
                    with db_conn() as c:
                        st = c.execute("SELECT status FROM lesson_status WHERE vk_id=? AND lesson_num=?", (vk_id, days)).fetchone()
                        if st and st['status'] == 'sent':
                            unread.append(nick)
                break

    if unread:
        txt = "⚠️ Не прочитали сегодняшний урок:\n" + "\n".join(f"• {n}" for n in unread)
        try: vk.messages.send(peer_id=WARNING_PEER_ID, message=txt, random_id=get_random_id())
        except: pass

# ---------- Обработчики ----------
def handle_callback(event, vk):
    obj = event.object
    uid = obj["user_id"]; peer = obj["peer_id"]; eid = obj["event_id"]
    try: payload = json.loads(obj.get("payload") or "{}")
    except: return
    if payload.get("cmd") == "get_lesson":
        num = payload["num"]
        text = load_lessons().get(str(num), "Урок не найден.")
        vk.messages.send(user_id=uid, message=text, random_id=get_random_id())
        mark_lesson_read(uid, num)
        vk.messages.send_message_event_answer(event_id=eid, user_id=uid, peer_id=peer, event_data=json.dumps({"type": "show_snackbar", "text": "Урок получен!"}))

def handle_message(event, vk):
    msg = event.object.message
    text = (msg.get("text") or "").strip()
    peer_id = msg["peer_id"]
    from_id = msg["from_id"]
    
    # Проверяем, это личные сообщения (ЛС) или беседа
    is_dm = (peer_id == from_id)
    
    if not text: return

    # --- АДМИНСКАЯ КОМАНДА: РУЧНАЯ ВЫДАЧА ---
    if from_id == ADMIN_ID and text.lower().startswith("/выдать "):
        try:
            parts = text.split()
            if len(parts) < 3:
                vk.messages.send(peer_id=peer_id, message="⚠️ Использование: /выдать NickName https://vk.com/id12345", random_id=get_random_id())
                return
            
            nick = parts[1]
            vk_link = parts[2]
            
            match_id = re.search(r'id(\d+)', vk_link)
            if match_id:
                target_vk_id = int(match_id.group(1))
            else:
                username = vk_link.strip('/').split('/')[-1]
                try:
                    res = vk.users.get(user_ids=username)
                    if res: target_vk_id = res[0]['id']
                    else:
                        vk.messages.send(peer_id=peer_id, message="❌ Не удалось найти пользователя.", random_id=get_random_id())
                        return
                except:
                    vk.messages.send(peer_id=peer_id, message="❌ Не удалось найти пользователя.", random_id=get_random_id())
                    return

            rows = fetch_rows()
            keys = list(rows[0].keys()) if rows else []
            key_nick = _find_key(keys, 'nickname')
            found = False
            if key_nick:
                for r in rows:
                    if str(r.get(key_nick, '')).strip().lower() == nick.lower():
                        found = True
                        break
            
            if not found:
                vk.messages.send(peer_id=peer_id, message=f"⚠️ Ника '{nick}' нет в таблице. Выдаю принудительно.", random_id=get_random_id())

            save_user_nickname(target_vk_id, nick)
            
            vk.messages.send(
                peer_id=peer_id, 
                message=f"✅ Успешно выдано — {nick} ([id{target_vk_id}|Профиль])", 
                random_id=get_random_id(),
                format="html"
            )
            
            try:
                vk.messages.send(user_id=target_vk_id, message=f"✅ Администратор выдал тебе доступ. Твой ник: {nick}.", random_id=get_random_id())
            except: pass
                
        except Exception as e:
            vk.messages.send(peer_id=peer_id, message=f"❌ Ошибка: {e}", random_id=get_random_id())
        return
    # -----------------------------------------

    # --- ЛОГИКА ДЛЯ БЕСЕД И ЛС ---
    
    # Если это беседа - реагируем ТОЛЬКО на команды статистики
    if not is_dm:
        allowed_cmds = ["/агентометр", "/агентосчет", "/звоносчет", "/стата", "/help", "/помощь", "/start"]
        if text.lower() not in allowed_cmds and not text.lower().startswith("/урок "):
            return # В беседе молчим на всё остальное

    # --- АВТОРИЗАЦИЯ (только для ЛС) ---
    nickname = get_user_nickname(from_id)
    if not nickname:
        if not is_dm:
            return # В беседе неавторизованных просто игнорируем
        
        if text.startswith("/"):
            vk.messages.send(peer_id=peer_id, message="👋 Привет! Чтобы пользоваться ботом, напиши свой игровой никнейм (как в таблице).", random_id=get_random_id())
            return
        
        try: rows = fetch_rows()
        except: rows = []
        keys = list(rows[0].keys()) if rows else []
        key_nick = _find_key(keys, 'nickname')
        
        found = False
        if key_nick:
            for r in rows:
                if str(r.get(key_nick, '')).strip().lower() == text.lower():
                    found = True
                    break
        
        if found:
            save_user_nickname(from_id, text)
            vk.messages.send(peer_id=peer_id, message=f"✅ Отлично, {text}! Ты авторизован. Теперь тебе доступны команды.", random_id=get_random_id())
        else:
            vk.messages.send(peer_id=peer_id, message="❌ Я не нашел тебя в составе. Проверь написание ника (он должен точно совпадать с таблицей).", random_id=get_random_id())
        return

    # --- КОМАНДЫ ---
    text_lower = text.lower()

    user_cmds = user_cooldowns.setdefault(from_id, {})
    now = time.time()
    def check_cd(cmd: str) -> bool:
        if now - user_cmds.get(cmd, 0) < COOLDOWN_SECONDS: return False
        user_cmds[cmd] = now
        return True

    if text_lower in ("/help", "/start", "/помощь"):
        if not check_cd("help"): return
        vk.messages.send(peer_id=peer_id, message="📖 Команды:\n/агентометр\n/агентосчет\n/звоносчет\n/стата\n\n👑 Админ-команды:\n/урок N\n/выдать NickName Ссылка", random_id=get_random_id())
        return

    if text_lower == "/агентометр":
        if not check_cd("am"): return
        vk.messages.send(peer_id=peer_id, message=get_agentometer(), random_id=get_random_id())
        return

    if text_lower == "/агентосчет":
        if not check_cd("ac"): return
        vk.messages.send(peer_id=peer_id, message=get_agent_count(), random_id=get_random_id())
        return

    if text_lower == "/звоносчет":
        if not check_cd("zc"): return
        vk.messages.send(peer_id=peer_id, message=get_zvon_count(), random_id=get_random_id())
        return

    if text_lower == "/стата":
        if not check_cd("st"): return
        vk.messages.send(peer_id=peer_id, message=get_full_stats(), random_id=get_random_id())
        return

    if from_id == ADMIN_ID and text_lower.startswith("/урок "):
        try:
            num = int(text_lower.split()[1])
            rows = fetch_rows()
            with db_conn() as c:
                users = c.execute("SELECT vk_id, nickname FROM users").fetchall()
            
            keys = list(rows[0].keys()) if rows else []
            kn = _find_key(keys, 'nickname')
            kp = _find_key(keys, 'должность')
            
            count = 0
            for u in users:
                for r in rows:
                    if str(r.get(kn, '')).strip().lower() == u['nickname'].strip().lower():
                        if 'агент поддержки' in str(r.get(kp, '')).lower():
                            if send_lesson_announcement(vk, u['vk_id'], num, u['nickname']):
                                count += 1
                        break
            vk.messages.send(peer_id=peer_id, message=f"✅ Урок {num} разослан {count} чел.", random_id=get_random_id())
        except Exception as e:
            vk.messages.send(peer_id=peer_id, message=f"Ошибка: {e}", random_id=get_random_id())
        return

# ---------- Планировщик ----------
def scheduler_loop(vk):
    global last_lesson_check, last_unread_report
    msk = pytz.timezone('Europe/Moscow')
    while True:
        try:
            now = datetime.now(msk); today = now.date()
            if now.hour == 10 and now.minute == 0 and last_lesson_check != today:
                log.info("Рассылка уроков...")
                daily_lesson_check(vk)
                last_lesson_check = today
            if now.hour == 23 and now.minute == 0 and last_unread_report != today:
                log.info("Отчет о непрочитанных...")
                send_unread_report(vk)
                last_unread_report = today
            time.sleep(30)
        except Exception as e:
            log.error(f"Scheduler error: {e}")
            time.sleep(60)

# ---------- main ----------
def main():
    init_db()
    vk_session = VkApi(token=TOKEN)
    vk = vk_session.get_api()
    threading.Thread(target=scheduler_loop, args=(vk,), daemon=True).start()
    log.info("Бот запущен.")
    longpoll = VkBotLongPoll(vk_session, GROUP_ID)
    for event in longpoll.listen():
        try:
            if event.type == VkBotEventType.MESSAGE_NEW: handle_message(event, vk)
            elif event.type == VkBotEventType.MESSAGE_EVENT: handle_callback(event, vk)
        except Exception: log.exception("Ошибка")

if __name__ == "__main__":
    main()
