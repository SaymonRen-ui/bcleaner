"""Работа с установленными программами: чтение реестра, удаление, поиск остатков."""
from __future__ import annotations

import csv
import io
import os
import re
import shlex
import subprocess
import time
import winreg
from dataclasses import dataclass, field
from pathlib import Path

from .utils import get_dir_size, normalize_name, safe_remove, tokenize

UNINSTALL_KEYS = [
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
]


@dataclass
class AppInfo:
    name: str
    key: str
    hive: str
    subkey: str
    version: str = ""
    publisher: str = ""
    install_date: str = ""
    size_bytes: int = 0
    install_location: str = ""
    uninstall_string: str = ""
    quiet_uninstall_string: str = ""
    display_icon: str = ""
    is_update: bool = False

    @property
    def display_size(self) -> str:
        from .utils import format_size
        return format_size(self.size_bytes) if self.size_bytes else "—"


def _read_value(key, name: str, default=""):
    try:
        v, _ = winreg.QueryValueEx(key, name)
        return v
    except OSError:
        return default


def get_installed_apps(include_system: bool = False) -> list[AppInfo]:
    apps: list[AppInfo] = []
    for hive, base in UNINSTALL_KEYS:
        try:
            root = winreg.OpenKey(hive, base)
        except OSError:
            continue
        hive_name = "HKLM" if hive == winreg.HKEY_LOCAL_MACHINE else "HKCU"
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
                i += 1
            except OSError:
                break
            try:
                with winreg.OpenKey(root, sub) as k:
                    name = str(_read_value(k, "DisplayName", "")).strip()
                    if not name:
                        continue
                    system_component = _read_value(k, "SystemComponent", 0)
                    parent = _read_value(k, "ParentKeyName", "")
                    release_type = str(_read_value(k, "ReleaseType", ""))
                    if not include_system and (
                        str(system_component) == "1" or parent or release_type in ("Security Update", "Update Rollup", "Hotfix")
                    ):
                        continue
                    version = str(_read_value(k, "DisplayVersion", ""))
                    publisher = str(_read_value(k, "Publisher", ""))
                    install_date = str(_read_value(k, "InstallDate", ""))
                    est_kb = _read_value(k, "EstimatedSize", 0)
                    try:
                        size_bytes = int(est_kb) * 1024
                    except (ValueError, TypeError):
                        size_bytes = 0
                    apps.append(
                        AppInfo(
                            name=name,
                            key=sub,
                            hive=hive_name,
                            subkey=f"{base}\\{sub}",
                            version=version,
                            publisher=publisher,
                            install_date=install_date,
                            size_bytes=size_bytes,
                            install_location=str(_read_value(k, "InstallLocation", "")),
                            uninstall_string=str(_read_value(k, "UninstallString", "")),
                            quiet_uninstall_string=str(_read_value(k, "QuietUninstallString", "")),
                            display_icon=str(_read_value(k, "DisplayIcon", "")),
                        )
                    )
            except OSError:
                continue
        try:
            winreg.CloseKey(root)
        except OSError:
            pass
    # дедуп по имени+hive+key
    seen = set()
    uniq: list[AppInfo] = []
    for a in sorted(apps, key=lambda x: x.name.lower()):
        k = (a.name.lower(), a.hive, a.key)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(a)
    return uniq


