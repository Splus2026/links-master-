"""Обработчики ссылок для разных сайтов.

  teros.org.ru/forum/threads/...   — ветка форума: все страницы, тексты сообщений → один TXT
  bn-abramov.ru/grani/grani_1950   — «Грани Агни Йоги»: весь текст страницы → Г50.txt
  newzapovedrussia.ru/2025         — «Спирали»: найти кнопку на OneDrive и скачать оттуда
  drive.google.com/.../folders/... — папка Google Диска (через gdown, если установлен)
  всё остальное                    — обычная загрузка файла; если сайт отдал страницу,
                                     ищем на ней ссылку на файл
"""
import base64
import html as htmlmod
import re
import time
from urllib.parse import parse_qs, quote, urljoin, urlparse

import requests

from .common import Cancelled, Result, element_text, make_soup, safe_name
from .http_utils import (TIMEOUT, HtmlInsteadOfFile, already_have, download_file,
                         fetch_html, site_root, warm_up)

FORUM_PAGE_PAUSE = 2   # пауза между страницами форума, секунды


class Context:
    def __init__(self, session, stop_event, skip_existing, log):
        self.session = session
        self.stop = stop_event
        self.skip_existing = skip_existing
        self.log = log

    def check_stop(self):
        if self.stop.is_set():
            raise Cancelled()


# ---------------------------------------------------------------- выбор обработчика

def process_url(ctx, section_dir, url):
    """Обрабатывает одну ссылку. Никогда не бросает исключений, кроме Cancelled."""
    host = urlparse(url).netloc.lower()
    if 'teros.org.ru' in host and '/forum/threads/' in url:
        handler = handle_forum
    elif 'bn-abramov.ru' in host and '/grani/' in url:
        handler = handle_grani
    elif 'newzapovedrussia.ru' in host:
        handler = handle_zapoved
    elif 'drive.google.com' in host and '/folders/' in url:
        handler = handle_gdrive_folder
    else:
        handler = handle_generic

    try:
        res = handler(ctx, section_dir, url)
    except Cancelled:
        raise
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else '?'
        res = Result('fail', f'ошибка сайта HTTP {code}{_http_hint(code)}')
    except requests.Timeout:
        res = Result('fail', 'сайт не ответил вовремя')
    except requests.ConnectionError:
        res = Result('fail', 'нет соединения с сайтом')
    except Exception as e:  # noqa: BLE001 — любую ошибку показываем пользователю
        res = Result('fail', f'{type(e).__name__}: {e}')

    if res.status == 'fail' and not any(u == url for u, _ in res.manual):
        res.manual.insert(0, (url, ''))
    return res


def _http_hint(code):
    return {403: ' (доступ запрещён)', 404: ' (страница не найдена)',
            429: ' (слишком много запросов)', 503: ' (сайт перегружен)'}.get(code, '')


# ---------------------------------------------------------------- форум teros.org.ru

def _thread_base(url):
    url = url.split('#')[0].split('?')[0]
    url = re.sub(r'page-\d+/?$', '', url)
    return url if url.endswith('/') else url + '/'


def _forum_last_page(soup, base):
    last = 1
    for nav in soup.find_all(attrs={'data-last': True}):
        try:
            last = max(last, int(nav['data-last']))
        except (ValueError, TypeError):
            pass
    slug = urlparse(base).path.rstrip('/').split('/')[-1]
    for a in soup.find_all('a', href=True):
        href = a['href']
        if slug in href:
            m = re.search(r'page-(\d+)', href)
            if m:
                last = max(last, int(m.group(1)))
    return last


def _forum_title(soup, fallback):
    h1 = soup.find('h1')
    if h1 and h1.get_text(strip=True):
        return h1.get_text(' ', strip=True)
    if soup.title and soup.title.string:
        return soup.title.string.split('|')[0].strip()
    return fallback


