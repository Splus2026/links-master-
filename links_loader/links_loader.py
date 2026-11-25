"""Загрузчик файлов по списку ссылок из links.ini.

Запуск:  python links_loader.py
Результаты: подпапка downloads рядом с программой, по папке на каждый раздел links.ini.
"""
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

APP_DIR = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
DOWNLOADS = APP_DIR / 'downloads'
SETTINGS = APP_DIR / 'links_loader_settings.json'
LOG_FILE = DOWNLOADS / 'загрузка.log'
REPORT_FILE = DOWNLOADS / 'ручная_загрузка.html'
PAUSE_BETWEEN_LINKS = 1.0   # секунды, чтобы не перегружать сайты
BLINK_AFTER = 10            # сколько секунд итоговые строки мигают после окончания загрузки
FONT_SIZE = 14              # основной размер шрифта окна

try:
    import requests  # noqa: F401
    import bs4  # noqa: F401
except ImportError:
    _r = tk.Tk()
    _r.withdraw()
    messagebox.showerror('Не хватает библиотек',
                         'Установите библиотеки командой:\n\n'
                         'pip install requests beautifulsoup4 lxml')
    sys.exit(1)

from loader.common import Cancelled, safe_name
from loader.http_utils import make_session
from loader.ini_parser import Section, parse_links_file
from loader.postprocess import convert_docs, unpack_section
from loader.report import write_report
from loader.sources import Context, process_url

try:   # файл loader/_build.py создаётся при сборке exe на GitHub
    from loader._build import BUILD, DATE
    BUILD_INFO = f'сборка {BUILD} от {DATE}'
except ImportError:
    BUILD_INFO = 'версия из исходников'

STATUS_TEXT = {'ok': '✔ загружено', 'skip': '• уже было', 'fail': '✖ не удалось', 'time': '',
               'post_ok': '✔ готово', 'post_fail': '✖ ошибка'}


def open_path(path):
    path = str(path)
    if os.name == 'nt':
        os.startfile(path)  # noqa: S606
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', path])
    else:
        subprocess.Popen(['xdg-open', path])


def color_button(parent, text, command, bg, bold=False, big=False):
    """Цветная кнопка (у стандартных ttk-кнопок в Windows цвет не меняется)."""
    size = FONT_SIZE + (3 if big else 0)
    return tk.Button(parent, text=text, command=command, bg=bg, fg='white',
                     activebackground=bg, activeforeground='white', disabledforeground='#d0d0d0',
                     font=('Segoe UI', size, 'bold' if bold else 'normal'),
                     relief='flat', cursor='hand2', padx=12, pady=4)