def build_uninstall_cmd(app: AppInfo, quiet: bool = True) -> list[str] | None:
    raw = (app.quiet_uninstall_string if quiet and app.quiet_uninstall_string else app.uninstall_string) or ""
    raw = raw.strip()
    if not raw:
        return None
    # MsiExec.exe /I{GUID} -> /X{GUID} /qn
    m = re.search(r"msiexec(?:\.exe)?\s+/[IX]\{?([0-9A-Fa-f\-]+)\}?", raw, re.IGNORECASE)
    if m:
        guid = m.group(0).split("{")[-1] if "{" in m.group(0) else m.group(1)
        # нормализуем guid
        guid_match = re.search(r"\{[0-9A-Fa-f\-]+\}", raw)
        guid_full = guid_match.group(0) if guid_match else "{" + m.group(1) + "}"
        cmd = ["MsiExec.exe", "/X" + guid_full]
        cmd += ["/qn", "/norestart"] if quiet else ["/passive"]
        return cmd
    # обычный exe: пробуем распарсить
    try:
        # Windows-кавычки: используем posix=False
        parts = shlex.split(raw, posix=False)
    except ValueError:
        parts = [raw]
    # убираем внешние кавычки
    parts = [p.strip('"') for p in parts]
    if quiet and not re.search(r"/(S|s|silent|quiet|qn|verysilent)", raw):
        # популярные тихие ключи пробуем только если exe похож на инсталлятор
        low = raw.lower()
        if any(t in low for t in ("unins", "uninstall", "setup", "uninst")):
            parts = parts + ["/S"]
    return parts


def run_uninstall(app: AppInfo, quiet: bool = False) -> tuple[bool, str]:
    cmd = build_uninstall_cmd(app, quiet=quiet)
    if not cmd:
        return False, "Не найдена строка удаления (UninstallString пустая)."
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, shell=False)
        ok = proc.returncode == 0
        msg = f"Код выхода: {proc.returncode}"
        if proc.stderr:
            msg += f"\n{proc.stderr[:1000]}"
        return ok, msg
    except subprocess.TimeoutExpired:
        return False, "Превышено время ожидания (10 мин)."
    except FileNotFoundError:
        return False, f"Не найден файл: {cmd[0]}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


# ---------- Поиск остатков ----------

SEARCH_ROOTS = [
    os.environ.get("ProgramFiles", r"C:\Program Files"),
    os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    os.environ.get("ProgramData", r"C:\ProgramData"),
    os.path.join(os.environ.get("APPDATA", ""), "") or "",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "") or "",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Low") if os.environ.get("LOCALAPPDATA") else "",
]

SOFTWARE_KEYS = [
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node"),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE"),
]


@dataclass
class Leftover:
    kind: str  # "file" | "registry" | "service" | "task"
    path: str
    size_bytes: int = 0
    detail: str = ""
    value_name: str = ""  # для kind="registry": отдельное значение (Run и т.п.)
    risky: bool = False  # спорное (сейвы) — в UI по умолчанию не выбрано


# Слова, которые ничего не значат для matching (версии, разрядность, generic).
# Их нельзя использовать как токены: иначе "media" тянет Windows Media/MediaTek,
# а "player" — все папки Player на диске.
_GENERIC_TOKENS = {
    "free", "pro", "plus", "soft", "labs", "inc", "app", "apps", "client",
    "desktop", "player", "browser", "setup", "installer", "launcher",
    "program", "software", "suite", "tool", "tools", "utility", "wizard",
    "microsoft", "windows", "google", "the", "media", "video", "audio",
    "music", "photo", "photos", "image", "images", "center", "centre",
    "manager", "editor", "viewer", "reader", "remote", "cloud", "drive",
    "office", "system", "service", "services", "framework", "runtime",
    "platform", "library", "libraries", "network", "internet", "security",
    "docs", "studio", "ultimate", "professional", "enterprise", "community",
    "trial", "portable", "version",
}
_GENERIC_SUFFIX = ("desktop", "client", "app", "player", "browser", "launcher", "setup")

# Имена exe, которые НЕ являются признаком приложения (деинсталляторы и co)
_JUNK_EXE_SUBSTR = (
    "unins", "setup", "update", "install", "helper", "maintenancetool",
    "crash", "report", "elevate", "bootstrapper",
)


def _is_junk_exe(basename_lower: str) -> bool:
    return any(k in basename_lower for k in _JUNK_EXE_SUBSTR)


# Папки, которые нельзя предлагать никогда
_PROTECTED_DIRS = {
    "windows", "microsoft", "commonfiles", "packagecache",
    "installshieldinstallationinformation", "systemvolumeinformation",
    "$recyclebin", "configmsi", "programfiles", "programfilesx86",
    "programdata", "appdata", "locallow", "perflogs", "recovery",
    "system32", "syswow64", "drivers", "fonts", "tasks",
}

