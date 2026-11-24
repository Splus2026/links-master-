"""После загрузки: распаковка архивов и преобразование DOC → DOCX."""
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

from .common import ARCHIVE_DIR, DONE_LIST, Cancelled, safe_name

ARCHIVE_EXTS = ('.zip', '.rar', '.7z')
_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0   # не показывать чёрное окно консоли


def _program_files():
    return [Path(os.environ.get(v, '')) for v in ('ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432')
            if os.environ.get(v)]


def find_7zip():
    exe = shutil.which('7z') or shutil.which('7z.exe')
    if exe:
        return exe
    for pf in _program_files():
        p = pf / '7-Zip' / '7z.exe'
        if p.exists():
            return str(p)
    return None


def find_unrar():
    exe = shutil.which('unrar') or shutil.which('UnRAR.exe')
    if exe:
        return exe
    for pf in _program_files():
        p = pf / 'WinRAR' / 'UnRAR.exe'
        if p.exists():
            return str(p)
    return None


def _zip_member_name(info):
    name = info.filename
    if not info.flag_bits & 0x800:          # имя не в UTF-8 — обычно русская DOS-кодировка
        try:
            name = name.encode('cp437').decode('cp866')
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return name


def extract_zip(archive, dest):
    count = 0
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            parts = [safe_name(p) for p in _zip_member_name(info).replace('\\', '/').split('/')
                     if p not in ('', '.', '..')]
            parts = [p for p in parts if p]
            if not parts:
                continue
            target = dest.joinpath(*parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, 'wb') as out:
                shutil.copyfileobj(src, out)
            count += 1
    return count


def _extract_with_tool(archive, dest):
    seven = find_7zip()
    if seven:
        cmd = [seven, 'x', '-y', f'-o{dest}', str(archive)]
    else:
        unrar = find_unrar() if archive.suffix.lower() == '.rar' else None
        if not unrar:
            raise RuntimeError('для RAR/7Z нужен 7-Zip или WinRAR — установите и повторите')
        cmd = [unrar, 'x', '-y', '-o+', str(archive), str(dest) + os.sep]
    r = subprocess.run(cmd, capture_output=True, timeout=600, creationflags=_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f'архиватор вернул код {r.returncode}')


def _mark_done(section_dir, name):
    with open(section_dir / DONE_LIST, 'a', encoding='utf-8') as f:
        f.write(name + '\n')


def unpack_section(section_dir, stop_event=None, delete_after=False):
    """Распаковывает все архивы раздела. Архив после успешной распаковки
    удаляется (delete_after=True) или переносится в _архивы.
    Возвращает список (имя_архива, успех, сообщение)."""
    results = []
    if not section_dir.exists():
        return results
    archives = sorted(p for p in section_dir.iterdir()
                      if p.is_file() and p.suffix.lower() in ARCHIVE_EXTS)
    for arc in archives:
        if stop_event is not None and stop_event.is_set():
            raise Cancelled()
        try:
            if arc.suffix.lower() == '.zip':
                n = extract_zip(arc, section_dir)
                if n == 0:
                    raise RuntimeError('архив пустой — оставлен как есть')
                msg = f'распаковано файлов: {n}'
            else:
                _extract_with_tool(arc, section_dir)
                msg = 'распаковано'
            if delete_after:
                arc.unlink()
                _mark_done(section_dir, arc.name)
                msg += ', архив удалён'
            else:
                store = section_dir / ARCHIVE_DIR
                store.mkdir(exist_ok=True)
                os.replace(arc, store / arc.name)
            results.append((arc.name, True, msg))
        except zipfile.BadZipFile:
            results.append((arc.name, False, 'архив повреждён или это не ZIP'))
        except Exception as e:  # noqa: BLE001
            results.append((arc.name, False, str(e)))
    return results


# ---------------------------------------------------------------- DOC → DOCX

def find_soffice():
    exe = shutil.which('soffice') or shutil.which('soffice.exe')
    if exe:
        return exe
    for pf in _program_files():
        p = pf / 'LibreOffice' / 'program' / 'soffice.exe'
        if p.exists():
            return str(p)
    return None


def _open_word():
    if os.name != 'nt':
        return None, None
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return None, 'no-pywin32'
    try:
        pythoncom.CoInitialize()
        # отдельный экземпляр Word, чтобы не трогать открытые пользователем документы
        word = win32com.client.DispatchEx('Word.Application')
        try:   # «раннее связывание»: без него Word иногда не находит SaveAs2
            word = win32com.client.gencache.EnsureDispatch(word)
        except Exception:  # noqa: BLE001
            pass
        word.Visible = False
        word.DisplayAlerts = 0
        return word, pythoncom
    except Exception:  # noqa: BLE001 — Word не установлен
        return None, None


def _word_save_docx(word, doc, out):
    """Сохраняет открытый документ как DOCX (16 = wdFormatXMLDocument).
    Пробует SaveAs2 (Word 2010+) и SaveAs (старые версии)."""
    last = None
    for target in (doc, word.ActiveDocument):
        for method in ('SaveAs2', 'SaveAs'):
            try:
                getattr(target, method)(str(out.resolve()), 16)
                return
            except AttributeError as e:
                last = e
    raise RuntimeError(f'Word не смог сохранить DOCX ({last})')


def convert_docs(dirs, stop_event=None):
    """Преобразует все .doc в .docx (через Word, а если его нет — через LibreOffice).
    Исходный .doc после успешного преобразования удаляется.
    Возвращает список (имя_файла, успех, сообщение)."""
    docs = []
    for d in dirs:
        if d.exists():
            docs += [p for p in d.rglob('*') if p.is_file() and p.suffix.lower() == '.doc'
                     and ARCHIVE_DIR not in p.parts]
    if not docs:
        return []

    results = []
    todo = []
    for p in docs:
        if p.with_suffix('.docx').exists():
            p.unlink()
            results.append((p.name, True, 'DOCX уже был, DOC удалён'))
        else:
            todo.append(p)
    if not todo:
        return results

    word, pythoncom = _open_word()
    soffice = None if word else find_soffice()
    if not word and not soffice:
        if pythoncom == 'no-pywin32':
            hint = ('нет связи с Word: выполните  pip install pywin32  (если Word установлен) '
                    'или установите LibreOffice — DOC оставлены как есть')
        else:
            hint = 'не найден ни Microsoft Word, ни LibreOffice — DOC оставлены как есть'
        return results + [(f'{len(todo)} файл(ов) DOC', False, hint)]
    try:
        for p in todo:
            if stop_event is not None and stop_event.is_set():
                raise Cancelled()
            out = p.with_suffix('.docx')
            try:
                if word:
                    doc = word.Documents.Open(str(p.resolve()), False, True, False)
                    try:
                        _word_save_docx(word, doc, out)
                    finally:
                        doc.Close(0)
                else:
                    subprocess.run([soffice, '--headless', '--convert-to', 'docx',
                                    '--outdir', str(p.parent), str(p)],
                                   capture_output=True, timeout=300, creationflags=_NO_WINDOW)
                if out.exists() and out.stat().st_size > 0:
                    p.unlink()
                    results.append((p.name, True, f'→ {out.name}'))
                else:
                    results.append((p.name, False, 'не удалось преобразовать'))
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                results.append((p.name, False, str(e)))
    finally:
        if word:
            try:
                word.Quit()
            except Exception:  # noqa: BLE001
                pass
            pythoncom.CoUninitialize()
    return results
