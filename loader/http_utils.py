"""Сеть: сессия, загрузка страниц и файлов."""
import os
import re
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests

from .common import ARCHIVE_DIR, DONE_LIST, Cancelled, safe_name

HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
    'Upgrade-Insecure-Requests': '1',
}

TIMEOUT = (20, 90)          # (соединение, чтение), секунды
CHUNK = 64 * 1024

FILE_EXTS = {'.zip', '.rar', '.7z', '.txt', '.doc', '.docx', '.rtf', '.pdf',
             '.fb2', '.epub', '.odt', '.djvu'}

CTYPE_EXT = {
    'application/zip': '.zip',
    'application/x-zip-compressed': '.zip',
    'application/x-rar-compressed': '.rar',
    'application/x-rar': '.rar',
    'application/vnd.rar': '.rar',
    'application/x-7z-compressed': '.7z',
    'text/plain': '.txt',
    'application/msword': '.doc',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
    'application/rtf': '.rtf',
    'text/rtf': '.rtf',
    'application/pdf': '.pdf',
    'application/epub+zip': '.epub',
}


class HtmlInsteadOfFile(Exception):
    """Сайт вместо файла вернул веб-страницу (нужна кнопка, капча, вход и т.п.)."""

    def __init__(self, html, final_url):
        super().__init__('сайт вернул веб-страницу вместо файла')
        self.html = html
        self.final_url = final_url


def make_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _fix_encoding(resp):
    enc = (resp.encoding or '').lower()
    if not enc or enc == 'iso-8859-1':
        resp.encoding = resp.apparent_encoding or 'utf-8'


def fetch_html(session, url, referer=None):
    """Возвращает (html, конечный_адрес)."""
    headers = {'Referer': referer} if referer else None
    r = session.get(url, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    _fix_encoding(r)
    return r.text, r.url


def name_from_disposition(cd):
    if not cd:
        return None
    m = re.search(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", cd, re.I)
    if m:
        enc = m.group(1) or 'utf-8'
        value = m.group(2).strip().strip('"')
        try:
            return unquote(value, encoding=enc)
        except LookupError:
            return unquote(value)
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd, re.I) or re.search(r'filename\s*=\s*([^;]+)', cd, re.I)
    if m:
        name = m.group(1).strip()
        # requests читает заголовки как latin-1, а сайты часто шлют utf-8 или cp1251
        for enc in ('utf-8', 'cp1251'):
            try:
                name = name.encode('latin-1').decode(enc)
                break
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
        if re.search(r'%[0-9A-Fa-f]{2}', name):   # royallib шлёт имя в виде %D0%A1...
            name = unquote(name)
        return name
    return None


def guess_filename(resp, fallback='file'):
    name = name_from_disposition(resp.headers.get('Content-Disposition'))
    if not name:
        path = urlparse(resp.url).path
        name = unquote(path.rstrip('/').split('/')[-1]) if path.strip('/') else ''
    name = safe_name(name) or fallback
    ext = Path(name).suffix.lower()
    if ext not in FILE_EXTS:
        ctype = resp.headers.get('Content-Type', '').split(';')[0].strip().lower()
        if ctype in CTYPE_EXT:
            if ext in ('.html', '.htm', '.php', '.aspx'):
                name = Path(name).stem
            name += CTYPE_EXT[ctype]
    return name


def unique_path(path):
    if not path.exists():
        return path
    i = 2
    while True:
        cand = path.with_name(f'{path.stem}_{i}{path.suffix}')
        if not cand.exists():
            return cand
        i += 1


def site_root(url):
    p = urlparse(url)
    return f'{p.scheme}://{p.netloc}/'


def warm_up(session, url):
    """Заходит на главную страницу сайта, как браузер: некоторые сайты
    без этого (без cookie и Referer) отвечают на загрузку ошибкой 403."""
    try:
        session.get(site_root(url), timeout=TIMEOUT)
    except requests.RequestException:
        pass


def already_have(dest_dir, fname):
    """Файл уже скачан раньше (лежит в разделе или уже распакован и убран в _архивы)."""
    for p in (dest_dir / fname, dest_dir / ARCHIVE_DIR / fname):
        if p.exists() and p.stat().st_size > 0:
            return p
    done = dest_dir / DONE_LIST
    if done.exists():
        names = done.read_text(encoding='utf-8', errors='ignore').splitlines()
        if fname in names:
            return dest_dir / fname
    return None


def download_file(session, url, dest_dir, stop_event=None, skip_existing=True,
                  referer=None, name=None):
    """Скачивает файл в dest_dir. Возвращает (путь, был_ли_пропущен)."""
    headers = {'Referer': referer} if referer else None
    with session.get(url, headers=headers, stream=True, allow_redirects=True,
                     timeout=TIMEOUT) as r:
        r.raise_for_status()
        ctype = r.headers.get('Content-Type', '').lower()
        has_name = bool(name_from_disposition(r.headers.get('Content-Disposition')))
        if 'text/html' in ctype and not has_name and not name:
            _fix_encoding(r)
            raise HtmlInsteadOfFile(r.text[:3_000_000], r.url)

        fname = safe_name(name) if name else guess_filename(r)
        dest_dir.mkdir(parents=True, exist_ok=True)
        if skip_existing:
            existing = already_have(dest_dir, fname)
            if existing:
                return existing, True
        target = unique_path(dest_dir / fname)
        part = target.with_name(target.name + '.part')
        try:
            with open(part, 'wb') as f:
                for chunk in r.iter_content(CHUNK):
                    if stop_event is not None and stop_event.is_set():
                        raise Cancelled()
                    if chunk:
                        f.write(chunk)
            if part.stat().st_size == 0:
                raise IOError('получен пустой файл')
            os.replace(part, target)
        finally:
            if part.exists():
                try:
                    part.unlink()
                except OSError:
                    pass
        return target, False