_VERSION_RE = re.compile(r"v?\d+(\.\d+)+|\bx0?64\b|\bx86\b|64bit|32bit|\(\d+\)")


@dataclass
class _MatchCtx:
    strong: set[str]      # сильные сигналы: полное имя, имя без суффикса, exe-имена
    tokens: list[str]     # значимые токены (len>=5)
    exe_names: set[str]   # имена exe-файлов lower (telegram.exe)
    publisher_norm: str
    install_dir: str      # normcase InstallLocation


def _strip_version(name: str) -> str:
    s = _VERSION_RE.sub(" ", name or "")
    return re.sub(r"\s+", " ", s).strip()


def _exe_from_string(raw: str) -> str:
    """Вытаскивает basename exe из строки типа '"C:\\a\\b.exe" --service'."""
    s = (raw or "").strip()
    m = re.match(r'^"([^"]+\.[Ee][Xx][Ee])"', s)
    if m:
        return os.path.basename(m.group(1))
    m = re.match(r"^([A-Za-z]:\\[^\"*?<>|]*?\.[Ee][Xx][Ee])", s)
    if m:
        return os.path.basename(m.group(1))
    m = re.search(r"([^\\/:;\"']+\.[Ee][Xx][Ee])", s)
    if m:
        return m.group(1)
    return ""


def _build_ctx(app: AppInfo) -> _MatchCtx:
    name = _strip_version(app.name)
    norm_full = normalize_name(name)
    strong: set[str] = set()
    if len(norm_full) >= 3:
        strong.add(norm_full)
    # имя без generic-хвоста: "Telegram Desktop" -> "telegram"
    words = re.split(r"[^A-Za-zА-Яа-я0-9]+", name)
    words = [w for w in words if w]
    while len(words) > 1 and words[-1].lower() in _GENERIC_SUFFIX:
        words.pop()
    short = normalize_name("".join(words))
    if len(short) >= 3 and short != norm_full:
        strong.add(short)

    exe_names: set[str] = set()
    for raw in (app.display_icon, app.uninstall_string, app.quiet_uninstall_string):
        if raw and "msiexec" not in raw.lower():
            b = _exe_from_string(raw)
            if b and not _is_junk_exe(b.lower()):
                exe_names.add(b.lower())
    # exe из папки установки (верхний уровень, без деинсталляторов)
    loc = (app.install_location or "").strip().strip('"')
    if loc and os.path.isdir(loc):
        try:
            for f in os.listdir(loc):
                if f.lower().endswith(".exe") and not _is_junk_exe(f.lower()):
                    exe_names.add(f.lower())
        except OSError:
            pass
    for exe in list(exe_names):
        stem = normalize_name(os.path.splitext(exe)[0])
        if len(stem) >= 3:
            strong.add(stem)

    # издатель — тоже сигнал (VideoLAN -> videolan), кроме мега-вендоров:
    # их папки/ключи есть у всех, совпадение по ним — почти всегда ложное
    pub = normalize_name(app.publisher or "")
    pub = re.sub(r"(corporation|incorporated|inc|llc|ltd|gmbh|co|limited)$", "", pub)
    if len(pub) >= 6 and not pub.startswith(("microsoft", "google", "apple")):
        if pub not in _GENERIC_TOKENS:
            strong.add(pub)

    tokens: list[str] = []
    for t in tokenize(name):
        tl = t.lower()
        if len(t) >= 5 and tl not in _GENERIC_TOKENS and tl not in tokens:
            tokens.append(tl)

    return _MatchCtx(
        strong=strong,
        tokens=tokens,
        exe_names=exe_names,
        publisher_norm=normalize_name(app.publisher or ""),
        install_dir=os.path.normcase(loc) if loc else "",
    )


