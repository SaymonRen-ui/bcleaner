"""Общие утилиты: размеры, права, безопасное удаление, реестр."""
from __future__ import annotations

import ctypes
import os
import shutil
import stat
from pathlib import Path


def format_size(num: float) -> str:
    num = float(num or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if num < 1024.0:
            if unit == "Б":
                return f"{int(num)} {unit}"
            return f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} ПБ"


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> tuple[bool, str]:
    """Перезапускает приложение с правами администратора (UAC).
    Возвращает (True, '') если запрос на повышение отправлен."""
    import sys
    try:
        if getattr(sys, "frozen", False):
            exe = sys.executable
            params = ""
        else:
            exe = sys.executable
            script = str(Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else Path("main.py").resolve())
            if not os.path.exists(script):
                script = str(Path.cwd() / "main.py")
            params = f'"{script}"'
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, os.path.dirname(exe) or None, 1)
        if int(rc) > 32:
            return True, ""
        return False, f"ShellExecute вернул код {int(rc)}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _canon(path: str | Path) -> str:
    """Канонический путь для дедупа: realpath (раскрывает junction/symlink) + normcase."""
    try:
        return os.path.normcase(os.path.realpath(os.fspath(path)))
    except OSError:
        try:
            return os.path.normcase(os.path.abspath(os.fspath(path)))
        except OSError:
            return os.fspath(path)


def get_dir_size_ex(path: str | Path, max_files: int = 200_000,
                    large_min: int = 0, _large: list | None = None,
                    cancel_flag=None) -> tuple[int, int, bool]:
    """Возвращает (bytes, files, truncated).

    - Считает содержимое junction/symlink-папок (как Проводник), но каждый
      реальный каталог — один раз (защита от циклов вида
      AppData\\Local\\Application Data -> AppData\\Local).
    - Дедуп по st_dev/st_ino НЕ используется: на Windows st_ino каталогов
      часто равен 0, что давало кратно заниженные размеры.
    - truncated=True, если сработал лимит max_files (размер приблизительный).
    - large_min/_large: попутно собирать файлы >= large_min (один проход
      вместо отдельного поиска крупных файлов).
    """
    total = 0
    count = 0
    truncated = False
    try:
        seen: set[str] = {_canon(path)}
        stack = [os.fspath(path)]
        while stack:
            if cancel_flag and cancel_flag():
                break
            cur = stack.pop()
            try:
                with os.scandir(cur) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                try:
                                    rp = _canon(entry.path)
                                except OSError:
                                    continue
                                if rp in seen:
                                    continue
                                seen.add(rp)
                                stack.append(entry.path)
                            elif entry.is_file(follow_symlinks=False):
                                try:
                                    sz = entry.stat(follow_symlinks=False).st_size
                                    total += sz
                                    count += 1
                                    if large_min and _large is not None and sz >= large_min:
                                        _large.append((entry.path, sz))
                                    if count >= max_files:
                                        return total, count, True
                                except OSError:
                                    continue
                            # остальное (битые ссылки и т.п.) — пропускаем
                        except OSError:
                            continue
            except (PermissionError, FileNotFoundError, OSError):
                continue
    except (PermissionError, FileNotFoundError, OSError):
        pass
    return total, count, truncated


def get_dir_size(path: str | Path, max_files: int = 200_000) -> tuple[int, int]:
    """Возвращает (bytes, files). Не падает на PermissionError / symlink loop."""
    total, count, _ = get_dir_size_ex(path, max_files)
    return total, count


def _on_rm_error(func, path, exc_info):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def safe_remove(paths: list[str | Path], use_trash_for_dirs: bool = False) -> tuple[int, int, list[str]]:
    """Удаляет файлы/папки. Возвращает (удалено_байт_примерно, кол-во, ошибки)."""
    removed_bytes = 0
    removed_count = 0
    errors: list[str] = []
    for p in paths:
        try:
            pp = Path(p)
            if not pp.exists() and not pp.is_symlink():
                continue
            # оценим размер для статистики (только файлы/папки, не реестр)
            try:
                if pp.is_file() or pp.is_symlink():
                    removed_bytes += pp.stat().st_size
                elif pp.is_dir():
                    s, _ = get_dir_size(pp, max_files=50_000)
                    removed_bytes += s
            except OSError:
                pass
            if pp.is_file() or pp.is_symlink():
                try:
                    pp.unlink(missing_ok=True)
                    removed_count += 1
                except PermissionError:
                    os.chmod(pp, stat.S_IWRITE)
                    pp.unlink(missing_ok=True)
                    removed_count += 1
            elif pp.is_dir():
                if use_trash_for_dirs:
                    try:
                        from send2trash import send2trash
                        send2trash(str(pp))
                        removed_count += 1
                        continue
                    except Exception:
                        pass
                shutil.rmtree(pp, onerror=_on_rm_error, ignore_errors=False)
                removed_count += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{p}: {e}")
    return removed_bytes, removed_count, errors


def empty_recycle_bin() -> bool:
    try:
        # SHEmptyRecycleBinW(None, None, SHERB_NOCONFIRMATION|SHERB_NOPROGRESSUI|SHERB_NOSOUND = 1|2|4 = 7)
        res = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 7)
        return res in (0, None)
    except Exception:
        return False


def normalize_name(name: str) -> str:
    return "".join(ch.lower() for ch in (name or "") if ch.isalnum())


def tokenize(name: str) -> list[str]:
    import re
    parts = re.split(r"[^A-Za-zА-Яа-я0-9]+", name or "")
    return [p for p in parts if len(p) >= 3]