def _forum_messages(soup):
    """Тексты сообщений страницы (разметка XenForo 1 и 2)."""
    blocks = soup.find_all('div', class_='messageContent')          # XenForo 1
    if not blocks:
        blocks = soup.select('article.message--post, div.message-userContent')  # XenForo 2
    if not blocks:
        blocks = soup.find_all('div', class_='message')

    texts = []
    for block in blocks:
        content = (block.find('blockquote', class_='messageText')
                   or block.find('div', class_='bbWrapper')
                   or block.find('div', class_='message-content')
                   or block.find('blockquote'))
        if content is None:
            continue
        for junk in content.select('.quoteExpand, .messageTextEndMarker, script, style'):
            junk.decompose()
        text = element_text(content).strip()
        if text:
            texts.append(text)
    return texts


def handle_forum(ctx, section_dir, url):
    base = _thread_base(url)
    html, _ = fetch_html(ctx.session, base)
    soup = make_soup(html)

    slug = urlparse(base).path.rstrip('/').split('/')[-1]
    title = _forum_title(soup, slug)
    fname = safe_name(title) + '.txt'
    if ctx.skip_existing and already_have(section_dir, fname):
        return Result('skip', f'уже скачано: {fname}')

    last = _forum_last_page(soup, base)
    texts = _forum_messages(soup)
    for page in range(2, last + 1):
        ctx.check_stop()
        time.sleep(FORUM_PAGE_PAUSE)
        page_html, _ = fetch_html(ctx.session, f'{base}page-{page}', referer=base)
        texts.extend(_forum_messages(make_soup(page_html)))
        ctx.log(f'    форум: страница {page} из {last}, сообщений {len(texts)}')

    if not texts:
        return Result('fail', 'сообщения не найдены (возможно, нужен вход на форум '
                              'или изменилась разметка)')

    section_dir.mkdir(parents=True, exist_ok=True)
    path = section_dir / fname
    path.write_text('\n\n'.join(texts) + '\n', encoding='utf-8')
    return Result('ok', f'{fname}: {last} стр., {len(texts)} сообщений', files=[path])


# ---------------------------------------------------------------- «Грани» bn-abramov.ru

def page_text(html):
    """Весь видимый текст страницы — то же, что Ctrl+A, Ctrl+C в браузере."""
    soup = make_soup(html)
    for tag in soup(['script', 'style', 'noscript', 'template', 'svg', 'iframe']):
        tag.decompose()
    body = soup.body or soup
    return element_text(body)


def grani_name(url):
    m = re.search(r'grani_(\d{4})', url)
    if m:
        return f'Г{m.group(1)[2:]}.txt'
    return safe_name(urlparse(url).path.strip('/').replace('/', '_')) + '.txt'


def handle_grani(ctx, section_dir, url):
    fname = grani_name(url)
    if ctx.skip_existing and already_have(section_dir, fname):
        return Result('skip', f'уже скачано: {fname}')
    html, _ = fetch_html(ctx.session, url)
    text = page_text(html)
    if len(text) < 300:
        return Result('fail', 'на странице почти нет текста')
    section_dir.mkdir(parents=True, exist_ok=True)
    path = section_dir / fname
    path.write_text(text, encoding='utf-8')
    return Result('ok', f'{fname}: {len(text) // 1000} тыс. знаков', files=[path])


# ---------------------------------------------------------------- «Спирали» → OneDrive

_ONEDRIVE_RE = re.compile(r'https?://(?:onedrive\.live\.com|1drv\.ms)/[^\s"\'<>\\)]+', re.I)


def find_onedrive_links(html):
    """Ищет ссылки на OneDrive и в обычных ссылках, и в данных скриптов страницы."""
    raw = html.replace('\\u002F', '/').replace('\\u002f', '/').replace('\\/', '/')
    raw = htmlmod.unescape(raw)
    links = []
    for m in _ONEDRIVE_RE.finditer(raw):
        link = m.group(0).rstrip('.,;')
        if link not in links:
            links.append(link)
    return links


def _share_id(link):
    return 'u!' + base64.urlsafe_b64encode(link.encode('utf-8')).decode('ascii').rstrip('=')