def _is_generic_name(en_norm: str, raw_lower: str) -> bool:
    """Имя состоит только из generic-слов (player, desktop, media player...)."""
    if en_norm in _GENERIC_TOKENS:
        return True
    parts = [p for p in re.split(r"[^a-zа-я0-9]+", raw_lower) if p]
    return bool(parts) and all(p in _GENERIC_TOKENS for p in parts)


def _match_name(en_norm: str, raw_lower: str, ctx: _MatchCtx, *, files: bool = False) -> str:
    """Возвращает причину совпадения или ''.

    Правила против ложных срабатываний:
    - короче 3 — никогда; generic-имена ("player", "desktop", "media player")
      совпадают только точно (а точно они в сигналах почти не бывают);
    - точное равенство (len>=3) — всегда strong ("vlc"=="vlc");
    - вхождение длинных (>=6 с обеих сторон): s in en — да;
      en in s — только если в имени есть отличительный токен приложения
      ("telegram" в "telegramdesktop" — да; "mediaplayer" в "vlcmediaplayer" — нет);
    - короткие сильные (3-5, напр. "vlc", "obs") — только по границе слова
      ("vlc-portable" — да; "github" для "git" — нет).
    """
    if not en_norm or len(en_norm) < 3 or en_norm in _PROTECTED_DIRS:
        return ""
    for s in ctx.strong:
        if len(s) >= 3 and s == en_norm:
            return "strong"
    if _is_generic_name(en_norm, raw_lower):
        return ""
    words = [p for p in re.split(r"[^a-zа-я0-9]+", raw_lower) if p]
    if len(en_norm) >= 6:
        for s in ctx.strong:
            if len(s) < 6:
                continue
            if s in en_norm:
                return "strong"
            if en_norm in s and any(t in en_norm for t in ctx.tokens):
                return "strong"
    for s in ctx.strong:
        if 3 <= len(s) <= 5 and s in words:
            return "strong"
    if not files and len(en_norm) >= 6:
        for t in ctx.tokens:
            if t in en_norm:
                return "token"
    return ""


