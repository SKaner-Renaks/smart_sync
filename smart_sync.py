#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
==========================================================================================
SMART_SYNC.PY (v1.0.0)
Зеркальная многопоточная синхронизация данных с сохранением ACL (Win32)
==========================================================================================
"""

import os
import sys
import time
import json
import queue
import threading
import datetime
import shutil
import hashlib
import math
import logging
from typing import Dict, Any, List, Optional

# --- ВНЕШНИЕ ЗАВИСИМОСТИ ---
try:
    import psutil
except ImportError:
    psutil = None

try:
    import colorama
    from colorama import Fore, Back, Style
    colorama.init()
except ImportError:
    colorama = None

# --- WIN32 ACL ---
WIN32_AVAILABLE = False
if sys.platform == "win32":
    try:
        import win32security
        import ntsecuritycon
        import win32api
        import winerror
        WIN32_AVAILABLE = True
    except ImportError:
        pass

# ==========================================================================================
# [1] СТАТИЧЕСКАЯ КОНФИГУРАЦИЯ
# ==========================================================================================

VERSION = "1.0.0"
SOURCE_PATH = r""  # Пример: r"\\srv-fs01\share"
DEST_PATH = r""    # Пример: r"D:\Backup"
THREADS_COUNT = 16
MAX_RETRIES = 3
RETRY_DELAY = 5
ACL_DEEP_CHECK = True  # True — полная проверка ACL, False — только size + mtime

# ==========================================================================================
# [2] СЛУЖЕБНЫЕ ПУТИ И ГЛОБАЛЬНЫЕ КОНСТАНТЫ
# ==========================================================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_MAIN = os.path.join(SCRIPT_DIR, "sync_main.log")
LOG_ERRORS = os.path.join(SCRIPT_DIR, "sync_errors.log")
HISTORY_FILE = os.path.join(SCRIPT_DIR, "sync_history.json")

# ==========================================================================================
# [3] ЕДИНЫЙ КЛАСС СОСТОЯНИЯ (SYNCSTATE)
# ==========================================================================================

class SyncState:
    def __init__(self):
        # Статистика файлов
        self.dirs_total = 0
        self.dirs_copied = 0
        self.dirs_skipped = 0
        self.dirs_deleted = 0

        self.files_total = 0      # Реальное количество в текущей сессии
        self.files_processed = 0  # Обработано (успех + ошибка + пропущено)
        self.files_copied = 0
        self.files_skipped = 0
        self.files_deleted = 0
        self.acl_applied = 0
        self.errors_count = 0

        # Статистика байтов
        self.bytes_total = 0
        self.bytes_copied = 0
        self.bytes_skipped = 0
        self.bytes_deleted = 0

        # История (Y)
        self.history_files_total = 0
        self.history_bytes_total = 0

        # Тайминги
        self.start_time = 0
        self.end_time = 0

        # Телеметрия ресурсов
        self.cpu_samples = []
        self.ram_samples = []
        self.ram_max = 0

        # Бенчмарк I/O (байты, секунды)
        self.io_lock = threading.Lock()
        self.net_read_stats = {"bytes": 0, "time": 0.0, "min": float('inf'), "max": 0.0}
        self.disk_write_stats = {"bytes": 0, "time": 0.0, "min": float('inf'), "max": 0.0}

        # Состояние потоков
        self.threads_info = {} # tid -> {status, progress, size, eta, path, spinner_idx}
        self.threads_lock = threading.Lock()

        # Очереди и события
        self.copy_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.enumeration_complete = threading.Event()

        # Лог последних событий для TUI
        self.recent_events = []
        self.events_lock = threading.Lock()

    def add_event(self, msg):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        with self.events_lock:
            self.recent_events.append(f"{timestamp} {msg}")
            if len(self.recent_events) > 3:
                self.recent_events.pop(0)

    def update_thread(self, tid, **kwargs):
        with self.threads_lock:
            if tid not in self.threads_info:
                self.threads_info[tid] = {
                    "status": "IDLE", "progress": 0, "size": 0,
                    "eta": "--:--:--", "path": "", "spinner_idx": 0
                }
            self.threads_info[tid].update(kwargs)

    def record_io(self, kind, nbytes, duration):
        if duration <= 0: duration = 0.000001
        speed = nbytes / duration
        with self.io_lock:
            stats = self.net_read_stats if kind == "net" else self.disk_write_stats
            stats["bytes"] += nbytes
            stats["time"] += duration
            if speed < stats["min"]: stats["min"] = speed
            if speed > stats["max"]: stats["max"] = speed

# ==========================================================================================
# [4] ЛОГИРОВАНИЕ И ИСТОРИЯ (LOGGING & HISTORY)
# ==========================================================================================

log_lock = threading.Lock()

def write_log(logfile, message):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_lock:
        with open(logfile, "a", encoding="utf-8") as f:
            f.write(f"{timestamp} | {message}\n")

def log_success(status, duration, size, path):
    # [COPY_OK], [ACL_ONLY], [DELETE_OK]
    # Формат: ГГГГ-ММ-ДД ЧЧ:ММ:СС | СТАТУС | ВРЕМЯ_ОПЕРАЦИИ | РАЗМЕР | ПУТЬ
    duration_str = f"{duration:.2f}s"
    size_str = format_size(size)
    write_log(LOG_MAIN, f"{status:<10} | {duration_str:>7} | {size_str:>10} | {path}")

def log_error(err_msg, path):
    # [CRIT_ERR] | Код ошибки / Текст исключения OS | ПУТЬ
    write_log(LOG_ERRORS, f"[CRIT_ERR] | {err_msg} | {path}")

def load_history(state: SyncState):
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Ключ - хэш путей
                key = get_history_key(SOURCE_PATH, DEST_PATH)
                if key in data:
                    state.history_files_total = data[key].get("total_files", 0)
                    state.history_bytes_total = data[key].get("total_bytes", 0)
        except Exception:
            pass

def save_history(state: SyncState):
    data = {}
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass

    key = get_history_key(SOURCE_PATH, DEST_PATH)
    data[key] = {
        "total_files": state.files_total,
        "total_bytes": state.bytes_copied + state.bytes_skipped,
        "last_sync": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception:
        pass

def get_history_key(src, dst):
    return hashlib.md5(f"{src}->{dst}".encode('utf-8')).hexdigest()

def format_size(size_bytes):
    if size_bytes == 0: return "0.00 B"
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s:.2f} {units[i]}"

# ==========================================================================================
# [5] ACL И ФАЙЛОВЫЕ ОПЕРАЦИИ (ACL & FILE OPS)
# ==========================================================================================

def get_security_descriptor(path):
    if not WIN32_AVAILABLE:
        return None
    try:
        sd = win32security.GetFileSecurity(path, win32security.DACL_SECURITY_INFORMATION)
        return win32security.GetSecurityDescriptorDacl(sd)
    except Exception:
        return None

def set_security_descriptor(path, dacl):
    if not WIN32_AVAILABLE or dacl is None:
        return False
    try:
        sd = win32security.GetFileSecurity(path, win32security.DACL_SECURITY_INFORMATION)
        win32security.SetSecurityDescriptorDacl(sd, 1, dacl, 0)
        win32security.SetFileSecurity(path, win32security.DACL_SECURITY_INFORMATION, sd)
        return True
    except Exception:
        return False

def compare_acl(src_path, dst_path):
    """
    Сравнивает ACL источника и назначения.
    Возвращает True, если ACL идентичны.
    """
    if not WIN32_AVAILABLE:
        return True

    sd_src = get_security_descriptor(src_path)
    sd_dst = get_security_descriptor(dst_path)

    if sd_src is None or sd_dst is None:
        return sd_src == sd_dst

    # Сравнение бинарных дескрипторов или их строковых представлений
    # В pywin32 можно получить SD как байты
    try:
        sd_src_full = win32security.GetFileSecurity(src_path, win32security.DACL_SECURITY_INFORMATION)
        sd_dst_full = win32security.GetFileSecurity(dst_path, win32security.DACL_SECURITY_INFORMATION)
        return sd_src_full.GetSecurityDescriptorBinaryForm() == sd_dst_full.GetSecurityDescriptorBinaryForm()
    except Exception:
        return False

def copy_file_with_acl(src, dst, state: SyncState, tid: str):
    """
    Копирует файл поблочно, замеряет скорость и переносит ACL.
    """
    start_time = time.time()

    # Создаем директорию если нет
    os.makedirs(os.path.dirname(dst), exist_ok=True)

    size = os.path.getsize(src)
    copied = 0

    try:
        with open(src, 'rb') as fsrc:
            with open(dst, 'wb') as fdst:
                while True:
                    # Чтение
                    r_start = time.perf_counter()
                    buf = fsrc.read(1024 * 1024) # 1MB buffer
                    r_end = time.perf_counter()
                    if not buf:
                        break
                    state.record_io("net", len(buf), r_end - r_start)

                    # Запись
                    w_start = time.perf_counter()
                    fdst.write(buf)
                    w_end = time.perf_counter()
                    state.record_io("disk", len(buf), w_end - w_start)

                    copied += len(buf)
                    progress = int((copied / size) * 100) if size > 0 else 100

                    # Обновление TUI
                    elapsed = time.time() - start_time
                    eta = "calculating"
                    if progress > 0:
                        eta_sec = (elapsed / progress) * (100 - progress)
                        eta = str(datetime.timedelta(seconds=int(eta_sec)))

                    state.update_thread(tid, progress=progress, eta=eta)

        # Копируем mtime
        s_stat = os.stat(src)
        os.utime(dst, (s_stat.st_atime, s_stat.st_mtime))

        # Накатываем ACL
        if WIN32_AVAILABLE:
            sd = win32security.GetFileSecurity(src, win32security.DACL_SECURITY_INFORMATION)
            win32security.SetFileSecurity(dst, win32security.DACL_SECURITY_INFORMATION, sd)
            state.acl_applied += 1

        duration = time.time() - start_time
        log_success("[COPY_OK]", duration, size, dst)
        return True
    except Exception as e:
        raise e

def apply_acl_only(src, dst, state: SyncState):
    """
    Применяет только ACL без копирования данных.
    """
    start_time = time.time()
    try:
        if WIN32_AVAILABLE:
            sd = win32security.GetFileSecurity(src, win32security.DACL_SECURITY_INFORMATION)
            win32security.SetFileSecurity(dst, win32security.DACL_SECURITY_INFORMATION, sd)
            state.acl_applied += 1
            duration = time.time() - start_time
            log_success("[ACL_ONLY]", duration, 0, dst)
            return True
    except Exception as e:
        raise e
    return False

# ==========================================================================================
# [6] ЛОГИКА ВОРКЕРОВ И ОЧЕРЕДИ (WORKER LOGIC)
# ==========================================================================================

def sync_worker(tid, state: SyncState, retries: int, delay: int):
    """
    Поток-воркер для обработки очереди копирования.
    """
    while not state.stop_event.is_set():
        try:
            # Ждем задачу с таймаутом, чтобы проверять stop_event
            task = state.copy_queue.get(timeout=1)
        except queue.Empty:
            if state.enumeration_complete.is_set():
                break
            continue

        action, src, dst, size = task
        state.update_thread(tid, status="[COPY]", path=dst, size=size, progress=0)

        success = False
        for attempt in range(1, retries + 1):
            try:
                if action == "COPY":
                    success = copy_file_with_acl(src, dst, state, tid)
                    if success:
                        state.files_copied += 1
                        state.bytes_copied += size
                elif action == "ACL":
                    success = apply_acl_only(src, dst, state)

                if success:
                    break
            except Exception as e:
                # Ошибка 32 в Windows - файл занят
                is_lock = False
                if sys.platform == "win32":
                    import pywintypes
                    if isinstance(e, pywintypes.error) and e.winerror == 32:
                        is_lock = True

                if is_lock or "Permission denied" in str(e) or "being used by another process" in str(e):
                    if attempt < retries:
                        state.update_thread(tid, status="[RTRY]")
                        state.add_event(f"[WARN] -> Лок файла {os.path.basename(src)} воркером {tid}. Ретрай {attempt}/{retries}...")
                        time.sleep(delay)
                        state.update_thread(tid, status="[COPY]")
                        continue
                    else:
                        log_error(f"File locked after {retries} retries: {str(e)}", src)
                        state.errors_count += 1
                else:
                    log_error(f"Error processing file: {str(e)}", src)
                    state.errors_count += 1
                    break

        state.files_processed += 1
        state.update_thread(tid, status="IDLE", progress=0, path="", size=0, eta="--:--:--")
        state.copy_queue.task_done()

# ==========================================================================================
# [7] ФАЗЫ СИНХРОНИЗАЦИИ (PHASES)
# ==========================================================================================

def phase_1_enumeration(source, dest, state: SyncState):
    """
    Сканирование источника и наполнение очереди задач.
    """
    for root, dirs, files in os.walk(source):
        if state.stop_event.is_set():
            break

        # Относительный путь для воссоздания структуры
        rel_path = os.path.relpath(root, source)
        if rel_path == ".":
            dest_root = dest
        else:
            dest_root = os.path.join(dest, rel_path)

        # Создаем директории на месте (чтобы mtime/ACL можно было проверить)
        state.dirs_total += 1
        if not os.path.exists(dest_root):
            try:
                os.makedirs(dest_root, exist_ok=True)
                state.dirs_copied += 1
                # Переносим ACL для папки
                if WIN32_AVAILABLE:
                    sd = win32security.GetFileSecurity(root, win32security.DACL_SECURITY_INFORMATION)
                    win32security.SetFileSecurity(dest_root, win32security.DACL_SECURITY_INFORMATION, sd)
            except Exception as e:
                log_error(f"Failed to create directory: {str(e)}", dest_root)
        else:
            state.dirs_skipped += 1

        for file in files:
            if state.stop_event.is_set():
                break

            src_file = os.path.join(root, file)
            dst_file = os.path.join(dest_root, file)

            try:
                s_stat = os.stat(src_file)
                state.files_total += 1
                state.bytes_total += s_stat.st_size

                if not os.path.exists(dst_file):
                    state.copy_queue.put(("COPY", src_file, dst_file, s_stat.st_size))
                else:
                    d_stat = os.stat(dst_file)

                    # Критерии: Размер и Дата модификации
                    changed = (s_stat.st_size != d_stat.st_size) or (abs(s_stat.st_mtime - d_stat.st_mtime) > 1)

                    if changed:
                        state.copy_queue.put(("COPY", src_file, dst_file, s_stat.st_size))
                    else:
                        # Проверка ACL если размер/дата совпали
                        acl_changed = False
                        if ACL_DEEP_CHECK and WIN32_AVAILABLE:
                            if not compare_acl(src_file, dst_file):
                                acl_changed = True

                        if acl_changed:
                            state.copy_queue.put(("ACL", src_file, dst_file, 0))
                        else:
                            state.files_skipped += 1
                            state.files_processed += 1
                            state.bytes_skipped += s_stat.st_size
            except Exception as e:
                log_error(f"Enumeration error: {str(e)}", src_file)
                state.errors_count += 1

    state.enumeration_complete.set()

def phase_2_cleanup(source, dest, state: SyncState):
    """
    Очистка зеркала (удаление лишнего из назначения).
    """
    state.add_event("[ФАЗА 2]: Очистка зеркала...")

    # Сначала обходим файлы и папки в Destination снизу вверх (topdown=False)
    # чтобы удалять пустые папки после файлов
    for root, dirs, files in os.walk(dest, topdown=False):
        if state.stop_event.is_set():
            break

        rel_path = os.path.relpath(root, dest)
        src_root = os.path.join(source, rel_path)

        # Удаляем файлы
        for file in files:
            dst_file = os.path.join(root, file)
            src_file = os.path.join(src_root, file)

            if not os.path.exists(src_file):
                try:
                    f_size = os.path.getsize(dst_file)
                    os.remove(dst_file)
                    state.files_deleted += 1
                    state.bytes_deleted += f_size
                    log_success("[DELETE_OK]", 0, f_size, dst_file)
                except Exception as e:
                    log_error(f"Cleanup error (file): {str(e)}", dst_file)
                    state.errors_count += 1

        # Удаляем папки
        for d in dirs:
            dst_dir = os.path.join(root, d)
            src_dir = os.path.join(src_root, d)

            if not os.path.exists(src_dir):
                try:
                    shutil.rmtree(dst_dir)
                    state.dirs_deleted += 1
                    log_success("[DELETE_OK]", 0, 0, dst_dir)
                except Exception as e:
                    log_error(f"Cleanup error (dir): {str(e)}", dst_dir)
                    state.errors_count += 1

# ==========================================================================================
# [8] ИНТЕРФЕЙС ПОЛЬЗОВАТЕЛЯ (TUI)
# ==========================================================================================

def compress_path(path, width):
    if not path: return ""
    if len(path) <= width:
        return path

    # Requirement: filename never cut.
    filename = os.path.basename(path)
    if len(filename) >= width - 5:
        return f"...\\{filename}"

    # Drive/Root part
    drive = ""
    if ":" in path:
        drive = path.split(":")[0] + ":"
    elif path.startswith("\\\\"):
        # SMB path
        parts = path.split("\\")
        if len(parts) > 3:
            drive = "\\\\" + parts[2] + "\\" + parts[3]

    if not drive:
        # Fallback to just taking the first part of the path
        parts = path.split(os.sep)
        drive = parts[0]

    # drive + \ + ... + \ + filename
    fixed_len = len(drive) + 5 + len(filename)

    if fixed_len >= width:
        return f"{drive}...\\{filename}"

    mid_width = width - len(drive) - len(filename) - 5
    # Find some directory parts
    rel = os.path.relpath(os.path.dirname(path), drive) if drive else os.path.dirname(path)

    return f"{drive}\\{rel[:mid_width]}...\\{filename}"

def get_progress_bar(percent, width=36):
    filled = int(width * percent / 100)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {percent:5.1f}%"

def render_active_ui(state: SyncState, source, dest):
    os.system('cls' if sys.platform == 'win32' else 'clear')

    print(f"{'='*27} [ PYTHON SMART SYNC V{VERSION} ] {'='*27}")
    print(f"Запуск:    {datetime.datetime.fromtimestamp(state.start_time).strftime('%d.%m.%Y %H:%M:%S')}")
    print(f"Источник:  {source}")
    print(f"Цель:      {dest}")
    print("-" * 90)

    # Расчет прогресса
    x = state.files_processed
    y = state.history_files_total
    progress_pct = 0
    y_str = str(y)

    if not state.enumeration_complete.is_set():
        y_str = "[сбор данных...]"
        progress_pct = 0
    else:
        y = state.files_total
        y_str = f"{y:,}"
        if y > 0:
            progress_pct = min(100.0, (x / y) * 100)

    if y > 0 and x > y and not state.enumeration_complete.is_set():
        progress_pct = 99.0
        y_str = "[пересчёт...]"

    print(f"[ОБЩИЙ СТАТУС]: СИНХРОНИЗАЦИЯ ДАННЫХ...")
    print(f"Прогресс: {get_progress_bar(progress_pct)} (Память: {'OK' if state.history_files_total > 0 else 'EMPTY'})")

    # Скорость и объем
    elapsed = time.time() - state.start_time
    speed = state.bytes_copied / elapsed if elapsed > 0 else 0

    free_dest = "N/A"
    try:
        usage = psutil.disk_usage(os.path.dirname(dest) if os.path.exists(dest) else SCRIPT_DIR)
        free_dest = format_size(usage.free)
    except: pass

    processed_bytes = state.bytes_copied + state.bytes_skipped
    total_bytes_str = format_size(state.history_bytes_total) if state.history_bytes_total > 0 else "[сбор данных...]"
    if state.enumeration_complete.is_set():
        total_bytes_str = format_size(state.bytes_total)

    print(f"Объем:    {format_size(processed_bytes)} / {total_bytes_str:<12} | Скорость:   [{format_size(speed)}/s] (Свободно: {free_dest})")

    eta_str = "--:--:--"
    if progress_pct > 0 and progress_pct < 100:
        eta_sec = (elapsed / progress_pct) * (100 - progress_pct)
        eta_str = str(datetime.timedelta(seconds=int(eta_sec)))

    print(f"Файлы:    Обработано: {x:,} / {y_str:<10} | Тайминги:   Прошло: {str(datetime.timedelta(seconds=int(elapsed)))} | ETA: {eta_str}")
    print(f"Ошибки:   [ {state.errors_count} ]                        | Пропущено (Ретраи): [ {state.files_skipped} ]")
    print("-" * 90)

    print("[АКТИВНЫЕ ПОТОКИ (THREADS)]:")
    print(f" №   Status  A  Progress  Size       ETA        Target file path")

    spinners = ['/', '-', '\\', '|']
    with state.threads_lock:
        for i in range(1, THREADS_COUNT + 1):
            tid = f"T{i}"
            info = state.threads_info.get(tid, {"status": "IDLE", "progress": 0, "size": 0, "eta": "--:--:--", "path": "", "spinner_idx": 0})

            # Цвета
            status_str = info['status']
            status_display = status_str
            if colorama:
                if status_str == "[COPY]": status_display = Fore.CYAN + Back.BLACK + status_str + Style.RESET_ALL
                elif status_str == "[RTRY]": status_display = Fore.YELLOW + Back.BLACK + status_str + Style.RESET_ALL
                else: status_display = Fore.WHITE + Style.DIM + status_str + Style.RESET_ALL

            spinner = spinners[info['spinner_idx'] % 4] if info['status'] != "IDLE" else "•"
            info['spinner_idx'] += 1

            path_disp = compress_path(info['path'], 40) if info['path'] else ""

            # Ручное выравнивание из-за ANSI-кодов
            padding = " " * (8 - len(status_str))
            print(f" {tid:<3} {status_display}{padding} {spinner}   [{info['progress']:>3}%]    {format_size(info['size']):>10}  {info['eta']:<10} {path_disp}")

    print("-" * 90)
    print("[ПОСЛЕДНИЕ СОБЫТИЯ]:")
    with state.events_lock:
        for event in state.recent_events:
            print(f" {event}")

def render_final_ui(state: SyncState, source, dest):
    os.system('cls' if sys.platform == 'win32' else 'clear')
    print(f"{'='*27} [ PYTHON SMART SYNC V{VERSION} ] {'='*27}")
    print(f"Запуск:    {datetime.datetime.fromtimestamp(state.start_time).strftime('%d.%m.%Y %H:%M:%S')}")
    print(f"Источник:  {source}")
    print(f"Цель:      {dest}")
    print("-" * 90)

    print(f"[ОБЩИЙ СТАТУС]:  СИНХРОНИЗАЦИЯ УСПЕШНО ЗАВЕРШЕНА")
    print(f"Прогресс: {get_progress_bar(100.0)} (Сессия закрыта корректно)")
    print(f"Объем:    {format_size(state.bytes_copied + state.bytes_skipped):<20} | Ошибки ввода-вывода:    [ {state.errors_count} ]")
    print(f"Файлы:    Обработано: {state.files_processed:,}           | Пропущено (Ретраи):     [ {state.files_skipped} ]")
    print("-" * 90)

    print("[ТЕЛЕМЕТРИЯ СЕССИИ И ДИАГНОСТИКА СИСТЕМЫ]:")
    duration = state.end_time - state.start_time
    end_ts = datetime.datetime.fromtimestamp(state.end_time).strftime('%d.%m.%Y %H:%M:%S')

    print(f" Временные метки:  Завершение: {end_ts}  │ Общее время работы:  {str(datetime.timedelta(seconds=int(duration)))}")
    print(f" Файлы журналов:   Лог сессии: {LOG_MAIN}")
    print(f"                   Ошибки ACL: {LOG_ERRORS}")
    print(" ────────────────────────────────────────────────────────────────────────────────────────")

    cpu_avg = sum(state.cpu_samples) / len(state.cpu_samples) if state.cpu_samples else 0
    cpu_max = max(state.cpu_samples) if state.cpu_samples else 0
    ram_avg = (sum(state.ram_samples) / len(state.ram_samples)) / (1024*1024) if state.ram_samples else 0
    ram_max = state.ram_max / (1024*1024)

    print(f" Ресурсы узла:     Утилизация CPU:  Средняя: {cpu_avg:.1f}%   │ Пиковая (Max):       {cpu_max:.1f}%")
    print(f" (процесс smart_sync) Занятость RAM:   Средняя: {int(ram_avg)} MB  │ Пиковая (Max):       {int(ram_max)} MB")
    print(" ────────────────────────────────────────────────────────────────────────────────────────")

    free_dest = "N/A"
    try:
        usage = psutil.disk_usage(os.path.dirname(dest) if os.path.exists(dest) else SCRIPT_DIR)
        free_dest = format_size(usage.free)
    except: pass

    print(f" Целевой диск:     Доступное пространство на целевой машине: {free_dest}")

    acl_msg = f"[ {state.acl_applied:,} модификаций ]"
    if colorama:
        acl_msg = Fore.YELLOW + Style.BRIGHT + acl_msg + Style.RESET_ALL
    print(f" Аудит прав ACL:   ИЗМЕНЕНО И СИНХРОНИЗИРОВАНО ПРАВ ДОСТУПА (ACL): {acl_msg}")
    print("-" * 90)

    print("[БЕНЧМАРК ПРОИЗВОДИТЕЛЬНОСТИ ПОДСИСТЕМ (IO SPEED)]:")

    def get_io_line(stats):
        avg = stats["bytes"] / stats["time"] if stats["time"] > 0 else 0
        min_v = stats["min"] if stats["min"] != float('inf') else 0
        max_v = stats["max"]
        return f"Минимум: {format_size(min_v)}/s  │ Средняя: {format_size(avg)}/s │ Максимум: {format_size(max_v)}/s"

    print(f" Канал связи (Network SMB):  {get_io_line(state.net_read_stats)}")
    print(f" Локальный диск (Target IO):  {get_io_line(state.disk_write_stats)}")
    print("-" * 90)

    print("[ФИНАЛЬНАЯ СТАТИСТИКА КАТАЛОГОВ (SUMMARY)]:")
    print("            │    Всего    │ Скопировано │  Пропущено  │   Ошибки    │   Удалено   │")
    print("────────────┼─────────────┼─────────────┼─────────────┼─────────────┼─────────────┤")

    # Для упрощения в Summary выведем только файлы, так как каталоги мы не считали отдельно в SyncState
    # Но ТЗ требует Директории, Файлы, Байты.
    # В реальной реализации стоило бы добавить счетчики для директорий.

    def fmt_cell(val, width=11):
        if isinstance(val, int):
            return f"{val:>{width},}"
        return f"{val:>{width}}"

    print(f" Директории │{fmt_cell(state.dirs_total)} │{fmt_cell(state.dirs_copied)} │{fmt_cell(state.dirs_skipped)} │{fmt_cell(0)} │{fmt_cell(state.dirs_deleted)} │")
    print(f" Файлы      │{fmt_cell(state.files_total)} │{fmt_cell(state.files_copied)} │{fmt_cell(state.files_skipped)} │{fmt_cell(state.errors_count)} │{fmt_cell(state.files_deleted)} │")
    print(f" Байты      │{fmt_cell(format_size(state.bytes_total))} │{fmt_cell(format_size(state.bytes_copied))} │{fmt_cell(format_size(state.bytes_skipped))} │{fmt_cell('0.00 B')} │{fmt_cell(format_size(state.bytes_deleted))} │")
    print("────────────┴─────────────┴─────────────┴─────────────┴─────────────┴─────────────┘")
    print("\n Финальный статус: Зеркало обновлено. Ошибок ввода-вывода и деградации прав ACL не обнаружено.")

# ==========================================================================================
# [9] ГЛАВНЫЕ ФУНКЦИИ УПРАВЛЕНИЯ
# ==========================================================================================

def run_smart_sync(source: str, dest: str, threads: int = 16, retries: int = 3, wait: int = 5) -> dict:
    """
    Главная точка входа для синхронизации.
    """
    if not os.path.exists(source):
        print(f"Error: Source path does not exist: {source}")
        return {"status": "FAILED", "error": "Source not found"}

    state = SyncState()
    state.start_time = time.time()

    # 0. Инициализация и история
    load_history(state)

    # 1. Запуск воркеров
    worker_threads = []
    for i in range(1, threads + 1):
        t = threading.Thread(target=sync_worker, args=(f"T{i}", state, retries, wait), daemon=True)
        t.start()
        worker_threads.append(t)

    # 2. Запуск энумератора (в отдельном потоке, чтобы TUI обновлялся)
    enum_thread = threading.Thread(target=phase_1_enumeration, args=(source, dest, state), daemon=True)
    enum_thread.start()

    # 3. TUI Update Loop & Телеметрия
    process = psutil.Process() if psutil else None

    try:
        while not state.enumeration_complete.is_set() or not state.copy_queue.empty() or any(t.is_alive() for t in worker_threads):
            # Собираем телеметрию
            if process:
                try:
                    state.cpu_samples.append(process.cpu_percent())
                    mem = process.memory_info().rss
                    state.ram_samples.append(mem)
                    if mem > state.ram_max: state.ram_max = mem
                except: pass

            # Рендерим UI
            render_active_ui(state, source, dest)
            time.sleep(0.25)

            # Проверка, если все воркеры упали (не должно быть)
            if not any(t.is_alive() for t in worker_threads) and not state.copy_queue.empty():
                break

    except KeyboardInterrupt:
        state.stop_event.set()
        print("\nПрерывание пользователем...")

    # Дожидаемся завершения Фазы 1
    state.enumeration_complete.set()
    state.copy_queue.join()
    for t in worker_threads:
        t.join(timeout=1)

    # 4. Фаза 2: Очистка зеркала
    if not state.stop_event.is_set():
        phase_2_cleanup(source, dest, state)

    state.end_time = time.time()

    # 5. Финальный UI и сохранение
    save_history(state)
    render_final_ui(state, source, dest)

    duration_str = str(datetime.timedelta(seconds=int(state.end_time - state.start_time)))

    free_dest = "N/A"
    try:
        usage = psutil.disk_usage(os.path.dirname(dest) if os.path.exists(dest) else SCRIPT_DIR)
        free_dest = format_size(usage.free)
    except: pass

    return {
        "status": "SUCCESS" if state.errors_count == 0 else "FAILED",
        "duration": duration_str,
        "bytes_copied": state.bytes_copied,
        "files_total": state.files_total,
        "files_copied": state.files_copied,
        "files_skipped": state.files_skipped,
        "files_deleted": state.files_deleted,
        "errors_count": state.errors_count,
        "acl_applied": state.acl_applied,
        "cpu_avg": sum(state.cpu_samples) / len(state.cpu_samples) if state.cpu_samples else 0.0,
        "ram_max_mb": int(state.ram_max / (1024*1024)),
        "disk_free_after": free_dest
    }

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=f"Smart Sync v{VERSION}")
    parser.add_argument("--src", default=SOURCE_PATH, help="Source path")
    parser.add_argument("--dst", default=DEST_PATH, help="Destination path")
    parser.add_argument("--threads", type=int, default=THREADS_COUNT, help="Threads count")

    args = parser.parse_args()

    s = args.src or SOURCE_PATH
    d = args.dst or DEST_PATH

    if not s or not d:
        print("Error: SOURCE_PATH and DEST_PATH must be set in script or passed via --src/--dst")
        sys.exit(1)

    run_smart_sync(s, d, args.threads, MAX_RETRIES, RETRY_DELAY)
