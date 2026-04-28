import logging
import sqlite3
import time
import threading
import jdatetime
from pyrubi import Client
from pyrubi.types import Message
from datetime import datetime

# --- تنظیمات اولیه ---
logging.basicConfig(level=logging.ERROR)
client = Client("2") 
db_lock = threading.Lock()

# --- دیتابیس ---
try:
    conn = sqlite3.connect('voice_pro.db', check_same_thread=False, timeout=10)
    cursor = conn.cursor()
except Exception as e:
    print(f"❌ خطا در اتصال به دیتابیس: {e}")
    exit(1)

def init_db():
    """راه‌اندازی دیتابیس"""
    try:
        with db_lock:
            cursor.execute('''CREATE TABLE IF NOT EXISTS call_memory 
                              (guid TEXT PRIMARY KEY, total_sec REAL, last_start REAL, name TEXT, 
                               is_in_call INTEGER, is_mute INTEGER, is_admin INTEGER, 
                               updated_at REAL, group_guid TEXT)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS settings 
                              (key TEXT PRIMARY KEY, value TEXT)''')
            conn.commit()
            print("✅ دیتابیس آماده شد")
    except Exception as e:
        print(f"❌ خطا در راه‌اندازی دیتابیس: {e}")

init_db()

# --- توابع کمکی ---
def clean_name(text):
    """تمیز کردن نام از ایموجی‌ها"""
    if not text:
        return "User"
    for e in ["🔊", "🔇", "👑", "🎤", "🎙", "📢"]:
        text = text.replace(e, "")
    return text.strip() or "User"

def format_duration(seconds):
    """فرمت‌بندی مدت زمان"""
    try:
        seconds = max(0, float(seconds))
        hrs, rem = divmod(int(seconds), 3600)
        mins, secs = divmod(rem, 60)
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    except Exception as e:
        print(f"⚠️ خطا در format_duration: {e}")
        return "00:00:00"

def check_daily_reset():
    """ریست کردن آمار روزانه"""
    try:
        today = jdatetime.datetime.now().strftime("%Y/%m/%d")
        with db_lock:
            cursor.execute("SELECT value FROM settings WHERE key = 'last_reset'")
            row = cursor.fetchone()
            
            if not row or row[0] != today:
                now = time.time()
                cursor.execute("UPDATE call_memory SET total_sec = 0, last_start = CASE WHEN is_in_call = 1 THEN ? ELSE NULL END", (now,))
                
                if not row:
                    cursor.execute("INSERT INTO settings VALUES ('last_reset', ?)", (today,))
                else:
                    cursor.execute("UPDATE settings SET value = ? WHERE key = 'last_reset'", (today,))
                
                conn.commit()
                print(f"✅ آمار روز جدید ریست شد: {today}")
    except Exception as e:
        print(f"❌ خطا در check_daily_reset: {e}")

def get_user_name(u_guid):
    """دریافت نام کاربر از API"""
    try:
        u_info = client.get_chat_info(u_guid)
        if not u_info or 'user' not in u_info:
            return "User"
        
        u_data = u_info.get('user', {})
        first_name = str(u_data.get('first_name', 'User')).strip() or 'User'
        last_name = str(u_data.get('last_name', '')).strip()
        
        display_name = clean_name(f"{first_name} {last_name}")
        return display_name if display_name else "User"
    except Exception as e:
        print(f"⚠️ خطا در دریافت نام {u_guid}: {e}")
        return "User"

# --- ناظر هوشمند ---
active_groups = {}

def monitor_logic():
    """لوپ نظارت اصلی"""
    
    while True:
        try:
            check_daily_reset()
            now = time.time()
            
            # پاک‌کردن گروپ‌های قدیمی (بیش از 10 دقیقه بدون درخواست)
            expired_groups = [g for g, t in active_groups.items() if now - t > 600]
            for g in expired_groups:
                active_groups.pop(g, None)
            
            for g_guid in list(active_groups.keys()):
                try:
                    chat_info = client.get_chat_info(g_guid)
                    
                    if not chat_info or 'chat' not in chat_info:
                        continue
                    
                    v_id = chat_info.get('chat', {}).get('group_voice_chat_id')
                    
                    if not v_id or v_id == "0":
                        # کال بسته شده، زمان همه رو ذخیره کن
                        with db_lock:
                            cursor.execute("SELECT guid, last_start, total_sec FROM call_memory WHERE is_in_call = 1 AND group_guid = ?", (g_guid,))
                            for db_guid, l_start, t_sec in cursor.fetchall():
                                if l_start:
                                    added = now - l_start
                                    new_total = t_sec + added
                                    cursor.execute("UPDATE call_memory SET total_sec = ?, last_start = NULL, is_in_call = 0, is_mute = 0 WHERE guid = ?", 
                                                   (new_total, db_guid))
                            conn.commit()
                        active_groups.pop(g_guid, None)
                        continue
                    
                    # دریافت اطلاعات شرکت‌کنندگان
                    response = client.get_voice_chat_participants(g_guid, v_id)
                    
                    if not response or 'participants' not in response:
                        continue
                    
                    participants = response.get('participants', [])
                    current_users = {p['user_guid']: p for p in participants if p.get('user_guid')}
                    
                    with db_lock:
                        # 1️⃣ مدیریت خروج
                        cursor.execute("SELECT guid, last_start, total_sec FROM call_memory WHERE is_in_call = 1 AND group_guid = ?", (g_guid,))
                        for db_guid, l_start, t_sec in cursor.fetchall():
                            if db_guid not in current_users:
                                added = max(0, now - l_start) if l_start else 0
                                new_total = t_sec + added
                                cursor.execute("UPDATE call_memory SET total_sec = ?, last_start = NULL, is_in_call = 0, is_mute = 0 WHERE guid = ?", 
                                               (new_total, db_guid))
                        
                        # 2️⃣ مدیریت ورود و آپدیت
                        for u_guid, p_data in current_users.items():
                            mute_status = 1 if p_data.get('is_mute') else 0
                            admin_status = 1 if p_data.get('can_manage_voice_chat') else 0
                            
                            cursor.execute("SELECT is_in_call, total_sec FROM call_memory WHERE guid = ?", (u_guid,))
                            row = cursor.fetchone()
                            
                            if not row:
                                # کاربر جدید
                                cursor.execute("INSERT OR IGNORE INTO call_memory VALUES (?, 0, ?, 'User', 1, ?, ?, ?, ?)", 
                                               (u_guid, now, mute_status, admin_status, now, g_guid))
                            elif row[0] == 0:
                                # کاربر برمی‌گردد
                                cursor.execute("UPDATE call_memory SET is_in_call = 1, last_start = ?, is_mute = ?, is_admin = ?, group_guid = ? WHERE guid = ?", 
                                               (now, mute_status, admin_status, g_guid, u_guid))
                            else:
                                # کاربر همچنان در کال
                                cursor.execute("UPDATE call_memory SET is_mute = ?, is_admin = ? WHERE guid = ?", 
                                               (mute_status, admin_status, u_guid))
                        
                        conn.commit()
                
                except Exception as e:
                    print(f"⚠️ خطا در پردازش گروپ {g_guid}: {e}")
                    continue
        
        except Exception as e:
            print(f"❌ خطا در monitor_logic: {e}")
        
        time.sleep(4)