def find_leftovers(app: AppInfo, deep_registry: bool = False, on_progress=None,
                   cancel_flag=None, known_dirs: set[str] | None = None) -> list[Leftover]:
    """Глубокий поиск остатков: папки (включая вложенные), файлы, ярлыки,
    драйверы, реестр SOFTWARE (с уровнем издателя), автозагрузка, службы,
    App Paths, задачи планировщика.

    on_progress(done, total, label), cancel_flag() -> bool.
    known_dirs — normcase-пути установок ДРУГИХ программ (не предлагать их).
    """
    from .utils import get_dir_size

    results: list[Leftover] = []
    ctx = _build_ctx(app)
    t_start = time.monotonic()
    deadline = t_start + 120  # общий бюджет, чтобы не висеть вечно

    def cancelled() -> bool:
        if cancel_flag and cancel_flag():
            return True
        return time.monotonic() > deadline

    known = {os.path.normcase(d) for d in (known_dirs or set()) if d}
    own = ctx.install_dir

    def is_other(p: str) -> bool:
        np_ = os.path.normcase(p)
        if own and (np_ == own or np_.startswith(own + os.sep)):
            return False
        for k in known:
            if not k:
                continue
            if np_ == k or np_.startswith(k + os.sep) or k.startswith(np_ + os.sep):
                return True
        return False

    PF = os.environ.get("ProgramFiles", r"C:\Program Files")
    PF86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    CPF = os.environ.get("CommonProgramFiles", "")
    CPF86 = os.environ.get("CommonProgramFiles(x86)", "")
    PD = os.environ.get("ProgramData", r"C:\ProgramData")
    ROAM = os.environ.get("APPDATA", "")
    LOC = os.environ.get("LOCALAPPDATA", "")
    DOCS = os.path.join(os.path.expanduser("~"), "Documents")
    SAVED = os.path.join(os.path.expanduser("~"), "Saved Games")
    SM_COMMON = os.path.join(PD, r"Microsoft\Windows\Start Menu\Programs")
    SM_USER = os.path.join(ROAM, r"Microsoft\Windows\Start Menu\Programs") if ROAM else ""
    DT_COMMON = os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "Desktop")
    DT_USER = os.path.join(os.path.expanduser("~"), "Desktop")

    def existing(*paths: str) -> list[str]:
        return [p for p in paths if p and os.path.isdir(p)]

    # ---------- шаг 1: папка установки ----------
    def step_install():
        if app.install_location and os.path.isdir(app.install_location):
            s, _ = get_dir_size(app.install_location, max_files=20_000)
            results.append(Leftover("file", app.install_location, s, "Папка установки из реестра"))

    # ---------- шаги 2-4: глубокий обход ----------
    def walk_step(roots: list[str], label: str, risky_roots: tuple = ()):
        for root in roots:
            if cancelled():
                return
            tick_n = [0]

            def on_tick(n, _lab=label):
                if on_progress:
                    on_progress(cur_step[0], total_steps, f"{_lab} · проверено {n}")

            hits, _ = _walk_match(
                root, ctx, match_files=True, lnk_tokens=True,
                max_dirs=30_000, deadline=deadline,
                cancel_flag=cancel_flag, on_tick=on_tick,
            )
            for path, is_dir, reason in hits:
                if cancelled():
                    return
                if is_other(path):
                    continue
                risky = path.lower().startswith(tuple(r.lower() for r in risky_roots)) if risky_roots else False
                try:
                    if is_dir:
                        s, _ = get_dir_size(path, max_files=20_000)
                        results.append(Leftover(
                            "file", path, s,
                            f"Папка: {os.path.basename(path.rstrip(chr(92) + '/'))}" + (" (сохранения?)" if risky else ""),
                            risky=risky))
                    else:
                        try:
                            sz = os.path.getsize(path)
                        except OSError:
                            sz = 0
                        results.append(Leftover("file", path, sz, f"Файл: {os.path.basename(path)}"))
                except OSError:
                    continue

    # ---------- шаг 5: ярлыки ----------
    def step_shortcuts():
        roots = existing(SM_COMMON, SM_USER, DT_COMMON, DT_USER)
        for root in roots:
            if cancelled():
                return
            hits, _ = _walk_match(root, ctx, match_files=True, lnk_tokens=True,
                                  max_dirs=5_000, deadline=deadline, cancel_flag=cancel_flag)
            for path, is_dir, reason in hits:
                if is_dir or is_other(path):
                    continue
                try:
                    sz = os.path.getsize(path)
                except OSError:
                    sz = 0
                kind_detail = "Ярлык" if path.lower().endswith(".lnk") else "Файл"
                results.append(Leftover("file", path, sz, f"{kind_detail}: {os.path.basename(path)}"))

    # ---------- шаг 6: драйверы ----------
    def step_drivers():
        drv = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "drivers")
        if not os.path.isdir(drv):
            return
        try:
            files = os.listdir(drv)
        except OSError:
            return
        for f in files:
            if cancelled():
                return
            if not f.lower().endswith(".sys"):
                continue
            if _match_name(normalize_name(os.path.splitext(f)[0]), f.lower(), ctx, files=True):
                p = os.path.join(drv, f)
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    sz = 0
                results.append(Leftover("file", p, sz, f"Драйвер: {f}"))

    # ---------- шаг 7: реестр SOFTWARE ----------
    def step_software():
        for hive, base in SOFTWARE_KEYS:
            if cancelled():
                return
            try:
                bk = winreg.OpenKey(hive, base)
            except OSError:
                continue
            tag = "HKLM" if hive == winreg.HKEY_LOCAL_MACHINE else "HKCU"
            try:
                idx = 0
                while True:
                    if cancelled() or len(results) > 120:
                        break
                    try:
                        sub = winreg.EnumKey(bk, idx)
                        idx += 1
                    except OSError:
                        break
                    sn = normalize_name(sub)
                    if not sn or sub.lower() in ("microsoft", "windows", "classes", "policies"):
                        continue
                    m = _match_name(sn, sub.lower(), ctx)
                    if m:
                        results.append(Leftover(
                            "registry", f"{tag}\\{base}\\{sub}", 0, "Ключ реестра приложения"))
                        continue
                    # совпало только с издателем — смотрим детей (уровень приложения)
                    pub = ctx.publisher_norm
                    if len(pub) >= 4 and (pub in sn or sn in pub):
                        try:
                            with winreg.OpenKey(bk, sub) as pk:
                                ci = 0
                                while ci < 300:
                                    try:
                                        child = winreg.EnumKey(pk, ci)
                                        ci += 1
                                    except OSError:
                                        break
                                    if _match_name(normalize_name(child), child.lower(), ctx):
                                        results.append(Leftover(
                                            "registry", f"{tag}\\{base}\\{sub}\\{child}", 0,
                                            "Ключ реестра приложения"))
                        except OSError:
                            pass
            finally:
                try:
                    winreg.CloseKey(bk)
                except OSError:
                    pass

    # ---------- шаг 8: автозагрузка ----------
    def step_run():
        run_keys = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce"),
        ]
        for hive, key in run_keys:
            if cancelled():
                return
            tag = "HKLM" if hive == winreg.HKEY_LOCAL_MACHINE else "HKCU"
            try:
                with winreg.OpenKey(hive, key) as k:
                    i = 0
                    while True:
                        try:
                            vname, vdata, _ = winreg.EnumValue(k, i)
                            i += 1
                        except OSError:
                            break
                        data_s = os.path.normcase(str(vdata or ""))
                        hit = bool(own) and own in data_s
                        if not hit:
                            b = _exe_from_string(str(vdata or "")).lower()
                            hit = bool(b) and b in ctx.exe_names
                        if hit:
                            results.append(Leftover(
                                "registry", f"{tag}\\{key}", 0,
                                f"Автозагрузка: {vname}", value_name=vname))
            except OSError:
                continue

    # ---------- шаг 9: службы ----------
    def step_services():
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services")
        except OSError:
            return
        try:
            idx = 0
            while idx < 3000:
                if cancelled():
                    break
                try:
                    name = winreg.EnumKey(root, idx)
                    idx += 1
                except OSError:
                    break
                try:
                    with winreg.OpenKey(root, name) as sk:
                        try:
                            img, _ = winreg.QueryValueEx(sk, "ImagePath")
                        except OSError:
                            continue
                except OSError:
                    continue
                img_s = str(img or "")
                hit = bool(own) and own in os.path.normcase(os.path.expandvars(img_s))
                if not hit:
                    b = _exe_from_string(img_s).lower()
                    hit = bool(b) and b in ctx.exe_names
                if hit:
                    results.append(Leftover(
                        "service", f"HKLM\\SYSTEM\\CurrentControlSet\\Services\\{name}", 0,
                        f"Служба Windows: {name}"))
        finally:
            try:
                winreg.CloseKey(root)
            except OSError:
                pass

    # ---------- шаг 10: App Paths ----------
    def step_apppaths():
        for hive, base in [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
        ]:
            if cancelled():
                return
            tag = "HKLM" if hive == winreg.HKEY_LOCAL_MACHINE else "HKCU"
            try:
                bk = winreg.OpenKey(hive, base)
            except OSError:
                continue
            try:
                idx = 0
                while True:
                    try:
                        sub = winreg.EnumKey(bk, idx)
                        idx += 1
                    except OSError:
                        break
                    hit = sub.lower() in ctx.exe_names
                    if not hit and own:
                        try:
                            with winreg.OpenKey(bk, sub) as sk:
                                try:
                                    dflt, _ = winreg.QueryValueEx(sk, "")
                                except OSError:
                                    dflt = ""
                            hit = own in os.path.normcase(str(dflt or ""))
                        except OSError:
                            pass
                    if hit:
                        results.append(Leftover(
                            "registry", f"{tag}\\{base}\\{sub}", 0, f"App Path: {sub}"))
            finally:
                try:
                    winreg.CloseKey(bk)
                except OSError:
                    pass

    # ---------- шаг 11: задачи планировщика ----------
    def step_tasks():
        try:
            p = subprocess.run(["schtasks", "/query", "/fo", "csv", "/nh"],
                               capture_output=True, timeout=25)
        except (OSError, subprocess.SubprocessError):
            return
        out = ""
        for enc in ("utf-8-sig", "cp866", "cp1251"):
            try:
                out = p.stdout.decode(enc)
                break
            except (UnicodeDecodeError, ValueError):
                continue
        if not out:
            return
        try:
            rows = list(csv.reader(io.StringIO(out)))
        except Exception:
            return
        for row in rows:
            if cancelled():
                return
            if not row:
                continue
            tname = row[0].strip()
            if not tname or tname.lower().startswith("\\microsoft\\windows"):
                continue
            tnorm = normalize_name(tname)
            tl = tname.lower().replace("\\", " ").replace("_", " ").replace("-", " ")
            hit = any(len(s) >= 5 and (s in tnorm or tnorm in s) for s in ctx.strong)
            if not hit:
                hit = any(t in tl for t in ctx.tokens)
            # защита: пропускаем системные ветки Microsoft (проверено выше)
            if hit:
                results.append(Leftover("task", tname, 0, "Задача планировщика"))

    prog_roots = existing(PF, PF86, CPF, CPF86)
    data_roots = existing(PD)
    user_roots = existing(ROAM, LOC,
                          os.path.join(LOC, "Low") if LOC else "",
                          DOCS, SAVED)
    risky_roots = tuple(r for r in (DOCS, SAVED) if r and os.path.isdir(r))

    steps: list[tuple[str, object]] = [
        ("Папка установки", step_install),
        ("Program Files", lambda: walk_step(prog_roots, "Program Files")),
        ("ProgramData", lambda: walk_step(data_roots, "ProgramData")),
        ("Данные пользователя", lambda: walk_step(user_roots, "Данные пользователя", risky_roots)),
        ("Ярлыки", step_shortcuts),
        ("Драйверы", step_drivers),
        ("Реестр", step_software),
        ("Автозагрузка", step_run),
        ("Службы", step_services),
        ("App Paths", step_apppaths),
        ("Планировщик", step_tasks),
    ]
    total_steps = len(steps) + 1
    cur_step = [0]
    for i, (label, fn) in enumerate(steps, 1):
        if cancelled():
            break
        cur_step[0] = i
        if on_progress:
            on_progress(i, total_steps, label)
        try:
            fn()
        except Exception:
            continue
    # сама uninstall-запись (если программа уже удалена штатно, но ключ остался).
    # Для ручного поиска по имени (пустой subkey) — не добавляем, там нечего искать.
    if app.subkey:
        results.append(
            Leftover("registry", f"{app.hive}\\{app.subkey}", 0, "Запись Uninstall (остаток)")
        )
    if on_progress:
        on_progress(total_steps, total_steps, "Готово")

    # дедуп
    seen = set()
    uniq: list[Leftover] = []
    for r in results:
        k = (r.kind, r.path.lower(), (r.value_name or "").lower())
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    return uniq[:120]