def make_check_images(size):
    """Рисует квадратики галочек: пустой и отмеченный (зелёный с белой галкой)."""
    def blank(fill, border):
        img = tk.PhotoImage(width=size, height=size)
        img.put(border, to=(0, 0, size, size))
        img.put(fill, to=(2, 2, size - 2, size - 2))
        return img

    off = blank('#ffffff', '#5b7f95')
    on = blank('#2e8b57', '#24714a')
    pts = [(0.22, 0.52), (0.42, 0.72), (0.78, 0.30)]
    w = max(2, size // 9)
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        steps = size * 2
        for i in range(steps + 1):
            t = i / steps
            x = int((x1 + (x2 - x1) * t) * size)
            y = int((y1 + (y2 - y1) * t) * size)
            on.put('#ffffff', to=(x - w // 2, y - w // 2, x - w // 2 + w, y - w // 2 + w))
    return off, on


def short_url(url, limit=58):
    """Короткий вид ссылки для списка баз: без http(s)://, длинная — с «…» в середине."""
    u = url.split('://', 1)[-1]
    return u if len(u) <= limit else u[:limit - 22] + '…' + u[-21:]


def setup_logger():
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger('links_loader')
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        h = logging.FileHandler(LOG_FILE, encoding='utf-8')
        h.setFormatter(logging.Formatter('%(asctime)s  %(message)s', '%Y-%m-%d %H:%M:%S'))
        logger.addHandler(h)
    return logger


class App:
    def __init__(self, root):
        self.root = root
        self.sections = []
        self.section_vars = []
        self.link_vars = []          # для каждой базы — галочки её ссылок
        self._sync = False           # защита от «эха» при связке галочек
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.row_urls = {}
        self.time_rows = []          # итоговые строки времени (мигают вместе)
        self.blink_phase = False
        self.blink_until = 0

        settings = self._load_settings()
        self.ini_path = tk.StringVar(value=settings.get('ini', str(APP_DIR / 'links.ini')))
        self.opt_unpack = tk.BooleanVar(value=settings.get('unpack', True))
        self.opt_convert = tk.BooleanVar(value=settings.get('convert', True))
        self.opt_skip = tk.BooleanVar(value=settings.get('skip', True))
        self.opt_delete = tk.BooleanVar(value=settings.get('delete_archives', False))
        self.t_start = None
        self.ini_mtime = None
        self._last_watch = 0
        self._saved_checked = set(settings.get('checked', []))
        self._saved_unchecked_links = set(settings.get('unchecked_links', []))

        root.title(f'Загрузчик файлов по списку ссылок — {BUILD_INFO}')
        root.geometry('1450x940')
        root.minsize(1200, 760)
        self.box_big = make_check_images(FONT_SIZE + 10)
        self.box_small = make_check_images(FONT_SIZE + 4)
        self._build_ui()
        self.load_ini()
        root.protocol('WM_DELETE_WINDOW', self.on_close)
        root.after(100, self.poll)
        root.after(450, self._blink_tick)

    # ------------------------------------------------------------ интерфейс

    def _build_ui(self):
        pad = {'padx': 8, 'pady': 4}
        mid = ttk.Frame(self.root)
        mid.pack(fill='x', **pad)

        # список разделов с галочками
        lst = ttk.LabelFrame(mid, text='Какие базы загружать')
        lst.pack(side='left', fill='y')
        holder = ttk.Frame(lst)
        holder.pack(fill='both', expand=True, padx=4, pady=4)
        self.canvas = tk.Canvas(holder, height=330, width=500, highlightthickness=0)
        sb = ttk.Scrollbar(holder, orient='vertical', command=self.canvas.yview)
        self.check_frame = ttk.Frame(self.canvas)
        self.check_frame.bind('<Configure>',
                              lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.create_window((0, 0), window=self.check_frame, anchor='nw')
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        self.root.bind_all('<MouseWheel>', self._on_wheel)
        btns = ttk.Frame(lst)
        btns.pack(fill='x', padx=4, pady=(0, 4))
        color_button(btns, '✔  Отметить все', lambda: self.set_all(True), '#2f6fb0').pack(side='left')
        color_button(btns, '✖  Снять все', lambda: self.set_all(False), '#8a7a6a').pack(side='left', padx=6)
        self.count_label = ttk.Label(lst, text='')
        self.count_label.pack(anchor='w', padx=6, pady=(0, 4))

        # центр: файл ссылок, «Редактор файла ссылок» и таймер
        center = ttk.Frame(mid)
        center.pack(side='left', fill='both', expand=True, padx=8)
        ttk.Label(center, text='Файл ссылок:').pack(anchor='w', pady=(6, 0))
        filerow = ttk.Frame(center)
        filerow.pack(fill='x')
        ttk.Entry(filerow, textvariable=self.ini_path, state='readonly').pack(
            side='left', fill='x', expand=True, ipady=3)
        color_button(filerow, '📂  Выбрать файл ссылок…', self.browse_ini, '#1f6f8a').pack(
            side='left', padx=(6, 0))
        self.btn_links = tk.Button(center, text='📝  Редактор файла ссылок',
                                   font=('Segoe UI', FONT_SIZE + 6, 'bold'), bg='#1f8a8a', fg='white',
                                   activebackground='#176c6c', activeforeground='white',
                                   relief='flat', cursor='hand2', command=self.edit_ini)
        self.btn_links.pack(fill='x', pady=(10, 4), ipady=14)
        self.timer_label = tk.Label(center, text='00:00', font=('Consolas', 56, 'bold'), fg='#1f6f8a')
        self.timer_label.pack(pady=(14, 0))
        ttk.Label(center, text='время загрузки', foreground='#5a6b6b').pack()
        tk.Label(center, text='Автоматические загрузки с некоторых сайтов могут блокироваться '
                              'настройками антивирусов',
                 font=('Segoe UI', FONT_SIZE), fg='#b2453a', wraplength=520,
                 justify='center').pack(pady=(16, 0))

        # настройки и кнопки
        opt = ttk.LabelFrame(mid, text='Настройки')
        opt.pack(side='left', fill='y', padx=(8, 0))
        for text, var in (('Распаковать архивы (ZIP, RAR, 7Z)', self.opt_unpack),
                          ('Преобразовать DOC в DOCX', self.opt_convert),
                          ('Удалять архив после успешной распаковки', self.opt_delete),
                          ('Пропускать уже скачанное', self.opt_skip)):
            self.check(opt, text, var).pack(anchor='w', padx=6, pady=2)
        ttk.Separator(opt).pack(fill='x', padx=8, pady=10)
        self.btn_start = color_button(opt, '▶  Старт', self.start, '#2e8b57', bold=True, big=True)
        self.btn_start.pack(fill='x', padx=8, pady=2, ipady=4)
        self.btn_stop = color_button(opt, '■  Стоп', self.stop, '#c0392b')
        self.btn_stop.config(state='disabled')
        self.btn_stop.pack(fill='x', padx=8, pady=2)
        color_button(opt, '📁  Открыть папку downloads', lambda: self.open_folder(),
                     '#2e8b57').pack(fill='x', padx=8, pady=(10, 2))
        self.btn_manual = color_button(opt, '🌐  Не скачанное (вручную)', self.open_report, '#d68910')
        self.btn_manual.pack(fill='x', padx=8, pady=2)
        color_button(opt, '📄  Открыть лог', lambda: LOG_FILE.exists() and open_path(LOG_FILE),
                     '#6c7a89').pack(fill='x', padx=8, pady=2)

        # прогресс
        prog = ttk.Frame(self.root)
        prog.pack(fill='x', **pad)
        self.progress = ttk.Progressbar(prog, mode='determinate')
        self.progress.pack(fill='x')
        self.status = ttk.Label(prog, text='Отметьте базы и нажмите «Старт».')
        self.status.pack(anchor='w', pady=(2, 0))

        # результаты
        res = ttk.LabelFrame(self.root, text='Результаты (двойной щелчок — открыть ссылку в браузере)')
        res.pack(fill='both', expand=True, **pad)
        cols = ('section', 'url', 'status', 'details')
        self.tree = ttk.Treeview(res, columns=cols, show='headings')
        for col, title, width in (('section', 'Раздел', 170), ('url', 'Ссылка / файл', 360),
                                  ('status', 'Результат', 110), ('details', 'Подробности', 380)):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, anchor='w', stretch=(col in ('url', 'details')))
        ysb = ttk.Scrollbar(res, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side='left', fill='both', expand=True, padx=(4, 0), pady=4)
        ysb.pack(side='right', fill='y', pady=4)
        self.tree.tag_configure('ok', foreground='#1b7a3a')
        self.tree.tag_configure('skip', foreground='#777777')
        self.tree.tag_configure('fail', foreground='#b2453a')
        self.tree.tag_configure('post_ok', foreground='#1f6f8a')
        self.tree.tag_configure('post_fail', foreground='#b2453a')
        self.tree.tag_configure('time', foreground='#6a2c91', background='#f1e9f7',
                                font=('Segoe UI', FONT_SIZE + 1, 'bold'))
        self.tree.tag_configure('time_blink', foreground='white', background='#8e44ad',
                                font=('Segoe UI', FONT_SIZE + 1, 'bold'))
        self.tree.bind('<Double-1>', self.on_row_double)

        # итог
        self.summary = ttk.Frame(self.root)
        self.summary.pack(side='bottom', fill='x', padx=8, pady=(0, 8), before=res)
        self.summary_label = ttk.Label(self.summary, text='', font=('Segoe UI', FONT_SIZE + 2, 'bold'))
        self.summary_label.pack(anchor='w')
        self.link_folder = self._make_link(self.summary, '📁  Папка с загруженными базами',
                                           lambda e: self.open_folder())
        self.link_report = self._make_link(self.summary, '🌐  Страница со ссылками для ручной загрузки',
                                           lambda e: REPORT_FILE.exists() and webbrowser.open(REPORT_FILE.as_uri()))

    def check(self, parent, text, variable, font=None, fg='black', small=False):
        """Галочка с крупным квадратиком (стандартный в Windows слишком мелкий)."""
        off, on = self.box_small if small else self.box_big
        cb = tk.Checkbutton(parent, text=text, variable=variable, image=off, selectimage=on,
                            compound='left', indicatoron=False, relief='flat', offrelief='flat',
                            overrelief='flat', bd=0, highlightthickness=0, padx=4, pady=1,
                            anchor='w', cursor='hand2', fg=fg,
                            font=font or ('Segoe UI', FONT_SIZE))
        bg = cb.cget('bg')
        cb.config(selectcolor=bg, activebackground=bg)
        return cb

    def open_report(self):
        if REPORT_FILE.exists():
            webbrowser.open(REPORT_FILE.as_uri())
        else:
            messagebox.showinfo('Ручная загрузка', 'Страницы пока нет: всё скачалось '
                                'или загрузка ещё не запускалась.', parent=self.root)

    def _make_link(self, parent, text, callback):
        lbl = tk.Label(parent, text=text, fg='#14607a', cursor='hand2',
                       font=('Segoe UI', FONT_SIZE + 1, 'underline'))
        lbl.bind('<Button-1>', callback)
        return lbl

    def _on_wheel(self, event):
        # крутим список баз, только если мышь над ним
        if str(event.widget).startswith(str(self.canvas)):
            self.canvas.yview_scroll(int(-event.delta / 120), 'units')

    # ------------------------------------------------------------ разделы

    def browse_ini(self):
        path = filedialog.askopenfilename(title='Выбрать файл ссылок', initialdir=str(APP_DIR),
                                          filetypes=[('Список ссылок (ini, txt, Word)', '*.ini *.txt *.docx'), ('Документ Word', '*.docx'), ('Все файлы', '*.*')])
        if path:
            self.ini_path.set(path)
            self.load_ini()

    def edit_ini(self):
        path = Path(self.ini_path.get() or (APP_DIR / 'links.ini'))
        if path.suffix.lower() == '.docx':
            if path.exists():
                open_path(path)          # откроется в Word
            else:
                messagebox.showinfo('Нет файла', f'Файл не найден:\n{path}')
            return
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# Название базы — отдельной строкой, под ним ссылки (по одной в строке).\n'
                            '# Между базами — пустая строка. Строки с # не читаются.\n\n',
                            encoding='utf-8')
            self.ini_path.set(str(path))
        if os.name == 'nt':
            subprocess.Popen(['notepad.exe', str(path)])
        else:
            open_path(path)

    def _watch_ini(self):
        """Раз в секунду: если links.ini сохранили — перечитать список."""
        now = time.time()
        if now - self._last_watch < 1:
            return
        self._last_watch = now
        if self.worker and self.worker.is_alive():
            return
        try:
            mtime = Path(self.ini_path.get()).stat().st_mtime
        except OSError:
            return
        if self.ini_mtime is not None and mtime != self.ini_mtime:
            old = {s.name for s in self.sections}
            self.load_ini()
            for sec, var in zip(self.sections, self.section_vars):
                if sec.name not in old:      # новые базы сразу отмечаем
                    var.set(True)
            self.status.config(text='Список ссылок обновлён.')

    def load_ini(self):
        try:
            self.ini_mtime = Path(self.ini_path.get()).stat().st_mtime
        except OSError:
            self.ini_mtime = None
        if self.sections:   # при обновлении списка сохраняем текущие галочки
            self._remember_checks()
        for w in self.check_frame.winfo_children():
            w.destroy()
        self.sections, self.section_vars, self.link_vars = [], [], []
        path = Path(self.ini_path.get())
        if not path.exists():
            self.status.config(text=f'Файл не найден: {path}')
            self.count_label.config(text='')
            return
        try:
            self.sections = parse_links_file(path)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror('Ошибка чтения', str(e))
            return
        link_font = ('Segoe UI', FONT_SIZE - 2)
        for i, sec in enumerate(self.sections):
            on = sec.name in self._saved_checked
            var = tk.BooleanVar(value=on)
            self.check(self.check_frame, f'{sec.name}   ({len(sec.urls)})', var,
                       font=('Segoe UI', FONT_SIZE, 'bold')).pack(anchor='w', padx=2, pady=(8, 0))
            lvars = []
            for url in sec.urls:
                lv = tk.BooleanVar(value=on and url not in self._saved_unchecked_links)
                row = tk.Frame(self.check_frame)
                row.pack(anchor='w', padx=(30, 4))
                self.check(row, short_url(url), lv, font=link_font, fg='#3f6070',
                           small=True).pack(side='left')
                go = tk.Label(row, text='↗', fg='#1f6f8a', cursor='hand2', font=link_font)
                go.pack(side='left', padx=(4, 0))
                go.bind('<Button-1>', lambda e, u=url: webbrowser.open(u))
                lv.trace_add('write', lambda *a, k=i: self._link_changed(k))
                lvars.append(lv)
            var.trace_add('write', lambda *a, k=i: self._section_changed(k))
            self.section_vars.append(var)
            self.link_vars.append(lvars)
        self._update_count()

    def _section_changed(self, k):
        """Галочка базы ставит или снимает галочки всех её ссылок."""
        if not self._sync:
            self._sync = True
            value = self.section_vars[k].get()
            for lv in self.link_vars[k]:
                lv.set(value)
            self._sync = False
        self._update_count()

    def _link_changed(self, k):
        """Если отмечена хоть одна ссылка — отмечена и база."""
        if not self._sync:
            self._sync = True
            self.section_vars[k].set(any(lv.get() for lv in self.link_vars[k]))
            self._sync = False
        self._update_count()

    def _remember_checks(self):
        self._saved_checked = {s.name for s, v in zip(self.sections, self.section_vars) if v.get()}
        self._saved_unchecked_links = {u for s, lvs in zip(self.sections, self.link_vars)
                                       for u, lv in zip(s.urls, lvs) if not lv.get()}

    def selected_sections(self):
        """Базы только с отмеченными ссылками."""
        result = []
        for sec, lvs in zip(self.sections, self.link_vars):
            urls = [u for u, lv in zip(sec.urls, lvs) if lv.get()]
            if urls:
                part = Section(sec.name)
                part.urls = urls
                result.append(part)
        return result

    def _update_count(self):
        sel = self.selected_sections()
        links = sum(len(s.urls) for s in sel)
        self.count_label.config(text=f'выбрано баз: {len(sel)} из {len(self.sections)}, ссылок: {links}')

    def set_all(self, value):
        for v in self.section_vars:
            v.set(value)

    def open_folder(self):
        DOWNLOADS.mkdir(parents=True, exist_ok=True)
        open_path(DOWNLOADS)

    # ------------------------------------------------------------ настройки

    def _load_settings(self):
        try:
            return json.loads(SETTINGS.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    def _save_settings(self):
        data = {'ini': self.ini_path.get(), 'unpack': self.opt_unpack.get(),
                'convert': self.opt_convert.get(), 'skip': self.opt_skip.get(),
                'delete_archives': self.opt_delete.get(),
                'checked': [s.name for s, v in zip(self.sections, self.section_vars) if v.get()],
                'unchecked_links': sorted({u for s, lvs in zip(self.sections, self.link_vars)
                                           for u, lv in zip(s.urls, lvs) if not lv.get()})}
        try:
            SETTINGS.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
        except OSError:
            pass

    # ------------------------------------------------------------ запуск

    def start(self):
        selected = self.selected_sections()
        if not selected:
            messagebox.showinfo('Нечего загружать', 'Отметьте галочками хотя бы одну базу или ссылку.')
            return
        self._save_settings()
        self.tree.delete(*self.tree.get_children())
        self.row_urls.clear()
        self.time_rows.clear()
        self.link_folder.pack_forget()
        self.link_report.pack_forget()
        self.summary_label.config(text='')
        try:                         # старая страница «не скачанного» относится к прошлому запуску
            REPORT_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        self.stop_event.clear()
        self.btn_start.config(state='disabled')
        self.btn_stop.config(state='normal')
        opts = {'unpack': self.opt_unpack.get(), 'convert': self.opt_convert.get(),
                'skip': self.opt_skip.get(), 'delete': self.opt_delete.get()}
        self.t_start = time.time()
        self.timer_label.config(fg='#1f6f8a')
        self.worker = threading.Thread(target=self.work, args=(selected, opts), daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_event.set()
        self.btn_stop.config(state='disabled')
        self.status.config(text='Останавливаю… (дожидаюсь окончания текущей загрузки)')

    def on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno('Идёт загрузка', 'Прервать загрузку и закрыть программу?'):
                return
            self.stop_event.set()
        self._save_settings()
        self.root.destroy()

    # ------------------------------------------------------------ рабочий поток

    def work(self, selected, opts):
        log = setup_logger()
        put = self.q.put
        total = sum(len(s.urls) for s in selected)
        done = 0
        stats = {'ok': 0, 'skip': 0, 'fail': 0}
        failed = []
        section_dirs = []
        cancelled = False

        log.info('=' * 70)
        log.info(f'НАЧАЛО. Разделов: {len(selected)}, ссылок: {total}. Настройки: {opts}')
        current = {'section': '', 'done': 0}

        def status(text):
            put(('progress', current['done'], total, f"{current['section']}: {text}"))

        def row(name, st, msg):
            log.info(f'      [{st.upper():4}] {name}  —  {msg}')
            put(('row', current['section'], name, st, msg))

        ctx = Context(make_session(), self.stop_event, opts['skip'], log.info,
                      status=status, row=row, ask=self.ask_from_worker)
        t_all = time.time()
        try:
            for sec in selected:
                t_sec = time.time()
                current['section'] = sec.name
                sdir = DOWNLOADS / (safe_name(sec.name) or 'Без названия')
                sdir.mkdir(parents=True, exist_ok=True)
                section_dirs.append(sdir)
                log.info(f'--- {sec.name}')
                for url in sec.urls:
                    ctx.check_stop()
                    put(('progress', done, total, f'{sec.name}: {url}'))
                    res = process_url(ctx, sdir, url)
                    done += 1
                    current['done'] = done
                    stats[res.status] += 1
                    log.info(f'  [{res.status.upper():4}] {url}  —  {res.message}')
                    for link, note in res.manual:
                        if link != url:
                            log.info(f'         вручную: {link}  {note}')
                    if res.status == 'fail' or res.manual:
                        failed.append({'section': sec.name, 'url': url,
                                       'message': res.message, 'manual': res.manual})
                    put(('row', sec.name, url, res.status, res.message))
                    if done < total:
                        time.sleep(PAUSE_BETWEEN_LINKS)

                if opts['unpack']:
                    put(('progress', done, total, f'{sec.name}: распаковка архивов…'))
                    for name, ok, msg in unpack_section(sdir, self.stop_event, opts['delete'], status):
                        log.info(f'  [{"UNZIP" if ok else "ERR  "}] {name}  —  {msg}')
                        put(('row', sec.name, f'распаковка: {name}', 'post_ok' if ok else 'post_fail', msg))

                spent = self._fmt_time(time.time() - t_sec)
                log.info(f'  ВРЕМЯ «{sec.name}»: {spent}')
                put(('row', sec.name, 'Итого время загрузки', 'time', spent))

            if opts['convert']:
                current['section'] = 'Все базы'
                status('преобразование DOC → DOCX…')
                t_conv = time.time()
                for name, ok, msg in convert_docs(section_dirs, self.stop_event, status):
                    log.info(f'  [{"DOCX " if ok else "ERR  "}] {name}  —  {msg}')
                    put(('row', '', f'DOC → DOCX: {name}', 'post_ok' if ok else 'post_fail', msg))
                put(('row', '', 'Итого время DOC → DOCX', 'time', self._fmt_time(time.time() - t_conv)))
        except Cancelled:
            cancelled = True
            log.info('ОСТАНОВЛЕНО пользователем')
        except Exception as e:  # noqa: BLE001
            log.exception('Непредвиденная ошибка')
            put(('row', '', 'ошибка программы', 'fail', str(e)))

        report = None
        if failed or cancelled:
            try:
                report = write_report(REPORT_FILE, failed, stats, DOWNLOADS, cancelled)
            except OSError as e:
                log.info(f'Не удалось записать страницу ручной загрузки: {e}')
        spent = self._fmt_time(time.time() - t_all)
        log.info(f'ИТОГ: загружено {stats["ok"]}, уже было {stats["skip"]}, не удалось {stats["fail"]}. '
                 f'Время: {spent}')
        put(('done', done, total, stats, report, cancelled))

    def ask_from_worker(self, question):
        """Задать вопрос «Да/Нет» из рабочего потока и дождаться ответа."""
        answer = {}
        ready = threading.Event()
        self.q.put(('ask', question, answer, ready))
        while not ready.wait(0.2):
            if self.stop_event.is_set():
                raise Cancelled()
        return answer.get('yes', False)

    # ------------------------------------------------------------ обновление окна

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == 'progress':
                    _, done, total, text = msg
                    self.progress.config(maximum=max(total, 1), value=done)
                    self.status.config(text=f'{done} из {total}  ·  {text}')
                elif kind == 'row':
                    _, section, url, status, details = msg
                    iid = self.tree.insert('', 'end', values=(section, url, STATUS_TEXT[status], details),
                                           tags=(status,))
                    if url.startswith('http'):
                        self.row_urls[iid] = url
                    self.tree.see(iid)
                    if status == 'time':
                        self.time_rows.append(iid)
                elif kind == 'ask':
                    _, question, answer, ready = msg
                    answer['yes'] = messagebox.askyesno('Подтверждение', question, parent=self.root)
                    ready.set()
                elif kind == 'done':
                    self.finish(*msg[1:])
        except queue.Empty:
            pass
        if self.t_start is not None:
            self.timer_label.config(text=self._fmt_time(time.time() - self.t_start))
        self._watch_ini()
        self.root.after(100, self.poll)

    @staticmethod
    def _fmt_time(sec):
        sec = int(sec)
        h, rest = divmod(sec, 3600)
        m, s = divmod(rest, 60)
        return f'{h}:{m:02}:{s:02}' if h else f'{m:02}:{s:02}'

    def finish(self, done, total, stats, report, cancelled):
        self.blink_until = time.time() + BLINK_AFTER
        spent = ''
        if self.t_start is not None:
            spent = self._fmt_time(time.time() - self.t_start)
            self.timer_label.config(text=spent, fg='#1b7a3a')
            self.t_start = None
        self.progress.config(maximum=max(total, 1), value=done)
        head = 'Остановлено. ' if cancelled else 'Готово. '
        self.status.config(text=f'{head}Обработано ссылок: {done} из {total}.')
        self.summary_label.config(
            text=f'{head}Загружено: {stats["ok"]}   ·   уже было: {stats["skip"]}   ·   '
                 f'не удалось: {stats["fail"]}')
        self.link_folder.pack(anchor='w', pady=(4, 0))
        if report:
            self.link_report.pack(anchor='w', pady=(2, 0))
        self.btn_start.config(state='normal')
        self.btn_stop.config(state='disabled')
        self.root.bell()
        self.show_done_dialog(stats, spent, report, cancelled)

    def show_done_dialog(self, stats, spent, report, cancelled):
        """Окно «Загрузка завершена» с кнопками «Не скачанное», «Папка», «OK»."""
        win = tk.Toplevel(self.root)
        win.title('Загрузка остановлена' if cancelled else 'Загрузка завершена')
        win.transient(self.root)
        win.resizable(False, False)
        head = '■  Загрузка остановлена' if cancelled else '✔  Загрузка завершена'
        tk.Label(win, text=head, font=('Segoe UI', FONT_SIZE + 4, 'bold'),
                 fg='#c0392b' if cancelled else '#2e8b57').pack(padx=30, pady=(18, 8))
        text = (f'Загружено: {stats["ok"]}\nУже было: {stats["skip"]}\n'
                f'Не удалось: {stats["fail"]}\n\nВремя: {spent}')
        tk.Label(win, text=text, font=('Segoe UI', FONT_SIZE), justify='left').pack(padx=30)
        btns = tk.Frame(win)
        btns.pack(padx=20, pady=18)
        if report:
            tk.Label(win, text='Не скачанное можно открыть списком ссылок в браузере:',
                     font=('Segoe UI', FONT_SIZE - 1), fg='#6a5a40').pack(before=btns, padx=30, pady=(12, 0))
            color_button(btns, '🌐  Открыть не скачанное', lambda: (self.open_report(), win.destroy()),
                         '#d68910', bold=True).pack(side='left', padx=4)
        color_button(btns, '📁  Папка downloads', lambda: (self.open_folder(), win.destroy()),
                     '#2e8b57').pack(side='left', padx=4)
        color_button(btns, 'OK', win.destroy, '#5b7f95').pack(side='left', padx=4)
        win.bind('<Escape>', lambda e: win.destroy())
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_height()) // 3
        win.geometry(f'+{max(x, 0)}+{max(y, 0)}')
        win.grab_set()
        win.focus_set()

    def _blink_tick(self):
        """Все итоговые строки времени мигают одновременно — во время загрузки
        и ещё BLINK_AFTER секунд после её окончания, затем остаются выделенными."""
        running = self.worker is not None and self.worker.is_alive()
        active = running or time.time() < self.blink_until
        self.blink_phase = not self.blink_phase if active else False
        tag = 'time_blink' if self.blink_phase else 'time'
        self.time_rows = [i for i in self.time_rows if self.tree.exists(i)]
        for iid in self.time_rows:
            self.tree.item(iid, tags=(tag,))
        self.root.after(450, self._blink_tick)

    def on_row_double(self, event):
        iid = self.tree.identify_row(event.y)
        if iid in self.row_urls:
            webbrowser.open(self.row_urls[iid])


def main():
    root = tk.Tk()
    style = ttk.Style()
    try:
        style.theme_use('vista' if os.name == 'nt' else 'clam')
    except tk.TclError:
        pass
    # крупнее все стандартные шрифты окна
    for name in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont', 'TkHeadingFont',
                 'TkCaptionFont', 'TkSmallCaptionFont', 'TkIconFont', 'TkTooltipFont'):
        try:
            tkfont.nametofont(name).configure(size=FONT_SIZE)
        except tk.TclError:
            pass
    style.configure('Treeview', rowheight=int(FONT_SIZE * 2.4))
    style.configure('Treeview.Heading', font=('Segoe UI', FONT_SIZE, 'bold'))
    style.configure('TLabelframe.Label', font=('Segoe UI', FONT_SIZE, 'bold'))
    style.configure('TButton', padding=(10, 5))
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