threading.Thread(target=monitor_logic, daemon=True).start()

@client.on_message()
def voice_handler(message: Message):
    """کمند امار کال"""
    try:
        if not message.text or message.text != "امار کال":
            return
        
        guid = message.object_guid
        if not guid:
            return
        
        active_groups[guid] = time.time()
        
        now = time.time()
        
        with db_lock:
            cursor.execute("SELECT name, total_sec, last_start, guid, is_mute, is_admin FROM call_memory WHERE is_in_call = 1 AND group_guid = ?", (guid,))
            users = cursor.fetchall()
            
            if not users:
                try:
                    message.reply("🎙 **در حال حاضر کسی در ویس‌کال نیست.**")
                except Exception as e:
                    print(f"⚠️ خطا در ارسال پیام خالی: {e}")
                return
            
            # ✅ دریافت نام‌های واقی و ذخیره در دیتابیس
            users_with_names = []
            for name, t_sec, l_start, u_guid, is_mute, is_admin in users:
                try:
                    real_name = get_user_name(u_guid)
                    
                    # ✅ اسم جدید رو در دیتابیس ذخیره کن
                    cursor.execute("UPDATE call_memory SET name = ?, updated_at = ? WHERE guid = ?", 
                                   (real_name, now, u_guid))
                    
                    users_with_names.append((real_name, t_sec, l_start, u_guid, is_mute, is_admin))
                except Exception as e:
                    print(f"⚠️ خطا در دریافت نام {u_guid}: {e}")
                    users_with_names.append((name, t_sec, l_start, u_guid, is_mute, is_admin))
            
            conn.commit()  # ✅ ذخیره اسم‌های جدید
            
            total_count = len(users_with_names)
            res_text = f"🎙 **آمار فعالیت در ویس‌کال ({total_count} نفر)**\n"
            res_text += f"━━━━━━━━━━━━━━\n"
            
            for index, (name, t_sec, l_start, u_guid, is_mute, is_admin) in enumerate(users_with_names, 1):
                try:
                    display_name = clean_name(name) if name and name != "User" else "User"
                    
                    status_icon = "🔇" if is_mute == 1 else "🔊"
                    admin_tag = " 👑" if is_admin == 1 else ""
                    
                    current_duration = t_sec + (max(0, now - l_start) if l_start else 0)
                    duration_str = format_duration(current_duration)
                    
                    name_truncated = display_name[:20]
                    res_text += f"{index}. {name_truncated}{admin_tag} {status_icon}\n└ ⏱ {duration_str}\n\n"
                
                except Exception as e:
                    print(f"⚠️ خطا در پردازش کاربر {u_guid}: {e}")
                    continue
            
            res_text += f"━━━━━━━━━━━━━━\n📅 {jdatetime.datetime.now().strftime('%Y/%m/%d')}"
            conn.commit()
        
        try:
            message.reply(res_text)
        except Exception as e:
            print(f"❌ خطا در ارسال پیام: {e}")
    
    except Exception as e:
        print(f"❌ خطا در voice_handler: {e}")

def cleanup():
    """پاک‌کردن منابع"""
    try:
        if conn:
            conn.close()
            print("✅ دیتابیس بسته شد")
    except Exception as e:
        print(f"⚠️ خطا در بستن دیتابیس: {e}")

print("--- سیستم مانیتورینگ ویس‌کال فعال شد ---")

try:
    client.run()
except KeyboardInterrupt:
    print("\n🛑 سیستم متوقف شد")
    cleanup()
except Exception as e:
    print(f"❌ خطای غیرمنتظره: {e}")
    cleanup()