def _onedrive_get(ctx, api_url):
    r = ctx.session.get(api_url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _onedrive_item(ctx, item, dest_dir, files, depth):
    ctx.check_stop()
    name = item.get('name') or 'file'
    if 'file' in item:
        dl = item.get('@content.downloadUrl') or item.get('@microsoft.graph.downloadUrl')
        if not dl:
            raise RuntimeError('OneDrive не дал прямую ссылку на файл')
        path, skipped = download_file(ctx.session, dl, dest_dir, ctx.stop,
                                      ctx.skip_existing, name=name)
        files.append((path, skipped))
    elif 'folder' in item:
        sub = dest_dir / safe_name(name)
        children = item.get('children')
        if children is None:
            if depth >= 3:
                return
            drive = (item.get('parentReference') or {}).get('driveId')
            item = _onedrive_get(ctx, f'https://api.onedrive.com/v1.0/drives/{drive}'
                                      f'/items/{item["id"]}?$expand=children')
            children = item.get('children') or []
        for child in children:
            _onedrive_item(ctx, child, sub, files, depth + 1)


def onedrive_download_urls(ctx, link):
    """Варианты прямой ссылки на скачивание для публичной ссылки OneDrive."""
    urls = []
    targets = [link]
    try:
        r = ctx.session.get(link, allow_redirects=False, timeout=TIMEOUT)
        if r.headers.get('Location'):
            targets.insert(0, urljoin(link, r.headers['Location']))
    except requests.RequestException:
        pass
    for u in targets:
        if '/redir?' in u:
            urls.append(u.replace('/redir?', '/download?'))
        q = parse_qs(urlparse(u).query)
        resid = (q.get('resid') or q.get('id') or [None])[0]
        auth = (q.get('authkey') or [None])[0]
        if resid and auth:
            urls.append(f'https://onedrive.live.com/download?resid={quote(resid)}&authkey={quote(auth)}')
    urls.append(link + ('&' if '?' in link else '?') + 'download=1')
    return list(dict.fromkeys(urls))


def download_onedrive(ctx, link, dest_dir):
    """Скачивает файл или папку по публичной ссылке OneDrive. Возвращает [(путь, пропущен)]."""
    errors = []
    for url in onedrive_download_urls(ctx, link):
        ctx.check_stop()
        try:
            path, skipped = download_file(ctx.session, url, dest_dir, ctx.stop,
                                          ctx.skip_existing, referer='https://onedrive.live.com/')
            return [(path, skipped)]
        except HtmlInsteadOfFile:
            errors.append('OneDrive показывает страницу вместо файла')
        except requests.RequestException as e:
            errors.append(str(e)[:80])
    try:   # старый способ через API (у Microsoft сейчас часто закрыт)
        item = _onedrive_get(ctx, f'https://api.onedrive.com/v1.0/shares/{_share_id(link)}'
                                  f'/driveItem?$expand=children')
    except requests.RequestException as e:
        raise RuntimeError(errors[0] if errors else str(e)[:80]) from e
    files = []
    _onedrive_item(ctx, item, dest_dir, files, 0)
    return files


def handle_zapoved(ctx, section_dir, url):
    html, final_url = fetch_html(ctx.session, url)
    links = find_onedrive_links(html)
    if not links:
        return Result('fail', 'кнопка OneDrive на странице не найдена '
                              '(страница может строиться скриптом)')

    got, skipped, manual, errors = [], 0, [], []
    for link in links:
        ctx.check_stop()
        try:
            for path, was_skipped in download_onedrive(ctx, link, section_dir):
                if was_skipped:
                    skipped += 1
                else:
                    got.append(path)
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
            manual.append((link, f'OneDrive со страницы {url}'))

    if manual and not got and not skipped:
        return Result('fail', f'OneDrive не отдал файлы автоматически ({errors[0]})',
                      manual=[(url, 'страница с кнопкой')] + manual)
    names = ', '.join(p.name for p in got[:3]) + (' …' if len(got) > 3 else '')
    if got:
        msg = f'с OneDrive: {len(got)} файл(ов): {names}'
        if manual:
            msg += f'; не удалось {len(manual)} — в списке для ручной загрузки'
        return Result('ok', msg, files=got, manual=manual)
    return Result('skip', f'уже скачано ({skipped} файл(ов))', manual=manual)


# ---------------------------------------------------------------- папка Google Диска

def handle_gdrive_folder(ctx, section_dir, url):
    note = 'откройте папку и нажмите «Скачать всё»'
    try:
        import gdown
    except ImportError:
        return Result('fail', 'папка Google Диска: скачайте вручную '
                              '(или установите gdown: pip install gdown)',
                      manual=[(url, note)])
    except Exception as e:  # noqa: BLE001 — gdown есть, но не запустился
        return Result('fail', f'папка Google Диска: gdown не запустился ({e})',
                      manual=[(url, note)])
    section_dir.mkdir(parents=True, exist_ok=True)
    try:
        files = gdown.download_folder(url=url, output=str(section_dir), quiet=True,
                                      remaining_ok=True)
    except TypeError:   # в новых версиях gdown этого параметра нет
        files = gdown.download_folder(url=url, output=str(section_dir), quiet=True)
    if not files:
        return Result('fail', 'Google Диск не отдал файлы', manual=[(url, note)])
    return Result('ok', f'с Google Диска: {len(files)} файл(ов)')


# ---------------------------------------------------------------- обычные ссылки

_FILE_PATH_RE = re.compile(r'\.(zip|rar|7z|txt|docx?|rtf|pdf|fb2|epub)$', re.I)
_FILE_QUERY_RE = re.compile(r'(^|&)(f|format|type|ext)=(txt|zip|rar|docx?|rtf|fb2|epub|pdf)\b', re.I)


_RAW_FILE_RE = re.compile(
    r'https?://[^\s"\'<>\\]+?(?:userapi\.com/[^\s"\'<>\\]+|'
    r'\.(?:zip|rar|7z|txt|docx?|rtf|pdf|fb2|epub)(?:\?[^\s"\'<>\\]*)?)(?=["\'<>\s\\]|$)', re.I)


def file_links_on_page(html, base_url):
    """Ссылки на файлы, найденные на странице (кнопки «Скачать TXT» и т.п.)."""
    soup = make_soup(html)
    found = []
    for a in soup.find_all('a', href=True):
        href = urljoin(base_url, a['href'].strip())
        if not href.startswith('http') or href == base_url:
            continue
        p = urlparse(href)
        if (_FILE_PATH_RE.search(p.path) or _FILE_QUERY_RE.search(p.query)
                or p.path.lower().endswith('get.php')):
            if href not in found:
                found.append(href)
    # ссылки, спрятанные в скриптах страницы (так делает, например, VK)
    raw = html.replace('\\u002F', '/').replace('\\/', '/')
    raw = htmlmod.unescape(raw)
    for m in _RAW_FILE_RE.finditer(raw):
        href = m.group(0)
        if href != base_url and href not in found:
            found.append(href)
    return found[:8]


def _download_with_retry(ctx, section_dir, url):
    """Обычная загрузка; при ошибке 403 — повтор «как из браузера» и через https."""
    try:
        return download_file(ctx.session, url, section_dir, ctx.stop, ctx.skip_existing)
    except requests.HTTPError as e:
        if e.response is None or e.response.status_code != 403:
            raise
        first = e
    warm_up(ctx.session, url)
    tries = [url]
    if url.startswith('http://'):
        tries.append('https://' + url[len('http://'):])
    for u in tries:
        ctx.check_stop()
        try:
            return download_file(ctx.session, u, section_dir, ctx.stop, ctx.skip_existing,
                                 referer=site_root(u))
        except requests.HTTPError:
            continue
        except requests.ConnectionError:
            continue
    raise first


def handle_generic(ctx, section_dir, url):
    try:
        path, skipped = _download_with_retry(ctx, section_dir, url)
    except HtmlInsteadOfFile as page:
        for cand in file_links_on_page(page.html, page.final_url):
            ctx.check_stop()
            try:
                path, skipped = download_file(ctx.session, cand, section_dir, ctx.stop,
                                              ctx.skip_existing, referer=page.final_url)
            except (HtmlInsteadOfFile, requests.RequestException):
                continue
            if skipped:
                return Result('skip', f'уже скачано: {path.name}')
            return Result('ok', f'{path.name} (ссылка найдена на странице)', files=[path])
        return Result('fail', 'сайт отдаёт страницу, а не файл — нужен ручной переход')
    if skipped:
        return Result('skip', f'уже скачано: {path.name}')
    return Result('ok', path.name, files=[path])
