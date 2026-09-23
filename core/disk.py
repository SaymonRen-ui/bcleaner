"""Аналитика диска: диски, сканирование папок, топ, крупные файлы."""
from __future__ import annotations

import os
import string
from dataclasses import dataclass, field
from pathlib import Path

from .utils import get_dir_size_ex

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False


@dataclass
class DriveInfo:
    letter: str
    label: str = ""
    total: int = 0
    used: int = 0
    free: int = 0


def list_drives() -> list[DriveInfo]:
    drives: list[DriveInfo] = []
    if _HAS_PSUTIL:
        try:
            for p in psutil.disk_partitions(all=False):
                try:
                    u = psutil.disk_usage(p.mountpoint)
                    drives.append(DriveInfo(
                        letter=p.mountpoint, label=p.fstype,
                        total=u.total, used=u.used, free=u.free,
                    ))
                except (PermissionError, OSError):
                    continue
            if drives:
                return drives
        except Exception:
            pass
    import shutil
    for letter in string.ascii_uppercase:
        path = f"{letter}:\\"
        if os.path.exists(path):
            try:
                u = shutil.disk_usage(path)
                drives.append(DriveInfo(letter=path, total=u.total, used=u.used, free=u.free))
            except OSError:
                continue
    return drives


@dataclass
class FolderStat:
    path: str
    size: int
    files: int = 0
    truncated: bool = False  # True — размер приблизительный (сработал лимит)
    children: list["FolderStat"] = field(default_factory=list)


def scan_folder_children(root: str, max_children: int = 60,
                         on_progress=None, cancel_flag=None,
                         large_min: int = 100 * 1024 * 1024, large_limit: int = 50,
                         workers: int = 8) -> tuple[FolderStat, list[tuple[str, int]]]:
    """Сканирует прямых детей root (папки+файлы), считает размер каждой папки.

    Возвращает (stat, крупные_файлы): крупные файлы собираются ЗА ТОТ ЖЕ
    проход (без второго обхода дерева), замер папок идёт в N потоков.

    on_progress(done, total, msg) — счётчик для UI.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    root_stat = FolderStat(path=root, size=0)
    large: list[tuple[str, int]] = []
    try:
        entries = list(os.scandir(root))
    except (PermissionError, FileNotFoundError, OSError):
        return root_stat, large
    total = len(entries) or 1
    folder_entries = []
    for i, e in enumerate(entries):
        if cancel_flag and cancel_flag():
            break
        try:
            if e.is_symlink():
                continue
            if e.is_dir(follow_symlinks=False):
                folder_entries.append(e.path)
            elif e.is_file(follow_symlinks=False):
                try:
                    s = e.stat(follow_symlinks=False).st_size
                    root_stat.size += s
                    root_stat.files += 1
                    root_stat.children.append(FolderStat(path=e.path, size=s, files=1))
                    if large_min and s >= large_min:
                        large.append((e.path, s))
                except OSError:
                    continue
        except OSError:
            continue
        if on_progress and i % 25 == 0:
            on_progress(i, total, f"Чтение списка: {e.name}")
    # тяжёлая часть — размеры папок, параллельно (syscall'ы отпускают GIL)
    nfolders = len(folder_entries)
    if nfolders and not (cancel_flag and cancel_flag()):
        nw = max(1, min(workers, nfolders, (os.cpu_count() or 4)))
        done_n = [0]

        def _measure(fpath: str):
            buf: list[tuple[str, int]] = []
            s, c, trunc = get_dir_size_ex(fpath, large_min=large_min, _large=buf,
                                          cancel_flag=cancel_flag)
            return fpath, s, c, trunc, buf

        try:
            with ThreadPoolExecutor(max_workers=nw, thread_name_prefix="disk") as ex:
                futs = {ex.submit(_measure, fp): fp for fp in folder_entries}
                for fut in as_completed(futs):
                    if cancel_flag and cancel_flag():
                        ex.shutdown(wait=False, cancel_futures=True)
                        break
                    try:
                        fpath, s, c, trunc, buf = fut.result()
                    except Exception:
                        continue
                    if trunc:
                        root_stat.truncated = True
                    root_stat.size += s
                    root_stat.files += c
                    root_stat.children.append(
                        FolderStat(path=fpath, size=s, files=c, truncated=trunc))
                    large.extend(buf)
                    done_n[0] += 1
                    if on_progress:
                        on_progress(done_n[0], nfolders,
                                    f"Замер ({done_n[0]}/{nfolders}): {os.path.basename(fpath)}")
        except Exception:
            pass
    large.sort(key=lambda x: x[1], reverse=True)
    large = large[:large_limit]
    root_stat.children.sort(key=lambda x: x.size, reverse=True)
    # файлы тоже в общем списке, но папки важнее — уже отсортировано по размеру
    root_stat.children = root_stat.children[:max_children]
    return root_stat, large


def find_large_files(root: str, min_size: int = 100 * 1024 * 1024,
                     limit: int = 50, cancel_flag=None, on_progress=None) -> list[tuple[str, int]]:
    """Ищет файлы крупнее min_size. on_progress(visited_dirs, found) — счётчик для UI."""
    found: list[tuple[str, int]] = []
    visited = 0
    stack = [root]
    while stack:
        if cancel_flag and cancel_flag():
            break
        cur = stack.pop()
        visited += 1
        if on_progress and visited % 50 == 0:
            on_progress(visited, len(found))
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            try:
                                s = e.stat(follow_symlinks=False).st_size
                            except OSError:
                                continue
                            if s >= min_size:
                                found.append((e.path, s))
                                if len(found) >= limit * 3:
                                    found.sort(key=lambda x: x[1], reverse=True)
                                    found = found[:limit]
                    except OSError:
                        continue
        except (PermissionError, FileNotFoundError, OSError):
            continue
    found.sort(key=lambda x: x[1], reverse=True)
    return found[:limit]
