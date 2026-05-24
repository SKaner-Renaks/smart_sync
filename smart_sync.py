"""
PYTHON SMART SYNC V1.0.3
Высокопроизводительная зеркальная синхронизация каталогов с сохранением ACL.
"""

import os
import sys
import time
import json
import threading
import queue
import shutil
import datetime
import traceback
import collections
from pathlib import Path

# --- ПРОВЕРКА ЗАВИСИМОСТЕЙ ---
try:
    import psutil
    from colorama import init, Fore, Back, Style
    init(autoreset=True)
except ImportError as e:
    print(f"Ошибка: Отсутствует зависимость. {e}")
    print("Пожалуйста, выполните: pip install -r requirements.txt")
    sys.exit(1)

# Специфичные для Windows импорты
WIN32_AVAILABLE = False
if sys.platform == "win32":
    try:
        import win32security
        import win32api
        import win32con
        import msvcrt
        import ntpath
        WIN32_AVAILABLE = True
    except ImportError:
        pass
else:
    import os.path as ntpath

# --- КОНСТАНТЫ ---
VERSION = "1.0.3"
SOURCE_PATH = r"\\192.168.88.3\Отдел продаж"
DEST_PATH = r"D:\BackUp\Отдел продаж"
THREADS_COUNT = 16
MAX_RETRIES = 3
RETRY_DELAY = 5

# Правила исключений (регистронезависимые)
EXCLUDE_RULES = {
    "extensions": [".tmp", ".bak", ".lnk", ".old"],
    "prefixes": ["~$"],
    "patterns": ["Thumbs.db", "desktop.ini"]
}

# --- ПУТИ ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_MAIN = os.path.join(BASE_DIR, "sync_main.log")
LOG_ERRORS = os.path.join(BASE_DIR, "sync_errors.log")
HISTORY_FILE = os.path.join(BASE_DIR, "sync_history.json")

# --- ЦВЕТОВАЯ ПАЛИТРА (ANSI) ---
C_MAIN = "\033[37m"         # Светло-серый
C_WHITE = "\033[97m"        # Белый
C_DARK = "\033[90m"         # Темно-серый
C_GREEN = "\033[92m"        # Салатный (Зеленый)
C_CYAN = "\033[96m"         # Бирюзовый (Циан)
C_YELLOW = "\033[93m"       # Лимонный (Желтый)
C_RED = "\033[91m"          # Красный
C_PINK = "\033[95m"         # Розовый
C_ORANGE = "\033[38;5;208m" # Оранжевый
BG_CYAN = "\033[106;30m"    # Плашка [COPY] (Циан фон, Черный текст)
BG_ORANGE = "\033[48;5;208;30m" # Плашка [RTRY] (Оранжевый фон, Черный текст)
BG_GREEN = "\033[102;30m"   # Статус успеха (Зеленый фон, Черный текст)
RESET = "\033[0m"

