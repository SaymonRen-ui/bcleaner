"""Главное окно BCleaner на CustomTkinter."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

try:
    import customtkinter as ctk
    from tkinter import filedialog, messagebox
except ImportError:
    raise SystemExit("Установите зависимости: pip install -r requirements.txt")

# --- Патч против шторма Configure при ресайзе/максимайзе ---
# CTkScrollbar._draw() в конце вызывает update_idletasks(), который рекурсивно
# сливает ВСЮ очередь Configure -> каскад перерисовок (замер: 0.46с из 0.9с
# кадра максимайза). Глушим вложенный флеш только внутри _draw скроллбара:
# отрисовка всё равно произойдёт на следующем проходе главного цикла.
_sb_draw_nesting = 0
try:
    from customtkinter.windows.widgets.ctk_scrollbar import CTkScrollbar as _CTkSB
    import tkinter as _tk
    _orig_sb_draw = _CTkSB._draw
    _orig_misc_update = _tk.Misc.update_idletasks

    def _sb_draw_noflush(self, no_color_updates=False):
        global _sb_draw_nesting
        _sb_draw_nesting += 1
        try:
            return _orig_sb_draw(self, no_color_updates)
        finally:
            _sb_draw_nesting -= 1

    def _guarded_update_idletasks(self):
        if _sb_draw_nesting:
            return
        return _orig_misc_update(self)

    _CTkSB._draw = _sb_draw_noflush
    _tk.Misc.update_idletasks = _guarded_update_idletasks
except Exception:
    pass

from core import apps as apps_core
from core import disk as disk_core
from core import icons as icons_core
from core import junk as junk_core
from core.utils import format_size, is_admin, relaunch_as_admin

def _base_dir() -> Path:
    # в собранном exe конфиг храним рядом с exe, иначе — в корне проекта
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


CONFIG_PATH = _base_dir() / "config.json"
ACCENT = "#3b82f6"
# Сколько строк списка показываем сразу: сотни виджетов душат перерисовку окна
APPS_PER_PAGE = 25
DISK_ROWS = 25


def eta_text(elapsed_s: float, done: int, total: int) -> str:
    """Оценка оставшегося времени по темпу. '' если оценить нельзя."""
    if done <= 0 or total <= 0 or elapsed_s <= 0:
        return ""
    per_item = elapsed_s / done
    left = per_item * max(total - done, 0)
    if left < 1:
        return ""
    if left < 60:
        return f"осталось ~{int(left)} с"
    return f"осталось ~{int(left // 60)} мин {int(left % 60)} с"


def mmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


class AppRow(ctk.CTkFrame):
    ICON = 36

    def __init__(self, master, app: apps_core.AppInfo, on_select, on_uninstall, on_leftovers, **kw):
        super().__init__(master, corner_radius=10, **kw)
        self.app = app
        self.on_select = on_select
        self._icon_ref = None
        self.grid_columnconfigure(1, weight=1)
        # иконка программы
        self.ico_label = ctk.CTkLabel(self, text="", width=self.ICON, height=self.ICON)
        self.ico_label.grid(row=0, column=0, rowspan=2, padx=(10, 2), pady=8, sticky="n")
        # имя
        self.lbl_name = ctk.CTkLabel(self, text=app.name, font=("Segoe UI", 13, "bold"), anchor="w")
        self.lbl_name.grid(row=0, column=1, sticky="ew", padx=(6, 6), pady=(8, 0))
        sub = f"{app.publisher or '—'}  •  {app.version or '—'}  •  {app.display_size}"
        self.lbl_sub = ctk.CTkLabel(self, text=sub, font=("Segoe UI", 11), text_color="gray", anchor="w")
        self.lbl_sub.grid(row=1, column=1, sticky="ew", padx=(6, 6), pady=(0, 8))
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=0, column=2, rowspan=2, padx=10, pady=8, sticky="e")
        ctk.CTkButton(btns, text="Остатки", width=90, fg_color="transparent", border_width=1,
                      command=lambda: on_leftovers(app)).pack(side="left", padx=(0, 6))
        ctk.CTkButton(btns, text="Удалить", width=90, fg_color="#ef4444", hover_color="#dc2626",
                      command=lambda: on_uninstall(app)).pack(side="left")
        self.bind("<Button-1>", lambda e: on_select(app))
        self.ico_label.bind("<Button-1>", lambda e: on_select(app))
        self.lbl_name.bind("<Button-1>", lambda e: on_select(app))
        self.lbl_sub.bind("<Button-1>", lambda e: on_select(app))

    def set_icon(self, pil_img):
        """Вызывать в главном потоке. pil_img — PIL.Image."""
        try:
            cimg = ctk.CTkImage(light_image=pil_img, dark_image=pil_img,
                                size=(self.ICON, self.ICON))
            self._icon_ref = cimg  # держим ссылку, иначе пропадёт
            self.ico_label.configure(image=cimg, text="")
        except Exception:
            pass


class BCleanerApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        cfg = load_config()
        mode = cfg.get("appearance", "System")
        ctk.set_appearance_mode(mode)
        ctk.set_default_color_theme("blue")

        self.title("BCleaner — чистка и оптимизация ПК")
        self.geometry("1180x720")
        self.minsize(1000, 620)
        # Иконка окна/таскбара: только iconbitmap — CTk при старте затирает
        # iconphoto своей CustomTkinter_icon_Windows.ico.
        # В собранном exe ресурсы лежат в sys._MEIPASS (--add-data assets),
        # в исходниках — в корне проекта.
        try:
            import sys as _sys
            _res = Path(_sys._MEIPASS) if getattr(_sys, "frozen", False) else _base_dir()
            _ico = _res / "assets" / "icon.ico"
            if _ico.is_file():
                self.iconbitmap(str(_ico))
        except Exception:
            pass

        self.cfg = cfg
        self.all_apps: list[apps_core.AppInfo] = []
        self.selected_app: apps_core.AppInfo | None = None
        self.leftovers: list[apps_core.Leftover] = []
        self.junk_cats: list[junk_core.JunkCategory] = []
        self.junk_vars: dict[str, object] = {}
        self.disk_stat: disk_core.FolderStat | None = None
        self.disk_history: list[str] = []
        self.disk_cancel = False
        self._disk_busy = False
        self._disk_row_btns: list = []
        self._apps_gen = 0
        self._apps_page = 0
        self._root_size = (0, 0)
        self._veil_after = None
        self._veiled = False
        self._treemap_after = None
        self._search_after = None
        self._left_cancel = False
        self._det_icon_ref = None
        self._ui_queue: queue.Queue = queue.Queue()

        self._build_layout()
        self._show_page("apps")
        self.bind("<Configure>", self._on_root_resize)
        self.after(150, self.refresh_apps_async)
        self.after(100, self._pump_queue)

    # ---------- layout ----------
    def _build_layout(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Sidebar
        sb = ctk.CTkFrame(self, width=210, corner_radius=0)
        sb.grid(row=0, column=0, sticky="nsew")
        sb.grid_rowconfigure(5, weight=1)

        ctk.CTkLabel(sb, text="⬢ BCleaner", font=("Segoe UI", 22, "bold")).pack(padx=16, pady=(20, 2), anchor="w")
        ctk.CTkLabel(sb, text="чистый ПК — быстрый ПК", font=("Segoe UI", 11), text_color="gray").pack(padx=16, pady=(0, 16), anchor="w")

        self.nav_btns: dict[str, ctk.CTkButton] = {}
        for key, label, icon in [
            ("apps", "Программы", "▦"),
            ("junk", "Очистка", "🧹"),
            ("disk", "Диск", "🗄"),
        ]:
            b = ctk.CTkButton(sb, text=f"{icon}  {label}", anchor="w", height=40,
                              fg_color="transparent", text_color=("black", "white"),
                              hover_color=("gray75", "gray25"),
                              command=lambda k=key: self._show_page(k))
            b.pack(fill="x", padx=12, pady=4)
            self.nav_btns[key] = b

        admin_txt = "● Администратор" if is_admin() else "○ Без прав админа"
        admin_color = "#22c55e" if is_admin() else "#f59e0b"
        ctk.CTkLabel(sb, text=admin_txt, text_color=admin_color, font=("Segoe UI", 11)).pack(padx=16, pady=(10, 2), anchor="w")
        if not is_admin():
            ctk.CTkButton(sb, text="🛡  Войти как админ", height=34, width=170,
                          fg_color="#f59e0b", hover_color="#d97706",
                          text_color="white", font=("Segoe UI", 12, "bold"),
                          command=self._elevate_to_admin).pack(padx=12, pady=(4, 2), anchor="w")
        ctk.CTkLabel(sb, text="Тема", font=("Segoe UI", 11), text_color="gray").pack(padx=16, pady=(12, 2), anchor="w")
        self.theme_menu = ctk.CTkOptionMenu(sb, values=["System", "Dark", "Light"],
                                            command=self._on_theme,
                                            width=170)
        cur = self.cfg.get("appearance", "System")
        self.theme_menu.set(cur)
        self.theme_menu.pack(padx=12, pady=(0, 16), anchor="w")
        ctk.CTkLabel(sb, text="v1.0 • Windows", font=("Segoe UI", 10), text_color="gray").pack(padx=16, pady=8, anchor="w")

        # Content
        self.content = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.content.grid(row=0, column=1, sticky="nsew", padx=0, pady=0)
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)

        self.pages = {}
        for key in ("apps", "junk", "disk"):
            f = ctk.CTkFrame(self.content, corner_radius=0, fg_color="transparent")
            f.grid(row=0, column=0, sticky="nsew")
            f.grid_columnconfigure(0, weight=1)
            self.pages[key] = f

        self._build_apps_page()
        self._build_junk_page()
        self._build_disk_page()

    def _show_page(self, key: str):
        # неактивные страницы выгружаем из layout: сотни скрытых виджетов
        # всё равно участвуют в перерисовке окна и душат максимайз
        for k, f in self.pages.items():
            try:
                if k == key:
                    f.grid()
                else:
                    f.grid_remove()
            except Exception:
                pass
        for k, b in self.nav_btns.items():
            try:
                b.configure(fg_color=("#dbeafe", "#1e3a5f") if k == key else "transparent")
            except Exception:
                pass
        if key == "junk" and not self.junk_cats:
            self.refresh_junk_async(scan=False)
        if key == "disk" and self.disk_stat is None:
            self._disk_load_drives()

    def _on_root_resize(self, e=None):
        # Вуаль: на время ресайза/максимайза прячем ВЕСЬ контент (замер: кадр
        # 0.86с -> 0.07с). Возврат — когда очередь событий опустеет (after_idle),
        # плюс страховка таймером. Сайдбар остаётся — окно не выглядит сломанным.
        try:
            if e is not None and e.widget is not self:
                return
            w, h = self.winfo_width(), self.winfo_height()
        except Exception:
            return
        if (w, h) == self._root_size or w < 50:
            return
        self._root_size = (w, h)
        self._set_veil(True)
        # re-arm: возврат только после реальной паузы событий.
        # update() короче задержки, поэтому restore не срабатывает внутри шторма.
        if self._veil_after:
            try:
                self.after_cancel(self._veil_after)
            except Exception:
                pass
        try:
            self._veil_after = self.after(300, self._restore_veiled)
        except Exception:
            pass

    def _restore_veiled(self):
        self._veil_after = None
        self._set_veil(False)

    def _set_veil(self, on: bool):
        if on == self._veiled:
            return
        self._veiled = on
        try:
            if on:
                self.content.grid_remove()
            else:
                self.content.grid()
        except Exception:
            pass

    def _on_theme(self, v: str):
        ctk.set_appearance_mode(v)
        self.cfg["appearance"] = v
        save_config(self.cfg)

    def _elevate_to_admin(self):
        if is_admin():
            messagebox.showinfo("Админ", "Уже запущено с правами администратора.")
            return
        if not messagebox.askyesno(
            "Права администратора",
            "Перезапустить BCleaner с правами администратора?\n\n"
            "Нужно для чистки Windows Temp, Prefetch, обновлений и удаления системных остатков.\n"
            "Появится запрос UAC — нажмите «Да».",
        ):
            return
        ok, err = relaunch_as_admin()
        if ok:
            # закрываем обычную копию — остаётся админская
            self.destroy()
        else:
            if "canceled" in err.lower() or "1223" in err:
                return  # пользователь отклонил UAC — молча выходим
            messagebox.showwarning("Не получилось", f"Не удалось повысить права:\n{err}")

    def _pump_queue(self):
        try:
            while True:
                fn = self._ui_queue.get_nowait()
                fn()
        except queue.Empty:
            pass
        self.after(100, self._pump_queue)

    def ui(self, fn):
        self._ui_queue.put(fn)

    # ================= ПРОГРАММЫ =================
    def _build_apps_page(self):
        p = self.pages["apps"]
        p.grid_rowconfigure(2, weight=1)
        # header
        hdr = ctk.CTkFrame(p, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 8))
        hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(hdr, text="Установленные программы", font=("Segoe UI", 20, "bold")).grid(row=0, column=0, sticky="w")
        self.apps_count = ctk.CTkLabel(hdr, text="", font=("Segoe UI", 12), text_color="gray")
        self.apps_count.grid(row=1, column=0, sticky="w")

        toolbar = ctk.CTkFrame(p, fg_color="transparent")
        toolbar.grid(row=1, column=0, sticky="ew", padx=18, pady=4)
        toolbar.grid_columnconfigure(0, weight=1)
        self.search_entry = ctk.CTkEntry(toolbar, placeholder_text="🔍 Поиск программы...", height=36)
        self.search_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.search_entry.bind("<KeyRelease>", self._on_search_key)
        ctk.CTkButton(toolbar, text="⟳ Обновить", width=110, height=36,
                      command=self.refresh_apps_async).grid(row=0, column=1, padx=4)
        self.quiet_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(toolbar, text="Тихое удаление", variable=self.quiet_var).grid(row=0, column=2, padx=8)

        # split: list + detail
        body = ctk.CTkFrame(p, fg_color="transparent")
        body.grid(row=2, column=0, sticky="nsew", padx=18, pady=8)
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)

        self.apps_scroll = ctk.CTkScrollableFrame(body, corner_radius=12)
        self.apps_scroll.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.apps_status = ctk.CTkLabel(self.apps_scroll, text="Загрузка...", text_color="gray")
        self.apps_status.pack(pady=20)
        pg = ctk.CTkFrame(body, fg_color="transparent")
        pg.grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(6, 0))
        pg.grid_columnconfigure(1, weight=1)
        self.pg_prev = ctk.CTkButton(pg, text="◀", width=44, height=30,
                                     fg_color="transparent", border_width=1,
                                     command=self._apps_prev)
        self.pg_prev.grid(row=0, column=0)
        self.pg_label = ctk.CTkLabel(pg, text="", font=("Segoe UI", 11), text_color="gray")
        self.pg_label.grid(row=0, column=1)
        self.pg_next = ctk.CTkButton(pg, text="▶", width=44, height=30,
                                     fg_color="transparent", border_width=1,
                                     command=self._apps_next)
        self.pg_next.grid(row=0, column=2)

        # detail
        det = ctk.CTkFrame(body, corner_radius=12)
        det.grid(row=0, column=1, rowspan=2, sticky="nsew")
        det.grid_columnconfigure(0, weight=1)
        det_hdr = ctk.CTkFrame(det, fg_color="transparent")
        det_hdr.pack(fill="x", padx=14, pady=(12, 4))
        self.det_icon = ctk.CTkLabel(det_hdr, text="", width=48, height=48)
        self.det_icon.pack(side="left", padx=(0, 10))
        ctk.CTkLabel(det_hdr, text="Детали", font=("Segoe UI", 14, "bold")).pack(side="left", anchor="w")
        self.det_text = ctk.CTkLabel(det, text="Выберите программу слева.", text_color="gray",
                                     font=("Segoe UI", 12), wraplength=300, justify="left")
        self.det_text.pack(anchor="w", padx=14, pady=4)
        ctk.CTkLabel(det, text="Остатки (файлы + реестр)", font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=14, pady=(10, 4))
        # кнопка удаления — СРАЗУ под заголовком, чтобы всегда была видна
        # (раньше была в самом низу и уходила за край окна)
        del_row = ctk.CTkFrame(det, fg_color="transparent")
        del_row.pack(fill="x", padx=10, pady=(0, 4))
        ctk.CTkButton(del_row, text="Удалить выбранные остатки", fg_color="#ef4444", hover_color="#dc2626",
                      height=34, font=("Segoe UI", 13, "bold"),
                      command=self.delete_selected_leftovers).pack(fill="x")
        self.left_prog_lbl = ctk.CTkLabel(det, text="", font=("Segoe UI", 11), text_color="gray", anchor="w")
        self.left_prog_lbl.pack(fill="x", padx=14)
        lp_row = ctk.CTkFrame(det, fg_color="transparent")
        lp_row.pack(fill="x", padx=14, pady=(2, 0))
        lp_row.grid_columnconfigure(0, weight=1)
        self.left_prog = ctk.CTkProgressBar(lp_row, mode="determinate", height=8)
        self.left_prog.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.left_prog.set(0)
        self.left_stop_btn = ctk.CTkButton(lp_row, text="■", width=32, height=24,
                                           fg_color="transparent", border_width=1,
                                           command=lambda: setattr(self, "_left_cancel", True))
        self.left_stop_btn.grid(row=0, column=1)
        # ручной поиск остатков уже удалённой программы (её больше нет в списке)
        gone_row = ctk.CTkFrame(det, fg_color="transparent")
        gone_row.pack(fill="x", padx=10, pady=(6, 0))
        gone_row.grid_columnconfigure(0, weight=1)
        self.gone_entry = ctk.CTkEntry(gone_row, placeholder_text="Остатки удалённой: введите имя…", height=30)
        self.gone_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.gone_entry.bind("<Return>", lambda e: self._search_gone_async())
        ctk.CTkButton(gone_row, text="🔍 Найти", width=90, height=30,
                      command=self._search_gone_async).grid(row=0, column=1)
        self.left_scroll = ctk.CTkScrollableFrame(det, height=220, corner_radius=8)
        self.left_scroll.pack(fill="both", expand=True, padx=10, pady=4)
        self.left_status = ctk.CTkLabel(self.left_scroll, text="Нажмите «Остатки» у программы.", text_color="gray")
        self.left_status.pack(pady=10)

    def refresh_apps_async(self):
        def _mark():
            st = getattr(self, "apps_status", None)
            try:
                if st is not None and st.winfo_exists():
                    st.configure(text="Чтение реестра...")
            except Exception:
                pass
        self.ui(_mark)
        def work():
            data = apps_core.get_installed_apps()
            self.ui(lambda: self._set_apps(data))
        threading.Thread(target=work, daemon=True).start()

    def _set_apps(self, data):
        # очистить
        for w in list(self.apps_scroll.winfo_children()):
            if w is not self.apps_status:
                w.destroy()
        self.all_apps = data
        self._apps_page = 0
        self._filter_apps()

    def _remove_app_local(self, app: apps_core.AppInfo):
        """Мгновенно убирает удалённую программу из списка без перечитывания реестра."""
        key = (app.name.lower(), app.hive, app.key)
        self.all_apps = [a for a in self.all_apps
                         if (a.name.lower(), a.hive, a.key) != key]
        self._filter_apps()

    def _on_search_key(self, _e=None):
        # дебаунс: пересобираем список только после паузы ввода
        if self._search_after:
            try:
                self.after_cancel(self._search_after)
            except Exception:
                pass
        self._apps_page = 0
        self._search_after = self.after(250, self._filter_apps)

    def _apps_prev(self):
        if self._apps_page > 0:
            self._apps_page -= 1
            self._filter_apps()

    def _apps_next(self):
        self._apps_page += 1
        self._filter_apps()

    def _filter_apps(self):
        q = self.search_entry.get().strip().lower() if hasattr(self, "search_entry") else ""
        items = [a for a in self.all_apps if q in a.name.lower() or q in (a.publisher or "").lower()] if q else self.all_apps
        for w in list(self.apps_scroll.winfo_children()):
            w.destroy()
        self._apps_gen += 1
        gen = self._apps_gen
        pages = max((len(items) + APPS_PER_PAGE - 1) // APPS_PER_PAGE, 1)
        self._apps_page = max(0, min(self._apps_page, pages - 1))
        if not items:
            ctk.CTkLabel(self.apps_scroll, text="Ничего не найдено.", text_color="gray").pack(pady=20)
        else:
            # пейджер: одновременно замаплено ~25 строк, иначе максимайз рисуется кадрами
            shown = items[self._apps_page * APPS_PER_PAGE:(self._apps_page + 1) * APPS_PER_PAGE]
            rows: list[AppRow] = []
            for a in shown:
                r = AppRow(self.apps_scroll, a, self._select_app, self._ask_uninstall, self._find_leftovers_async)
                r.pack(fill="x", pady=4, padx=4)
                rows.append(r)
            self._load_row_icons_async(rows, gen)
        self.apps_count.configure(text=f"Найдено: {len(items)} из {len(self.all_apps)}")
        if hasattr(self, "pg_label"):
            a0 = self._apps_page * APPS_PER_PAGE + 1 if items else 0
            a1 = min((self._apps_page + 1) * APPS_PER_PAGE, len(items))
            self.pg_label.configure(text=f"{a0}–{a1} из {len(items)} · стр. {self._apps_page + 1}/{pages}")
            self.pg_prev.configure(state="normal" if self._apps_page > 0 else "disabled")
            self.pg_next.configure(state="normal" if self._apps_page < pages - 1 else "disabled")
        # новая страница/поиск — всегда смотрим с верха списка
        try:
            self.apps_scroll._parent_canvas.yview_moveto(0.0)
        except Exception:
            pass

    def _load_row_icons_async(self, rows: list, gen: int):
        def work():
            for r in rows:
                if gen != self._apps_gen:
                    break
                try:
                    pil = icons_core.get_app_pil_icon(r.app, AppRow.ICON)
                except Exception:
                    continue
                if gen != self._apps_gen:
                    break
                self.ui(lambda rr=r, im=pil: rr.set_icon(im) if rr.winfo_exists() else None)
        threading.Thread(target=work, daemon=True).start()

    def _select_app(self, app: apps_core.AppInfo):
        self.selected_app = app
        txt = (f"{app.name}\n"
               f"Издатель: {app.publisher or '—'}\n"
               f"Версия: {app.version or '—'}\n"
               f"Размер: {app.display_size}\n"
               f"Дата: {app.install_date or '—'}\n"
               f"Путь: {app.install_location or '—'}\n"
               f"Удаление: {(app.quiet_uninstall_string or app.uninstall_string or '—')[:120]}")
        self.det_text.configure(text=txt, text_color=("black", "white"))
        # иконка в панели деталей — грузим в фоне, чтобы не фризить
        def work(target=app):
            try:
                pil = icons_core.get_app_pil_icon(target, 48).resize((48, 48))
            except Exception:
                return
            def done():
                if self.selected_app is not target or not self.det_icon.winfo_exists():
                    return
                try:
                    cimg = ctk.CTkImage(light_image=pil, dark_image=pil, size=(48, 48))
                    self._det_icon_ref = cimg
                    self.det_icon.configure(image=cimg, text="")
                except Exception:
                    pass
            self.ui(done)
        threading.Thread(target=work, daemon=True).start()

    def _ask_uninstall(self, app: apps_core.AppInfo):
        self._select_app(app)
        quiet = self.quiet_var.get()
        if not messagebox.askyesno("Удаление", f"Удалить «{app.name}»?\n\nПосле удаления предложим найти остатки."):
            return
        alive = {"run": True}
        t0 = time.monotonic()

        def tick():
            if not alive["run"] or not self.det_text.winfo_exists():
                return
            self.det_text.configure(
                text=f"Удаление «{app.name}»…  ⏳ {mmss(time.monotonic() - t0)}\n"
                     "(работает внешний деинсталлятор — ждём завершения)")
            self.after(1000, tick)
        tick()

        def work():
            ok, msg = apps_core.run_uninstall(app, quiet=quiet)
            def done():
                alive["run"] = False
                if ok:
                    messagebox.showinfo("Готово", f"«{app.name}» удалена.\n{msg}\n\nСейчас поищем остатки.")
                    self._remove_app_local(app)
                    self._find_leftovers_async(app)
                else:
                    # пробуем открыть деинсталлятор visibly / показать ошибку
                    if messagebox.askyesno("Не получилось тихо",
                                           f"{msg}\n\nПопробовать обычное удаление (с окнами)?"):
                        ok2, msg2 = apps_core.run_uninstall(app, quiet=False)
                        if ok2:
                            messagebox.showinfo("Результат", f"{msg2}\n\nСейчас поищем остатки.")
                            self._remove_app_local(app)
                            self._find_leftovers_async(app)
                        else:
                            messagebox.showinfo("Результат", msg2)
                            self.refresh_apps_async()
                    else:
                        messagebox.showwarning("Удаление", msg)
            self.ui(done)
        threading.Thread(target=work, daemon=True).start()

    def _find_leftovers_async(self, app: apps_core.AppInfo):
        self._select_app(app)
        for w in list(self.left_scroll.winfo_children()):
            w.destroy()
        ctk.CTkLabel(self.left_scroll, text=f"Поиск остатков «{app.name}»...", text_color="gray").pack(pady=10)
        self.left_prog.set(0)
        self.left_prog_lbl.configure(text="Поиск остатков: 0/…")
        self._left_cancel = False
        known = {os.path.normcase(a.install_location) for a in self.all_apps
                 if a.install_location and a.name != app.name}

        def work(target=app):
            def prog(done, total, label):
                self.ui(lambda: (
                    self.left_prog.set(done / total if total else 0),
                    self.left_prog_lbl.configure(text=f"Поиск остатков: {done}/{total} · {label}"),
                ) if self.selected_app is target else None)
            items = apps_core.find_leftovers(
                target, on_progress=prog, known_dirs=known,
                cancel_flag=lambda: getattr(self, "_left_cancel", False))
            cancelled = getattr(self, "_left_cancel", False)
            self.ui(lambda: self._show_leftovers(items, target, cancelled))
        threading.Thread(target=work, daemon=True).start()

    def _search_gone_async(self):
        """Поиск остатков программы, которой уже нет в списке (удалена ранее)."""
        name = self.gone_entry.get().strip() if hasattr(self, "gone_entry") else ""
        if len(name) < 3:
            messagebox.showinfo("Поиск остатков", "Введите имя удалённой программы (минимум 3 буквы).")
            return
        pseudo = apps_core.AppInfo(name=name, key="", hive="", subkey="")
        self._select_app(pseudo)
        self._find_leftovers_async(pseudo)

    def _show_leftovers(self, items: list[apps_core.Leftover], target=None, cancelled: bool = False):
        if target is not None and self.selected_app is not target:
            return  # устаревший поиск — пользователь уже выбрал другую программу
        self.leftovers = items
        self.left_prog.set(1)
        note = " (поиск остановлен — показано частично)" if cancelled else ""
        self.left_prog_lbl.configure(
            text=f"Найдено остатков: {len(items)}{note}" if items else ("Поиск остановлен" if cancelled else ""))
        self._render_leftovers()

    def _render_leftovers(self):
        """Перерисовывает чекбоксы из self.leftovers (без нового поиска)."""
        for w in list(self.left_scroll.winfo_children()):
            w.destroy()
        if not self.leftovers:
            ctk.CTkLabel(self.left_scroll, text="Остатков не найдено. Чисто ✨", text_color="gray").pack(pady=10)
            self.left_vars = []
            return
        icons = {"file": "📁", "registry": "🧾", "service": "⚙", "task": "🕒"}
        self.left_vars: list[tuple[ctk.BooleanVar, apps_core.Leftover]] = []
        for it in self.leftovers:
            v = ctk.BooleanVar(value=not it.risky)
            self.left_vars.append((v, it))
            icon = icons.get(it.kind, "📁")
            if it.kind == "file" and it.path.lower().endswith((".lnk", ".url")):
                icon = "🔗"
            size = f" • {format_size(it.size_bytes)}" if it.size_bytes else ""
            warn = "⚠ " if it.risky else ""
            val = f" [{it.value_name}]" if it.value_name else ""
            cb = ctk.CTkCheckBox(self.left_scroll, variable=v,
                                 text=f"{warn}{icon} {it.path[:70]}{val}{size}\n     {it.detail}",
                                 font=("Segoe UI", 11))
            cb.pack(anchor="w", padx=8, pady=4)

    def delete_selected_leftovers(self):
        if not hasattr(self, "left_vars") or not self.left_vars:
            messagebox.showinfo("Остатки", "Сначала нажмите «Остатки» у программы.")
            return
        sel = [it for v, it in self.left_vars if v.get()]
        if not sel:
            return
        total = sum(i.size_bytes for i in sel)
        kinds = {it.kind for it in sel}
        extra = []
        if "service" in kinds:
            extra.append("службы будут остановлены и удалены (нужен админ)")
        if "task" in kinds:
            extra.append("задачи планировщика будут удалены")
        if not messagebox.askyesno("Подтверждение",
                                    f"Удалить {len(sel)} остатков (~{format_size(total)})?\n"
                                    "Файлы удаляются безвозвратно, ключи реестра — только выбранные.\n"
                                    + ("\n".join(extra) if extra else "")):
            return
        self.left_prog.set(0)
        n = len(sel)

        def work(items=list(sel)):
            ok_n, fail = 0, []
            oks: list[bool] = []
            for i, it in enumerate(items, 1):
                short = os.path.basename(it.path.rstrip("\\/")) or it.path[:40]
                self.ui(lambda i=i, s=short: (
                    self.left_prog.set(i / n),
                    self.left_prog_lbl.configure(text=f"Удаление остатков: {i}/{n} · {s}"),
                ))
                ok, msg = apps_core.delete_leftover(it)
                oks.append(ok)
                if ok:
                    ok_n += 1
                else:
                    fail.append(f"{it.path}: {msg}")

            def done():
                self.left_prog.set(1)
                # убираем удалённое из списка локально — без долгого перескана
                gone = {(it.kind, it.path.lower(), (it.value_name or "").lower())
                        for it, good in zip(items, oks) if good}
                self.leftovers = [it for it in self.leftovers
                                  if (it.kind, it.path.lower(), (it.value_name or "").lower()) not in gone]
                self._render_leftovers()
                rest = f" · осталось: {len(self.leftovers)}" if self.leftovers else ""
                self.left_prog_lbl.configure(text=f"Удалено: {ok_n}/{n}{rest}")
                if fail:
                    messagebox.showwarning("Готово частично", f"Удалено: {ok_n}/{n}\n\n" + "\n".join(fail[:5]))
                else:
                    messagebox.showinfo("Готово", f"Удалено остатков: {ok_n}")
            self.ui(done)
        threading.Thread(target=work, daemon=True).start()

    # ================= ОЧИСТКА =================
    def _build_junk_page(self):
        p = self.pages["junk"]
        p.grid_rowconfigure(2, weight=1)
        ctk.CTkLabel(p, text="Очистка мусора", font=("Segoe UI", 20, "bold")).grid(row=0, column=0, sticky="w", padx=18, pady=(16, 2))
        self.junk_total = ctk.CTkLabel(p, text="Нажмите «Сканировать», чтобы оценить мусор.", font=("Segoe UI", 12), text_color="gray")
        self.junk_total.grid(row=1, column=0, sticky="w", padx=18, pady=(0, 6))

        bar = ctk.CTkFrame(p, fg_color="transparent")
        bar.grid(row=2, column=0, sticky="nsew", padx=18, pady=4)
        bar.grid_columnconfigure(0, weight=1)
        bar.grid_rowconfigure(1, weight=1)
        tools = ctk.CTkFrame(bar, fg_color="transparent")
        tools.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ctk.CTkButton(tools, text="🔍 Сканировать", command=lambda: self.refresh_junk_async(scan=True)).pack(side="left", padx=(0, 8))
        ctk.CTkButton(tools, text="🧹 Очистить выбранное", fg_color="#22c55e", hover_color="#16a34a",
                      command=self.clean_junk_async).pack(side="left")
        self.junk_progress = ctk.CTkProgressBar(bar, mode="indeterminate")
        self.junk_progress.grid(row=0, column=1, sticky="ew", padx=12)
        self.junk_progress.set(0)

        self.junk_scroll = ctk.CTkScrollableFrame(bar, corner_radius=12)
        self.junk_scroll.grid(row=1, column=0, columnspan=2, sticky="nsew")
        self.junk_status = ctk.CTkLabel(bar, text="", font=("Segoe UI", 11), text_color="gray", anchor="w")
        self.junk_status.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))

    def refresh_junk_async(self, scan: bool):
        self.junk_progress.configure(mode="determinate")
        self.junk_progress.set(0)
        self.junk_status.configure(text="Подготовка...")
        def work():
            t0 = time.monotonic()
            cats = junk_core.get_categories()
            n = len(cats) or 1
            if scan:
                for i, c in enumerate(cats, 1):
                    self.ui(lambda i=i, t=c.title: (
                        self.junk_progress.set(i / n),
                        self.junk_status.configure(
                            text=f"Сканирование: {i}/{n} · {t} · {eta_text(time.monotonic() - t0, i - 1, n)}"),
                    ))
                    junk_core.scan_category(c)
                    self.ui(lambda i=i: self.junk_progress.set(i / n))
            self.ui(lambda: self._show_junk(cats))
        threading.Thread(target=work, daemon=True).start()

    def _show_junk(self, cats):
        self.junk_progress.stop()
        self.junk_progress.set(0)
        self.junk_cats = cats
        for w in list(self.junk_scroll.winfo_children()):
            w.destroy()
        self.junk_vars = {}
        total = sum(c.size_bytes for c in cats)
        self.junk_total.configure(text=f"Мусора: ~{format_size(total)} в {len(cats)} категориях")
        self.junk_status.configure(text=f"Готово: проверено {len(cats)} категорий")
        for c in cats:
            card = ctk.CTkFrame(self.junk_scroll, corner_radius=10)
            card.pack(fill="x", padx=6, pady=5)
            var = ctk.BooleanVar(value=c.enabled)
            self.junk_vars[c.id] = var
            cb = ctk.CTkCheckBox(card, variable=var, text=f"{c.title} — {format_size(c.size_bytes)} ({c.files} файлов)",
                                 font=("Segoe UI", 13, "bold"))
            cb.pack(anchor="w", padx=12, pady=(10, 2))
            ctk.CTkLabel(card, text=c.desc, font=("Segoe UI", 11), text_color="gray",
                         wraplength=800, justify="left").pack(anchor="w", padx=38, pady=(0, 4))
            paths_txt = "\n".join(c.paths[:3]) if c.paths else ("Корзина" if c.special == "recycle" else "—")
            ctk.CTkLabel(card, text=paths_txt, font=("Consolas", 10), text_color="gray").pack(anchor="w", padx=38, pady=(0, 10))

    def clean_junk_async(self):
        sel = [c for c in self.junk_cats if self.junk_vars.get(c.id) and self.junk_vars[c.id].get()]
        if not sel:
            messagebox.showinfo("Очистка", "Выберите хотя бы одну категорию.")
            return
        est = sum(c.size_bytes for c in sel)
        if not messagebox.askyesno("Очистка", f"Очистить {len(sel)} категорий (~{format_size(est)})?\nДействие необратимо."):
            return
        self.junk_progress.configure(mode="determinate")
        self.junk_progress.set(0)
        def work(items=list(sel)):
            t0 = time.monotonic()
            n = len(items) or 1
            freed_total = 0
            errs: list[str] = []
            for i, c in enumerate(items, 1):
                self.ui(lambda i=i, t=c.title: (
                    self.junk_progress.set((i - 1) / n),
                    self.junk_status.configure(
                        text=f"Очистка: {i}/{n} · {t} · {eta_text(time.monotonic() - t0, i - 1, n)}"),
                ))
                f, e = junk_core.clean_category(c)
                freed_total += f
                errs.extend(e)
                self.ui(lambda i=i: self.junk_progress.set(i / n))
            # пересчёт ТОЛЬКО очищенных категорий (они уже пустые — это быстро);
            # остальные сохраняют свои размеры, список обновляется локально
            m = len(items) or 1
            t1 = time.monotonic()
            for j, c in enumerate(items, 1):
                self.ui(lambda j=j, t=c.title: (
                    self.junk_progress.set(j / m),
                    self.junk_status.configure(
                        text=f"Пересчёт: {j}/{m} · {t} · {eta_text(time.monotonic() - t1, j - 1, m)}"),
                ))
                junk_core.scan_category(c)
            def done():
                self._show_junk(self.junk_cats)
                self.junk_status.configure(
                    text=f"Готово: освобождено ~{format_size(freed_total)}" + (f", ошибок: {len(errs)}" if errs else ""))
                if errs:
                    messagebox.showwarning("Готово с ошибками",
                                           f"Освобождено ~{format_size(freed_total)}.\n\n" + "\n".join(errs[:6]))
                else:
                    messagebox.showinfo("Готово", f"Освобождено ~{format_size(freed_total)} 🎉")
            self.ui(done)
        threading.Thread(target=work, daemon=True).start()

    # ================= ДИСК =================
    def _build_disk_page(self):
        p = self.pages["disk"]
        p.grid_rowconfigure(3, weight=1)
        ctk.CTkLabel(p, text="Аналитика диска", font=("Segoe UI", 20, "bold")).grid(row=0, column=0, sticky="w", padx=18, pady=(16, 2))
        self.disk_info = ctk.CTkLabel(p, text="", font=("Segoe UI", 12), text_color="gray")
        self.disk_info.grid(row=1, column=0, sticky="w", padx=18)

        tools = ctk.CTkFrame(p, fg_color="transparent")
        tools.grid(row=2, column=0, sticky="ew", padx=18, pady=8)
        tools.grid_columnconfigure(1, weight=1)
        self.drive_menu = ctk.CTkOptionMenu(tools, values=["C:\\"], width=140)
        self.drive_menu.grid(row=0, column=0, padx=(0, 8))
        self.disk_path = ctk.CTkEntry(tools, placeholder_text="Путь для анализа, напр. C:\\", height=34)
        self.disk_path.grid(row=0, column=1, sticky="ew", padx=4)
        self.disk_path.insert(0, "C:\\")
        self.disk_browse_btn = ctk.CTkButton(tools, text="📁…", width=44, command=self._browse_disk)
        self.disk_browse_btn.grid(row=0, column=2, padx=4)
        self.disk_scan_btn = ctk.CTkButton(tools, text="Сканировать", command=lambda: self.scan_disk_async(fresh=True))
        self.disk_scan_btn.grid(row=0, column=3, padx=4)
        self.disk_stop_btn = ctk.CTkButton(tools, text="✕ Стоп", fg_color="transparent", border_width=1,
                                           command=lambda: setattr(self, "disk_cancel", True),
                                           state="disabled")
        self.disk_stop_btn.grid(row=0, column=4, padx=4)
        self.disk_progress = ctk.CTkProgressBar(tools, mode="indeterminate")
        self.disk_progress.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(8, 0))
        self.disk_progress.set(0)
        self.disk_status = ctk.CTkLabel(tools, text="", font=("Segoe UI", 11), text_color="gray")
        self.disk_status.grid(row=2, column=0, columnspan=5, sticky="w")

        body = ctk.CTkFrame(p, fg_color="transparent")
        body.grid(row=3, column=0, sticky="nsew", padx=18, pady=4)
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(1, weight=1)

        self.crumb = ctk.CTkLabel(body, text="", font=("Segoe UI", 12), anchor="w")
        self.crumb.grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.disk_back_btn = ctk.CTkButton(body, text="⬆ Назад", width=90, fg_color="transparent", border_width=1,
                                           command=self._disk_back, state="disabled")
        self.disk_back_btn.grid(row=0, column=1, sticky="e", pady=(0, 4))

        self.disk_scroll = ctk.CTkScrollableFrame(body, corner_radius=12)
        self.disk_scroll.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        right = ctk.CTkFrame(body, corner_radius=12)
        right.grid(row=1, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(right, text="Treemap", font=("Segoe UI", 14, "bold")).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 4))
        self.treemap = ctk.CTkCanvas(right, highlightthickness=0)
        self.treemap.grid(row=1, column=0, sticky="nsew", padx=10, pady=6)
        self.treemap.bind("<Configure>", self._on_treemap_resize)
        ctk.CTkLabel(right, text="Крупные файлы (двойной клик — открыть папку)", font=("Segoe UI", 11, "bold")).grid(row=2, column=0, sticky="w", padx=12, pady=(6, 2))
        self.big_list = ctk.CTkTextbox(right, height=140, font=("Segoe UI", 11))
        self.big_list.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 10))
        self.big_list.bind("<Double-Button-1>", self._on_big_file_dblclick)

    def _disk_load_drives(self):
        def work():
            drives = disk_core.list_drives()
            def done():
                vals = [d.letter for d in drives] or ["C:\\"]
                self.drive_menu.configure(values=vals)
                self.drive_menu.set(vals[0])
                self.disk_path.delete(0, "end")
                self.disk_path.insert(0, vals[0])
                txt = "  •  ".join(f"{d.letter} {format_size(d.free)} своб. из {format_size(d.total)}" for d in drives[:6])
                self.disk_info.configure(text=txt)
                # синхронизируем выбор диска с полем пути
                self.drive_menu.configure(command=self._on_drive_pick)
            self.ui(done)
        threading.Thread(target=work, daemon=True).start()

    def _on_drive_pick(self, v: str):
        self.disk_history = []
        self.disk_path.delete(0, "end")
        self.disk_path.insert(0, v)

    def _browse_disk(self):
        d = filedialog.askdirectory()
        if d:
            self.disk_path.delete(0, "end")
            self.disk_path.insert(0, d)

    def scan_disk_async(self, fresh: bool = False):
        if getattr(self, "_disk_busy", False):
            self.disk_status.configure(text="Сканирование уже идёт — дождитесь или нажмите ✕ Стоп.")
            return
        root = self.disk_path.get().strip() or "C:\\"
        if not os.path.exists(root):
            messagebox.showwarning("Диск", f"Путь не найден: {root}")
            return
        self._disk_busy = True
        self.disk_cancel = False
        self.disk_progress.configure(mode="determinate")
        self.disk_progress.set(0)
        self.disk_status.configure(text="Сканирование...")
        self._update_disk_buttons()
        if fresh:
            # новый корневой скан (кнопка/диск) — историю «Назад» сбрасываем;
            # при _disk_enter/_disk_back история сохраняется
            self.disk_history = []
        t0 = time.monotonic()

        def work(path=root):
            def prog(done, total, msg):
                frac = done / total if total else 0
                eta = eta_text(time.monotonic() - t0, done, total)
                self.ui(lambda: (
                    self.disk_progress.set(frac),
                    self.disk_status.configure(
                        text=f"Замер папок: {done}/{total} · {msg}" + (f" · {eta}" if eta else "")),
                ))
            # один проход: размеры + крупные файлы, замер папок — в потоках
            stat, big = disk_core.scan_folder_children(
                path,
                on_progress=prog,
                cancel_flag=lambda: self.disk_cancel,
            )
            self.ui(lambda: self._show_disk(stat, big))
        threading.Thread(target=work, daemon=True).start()

    def _update_disk_buttons(self):
        """Сканирование идёт — всё блокируем кроме Стоп; иначе наоборот."""
        busy = getattr(self, "_disk_busy", False)
        try:
            self.disk_scan_btn.configure(state="disabled" if busy else "normal")
            self.disk_browse_btn.configure(state="disabled" if busy else "normal")
            self.drive_menu.configure(state="disabled" if busy else "normal")
            self.disk_stop_btn.configure(state="normal" if busy else "disabled")
            self.disk_back_btn.configure(
                state="normal" if (not busy and self.disk_history) else "disabled")
            for b in getattr(self, "_disk_row_btns", []):
                try:
                    if b.winfo_exists():
                        b.configure(state="disabled" if busy else "normal")
                except Exception:
                    pass
        except Exception:
            pass

    def _show_disk(self, stat: disk_core.FolderStat, big: list):
        self._disk_busy = False
        self.disk_progress.stop()
        self.disk_progress.configure(mode="determinate")
        self.disk_progress.set(0)
        approx = "~" if stat.truncated else ""
        extra = " · ~частично (упёрся в лимит подсчёта)" if stat.truncated else ""
        cancelled = " (остановлено)" if self.disk_cancel else ""
        self.disk_status.configure(
            text=f"Готово: {approx}{format_size(stat.size)} в {stat.files} файлах{extra}{cancelled}")
        self.disk_stat = stat
        self._update_disk_buttons()
        self._render_disk_children(stat)
        self.big_list.delete("1.0", "end")
        if big:
            for path, s in big[:30]:
                self.big_list.insert("end", f"{format_size(s):>10}  {path}\n")
        else:
            self.big_list.insert("end", "Файлов >100 МБ не найдено (или сканирование остановлено).")
        self._draw_treemap()

    def _render_disk_children(self, stat: disk_core.FolderStat):
        for w in list(self.disk_scroll.winfo_children()):
            w.destroy()
        self.crumb.configure(text=stat.path)
        if not stat.children:
            ctk.CTkLabel(self.disk_scroll, text="Пусто или нет доступа.", text_color="gray").pack(pady=16)
            return
        mx = max((c.size for c in stat.children), default=1)
        # кап строк: ресайз/максимайз с сотнями виджетов рисуется кадрами
        shown = stat.children[:DISK_ROWS]
        self._disk_row_btns = []
        busy_now = getattr(self, "_disk_busy", False)
        for c in shown:
            is_dir = os.path.isdir(c.path)
            row = ctk.CTkFrame(self.disk_scroll, corner_radius=8, fg_color="transparent")
            row.pack(fill="x", padx=4, pady=2)
            name = os.path.basename(c.path.rstrip("\\/")) or c.path
            icon = "📁" if is_dir else "📄"
            approx = "~" if c.truncated else ""
            name_lbl = ctk.CTkLabel(row, text=f"{icon} {name}", font=("Segoe UI", 12, "bold"), anchor="w")
            name_lbl.pack(anchor="w", padx=8, pady=(6, 0))
            sub_lbl = ctk.CTkLabel(row, text=f"{approx}{format_size(c.size)}  •  {c.files} файлов  •  {c.path[:80]}",
                                   font=("Segoe UI", 11), text_color="gray", anchor="w")
            sub_lbl.pack(anchor="w", padx=8)
            bar = ctk.CTkProgressBar(row, height=6)
            bar.pack(fill="x", padx=8, pady=(4, 2))
            bar.set(c.size / mx if mx else 0)
            btns = ctk.CTkFrame(row, fg_color="transparent")
            btns.pack(anchor="e", padx=8, pady=(0, 6))
            if is_dir:
                open_btn = ctk.CTkButton(btns, text="Открыть →", width=100, height=26,
                                         command=lambda p=c.path: self._disk_enter(p))
                open_btn.pack(side="left", padx=4)
                if busy_now:
                    open_btn.configure(state="disabled")
                self._disk_row_btns.append(open_btn)
                ctk.CTkButton(btns, text="В проводнике", width=110, height=26, fg_color="transparent", border_width=1,
                              command=lambda p=c.path: subprocess.Popen(["explorer", p])).pack(side="left")
                # быстрый переход: двойной клик по строке/названию — войти внутрь
                for w in (row, name_lbl, sub_lbl):
                    w.bind("<Double-Button-1>", lambda e, p=c.path: self._disk_enter(p))
        rest = len(stat.children) - len(shown)
        if rest > 0:
            ctk.CTkLabel(self.disk_scroll, text=f"…и ещё {rest} (мелкие, войдите в папку чтобы увидеть)",
                         text_color="gray", font=("Segoe UI", 11)).pack(pady=8)

    def _disk_enter(self, path: str):
        if getattr(self, "_disk_busy", False):
            self.disk_status.configure(text="Дождитесь окончания сканирования…")
            return
        if self.disk_stat:
            self.disk_history.append(self.disk_stat.path)
        self.disk_path.delete(0, "end")
        self.disk_path.insert(0, path)
        self.scan_disk_async()

    def _disk_back(self):
        if getattr(self, "_disk_busy", False):
            return
        if self.disk_history:
            prev = self.disk_history.pop()
            self.disk_path.delete(0, "end")
            self.disk_path.insert(0, prev)
            self.scan_disk_async()

    def _on_big_file_dblclick(self, event):
        """Двойной клик по строке крупных файлов — показать папку в проводнике."""
        try:
            idx = self.big_list.index(f"@{event.x},{event.y}")
            line = self.big_list.get(f"{idx} linestart", f"{idx} lineend").strip()
        except Exception:
            return
        parts = line.split(None, 1)
        if len(parts) < 2:
            return
        path = parts[1].strip()
        try:
            if os.path.isdir(path):
                subprocess.Popen(["explorer", path])
            elif os.path.exists(path):
                subprocess.Popen(["explorer", "/select,", path])
        except Exception:
            pass

    def _on_treemap_resize(self, _e=None):
        # дебаунс: при максимайзе сыплются десятки Configure — рисуем один раз в конце
        if self._treemap_after:
            try:
                self.after_cancel(self._treemap_after)
            except Exception:
                pass
        self._treemap_after = self.after(150, self._draw_treemap)

    # --- treemap (squarify-lite: полоски пропорционально размеру) ---
    def _draw_treemap(self):
        try:
            cv = self.treemap
            cv.delete("all")
            if not self.disk_stat or not self.disk_stat.children:
                return
            W = cv.winfo_width() or 300
            H = cv.winfo_height() or 250
            items = [c for c in self.disk_stat.children if os.path.isdir(c.path)][:12]
            total = sum(c.size for c in items) or 1
            palette = ["#3b82f6", "#22c55e", "#f59e0b", "#ef4444", "#8b5cf6",
                       "#06b6d4", "#ec4899", "#84cc16", "#f97316", "#14b8a6",
                       "#6366f1", "#eab308"]
            x = 0
            for i, c in enumerate(items):
                w = max(W * c.size / total, 4)
                color = palette[i % len(palette)]
                cv.create_rectangle(x, 0, x + w, H, fill=color, outline="white", width=2)
                name = os.path.basename(c.path.rstrip("\\/"))[:14]
                if w > 54:
                    cv.create_text(x + w / 2, H / 2 - 8, text=name, fill="white", font=("Segoe UI", 9, "bold"))
                    cv.create_text(x + w / 2, H / 2 + 8, text=format_size(c.size), fill="white", font=("Segoe UI", 8))
                x += w
        except Exception:
            pass


def run():
    app = BCleanerApp()
    app.mainloop()