def _is_reparse(entry) -> bool:
    """True для symlink/junction/mount (туда не спускаемся — защита от циклов)."""
    try:
        return bool(entry.stat(follow_symlinks=False).st_file_attributes & 0x400)
    except (OSError, AttributeError):
        return False


_FILE_EXTS = (".lnk", ".exe", ".sys", ".dll", ".msi", ".log", ".ini", ".cfg", ".dat", ".url")


def _walk_match(root: str, ctx: _MatchCtx, *, match_files: bool = False,
                lnk_tokens: bool = False, max_dirs: int = 30_000,
                deadline: float = 0, cancel_flag=None, on_tick=None) -> tuple[list, int]:
    """BFS-обход дерева, совпадения по именам папок/файлов.

    Возвращает (hits, visited), hit = (path, is_dir, reason).
    В reparse-папки спускаемся? Нет — только сверяем их имя (циклы исключены).
    """
    from collections import deque
    hits: list[tuple[str, bool, str]] = []
    q: deque[str] = deque([root])
    visited = 0
    while q:
        if cancel_flag and cancel_flag():
            break
        if deadline and time.monotonic() > deadline:
            break
        if visited >= max_dirs:
            break
        cur = q.popleft()
        visited += 1
        if on_tick and visited % 500 == 0:
            try:
                on_tick(visited)
            except Exception:
                pass
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            try:
                name = entry.name
                en = normalize_name(name)
                if entry.is_dir(follow_symlinks=False):
                    r = _match_name(en, entry.name.lower(), ctx)
                    if r:
                        hits.append((entry.path, True, r))
                    if not _is_reparse(entry):
                        q.append(entry.path)
                elif match_files and entry.is_file(follow_symlinks=False):
                    low = name.lower()
                    if low.endswith(_FILE_EXTS):
                        r = _match_name(en, name.lower(), ctx, files=True)
                        if not r and lnk_tokens and low.endswith((".lnk", ".url")):
                            if len(en) >= 5 and any(t in en for t in ctx.tokens):
                                r = "token"
                        if r:
                            hits.append((entry.path, False, r))
            except OSError:
                continue
    return hits, visited