# --- СОСТОЯНИЕ СИНХРОНИЗАЦИИ ---
class SyncState:
    def __init__(self):
        self.start_time = time.time()
        self.end_time = None
        self.stop_event = threading.Event()

        # Статистика
        self.files_processed = 0
        self.files_total = 0
        self.files_copied = 0
        self.files_skipped = 0
        self.files_excluded = 0
        self.files_failed = 0
        self.files_deleted = 0
        self.dirs_total = 0
        self.dirs_copied = 0
        self.dirs_skipped = 0
        self.dirs_failed = 0
        self.dirs_deleted = 0

        self.bytes_processed = 0
        self.bytes_total = 0
        self.bytes_copied = 0
        self.bytes_deleted = 0

        self.acl_applied = 0
        self.retries = 0

        self.stats_lock = threading.Lock()

        # Производительность
        self.speed_history = collections.deque(maxlen=60)
        self.current_speed_counter = 0
        self.target_io_history = collections.deque(maxlen=60)

        # Телеметрия
        self.cpu_usage = []
        self.ram_usage = []

        # Статус потоков
        self.thread_info = {} # tid -> {status, progress, size, eta, path, spinner_idx}
        for i in range(1, THREADS_COUNT + 1):
            self.thread_info[i] = {"status": "IDLE", "progress": 0, "size": 0, "eta": "--:--:--", "path": "", "spinner_idx": 0}

        # История
        self.history_data = self.load_history()
        self.is_first_run = not bool(self.history_data)

        if not self.is_first_run:
            self.files_total = self.history_data.get("last_total_files", 0)
            self.bytes_total = self.history_data.get("last_total_bytes", 0)

    def load_history(self):
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, 'r') as f:
                    return json.load(f)
            except:
                pass
        return {}

    def save_history(self):
        data = {
            "last_total_bytes": self.bytes_processed,
            "last_total_files": self.files_processed,
            "last_elapsed_seconds": int(time.time() - self.start_time),
            "last_successful_run": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        try:
            with open(HISTORY_FILE, 'w') as f:
                json.dump(data, f, indent=4)
        except:
            pass

    def update_speed(self, bytes_chunk):
        with self.stats_lock:
            self.current_speed_counter += bytes_chunk

    def get_eta(self):
        avg_speed = sum(self.speed_history) / len(self.speed_history) if self.speed_history else 0
        if avg_speed <= 0:
            return f"{C_DARK}--:--:--{C_WHITE}"

        if self.bytes_total == 0:
            return f"{C_DARK}--:--:--{C_WHITE}"

        remaining_bytes = max(0, self.bytes_total - self.bytes_processed)
        if remaining_bytes <= 0: return "00:00:00"

        eta_seconds = remaining_bytes / avg_speed
        return str(datetime.timedelta(seconds=int(eta_seconds)))

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def format_size(bytes_num):
    if bytes_num == 0: return "0.00 KB"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_num < 1024.0:
            return f"{bytes_num:3.2f} {unit}"
        bytes_num /= 1024.0
    return f"{bytes_num:3.2f} PB"

def format_size_short(bytes_num):
    if bytes_num == 0: return "0.0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_num < 1024.0:
            return f"{bytes_num:.1f} {unit}"
        bytes_num /= 1024.0
    return f"{bytes_num:.1f} PB"

def compress_path(path, max_len=45):
    if not path: return ""
    if len(path) <= max_len:
        return path

    half = (max_len - 7) // 2
    start = path[:half]
    end = path[-half:]
    return f"{start}... ...{end}"

def get_relative_path(full_path, root_path):
    try:
        rel = ntpath.relpath(full_path, root_path)
        if rel == ".": return ""
        return rel
    except:
        return full_path

# --- ЛОГИРОВАНИЕ ---
def log_main(msg, state, last_events):
    timestamp_full = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    timestamp_short = datetime.datetime.now().strftime("%H:%M:%S")
    with open(LOG_MAIN, 'a', encoding='utf-8') as f:
        f.write(f"{timestamp_full} | {msg}\n")

    if "[ACL_ONLY]" in msg:
        rel_path = msg.split('|')[-1].strip()
        last_events.append(f"{C_WHITE}{timestamp_short} {C_CYAN}[INFO]{C_WHITE} -> Успешно синхронизирован ACL для папки: {rel_path}")
    elif "[DELETE_OK]" in msg:
        rel_path = msg.split('|')[-1].strip()
        last_events.append(f"{C_WHITE}{timestamp_short} {C_CYAN}[INFO]{C_WHITE} -> Удален объект: {rel_path}")

def log_error(code, desc, rel_path, state, last_events):
    timestamp_full = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    timestamp_short = datetime.datetime.now().strftime("%H:%M:%S")
    with open(LOG_ERRORS, 'a', encoding='utf-8') as f:
        f.write(f"{timestamp_full} | [CRIT_ERR] | {code} / {desc} | {rel_path}\n")

    last_events.append(f"{C_WHITE}{timestamp_short} {C_RED}[ERR]{C_WHITE} -> {code}: {desc} ({rel_path})")

# --- РАБОТА С ACL ---
def apply_acl(src_path, dst_path):
    if not WIN32_AVAILABLE:
        return False
    try:
        sd = win32security.GetFileSecurity(src_path, win32security.DACL_SECURITY_INFORMATION)
        win32security.SetFileSecurity(dst_path, win32security.DACL_SECURITY_INFORMATION, sd)
        return True
    except:
        return False

def compare_acls(src_path, dst_path):
    if not WIN32_AVAILABLE:
        return True
    try:
        sd1 = win32security.GetFileSecurity(src_path, win32security.DACL_SECURITY_INFORMATION)
        sd2 = win32security.GetFileSecurity(dst_path, win32security.DACL_SECURITY_INFORMATION)
        return sd1.GetSecurityDescriptorDacl().GetBinaryForm() == sd2.GetSecurityDescriptorDacl().GetBinaryForm()
    except:
        return False

# --- ДВИЖОК TUI ---
def render_tui(state, last_events):
    sys.stdout.write("\033[H")

    width = 90

    # 1. Заголовок
    header_title = f" [ PYTHON SMART SYNC V{VERSION} ] "
    side_len = (width - len(header_title)) // 2
    header = "=" * side_len + header_title + "=" * (width - side_len - len(header_title))
    print(f"{C_DARK}{header}")

    # 2. Информация о сессии
    print(f"{C_WHITE}Запуск:    {C_MAIN}{datetime.datetime.fromtimestamp(state.start_time).strftime('%d.%m.%Y %H:%M:%S')}")
    print(f"{C_WHITE}Источник:  {C_CYAN}{SOURCE_PATH}")
    print(f"{C_WHITE}Цель:      {C_CYAN}{DEST_PATH}")
    print(f"{C_DARK}{'-'*width}")

    # 3. Статус
    if state.end_time:
        status_text = f"{BG_GREEN} СИНХРОНИЗАЦИЯ УСПЕШНО ЗАВЕРШЕНА {RESET}"
    else:
        status_text = f"{C_WHITE}СИНХРОНИЗАЦИЯ ДАННЫХ..."
    print(f"{C_WHITE}[ОБЩИЙ СТАТУС]: {status_text}")

    # 4. Прогресс-бар / Инфо
    bar_width = 38
    if state.is_first_run and not state.end_time:
        msg = "[ИНФО]: Первый запуск утилиты для данной пары каталогов. Данных о суммарном объеме нет."
        print(f"{C_PINK}{msg.center(width)}")
    else:
        if state.bytes_total > 0:
            percent = (state.bytes_processed / state.bytes_total * 100)
        else:
            percent = 0.0 if not state.end_time else 100.0

        if state.end_time: percent = 100.0
        filled = int(bar_width * percent / 100)
        bar_str = f"{C_GREEN}{'█'*filled}{C_DARK}{'░'*(bar_width-filled)}"
        session_info = "(Сессия закрыта корректно)" if state.end_time else "(Память: OK)"
        print(f"{C_WHITE}Прогресс: [{bar_str}{C_WHITE}] {C_GREEN}{percent:5.1f}%{C_WHITE} {session_info}")

    # 5. Статистика
    avg_speed_mb = (sum(state.speed_history) / len(state.speed_history)) / (1024*1024) if state.speed_history else 0
    speed_val_str = f"{avg_speed_mb:5.1f} MB/s"

    elapsed = int((state.end_time or time.time()) - state.start_time)
    elapsed_str = str(datetime.timedelta(seconds=elapsed))

    try:
        free_bytes = psutil.disk_usage(os.path.splitdrive(DEST_PATH)[0] or "/").free
        free_str = format_size_short(free_bytes)
    except:
        free_str = "?.? TB"

    if not state.end_time:
        if state.is_first_run:
            vol_line = f"Объем:    {C_WHITE}{format_size_short(state.bytes_processed)} обработано           | Скорость:   {C_CYAN}[{speed_val_str}]{C_WHITE}"
            total_files_str = f"{C_DARK}[сбор данных...]{C_WHITE}"
            timing_line = f"Файлы:    Обработано: {state.files_processed:,} / {total_files_str} | Тайминги:   Прошло: {elapsed_str}"
        else:
            vol_line = f"Объем:    {format_size_short(state.bytes_processed)} / {format_size_short(state.bytes_total)}       | Скорость:   {C_CYAN}[{speed_val_str}]{C_WHITE} (Свободно: {free_str})"
            total_files_str = f"{state.files_total:,}"
            eta = state.get_eta()
            timing_line = f"Файлы:    Обработано: {state.files_processed:,} / {total_files_str} | Тайминги:   Прошло: {elapsed_str} | ETA: {eta}"

        print(f"{C_WHITE}{vol_line}")
        print(f"{C_WHITE}{timing_line}")
        print(f"{C_WHITE}Ошибки:   [ {state.files_failed} ]                       | Пропущено (Ретраи):  [ {state.files_skipped} ]")
        print(f"            | Исключено (Фильтр):  [ {state.files_excluded} ]")

        print(f"{C_DARK}{'-'*width}")

        # 6. Активные потоки
        print(f"{C_WHITE}[АКТИВНЫЕ ПОТОКИ (THREADS)]:")
        print(f"{C_WHITE} №  Status  A  Progress   Size       ETA        Source relative file path")
        spinners = ['|', '/', '-', '\\']
        for i in range(1, THREADS_COUNT + 1):
            info = state.thread_info.get(i)
            if info["status"] == "IDLE":
                print(f"{C_MAIN} T{i:<2} {C_DARK}[IDLE]  •   [ --%]     0.00 KB  --:--:--   ")
            else:
                status_clr = BG_CYAN if info["status"] == "COPY" else BG_ORANGE
                spinner_color = C_YELLOW if info["status"] == "RTRY" else C_CYAN
                spinner = spinners[info["spinner_idx"] % 4]
                prog = f"[{info['progress']:3d}%]"
                size = format_size(info["size"])
                path = compress_path(info["path"], 45)
                print(f"{C_MAIN} T{i:<2} {status_clr}[{info['status']}] {RESET}{spinner_color}{spinner}{C_MAIN} | {C_WHITE}  {prog}  {size:>10}  {info['eta']}   {path}")

        print(f"{C_DARK}{'-'*width}")
        print(f"{C_WHITE}[УПРАВЛЕНИЕ СЕССИЕЙ]: Для экстренной аварийной остановки нажмите {C_YELLOW}[F10]{C_WHITE} или {C_YELLOW}[Ctrl+C]{C_WHITE}")
        print(f"{C_WHITE}[ПОСЛЕДНИЕ СОБЫТИЯ]:")
        for ev in last_events:
            print(f" {ev}")
    else:
        # Экран финального отчета
        print(f"{C_WHITE}[ТЕЛЕМЕТРИЯ СЕССИИ И ДИАГНОСТИКА СИСТЕМЫ]:")
        finish_time = datetime.datetime.fromtimestamp(state.end_time).strftime('%d.%m.%Y %H:%M:%S')
        print(f"{C_WHITE} Временные метки:  Завершение: {C_YELLOW}{finish_time}{C_WHITE}  │ Общее время работы:  {C_CYAN}{elapsed_str}")
        print(f"{C_WHITE} Файлы журналов:   Лог сессии: {C_YELLOW}{LOG_MAIN}")
        print(f"{C_WHITE}                   Ошибки ACL: {C_YELLOW}{LOG_ERRORS}")
        print(f"{C_DARK} {'─'*86}")

        cpu_avg = sum(state.cpu_usage) / len(state.cpu_usage) if state.cpu_usage else 0
        cpu_max = max(state.cpu_usage) if state.cpu_usage else 0
        ram_avg = sum(state.ram_usage) / len(state.ram_usage) if state.ram_usage else 0
        ram_max = max(state.ram_usage) if state.ram_usage else 0

        print(f"{C_WHITE} Ресурсы узла:     Утилизация CPU:  Средняя: {C_GREEN}{cpu_avg:5.1f}%{C_WHITE}   │ Пиковая (Max):       {C_YELLOW}{cpu_max:5.1f}%")
        print(f"{C_WHITE}                   Занятость RAM:   Средняя: {C_GREEN}{ram_avg:5.0f} MB{C_WHITE}  │ Пиковая (Max):       {C_YELLOW}{ram_max:5.0f} MB")
        print(f"{C_DARK} {'─'*86}")

        drive_letter = (os.path.splitdrive(DEST_PATH)[0] or "D:")
        print(f"{C_WHITE} Целевой диск:     Доступное пространство на целевой машине: {C_GREEN}{free_str}{C_WHITE} (Том {drive_letter})")
        print(f"{C_WHITE} {C_ORANGE}Аудит прав ACL:   ИЗМЕНЕНО И СИНХРОНИЗИРОВАНО ПРАВ ДОСТУПА (ACL): [ {state.acl_applied:,} модификаций ]{C_WHITE}")

        print(f"{C_DARK}{'-'*width}")
        print(f"{C_WHITE}[БЕНЧМАРК ПРОИЗВОДИТЕЛЬНОСТИ ПОДСИСТЕМ (IO SPEED)]:")
        net_min = min(state.speed_history) / (1024**2) if state.speed_history else 0
        net_avg = (sum(state.speed_history) / len(state.speed_history)) / (1024**2) if state.speed_history else 0
        net_max = max(state.speed_history) / (1024**2) if state.speed_history else 0
        print(f"{C_WHITE} Канал связи (Network SMB):  Минимум: {C_RED}{net_min:5.1f} MB/s{C_WHITE}  │ Средняя: {C_GREEN}{net_avg:5.1f} MB/s{C_WHITE} │ Максимум: {C_CYAN}{net_max:5.1f} MB/s")
        io_min = min(state.target_io_history) / (1024**2) if state.target_io_history else 0
        io_avg = (sum(state.target_io_history) / len(state.target_io_history)) / (1024**2) if state.target_io_history else 0
        io_max = max(state.target_io_history) / (1024**2) if state.target_io_history else 0
        print(f"{C_WHITE} Локальный диск (Target IO):  Минимум: {C_RED}{io_min:5.1f} MB/s{C_WHITE}  │ Средняя: {C_GREEN}{io_avg:5.1f} MB/s{C_WHITE} │ Максимум: {C_CYAN}{io_max:5.1f} MB/s")

        print(f"{C_DARK}{'-'*width}")
        print(f"{C_WHITE}[ФИНАЛЬНАЯ СТАТИСТИКА КАТАЛОГОВ (SUMMARY)]:\n")
        print(f"{C_DARK}            │    Всего    │ Скопировано │  Пропущено  │   Ошибки    │   Удалено   │")
        print(f"────────────┼─────────────┼─────────────┼─────────────┼─────────────┼─────────────┤")
        def row(label, total, copied, skipped, errors, deleted):
            return f" {label:<11}│ {total:>11} │ {copied:>11} │ {skipped:>11} │ {errors:>11} │ {deleted:>11} │"

        f_dirs_total = f"{state.dirs_total:,}"
        f_dirs_copied = f"{state.dirs_copied:,}"
        f_dirs_skipped = f"{state.dirs_skipped:,}"
        f_dirs_failed = f"{state.dirs_failed:,}"
        f_dirs_deleted = f"{state.dirs_deleted:,}"

        f_files_total = f"{state.files_processed:,}"
        f_files_copied = f"{state.files_copied:,}"
        f_files_skipped = f"{state.files_skipped + state.files_excluded:,}"
        f_files_failed = f"{state.files_failed:,}"
        f_files_deleted = f"{state.files_deleted:,}"

        f_bytes_total = format_size_short(state.bytes_processed)
        f_bytes_copied = format_size_short(state.bytes_copied)
        f_bytes_skipped = format_size_short(state.bytes_processed - state.bytes_copied)
        f_bytes_failed = "0.0 B"
        f_bytes_deleted = format_size_short(state.bytes_deleted)

        print(f"{C_WHITE}{row('Директории', f_dirs_total, f_dirs_copied, f_dirs_skipped, f_dirs_failed, f_dirs_deleted)}")
        print(f"{C_WHITE}{row('Файлы', f_files_total, f_files_copied, f_files_skipped, f_files_failed, f_files_deleted)}")
        print(f"{C_WHITE}{row('Байты', f_bytes_total, f_bytes_copied, f_bytes_skipped, f_bytes_failed, f_bytes_deleted)}")
        print(f"{C_DARK}────────────┼─────────────┼─────────────┼─────────────┼─────────────┼─────────────┘{C_WHITE}")
        print(f"\n Финальный статус: {C_GREEN}Зеркало обновлено. Ошибок ввода-вывода и деградации прав ACL не обнаружено.")

# --- ФИЛЬТРАЦИЯ ---
def is_excluded(filename):
    name_lower = filename.lower()
    for ext in EXCLUDE_RULES.get("extensions", []):
        if name_lower.endswith(ext.lower()): return True
    for pre in EXCLUDE_RULES.get("prefixes", []):
        if name_lower.startswith(pre.lower()): return True
    for pat in EXCLUDE_RULES.get("patterns", []):
        if name_lower == pat.lower(): return True
    return False

# --- ВОРКЕР ---
def sync_worker(task_queue, state, tid, last_events):
    while not state.stop_event.is_set():
        try:
            task = task_queue.get(timeout=0.5)
            if task is None:
                task_queue.task_done()
                break
            src, dst, rel_path, size = task
            process_task(src, dst, rel_path, size, state, tid, last_events)
            task_queue.task_done()
        except queue.Empty:
            continue

def process_task(src, dst, rel_path, size, state, tid, last_events):
    state.thread_info[tid]["status"] = "COPY"
    state.thread_info[tid]["progress"] = 0
    state.thread_info[tid]["size"] = size
    state.thread_info[tid]["eta"] = "--:--:--"
    state.thread_info[tid]["path"] = rel_path

    exists = os.path.exists(dst)
    if exists:
        try:
            src_stat = os.stat(src)
            dst_stat = os.stat(dst)
            if src_stat.st_size == dst_stat.st_size and abs(src_stat.st_mtime - dst_stat.st_mtime) < 1:
                if not compare_acls(src, dst):
                    if apply_acl(src, dst):
                        with state.stats_lock: state.acl_applied += 1
                        log_main(f"[ACL_ONLY] | 0.00s | {format_size(size)} | {rel_path}", state, last_events)
                    else:
                        with state.stats_lock: state.files_failed += 1
                        log_error("ACL_ERR", "Не удалось применить дескриптор безопасности", rel_path, state, last_events)
                else:
                    with state.stats_lock: state.files_skipped += 1
                with state.stats_lock:
                    state.bytes_processed += size
                    state.files_processed += 1
                state.thread_info[tid]["status"] = "IDLE"
                return
        except:
            pass

    success = False
    start_time = time.time()
    for attempt in range(1, MAX_RETRIES + 1):
        if state.stop_event.is_set(): break
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(src, 'rb') as fsrc:
                with open(dst, 'wb') as fdst:
                    copied = 0
                    while True:
                        if state.stop_event.is_set(): break
                        chunk = fsrc.read(1024*1024)
                        if not chunk: break
                        fdst.write(chunk)
                        copied += len(chunk)
                        state.update_speed(len(chunk))
                        state.thread_info[tid]["progress"] = int(copied / size * 100) if size > 0 else 100
                        state.thread_info[tid]["spinner_idx"] += 1

                        # Динамический ETA для потока
                        elapsed_thread = time.time() - start_time
                        if elapsed_thread > 1:
                            thread_speed = copied / elapsed_thread
                            if thread_speed > 0:
                                t_eta_sec = (size - copied) / thread_speed
                                state.thread_info[tid]["eta"] = str(datetime.timedelta(seconds=int(t_eta_sec)))

            if state.stop_event.is_set():
                if os.path.exists(dst): os.remove(dst)
                break

            src_stat = os.stat(src)
            os.utime(dst, (src_stat.st_atime, src_stat.st_mtime))
            apply_acl(src, dst)
            success = True
            duration = time.time() - start_time
            log_main(f"[COPY_OK] | {duration:.2f}s | {format_size(size)} | {rel_path}", state, last_events)
            with state.stats_lock:
                state.files_copied += 1
                state.bytes_copied += size
                state.files_processed += 1
                state.bytes_processed += size
            break
        except Exception as e:
            state.thread_info[tid]["status"] = "RTRY"
            with state.stats_lock: state.retries += 1
            if attempt < MAX_RETRIES:
                last_events.append(f"{datetime.datetime.now().strftime('%H:%M:%S')} {C_YELLOW}[WARN]{C_WHITE} -> Лок файла {os.path.basename(rel_path)} воркером T{tid}. Попытка {attempt}/{MAX_RETRIES}...")
                time.sleep(RETRY_DELAY)
            else:
                log_error("IO_ERR", str(e), rel_path, state, last_events)
                with state.stats_lock:
                    state.files_failed += 1
                    state.files_processed += 1
                    state.bytes_processed += size
    state.thread_info[tid]["status"] = "IDLE"

# --- ОСНОВНОЙ ДВИЖОК ---
def run_smart_sync():
    state = SyncState()
    task_queue = queue.Queue(maxsize=1000)
    last_events = collections.deque(maxlen=10)
    workers = []

    for i in range(1, THREADS_COUNT + 1):
        t = threading.Thread(target=sync_worker, args=(task_queue, state, i, last_events), daemon=True)
        t.start()
        workers.append(t)

    def tui_loop():
        last_calc_time = time.time()
        while not state.stop_event.is_set() and not state.end_time:
            now = time.time()
            dt = now - last_calc_time

            state.cpu_usage.append(psutil.cpu_percent())
            state.ram_usage.append(psutil.Process().memory_info().rss / (1024**2))

            if dt >= 0.2:
                with state.stats_lock:
                    speed_bps = state.current_speed_counter / dt
                    state.speed_history.append(speed_bps)
                    state.target_io_history.append(speed_bps)
                    state.current_speed_counter = 0
                last_calc_time = now

            render_tui(state, list(last_events))
            time.sleep(0.2)

    tui_thread = threading.Thread(target=tui_loop, daemon=True)
    tui_thread.start()

    source_items = set()
    excluded_paths = set()
    try:
        if WIN32_AVAILABLE:
            kb_thread = threading.Thread(target=kb_listener, args=(state,), daemon=True)
            kb_thread.start()

        for root, dirs, files in os.walk(SOURCE_PATH):
            if state.stop_event.is_set(): break

            rel_root = get_relative_path(root, SOURCE_PATH)

            # Обработка исключаемых директорий
            filtered_dirs = []
            for d in dirs:
                rel_d = ntpath.join(rel_root, d) if rel_root else d
                if is_excluded(d):
                    excluded_paths.add(rel_d.lower())
                else:
                    filtered_dirs.append(d)
            dirs[:] = filtered_dirs

            for d in dirs:
                rel_d = ntpath.join(rel_root, d) if rel_root else d
                source_items.add(rel_d.lower())
                with state.stats_lock: state.dirs_total += 1
                dst_dir = os.path.join(DEST_PATH, rel_d)
                if not os.path.exists(dst_dir):
                    try:
                        os.makedirs(dst_dir, exist_ok=True)
                        apply_acl(os.path.join(root, d), dst_dir)
                        with state.stats_lock: state.dirs_copied += 1
                    except:
                        with state.stats_lock: state.dirs_failed += 1
                else:
                    if not compare_acls(os.path.join(root, d), dst_dir):
                        if apply_acl(os.path.join(root, d), dst_dir):
                            with state.stats_lock: state.acl_applied += 1
                    with state.stats_lock: state.dirs_skipped += 1

            for f in files:
                if state.stop_event.is_set(): break
                rel_f = ntpath.join(rel_root, f) if rel_root else f
                if is_excluded(f):
                    excluded_paths.add(rel_f.lower())
                    with state.stats_lock:
                        state.files_excluded += 1
                        state.files_processed += 1
                    continue

                source_items.add(rel_f.lower())
                src_file = os.path.join(root, f)
                dst_file = os.path.join(DEST_PATH, rel_f)
                try:
                    size = os.path.getsize(src_file)
                    task_queue.put((src_file, dst_file, rel_f, size))
                except:
                    with state.stats_lock:
                        state.files_failed += 1
                        state.files_processed += 1

        for _ in range(THREADS_COUNT): task_queue.put(None)

        while any(w.is_alive() for w in workers):
            if state.stop_event.is_set(): break
            time.sleep(0.1)

        if not state.stop_event.is_set():
            # Фаза 2: Очистка (Зеркалирование)
            for root, dirs, files in os.walk(DEST_PATH, topdown=False):
                if state.stop_event.is_set(): break
                rel_root = get_relative_path(root, DEST_PATH)

                # Проверка, находится ли текущий путь внутри исключенного пути
                is_path_excluded = False
                temp_path = rel_root.lower()
                while temp_path:
                    if temp_path in excluded_paths:
                        is_path_excluded = True
                        break
                    parent = ntpath.dirname(temp_path)
                    if parent == temp_path: break
                    temp_path = parent

                if is_path_excluded: continue

                for f in files:
                    rel_f = ntpath.join(rel_root, f) if rel_root else f
                    if rel_f.lower() in excluded_paths: continue
                    if rel_f.lower() not in source_items:
                        try:
                            os.remove(os.path.join(root, f))
                            log_main(f"[DELETE_OK] | 0.00s | 0.00 B | {rel_f}", state, last_events)
                            with state.stats_lock: state.files_deleted += 1
                        except: pass

                for d in dirs:
                    rel_d = ntpath.join(rel_root, d) if rel_root else d
                    if rel_d.lower() in excluded_paths: continue
                    if rel_d.lower() not in source_items:
                        try:
                            shutil.rmtree(os.path.join(root, d))
                            log_main(f"[DELETE_OK] | 0.00s | 0.00 B | {rel_d}", state, last_events)
                            with state.stats_lock: state.dirs_deleted += 1
                        except: pass

    except KeyboardInterrupt:
        state.stop_event.set()
    except Exception as e:
        log_error("CRITICAL", str(e), "Main Loop", state, last_events)
    finally:
        state.end_time = time.time()
        state.save_history()
        render_tui(state, list(last_events))
    return vars(state)

def kb_listener(state):
    if not WIN32_AVAILABLE: return
    while not state.stop_event.is_set():
        if msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch == b'\x00' or ch == b'\xe0':
                ch2 = msvcrt.getch()
                if ch2 == b'D': # F10
                    state.stop_event.set()
                    break
        time.sleep(0.1)

if __name__ == "__main__":
    if sys.platform == "win32":
        import ctypes
        if not ctypes.windll.shell32.IsUserAnAdmin():
            pass
    run_smart_sync()
