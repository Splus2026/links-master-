"""Чтение links.ini.

Формат: строка с названием базы, под ней одна или несколько ссылок.
Пустые строки между разделами не обязательны. Строки, начинающиеся с # или ;, пропускаются.
"""
import re
from pathlib import Path

_URL_RE = re.compile(r'^(https?://|www\.)', re.I)


class Section:
    def __init__(self, name):
        self.name = name
        self.urls = []


def _read_lines(path):
    data = Path(path).read_bytes()
    for enc in ('utf-8-sig', 'cp1251'):
        try:
            return data.decode(enc).splitlines()
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', errors='replace').splitlines()


def parse_links_ini(path):
    sections = []
    current = None
    for raw in _read_lines(path):
        line = raw.strip()
        if not line or line.startswith(('#', ';')):
            continue
        if _URL_RE.match(line):
            if current is None:
                current = Section('Без названия')
                sections.append(current)
            if line.lower().startswith('www.'):
                line = 'http://' + line
            if line not in current.urls:
                current.urls.append(line)
        else:
            current = Section(line)
            sections.append(current)
    return [s for s in sections if s.urls]
