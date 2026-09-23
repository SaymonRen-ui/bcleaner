"""Извлечение иконок установленных программ (exe/ico) + плейсхолдеры."""
from __future__ import annotations

import ctypes
import hashlib
import os
import re
import tempfile
from ctypes import wintypes
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_CACHE_DIR = Path(tempfile.gettempdir()) / "BCleaner_icons"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_PIL_CACHE: dict[str, Image.Image] = {}

# ---------- WinAPI ----------
_SHGFI_ICON = 0x100
_SHGFI_LARGEICON = 0x0
_SHGFI_SMALLICON = 0x1
_DIB_RGB_COLORS = 0
_BI_RGB = 0


class _SHFILEINFOW(ctypes.Structure):
    _fields_ = [
        ("hIcon", wintypes.HICON),
        ("iIcon", ctypes.c_int),
        ("dwAttributes", ctypes.c_uint32),
        ("szDisplayName", ctypes.c_wchar * 260),
        ("szTypeName", ctypes.c_wchar * 80),
    ]


class _ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", ctypes.c_uint32),
        ("yHotspot", ctypes.c_uint32),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


class _BITMAP(ctypes.Structure):
    _fields_ = [
        ("bmType", ctypes.c_long),
        ("bmWidth", ctypes.c_long),
        ("bmHeight", ctypes.c_long),
        ("bmWidthBytes", ctypes.c_long),
        ("bmPlanes", ctypes.c_ushort),
        ("bmBitsPixel", ctypes.c_ushort),
        ("bmBits", ctypes.c_void_p),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.c_ushort),
        ("biBitCount", ctypes.c_ushort),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


def _api():
    try:
        shell32 = ctypes.windll.shell32
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
    except Exception:
        return None
    return shell32, user32, gdi32


def resolve_icon_source(app) -> str | None:
    """Находит файл иконки: DisplayIcon -> InstallLocation/*.exe -> UninstallString exe."""
    # 1) DisplayIcon
    raw = (getattr(app, "display_icon", "") or "").strip()
    if raw:
        raw = os.path.expandvars(os.path.expanduser(raw))
        cand = _extract_path(raw)
        if cand:
            return cand
    # 2) InstallLocation
    loc = (getattr(app, "install_location", "") or "").strip().strip('"')
    if loc and os.path.isdir(loc):
        best = _find_exe_in_dir(loc, getattr(app, "name", ""))
        if best:
            return best
    # 3) UninstallString exe (не msiexec)
    raw_u = (getattr(app, "uninstall_string", "") or "").strip()
    if raw_u and "msiexec" not in raw_u.lower():
        cand = _extract_path(os.path.expandvars(raw_u))
        if cand and cand.lower().endswith((".exe", ".ico")):
            return cand
    return None


def _extract_path(raw: str) -> str | None:
    s = raw.strip().strip('"').strip("'")
    # "C:\path\app.exe",0  или  C:\path\app.exe,0
    m = re.match(r'^"([^"]+)"', raw.strip())
    if m and os.path.exists(m.group(1)):
        return m.group(1)
    # убираем ,index в конце
    m2 = re.match(r"^(.*?)(,\-?\d+)?$", s)
    if m2:
        p = m2.group(1).strip().strip('"')
        if p and os.path.exists(p):
            return p
    if os.path.exists(s):
        return s
    return None


def _find_exe_in_dir(folder: str, app_name: str = "") -> str | None:
    try:
        files = [os.path.join(folder, f) for f in os.listdir(folder)
                 if f.lower().endswith(".exe")]
    except OSError:
        return None
    if not files:
        return None
    # отсеиваем деинсталляторы на второй план
    def score(p: str) -> tuple:
        n = os.path.basename(p).lower()
        is_un = any(k in n for k in ("unins", "uninstall", "setup", "update", "helper", "crash"))
        tokens = [t.lower() for t in re.split(r"[^a-z0-9]+", app_name or "") if len(t) >= 4]
        hit = any(t in n for t in tokens)
        try:
            sz = os.path.getsize(p)
        except OSError:
            sz = 0
        return (hit, not is_un, sz)
    files.sort(key=score, reverse=True)
    # если лучший — деинсталлятор и есть другие, берём первый не-деинсталлятор
    return files[0]


def _cache_key(path: str) -> str:
    try:
        st = os.stat(path)
        sig = f"{path}|{st.st_size}|{st.st_mtime}"
    except OSError:
        sig = path
    return hashlib.sha1(sig.encode("utf-8", "ignore")).hexdigest() + ".png"