def delete_leftover(item: Leftover) -> tuple[bool, str]:
    if item.kind == "file":
        _, _, errors = safe_remove([item.path])
        if errors:
            return False, "; ".join(errors)[:500]
        return True, "Удалено"
    if item.kind == "service":
        # удаление службы Windows: сначала стоп, потом delete
        name = item.path.rsplit("\\", 1)[-1]
        try:
            subprocess.run(["sc", "stop", name], capture_output=True, timeout=30)
            p = subprocess.run(["sc", "delete", name], capture_output=True,
                               text=True, timeout=30)
            if p.returncode == 0:
                return True, "Служба удалена"
            out = (p.stdout or "") + (p.stderr or "")
            if "1060" in out or "service does not exist" in out.lower():
                return True, "Службы уже нет"
            return False, out.strip()[:300] or f"sc вернул код {p.returncode}"
        except FileNotFoundError:
            return False, "Нет утилиты sc.exe"
        except Exception as e:  # noqa: BLE001
            return False, str(e)[:300]
    if item.kind == "task":
        try:
            p = subprocess.run(["schtasks", "/delete", "/tn", item.path, "/f"],
                               capture_output=True, text=True, timeout=30)
            if p.returncode == 0:
                return True, "Задача удалена"
            return False, ((p.stdout or "") + (p.stderr or "")).strip()[:300]
        except Exception as e:  # noqa: BLE001
            return False, str(e)[:300]
    # registry (ключ целиком или отдельное значение)
    try:
        m = re.match(r"(HKLM|HKCU)\\(.+)\\([^\\]+)$", item.path)
        if not m:
            return False, "Не смог разобрать путь реестра"
        hive_s, base, sub = m.groups()
        hive = winreg.HKEY_LOCAL_MACHINE if hive_s == "HKLM" else winreg.HKEY_CURRENT_USER
        if item.value_name:
            # отдельное значение (автозагрузка)
            try:
                with winreg.OpenKey(hive, base + "\\" + sub, 0, winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, item.value_name)
                return True, "Значение удалено"
            except FileNotFoundError:
                return True, "Значения уже нет"
            except PermissionError:
                return False, "Нет прав (запустите от администратора)"
        # рекурсивное удаление ключа
        def _del_rec(h, path: str):
            try:
                with winreg.OpenKey(h, path) as k:
                    subs = []
                    i = 0
                    while True:
                        try:
                            subs.append(winreg.EnumKey(k, i))
                            i += 1
                        except OSError:
                            break
                for s in subs:
                    _del_rec(h, path + "\\" + s)
                winreg.DeleteKey(h, path)
            except FileNotFoundError:
                pass
        _del_rec(hive, base + "\\" + sub)
        return True, "Ключ реестра удалён"
    except PermissionError:
        return False, "Нет прав (запустите от администратора)"
    except OSError as e:
        return False, f"Реестр: {e}"
