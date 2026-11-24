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
FONT_SIZE = 12              # основной размер шрифта окна (было 9)

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
from loader.ini_parser import parse_links_file
from loader.postprocess import convert_docs, unpack_section
from loader.report import write_report
from loader.sources import Context, process_url

STATUS_TEXT = {'ok': '✔ загружено', 'skip': '• уже было', 'fail': '✖ не удалось',
               'post_ok': '✔ готово', 'post_fail': '✖ ошибка'}


def open_path(path):
    path = str(path)
    if os.name == 'nt':
        os.startfile(path)  # noqa: S606
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', path])
    else:
        subprocess.Popen(['xdg-open', path])


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
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.row_urls = {}

        settings = self._load_settings()
        self.ini_path = tk.StringVar(value=settings.get('ini', str(APP_DIR / 'links.ini')))
        self.opt_unpack = tk.BooleanVar(value=settings.get('unpack', True))
        self.opt_convert = tk.BooleanVar(value=settings.get('convert', False))
        self.opt_skip = tk.BooleanVar(value=settings.get('skip', True))
        self.opt_delete = tk.BooleanVar(value=settings.get('delete_archives', False))
        self.t_start = None
        self.ini_mtime = None
        self._last_watch = 0
        self._saved_checked = set(settings.get('checked', []))

        root.title('Загрузчик файлов по списку ссылок')
        root.geometry('1300x880')
        root.minsize(1050, 700)
        self._build_ui()
        self.load_ini()
        root.protocol('WM_DELETE_WINDOW', self.on_close)
        root.after(100, self.poll)

    # ------------------------------------------------------------ интерфейс

    def _build_ui(self):
        pad = {'padx': 8, 'pady': 4}
        top = ttk.Frame(self.root)
        top.pack(fill='x', **pad)
        ttk.Label(top, text='Файл ссылок:').pack(side='left')
        ttk.Entry(top, textvariable=self.ini_path).pack(side='left', fill='x', expand=True, padx=6)
        ttk.Button(top, text='Обзор…', command=self.browse_ini).pack(side='left')
        ttk.Button(top, text='📄 Из Word…', command=self.browse_word).pack(side='left', padx=(6, 0))
        ttk.Button(top, text='Перечитать', command=self.load_ini).pack(side='left', padx=(6, 0))

        mid = ttk.Frame(self.root)
        mid.pack(fill='x', **pad)

        # список разделов с галочками
        lst = ttk.LabelFrame(mid, text='Какие базы загружать')
        lst.pack(side='left', fill='y')
        holder = ttk.Frame(lst)
        holder.pack(fill='both', expand=True, padx=4, pady=4)
        self.canvas = tk.Canvas(holder, height=260, width=360, highlightthickness=0)
        sb = ttk.Scrollbar(holder, orient='vertical', command=self.canvas.yview)
        self.check_frame = ttk.Frame(self.canvas)
        self.check_frame.bind('<Configure>',
                              lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.create_window((0, 0), window=self.check_frame, anchor='nw')
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        self.canvas.bind('<Enter>', lambda e: self.canvas.bind_all('<MouseWheel>', self._on_wheel))
        self.canvas.bind('<Leave>', lambda e: self.canvas.unbind_all('<MouseWheel>'))
        btns = ttk.Frame(lst)
        btns.pack(fill='x', padx=4, pady=(0, 4))
        ttk.Button(btns, text='Отметить все', command=lambda: self.set_all(True)).pack(side='left')
        ttk.Button(btns, text='Снять все', command=lambda: self.set_all(False)).pack(side='left', padx=6)
        self.count_label = ttk.Label(lst, text='')
        self.count_label.pack(anchor='w', padx=6, pady=(0, 4))

        # центр: большая кнопка «Ссылки для загрузки» и таймер
        center = ttk.Frame(mid)
        center.pack(side='left', fill='both', expand=True, padx=8)
        self.btn_links = tk.Button(center, text='📝  Ссылки для загрузки',
                                   font=('Segoe UI', FONT_SIZE + 6, 'bold'), bg='#1f8a8a', fg='white',
                                   activebackground='#176c6c', activeforeground='white',
                                   relief='flat', cursor='hand2', command=self.edit_ini)
        self.btn_links.pack(fill='x', pady=(10, 4), ipady=14)
        ttk.Label(center, text='Вставьте ссылки, сохраните файл (Ctrl+S) — список слева обновится сам.',
                  foreground='#5a6b6b', wraplength=380, justify='center').pack()
        self.timer_label = tk.Label(center, text='00:00', font=('Consolas', 56, 'bold'), fg='#1f6f8a')
        self.timer_label.pack(pady=(14, 0))
        ttk.Label(center, text='время загрузки', foreground='#5a6b6b').pack()

        # настройки и кнопки
        opt = ttk.LabelFrame(mid, text='Настройки')
        opt.pack(side='left', fill='y', padx=(8, 0))
        ttk.Checkbutton(opt, text='Распаковать архивы (ZIP, RAR, 7Z)',
                        variable=self.opt_unpack).pack(anchor='w', padx=8, pady=(6, 2))
        ttk.Checkbutton(opt, text='Преобразовать DOC в DOCX',
                        variable=self.opt_convert).pack(anchor='w', padx=8, pady=2)
        ttk.Checkbutton(opt, text='Удалять архив после успешной распаковки',
                        variable=self.opt_delete).pack(anchor='w', padx=8, pady=2)
        ttk.Checkbutton(opt, text='Пропускать уже скачанное',
                        variable=self.opt_skip).pack(anchor='w', padx=8, pady=2)
        ttk.Separator(opt).pack(fill='x', padx=8, pady=10)
        self.btn_start = ttk.Button(opt, text='▶  Старт', command=self.start)
        self.btn_start.pack(fill='x', padx=8, pady=2)
        self.btn_stop = ttk.Button(opt, text='■  Стоп', command=self.stop, state='disabled')
        self.btn_stop.pack(fill='x', padx=8, pady=2)
        ttk.Button(opt, text='Открыть папку downloads',
                   command=lambda: self.open_folder()).pack(fill='x', padx=8, pady=(10, 2))
        ttk.Button(opt, text='Открыть лог',
                   command=lambda: LOG_FILE.exists() and open_path(LOG_FILE)).pack(fill='x', padx=8, pady=2)

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
        self.tree.bind('<Double-1>', self.on_row_double)

        # итог
        self.summary = ttk.Frame(self.root)
        self.summary.pack(fill='x', padx=8, pady=(0, 8))
        self.summary_label = ttk.Label(self.summary, text='', font=('Segoe UI', FONT_SIZE + 2, 'bold'))
        self.summary_label.pack(anchor='w')
        self.link_folder = self._make_link(self.summary, '📁  Папка с загруженными базами',
                                           lambda e: self.open_folder())
        self.link_report = self._make_link(self.summary, '🌐  Страница со ссылками для ручной загрузки',
                                           lambda e: REPORT_FILE.exists() and webbrowser.open(REPORT_FILE.as_uri()))

    def _make_link(self, parent, text, callback):
        lbl = tk.Label(parent, text=text, fg='#14607a', cursor='hand2',
                       font=('Segoe UI', FONT_SIZE + 1, 'underline'))
        lbl.bind('<Button-1>', callback)
        return lbl

    def _on_wheel(self, event):
        self.canvas.yview_scroll(int(-event.delta / 120), 'units')

    # ------------------------------------------------------------ разделы

    def browse_ini(self):
        path = filedialog.askopenfilename(title='Файл ссылок', initialdir=str(APP_DIR),
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

    def browse_word(self):
        path = filedialog.askopenfilename(title='Список ссылок в Word', initialdir=str(APP_DIR),
                                          filetypes=[('Документ Word', '*.docx'), ('Все файлы', '*.*')])
        if path:
            self.ini_path.set(path)
            self.load_ini()
            self.set_all(True)
            self.status.config(text=f'Загружено из Word: {len(self.sections)} баз, '
                                    f'{sum(len(s.urls) for s in self.sections)} ссылок.')

    def load_ini(self):
        self.btn_links.config(text=f'📝  Ссылки для загрузки\n(открыть {Path(self.ini_path.get()).name})')
        try:
            self.ini_mtime = Path(self.ini_path.get()).stat().st_mtime
        except OSError:
            self.ini_mtime = None
        if self.sections:   # при «Перечитать» сохраняем текущие галочки
            self._saved_checked = {s.name for s, v in zip(self.sections, self.section_vars) if v.get()}
        for w in self.check_frame.winfo_children():
            w.destroy()
        self.sections, self.section_vars = [], []
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
        for sec in self.sections:
            var = tk.BooleanVar(value=sec.name in self._saved_checked)
            var.trace_add('write', lambda *a: self._update_count())
            n = len(sec.urls)
            ttk.Checkbutton(self.check_frame, text=f'{sec.name}   ({n} ссыл.)',
                            variable=var).pack(anchor='w', padx=4, pady=1)
            self.section_vars.append(var)
        self._update_count()

    def _update_count(self):
        sel = [s for s, v in zip(self.sections, self.section_vars) if v.get()]
        links = sum(len(s.urls) for s in sel)
        self.count_label.config(text=f'выбрано: {len(sel)} из {len(self.sections)}, ссылок: {links}')

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
                'checked': [s.name for s, v in zip(self.sections, self.section_vars) if v.get()]}
        try:
            SETTINGS.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
        except OSError:
            pass

    # ------------------------------------------------------------ запуск

    def start(self):
        selected = [s for s, v in zip(self.sections, self.section_vars) if v.get()]
        if not selected:
            messagebox.showinfo('Нечего загружать', 'Отметьте галочками хотя бы одну базу.')
            return
        self._save_settings()
        self.tree.delete(*self.tree.get_children())
        self.row_urls.clear()
        self.link_folder.pack_forget()
        self.link_report.pack_forget()
        self.summary_label.config(text='')
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
        ctx = Context(make_session(), self.stop_event, opts['skip'], log.info)
        try:
            for sec in selected:
                sdir = DOWNLOADS / (safe_name(sec.name) or 'Без названия')
                sdir.mkdir(parents=True, exist_ok=True)
                section_dirs.append(sdir)
                log.info(f'--- {sec.name}')
                for url in sec.urls:
                    ctx.check_stop()
                    put(('progress', done, total, f'{sec.name}: {url}'))
                    res = process_url(ctx, sdir, url)
                    done += 1
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
                    for name, ok, msg in unpack_section(sdir, self.stop_event, opts['delete']):
                        log.info(f'  [{"UNZIP" if ok else "ERR  "}] {name}  —  {msg}')
                        put(('row', sec.name, f'распаковка: {name}', 'post_ok' if ok else 'post_fail', msg))

            if opts['convert']:
                put(('progress', done, total, 'Преобразование DOC → DOCX…'))
                for name, ok, msg in convert_docs(section_dirs, self.stop_event):
                    log.info(f'  [{"DOCX " if ok else "ERR  "}] {name}  —  {msg}')
                    put(('row', '', f'DOC → DOCX: {name}', 'post_ok' if ok else 'post_fail', msg))
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
        log.info(f'ИТОГ: загружено {stats["ok"]}, уже было {stats["skip"]}, не удалось {stats["fail"]}')
        put(('done', done, total, stats, report, cancelled))

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
        if self.t_start is not None:
            self.timer_label.config(text=self._fmt_time(time.time() - self.t_start), fg='#1b7a3a')
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