def get_file_pil_icon(path: str, size: int = 36) -> Image.Image | None:
    """Возвращает PIL-иконку для exe/ico/dll. Кэширует на диск + в память."""
    if not path or not os.path.exists(path):
        return None
    mem_key = f"{path}:{size}"
    if mem_key in _PIL_CACHE:
        return _PIL_CACHE[mem_key]
    ck = _CACHE_DIR / _cache_key(path)
    if ck.exists():
        try:
            img = Image.open(ck).convert("RGBA").resize((size, size), Image.LANCZOS)
            _PIL_CACHE[mem_key] = img
            return img
        except Exception:
            pass
    img: Image.Image | None = None
    if path.lower().endswith(".ico"):
        try:
            with Image.open(path) as im:
                img = im.convert("RGBA").resize((size, size), Image.LANCZOS)
        except Exception:
            img = None
    if img is None:
        img = _extract_via_shell(path, size)
    if img is not None:
        try:
            img.convert("RGBA").resize((size, size), Image.LANCZOS).save(ck)
        except Exception:
            pass
        _PIL_CACHE[mem_key] = img
    return img


def _extract_via_shell(path: str, size: int) -> Image.Image | None:
    apis = _api()
    if apis is None:
        return None
    shell32, user32, gdi32 = apis
    try:
        sfi = _SHFILEINFOW()
        flags = _SHGFI_ICON | _SHGFI_LARGEICON
        if not shell32.SHGetFileInfoW(path, 0, ctypes.byref(sfi), ctypes.sizeof(sfi), flags):
            return None
        hicon = sfi.hIcon
        if not hicon:
            return None
        try:
            return _hicon_to_pil(user32, gdi32, hicon, size)
        finally:
            try:
                user32.DestroyIcon(hicon)
            except Exception:
                pass
    except Exception:
        return None


def _hicon_to_pil(user32, gdi32, hicon, size: int) -> Image.Image | None:
    ii = _ICONINFO()
    if not user32.GetIconInfo(hicon, ctypes.byref(ii)):
        return None
    try:
        if not ii.hbmColor:
            return None
        bmp = _BITMAP()
        if not gdi32.GetObjectW(ii.hbmColor, ctypes.sizeof(bmp), ctypes.byref(bmp)):
            return None
        w, h = int(bmp.bmWidth), int(bmp.bmHeight)
        if w <= 0 or h <= 0 or w > 256 or h > 256:
            return None
        # top-down DIB, 32 bit
        hdr = _BITMAPINFOHEADER()
        hdr.biSize = ctypes.sizeof(hdr)
        hdr.biWidth = w
        hdr.biHeight = -h
        hdr.biPlanes = 1
        hdr.biBitCount = 32
        hdr.biCompression = _BI_RGB
        buf = (ctypes.c_ubyte * (w * h * 4))()
        hdc = user32.GetDC(None)
        try:
            got = gdi32.GetDIBits(hdc, ii.hbmColor, 0, h, buf, ctypes.byref(hdr), _DIB_RGB_COLORS)
        finally:
            try:
                user32.ReleaseDC(None, hdc)
            except Exception:
                pass
        if not got:
            return None
        img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", w * 4, 1).copy()
        # старые иконки без альфы: alpha везде 0 -> делаем непрозрачными
        try:
            alpha = img.getchannel("A")
            if alpha.getextrema() == (0, 0):
                img.putalpha(255)
        except Exception:
            pass
        if (w, h) != (size, size):
            img = img.resize((size, size), Image.LANCZOS)
        return img
    finally:
        for hbm in (ii.hbmColor, ii.hbmMask):
            if hbm:
                try:
                    gdi32.DeleteObject(hbm)
                except Exception:
                    pass


# ---------- плейсхолдер ----------
_AVATAR_COLORS = [
    (59, 130, 246), (34, 197, 94), (245, 158, 11), (239, 68, 68),
    (139, 92, 246), (6, 182, 212), (236, 72, 153), (132, 204, 22),
    (249, 115, 22), (20, 184, 166), (99, 102, 241),
]


def placeholder(name: str, size: int = 36) -> Image.Image:
    letter = (name or "?").strip()[:1].upper() or "?"
    h = int(hashlib.md5((name or "?").lower().encode("utf-8")).hexdigest()[:8], 16)
    bg = _AVATAR_COLORS[h % len(_AVATAR_COLORS)]
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=size // 4, fill=bg + (255,))
    try:
        font = ImageFont.truetype("segoeui.ttf", size // 2)
    except Exception:
        try:
            font = ImageFont.truetype("arial.ttf", size // 2)
        except Exception:
            font = ImageFont.load_default()
    try:
        bbox = d.textbbox((0, 0), letter, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        d.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]),
               letter, font=font, fill=(255, 255, 255, 255))
    except Exception:
        d.text((size // 3, size // 4), letter, fill=(255, 255, 255, 255))
    return img


def get_app_pil_icon(app, size: int = 36) -> Image.Image:
    src = resolve_icon_source(app)
    if src:
        img = get_file_pil_icon(src, size)
        if img is not None:
            return img
    key = f"__ph__:{getattr(app, 'name', '?')}:{size}"
    if key in _PIL_CACHE:
        return _PIL_CACHE[key]
    img = placeholder(getattr(app, "name", "?"), size)
    _PIL_CACHE[key] = img
    return img
