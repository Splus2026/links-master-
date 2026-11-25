"""Общие вещи: исключения, результат обработки ссылки, имена файлов, чистка текста."""
import re

# Подпапка раздела, куда переносятся архивы после распаковки
ARCHIVE_DIR = '_архивы'
# Список архивов, удалённых после распаковки (чтобы не скачивать их снова)
DONE_LIST = '_скачано.lst'


class Cancelled(Exception):
    """Пользователь нажал «Стоп»."""


class Result:
    """Итог обработки одной ссылки.

    status:  'ok' — скачано, 'skip' — уже было скачано раньше, 'fail' — не удалось
    message: пояснение для окна результатов и лога
    files:   список сохранённых файлов
    manual:  список (ссылка, пояснение) для страницы ручной загрузки
    """

    def __init__(self, status, message='', files=None, manual=None):
        self.status = status
        self.message = message
        self.files = files or []
        self.manual = manual or []


_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {'CON', 'PRN', 'AUX', 'NUL'} | {f'COM{i}' for i in range(1, 10)} | {f'LPT{i}' for i in range(1, 10)}


def safe_name(name, max_len=150):
    """Делает из строки допустимое в Windows имя файла или папки."""
    name = _BAD_CHARS.sub('_', name or '')
    name = re.sub(r'\s+', ' ', name).strip().strip('.').strip()
    if len(name) > max_len:
        dot = name.rfind('.')
        ext = name[dot:] if 0 < dot and len(name) - dot <= 6 else ''
        name = name[:max_len - len(ext)].rstrip() + ext
    if name.split('.')[0].upper() in _RESERVED:
        name = '_' + name
    return name


def clean_text(text):
    """Убирает лишние пробелы и пустые строки (как при копировании страницы в Блокнот)."""
    text = (text or '').replace('\r', '').replace('\xa0', ' ')
    out = []
    blank = False
    for line in text.split('\n'):
        line = re.sub(r'[ \t]+', ' ', line).strip()
        if line:
            out.append(line)
            blank = False
        elif out and not blank:
            out.append('')
            blank = True
    return '\n'.join(out).strip() + '\n'


_BLOCK_TAGS = ['p', 'div', 'li', 'ul', 'ol', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'tr', 'table',
               'section', 'article', 'blockquote', 'pre', 'header', 'footer', 'nav', 'aside',
               'dd', 'dt', 'dl', 'form', 'fieldset', 'hr', 'main', 'figure', 'figcaption']


def element_text(el):
    """Текст элемента так, как его видно в браузере: абзацы — с новой строки,
    жирный шрифт и ссылки внутри абзаца строку не разрывают."""
    from bs4 import Comment
    for c in el.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for s in el.find_all(string=True):
        if s.parent is not None and s.parent.name not in ('pre', 'textarea'):
            s.replace_with(re.sub(r'\s+', ' ', str(s)))
    for br in el.find_all('br'):
        br.replace_with('\n')
    for tag in el.find_all(_BLOCK_TAGS):
        tag.insert_before('\n')
        tag.insert_after('\n')
    return clean_text(el.get_text())


def make_soup(html):
    from bs4 import BeautifulSoup
    try:
        return BeautifulSoup(html, 'lxml')
    except Exception:  # lxml не установлен
        return BeautifulSoup(html, 'html.parser')
