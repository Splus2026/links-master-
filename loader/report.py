"""HTML-страница со ссылками, которые не удалось скачать автоматически."""
from datetime import datetime
from html import escape


def write_report(path, failed, stats, downloads_dir, cancelled=False):
    """failed: список словарей {section, url, message, manual: [(ссылка, пояснение)]}."""
    by_section = {}
    for item in failed:
        by_section.setdefault(item['section'], []).append(item)

    parts = []
    for section, items in by_section.items():
        rows = []
        for it in items:
            links = []
            for link, note in it['manual'] or [(it['url'], '')]:
                note_html = f'<span class="note">{escape(note)}</span>' if note else ''
                links.append(
                    f'<label class="link"><input type="checkbox" data-key="{escape(link)}">'
                    f'<a href="{escape(link)}" target="_blank" rel="noopener">{escape(link)}</a>'
                    f'{note_html}</label>')
            rows.append(f'<li><div class="err">{escape(it["message"])}</div>{"".join(links)}</li>')
        parts.append(f'<section><h2>{escape(section)} <span class="cnt">{len(items)}</span></h2>'
                     f'<ul>{"".join(rows)}</ul></section>')

    folder_url = downloads_dir.resolve().as_uri()
    note = '<p class="warn">Загрузка была остановлена — обработаны не все ссылки.</p>' if cancelled else ''
    body = ''.join(parts) or '<p>Все отмеченные ссылки загружены автоматически.</p>'

    html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>Ручная загрузка</title>
<style>
 :root {{ --sea:#1f8a8a; --sea-l:#e6f4f3; --ink:#1d2b2b; --muted:#6a7b7b; --red:#b2453a; }}
 body {{ font:16px/1.5 "Segoe UI",Arial,sans-serif; color:var(--ink); background:#f7fafa;
        max-width:960px; margin:0 auto; padding:24px; }}
 h1 {{ color:var(--sea); margin:0 0 4px; }}
 .sum {{ color:var(--muted); margin-bottom:8px; }}
 .help {{ background:var(--sea-l); border-left:4px solid var(--sea); padding:10px 14px; border-radius:4px; }}
 .warn {{ color:var(--red); font-weight:600; }}
 section {{ background:#fff; border:1px solid #d6e6e5; border-radius:8px; padding:4px 18px 10px; margin:18px 0; }}
 h2 {{ font-size:19px; color:var(--sea); }}
 .cnt {{ background:var(--sea); color:#fff; border-radius:10px; padding:0 8px; font-size:13px; vertical-align:middle; }}
 ul {{ list-style:none; padding:0; margin:0; }}
 li {{ padding:8px 0; border-top:1px solid #eef3f3; }}
 li:first-child {{ border-top:none; }}
 .err {{ color:var(--red); font-size:14px; }}
 .link {{ display:block; margin:3px 0; word-break:break-all; cursor:pointer; }}
 .link input {{ margin-right:8px; }}
 .link a {{ color:#14607a; }}
 .link.done a {{ color:var(--muted); text-decoration:line-through; }}
 .note {{ color:var(--muted); font-size:13px; margin-left:8px; }}
</style></head><body>
<h1>Ручная загрузка</h1>
<div class="sum">{datetime.now():%d.%m.%Y %H:%M} · загружено: {stats['ok']} · уже было: {stats['skip']} ·
 не удалось: {stats['fail']}</div>
{note}
<div class="help">Откройте ссылку, скачайте файл (иногда нужно нажать кнопку или ввести капчу)
 и положите его в папку раздела: <a href="{escape(folder_url)}">{escape(str(downloads_dir))}</a>.
 Галочкой можно отметить, что уже сделано.</div>
{body}
<script>
 document.querySelectorAll('.link input').forEach(function (cb) {{
   var key = 'links-loader:' + cb.dataset.key;
   try {{ cb.checked = localStorage.getItem(key) === '1'; }} catch (e) {{}}
   cb.parentNode.classList.toggle('done', cb.checked);
   cb.addEventListener('change', function () {{
     cb.parentNode.classList.toggle('done', cb.checked);
     try {{ localStorage.setItem(key, cb.checked ? '1' : '0'); }} catch (e) {{}}
   }});
 }});
</script>
</body></html>"""
    path.write_text(html, encoding='utf-8')
    return path
