"""Чтение списка ссылок: links.ini или таблица в Word (.docx).

links.ini:

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


# ---------------------------------------------------------------- Word (.docx)

import zipfile
import xml.etree.ElementTree as ET

_W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
_R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
# «bibl.txt = Библия» — начало раздела
_HEADER_RE = re.compile(r'^\s*[\w.\-]+\.[A-Za-z0-9]{2,5}\s*=\s*(.+?)\s*$')
_URL_IN_TEXT = re.compile(r'(?:https?://|www\.)[^\s<>"]+', re.I)
_FIELD_LINK = re.compile(r'HYPERLINK\s+"([^"]+)"', re.I)
_LETTERS = re.compile(r'[A-Za-zА-Яа-яЁёІіЇїЄєҐґ]{3}')


def _docx_paragraphs(path):
    """Абзацы документа по порядку (и в таблицах): (текст, ссылки, первый_в_ячейке)."""
    with zipfile.ZipFile(path) as z:
        doc = ET.fromstring(z.read('word/document.xml'))
        rels = {}
        try:
            for rel in ET.fromstring(z.read('word/_rels/document.xml.rels')):
                target = rel.get('Target') or ''
                if target.lower().startswith(('http://', 'https://')):
                    rels[rel.get('Id')] = target
        except KeyError:
            pass

    parent = {child: p for p in doc.iter() for child in p}
    seen_cells = set()
    for p in doc.iter(_W + 'p'):
        parts, links = [], []
        for el in p.iter():
            if el.tag == _W + 't':
                parts.append(el.text or '')
            elif el.tag in (_W + 'tab',):
                parts.append(' ')
            elif el.tag in (_W + 'br', _W + 'cr'):
                parts.append('\n')
            elif el.tag == _W + 'instrText':
                links += _FIELD_LINK.findall(el.text or '')
            elif el.tag == _W + 'hyperlink' and el.get(_R + 'id') in rels:
                links.append(rels[el.get(_R + 'id')])
        text = ''.join(parts).replace('\xa0', ' ')

        # первый непустой абзац в своей ячейке таблицы?
        cell = p
        while cell is not None and cell.tag != _W + 'tc':
            cell = parent.get(cell)
        first_in_cell = False
        if cell is not None and text.strip() and id(cell) not in seen_cells:
            seen_cells.add(id(cell))
            first_in_cell = True

        for i, line in enumerate(text.split('\n')):
            yield line, (links if i == 0 else []), (first_in_cell and i == 0)


def parse_links_docx(path):
    """Чтение ссылок из Word: заголовок раздела — строка вида «имя.txt = Название»
    или «Название https://…» в начале ячейки; ссылки — все адреса ниже него.
    Подписи, инструкции и комментарии после ссылок пропускаются."""
    sections = []
    current = None

    def add(url):
        nonlocal current
        url = url.strip().rstrip('.,;:)»"\'')
        if url.lower().startswith('www.'):
            url = 'http://' + url
        if current is None:
            current = Section('Без названия')
            sections.append(current)
        if url not in current.urls:
            current.urls.append(url)

    for text, links, first_in_cell in _docx_paragraphs(path):
        line = text.strip()
        m = _HEADER_RE.match(line)
        if m and not _URL_IN_TEXT.search(m.group(1)):
            current = Section(m.group(1))
            sections.append(current)
            continue
        urls = _URL_IN_TEXT.findall(line)
        if urls and first_in_cell:
            before = line[:line.find(urls[0])].strip(' -–—:\t')
            if _LETTERS.search(before):          # «Несвятые святые https://…»
                current = Section(before)
                sections.append(current)
        for u in urls:
            add(u)
        for u in links:
            add(u)
    return [s for s in sections if s.urls]


def parse_links_file(path):
    """links.ini (текст) или таблица ссылок в Word (.docx)."""
    with open(path, 'rb') as f:
        is_zip = f.read(4) == b'PK\x03\x04'      # .docx — это ZIP-архив
    if str(path).lower().endswith('.docx') or is_zip:
        return parse_links_docx(path)
    return parse_links_ini(path)
