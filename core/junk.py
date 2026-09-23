"""Категории мусора и их очистка."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .utils import empty_recycle_bin, get_dir_size, safe_remove

TEMP = os.environ.get("TEMP", "")
TMP = os.environ.get("TMP", "")
LOCAL = os.environ.get("LOCALAPPDATA", "")
APPDATA = os.environ.get("APPDATA", "")
WINDIR = os.environ.get("WINDIR", r"C:\Windows")
SYS_DRIVE = os.environ.get("SystemDrive", "C:")


def _exists(p: str) -> str | None:
    return p if p and os.path.exists(p) else None


@dataclass
class JunkCategory:
    id: str
    title: str
    desc: str
    paths: list[str] = field(default_factory=list)
    special: str = ""  # "recycle" для корзины
    size_bytes: int = 0
    files: int = 0
    enabled: bool = True
    needs_admin: bool = False


def get_categories() -> list[JunkCategory]:
    cats: list[JunkCategory] = []

    def existing(*paths: str) -> list[str]:
        return [p for p in paths if p and os.path.exists(p)]

    cats.append(JunkCategory(
        id="user_temp", title="Временные файлы пользователя",
        desc="Содержимое %TEMP% — безопасно, пересоздаётся автоматически.",
        paths=existing(TEMP, TMP) if TEMP != TMP else existing(TEMP),
    ))
    cats.append(JunkCategory(
        id="win_temp", title="Временные файлы Windows",
        desc=r"C:\Windows\Temp — нужны права администратора.",
        paths=existing(os.path.join(WINDIR, "Temp")),
        needs_admin=True,
    ))
    cats.append(JunkCategory(
        id="prefetch", title="Prefetch",
        desc="Устаревшие .pf-файлы предзагрузки. Чистить редко.",
        paths=existing(os.path.join(WINDIR, "Prefetch")),
        needs_admin=True, enabled=False,
    ))
    cats.append(JunkCategory(
        id="recycle", title="Корзина",
        desc="Очистить корзину на всех дисках.", special="recycle",
    ))
    cats.append(JunkCategory(
        id="delivery", title="Кэш обновлений Windows",
        desc=r"SoftwareDistribution\Download — скачанные обновления.",
        paths=existing(os.path.join(WINDIR, "SoftwareDistribution", "Download")),
        needs_admin=True,
    ))
    cats.append(JunkCategory(
        id="logs", title="Логи и отчёты об ошибках",
        desc="CBS-логи, WER-отчёты, минидампы.",
        paths=existing(
            os.path.join(WINDIR, "Logs", "CBS"),
            os.path.join(LOCAL, "Microsoft", "Windows", "WER", "ReportQueue") if LOCAL else "",
            os.path.join(LOCAL, "Microsoft", "Windows", "WER", "ReportArchive") if LOCAL else "",
            os.path.join(LOCAL, "CrashDumps") if LOCAL else "",
            os.path.join(WINDIR, "Minidump"),
        ),
        needs_admin=True, enabled=False,
    ))
    cats.append(JunkCategory(
        id="thumbs", title="Кэш эскизов проводника",
        desc="thumbcache_*.db — пересоздаётся, иконки обновятся.",
        paths=existing(os.path.join(LOCAL, r"Microsoft\Windows\Explorer")) if LOCAL else [],
    ))
    cats.append(JunkCategory(
        id="shader", title="Кэш шейдеров DirectX",
        desc="Пересоздаётся играми автоматически.",
        paths=existing(os.path.join(LOCAL, "D3DSCache") if LOCAL else "") ,
        enabled=False,
    ))
    # Браузеры
    bpaths = existing(
        os.path.join(LOCAL, r"Google\Chrome\User Data\Default\Cache") if LOCAL else "",
        os.path.join(LOCAL, r"Google\Chrome\User Data\Default\Code Cache") if LOCAL else "",
        os.path.join(LOCAL, r"Microsoft\Edge\User Data\Default\Cache") if LOCAL else "",
        os.path.join(LOCAL, r"Microsoft\Edge\User Data\Default\Code Cache") if LOCAL else "",
        os.path.join(LOCAL, r"Mozilla\Firefox\Profiles") if LOCAL else "",
    )
    cats.append(JunkCategory(
        id="browsers", title="Кэш браузеров",
        desc="Chrome / Edge / Firefox. Пароли и закладки не трогаем.",
        paths=bpaths, enabled=False,
    ))
    # Firefox: внутри Profiles лежат cache2 — развернём при сканировании
    return [c for c in cats if c.special or c.paths]


def _iter_firefox_cache(profiles_root: str) -> list[str]:
    out = []
    try:
        for prof in os.scandir(profiles_root):
            if prof.is_dir():
                for sub in ("cache2", "startupCache"):
                    p = os.path.join(prof.path, sub)
                    if os.path.exists(p):
                        out.append(p)
    except OSError:
        pass
    return out


def scan_category(cat: JunkCategory) -> JunkCategory:
    total, files = 0, 0
    if cat.special == "recycle":
        # размер корзины оценить сложно без COM — ставим 0, очистка всё равно работает
        cat.size_bytes, cat.files = 0, 0
        return cat
    paths = list(cat.paths)
    if cat.id == "browsers":
        extra = []
        for p in paths:
            if p.endswith("Profiles"):
                extra.extend(_iter_firefox_cache(p))
        paths = [p for p in paths if not p.endswith("Profiles")] + extra
    if cat.id == "thumbs":
        # считаем только thumbcache_*.db
        for d in paths:
            try:
                with os.scandir(d) as it:
                    for e in it:
                        if e.is_file() and e.name.lower().startswith("thumbcache_"):
                            try:
                                total += e.stat().st_size
                                files += 1
                            except OSError:
                                pass
            except OSError:
                pass
        cat.size_bytes, cat.files = total, files
        return cat
    for p in paths:
        s, c = get_dir_size(p)
        total += s
        files += c
    cat.size_bytes, cat.files = total, files
    return cat


def clean_category(cat: JunkCategory) -> tuple[int, list[str]]:
    """Возвращает (освобождено_байт, ошибки). Удаляет СОДЕРЖИМОЕ папок, не сами папки."""
    errors: list[str] = []
    freed = 0
    if cat.special == "recycle":
        before = 0
        ok = empty_recycle_bin()
        if not ok:
            errors.append("Не удалось очистить корзину (попробуйте от администратора).")
        return before, errors
    targets: list[str] = []
    if cat.id == "thumbs":
        for d in cat.paths:
            try:
                with os.scandir(d) as it:
                    for e in it:
                        if e.is_file() and e.name.lower().startswith("thumbcache_"):
                            targets.append(e.path)
            except OSError as ex:
                errors.append(f"{d}: {ex}")
    elif cat.id == "browsers":
        for p in cat.paths:
            if p.endswith("Profiles"):
                targets.extend(_iter_firefox_cache(p))
            else:
                targets.append(p)
    else:
        # содержимое каждой папки
        for d in cat.paths:
            try:
                with os.scandir(d) as it:
                    for e in it:
                        # не трогаем саму папку
                        targets.append(e.path)
            except OSError as ex:
                errors.append(f"{d}: {ex}")
    # считаем перед удалением (быстро, т.к. targets — верхний уровень)
    for t in targets:
        try:
            if os.path.isfile(t) or os.path.islink(t):
                try:
                    freed += os.path.getsize(t)
                except OSError:
                    pass
            elif os.path.isdir(t):
                s, _ = get_dir_size(t, max_files=30_000)
                freed += s
        except OSError:
            pass
    _, _, errs = safe_remove(targets)
    errors.extend(errs)
    return freed, errors
