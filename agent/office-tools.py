"""Office document tools: reading and creating PDF / Word / PowerPoint / Excel.

Reads JSON from stdin, writes JSON to stdout. Called by mamidlabs-inference.ps1.
"""
import json
import os
import re
import sys


MAX_ARCHIVE_ENTRY_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 2000
MAX_ARCHIVE_NAME_LENGTH = 512
MAX_TEXT_CHARS = 30000
MAX_INPUT_FILE_BYTES = 256 * 1024 * 1024


def _archive_listing(infos):
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise ValueError('The number of files in the archive exceeded the safety limit.')
    total = 0
    lines = []
    for index, info in enumerate(infos):
        name = str(info.filename)
        if len(name) > MAX_ARCHIVE_NAME_LENGTH and index < 500:
            name = name[:MAX_ARCHIVE_NAME_LENGTH] + '...'
        size = max(0, int(getattr(info, 'file_size', 0)))
        total += size
        if size > MAX_ARCHIVE_ENTRY_BYTES or total > MAX_ARCHIVE_TOTAL_BYTES:
            raise ValueError('The archive exceeded the uncompressed size limit.')
        if index < 500:
            lines.append('%s\t%d bytes' % (name, size))
    return '\n'.join(lines), total


def _read_archive_entry(stream, declared_size):
    if declared_size < 0 or declared_size > MAX_ARCHIVE_ENTRY_BYTES:
        raise ValueError('The archive entry exceeded the uncompressed size limit.')
    chunks = []
    total = 0
    while True:
        chunk = stream.read(min(1024 * 1024, MAX_ARCHIVE_ENTRY_BYTES - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_ARCHIVE_ENTRY_BYTES:
            raise ValueError('The archive entry exceeded the uncompressed size limit.')
        chunks.append(chunk)
    return b''.join(chunks)


def _decode_archive_entry(entry, data):
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return {'entry': entry, 'binary': True, 'bytes': len(data), 'note': 'binary file, could not be read as text'}
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + '\n[content truncated at 30K characters]'
    return {'entry': entry, 'text': text}


def _validate_input_file(path):
    size = os.path.getsize(path)
    if size > MAX_INPUT_FILE_BYTES:
        raise ValueError('The document size exceeded the safety limit.')


def _validate_office_archive(path):
    import zipfile
    _validate_input_file(path)
    with zipfile.ZipFile(path) as zf:
        _archive_listing(zf.infolist())


# ----------------------------------------------------------------- markdown parsing
def parse_markdown(text):
    """Turns markdown text into a list of blocks.

    Block types: heading(level,text), bullet(items), numbered(items),
    table(header,rows), code(text), para(text)
    """
    blocks = []
    lines = (text or '').replace('\r\n', '\n').split('\n')
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        if stripped.startswith('```'):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith('```'):
                buf.append(lines[i])
                i += 1
            i += 1
            blocks.append(('code', '\n'.join(buf)))
            continue

        m = re.match(r'^(#{1,6})\s+(.*)$', stripped)
        if m:
            blocks.append(('heading', len(m.group(1)), m.group(2).strip()))
            i += 1
            continue

        if stripped.startswith('|') and stripped.endswith('|'):
            rows = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                cells = [c.strip() for c in lines[i].strip().strip('|').split('|')]
                if not all(re.fullmatch(r':?-{2,}:?', c) for c in cells if c):
                    rows.append(cells)
                i += 1
            if rows:
                blocks.append(('table', rows[0], rows[1:]))
            continue

        if re.match(r'^[-*+]\s+', stripped):
            items = []
            while i < len(lines) and re.match(r'^[-*+]\s+', lines[i].strip()):
                items.append(re.sub(r'^[-*+]\s+', '', lines[i].strip()))
                i += 1
            blocks.append(('bullet', items))
            continue

        if re.match(r'^\d+[.)]\s+', stripped):
            items = []
            while i < len(lines) and re.match(r'^\d+[.)]\s+', lines[i].strip()):
                items.append(re.sub(r'^\d+[.)]\s+', '', lines[i].strip()))
                i += 1
            blocks.append(('numbered', items))
            continue

        buf = []
        while i < len(lines) and lines[i].strip() and not re.match(r'^(#{1,6}\s|[-*+]\s|\d+[.)]\s|\||```)', lines[i].strip()):
            buf.append(lines[i].strip())
            i += 1
        if buf:
            blocks.append(('para', ' '.join(buf)))
    return blocks


def strip_inline(text):
    """Strips **bold** and *italic* markers (for plain text outputs)."""
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'\1', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    return text


# ----------------------------------------------------------------- reading
def read_pdf(req):
    from pypdf import PdfReader
    path = req['path']
    _validate_input_file(path)
    reader = PdfReader(path)
    if reader.is_encrypted:
        # Most "encrypted" PDFs actually only restrict permissions (printing/copying)
        # and open with an empty user password; if decrypt('') returns 0 a password is
        # really needed (is_encrypted stays True after decrypt as well, so the return
        # code is checked, not the property).
        if reader.decrypt('') == 0:
            return {'error': 'This PDF is password protected; it cannot be read without the password.'}
    limit = int(req.get('max_pages', 50))
    parts = []
    for idx, page in enumerate(reader.pages[:limit]):
        parts.append('--- page %d ---\n%s' % (idx + 1, page.extract_text() or ''))
    text = '\n'.join(parts)
    if len(text) > 30000:
        text = text[:30000] + '\n[document truncated at 30K characters]'
    return {'pages': len(reader.pages), 'text': text}


def read_docx(req):
    import docx
    _validate_office_archive(req['path'])
    document = docx.Document(req['path'])
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            parts.append(' | '.join(c.text.strip() for c in row.cells))
    text = '\n'.join(parts)
    if len(text) > 30000:
        text = text[:30000] + '\n[document truncated at 30K characters]'
    return {'text': text}


def read_xlsx(req):
    import openpyxl
    _validate_office_archive(req['path'])
    wb = openpyxl.load_workbook(req['path'], data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append('--- sheet: %s ---' % ws.title)
        for row in ws.iter_rows(max_row=200, values_only=True):
            if any(c is not None for c in row):
                out.append(' | '.join('' if c is None else str(c) for c in row))
    text = '\n'.join(out)
    if len(text) > 30000:
        text = text[:30000] + '\n[file truncated at 30K characters]'
    return {'text': text}


def read_zip(req):
    import zipfile
    path = req['path']
    entry = req.get('entry')
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise ValueError('The number of files in the archive exceeded the safety limit.')
        if entry:
            info = zf.getinfo(entry)
            with zf.open(info) as stream:
                return _decode_archive_entry(entry, _read_archive_entry(stream, info.file_size))
        listing, _ = _archive_listing(infos)
        return {'archive': path, 'entry_count': len(infos), 'listing': listing,
                'note': 'to read a specific file add the entry parameter to the same tool call'}


def read_rar(req):
    try:
        import rarfile
    except ImportError:
        return {'error': 'RAR support is not installed (the rarfile package is missing).'}
    # Derive the install root from this file's own location: this file always lives under <root>/agent/.
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # 7-Zip is now installed system-wide with winget (see setup-run.ps1
    # Test-SevenZipReady) rather than moved under the install root; check the standard
    # Program Files locations. tools\7zip\7z.exe is tried first for backward
    # compatibility with an old/manually placed copy.
    candidates = [
        os.path.join(root, 'tools', '7zip', '7z.exe'),
        os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'), '7-Zip', '7z.exe'),
        os.path.join(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'), '7-Zip', '7z.exe'),
    ]
    sevenzip = next((c for c in candidates if os.path.exists(c)), None)
    if sevenzip:
        rarfile.SEVENZIP_TOOL = sevenzip
        rarfile.tool_setup(sevenzip=True, unrar=False, unar=False, bsdtar=False, force=True)
    path = req['path']
    entry = req.get('entry')
    try:
        with rarfile.RarFile(path) as rf:
            infos = rf.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise ValueError('The number of files in the archive exceeded the safety limit.')
            if entry:
                info = rf.getinfo(entry)
                with rf.open(info) as stream:
                    return _decode_archive_entry(entry, _read_archive_entry(stream, int(info.file_size)))
            listing, _ = _archive_listing(infos)
            return {'archive': path, 'entry_count': len(infos), 'listing': listing,
                    'note': 'to read a specific file add the entry parameter to the same tool call'}
    except rarfile.RarCannotExec:
        return {'error': 'Neither unrar nor 7z was found to open RAR; RAR extraction support is not installed on this computer.'}


def read_pptx(req):
    from pptx import Presentation
    _validate_office_archive(req['path'])
    prs = Presentation(req['path'])
    out = []
    for idx, slide in enumerate(prs.slides):
        out.append('--- slide %d ---' % (idx + 1))
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                out.append(shape.text_frame.text)
    return {'slides': len(prs.slides), 'text': '\n'.join(out)}


# ----------------------------------------------------------------- creation
def create_pdf(req):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Preformatted
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    path = req['path']
    # System font for non-ASCII (e.g. accented) characters; falls back to Helvetica if not found.
    body_font, bold_font = 'Helvetica', 'Helvetica-Bold'
    try:
        win = os.environ.get('WINDIR', r'C:\Windows')
        pdfmetrics.registerFont(TTFont('DejaVu', os.path.join(win, 'Fonts', 'segoeui.ttf')))
        pdfmetrics.registerFont(TTFont('DejaVu-Bold', os.path.join(win, 'Fonts', 'segoeuib.ttf')))
        body_font, bold_font = 'DejaVu', 'DejaVu-Bold'
    except Exception:
        pass

    styles = getSampleStyleSheet()
    body = ParagraphStyle('body', parent=styles['Normal'], fontName=body_font, fontSize=10.5, leading=15)
    head_styles = {
        lvl: ParagraphStyle('h%d' % lvl, parent=styles['Normal'], fontName=bold_font,
                            fontSize=max(11, 20 - lvl * 2), leading=max(15, 24 - lvl * 2),
                            spaceBefore=10, spaceAfter=6)
        for lvl in range(1, 7)
    }

    def rich(text):
        text = (text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
        text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
        text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', text)
        text = re.sub(r'`(.+?)`', r'<font face="Courier">\1</font>', text)
        return text

    story = []
    if req.get('title'):
        story.append(Paragraph(rich(req['title']), head_styles[1]))
        story.append(Spacer(1, 4 * mm))

    for block in parse_markdown(req.get('content', '')):
        kind = block[0]
        if kind == 'heading':
            story.append(Paragraph(rich(block[2]), head_styles[min(block[1], 6)]))
        elif kind == 'para':
            story.append(Paragraph(rich(block[1]), body))
            story.append(Spacer(1, 2.5 * mm))
        elif kind == 'bullet':
            for item in block[1]:
                story.append(Paragraph('• ' + rich(item), body))
            story.append(Spacer(1, 2.5 * mm))
        elif kind == 'numbered':
            for n, item in enumerate(block[1], 1):
                story.append(Paragraph('%d. %s' % (n, rich(item)), body))
            story.append(Spacer(1, 2.5 * mm))
        elif kind == 'code':
            story.append(Preformatted(block[1], ParagraphStyle('code', parent=styles['Code'], fontSize=8.5)))
            story.append(Spacer(1, 2.5 * mm))
        elif kind == 'table':
            data = [[Paragraph(rich(c), body) for c in block[1]]]
            data += [[Paragraph(rich(c), body) for c in row] for row in block[2]]
            table = Table(data, hAlign='LEFT')
            table.setStyle(TableStyle([
                ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#9aa3b2')),
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e8ecf4')),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('LEFTPADDING', (0, 0), (-1, -1), 5),
                ('RIGHTPADDING', (0, 0), (-1, -1), 5),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ]))
            story.append(table)
            story.append(Spacer(1, 3.5 * mm))

    SimpleDocTemplate(path, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm,
                      leftMargin=20 * mm, rightMargin=20 * mm).build(story)
    return {'path': path, 'bytes': os.path.getsize(path)}


def create_docx(req):
    import docx
    from docx.shared import Pt

    document = docx.Document()
    style = document.styles['Normal']
    style.font.name = 'Segoe UI'
    style.font.size = Pt(11)

    if req.get('title'):
        document.add_heading(strip_inline(req['title']), level=0)

    def add_rich(paragraph, text):
        for part in re.split(r'(\*\*.+?\*\*|`.+?`)', text):
            if not part:
                continue
            if part.startswith('**') and part.endswith('**'):
                paragraph.add_run(part[2:-2]).bold = True
            elif part.startswith('`') and part.endswith('`'):
                run = paragraph.add_run(part[1:-1])
                run.font.name = 'Consolas'
            else:
                paragraph.add_run(part)

    for block in parse_markdown(req.get('content', '')):
        kind = block[0]
        if kind == 'heading':
            document.add_heading(strip_inline(block[2]), level=min(block[1], 4))
        elif kind == 'para':
            add_rich(document.add_paragraph(), block[1])
        elif kind == 'bullet':
            for item in block[1]:
                add_rich(document.add_paragraph(style='List Bullet'), item)
        elif kind == 'numbered':
            for item in block[1]:
                add_rich(document.add_paragraph(style='List Number'), item)
        elif kind == 'code':
            para = document.add_paragraph()
            run = para.add_run(block[1])
            run.font.name = 'Consolas'
            run.font.size = Pt(9)
        elif kind == 'table':
            header, rows = block[1], block[2]
            table = document.add_table(rows=1, cols=len(header))
            table.style = 'Light Grid Accent 1'
            for idx, cell in enumerate(header):
                table.rows[0].cells[idx].text = strip_inline(cell)
            for row in rows:
                cells = table.add_row().cells
                for idx, value in enumerate(row[:len(header)]):
                    cells[idx].text = strip_inline(value)

    document.save(req['path'])
    return {'path': req['path'], 'bytes': os.path.getsize(req['path'])}


def create_pptx(req):
    from pptx import Presentation
    from pptx.util import Inches, Pt

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    slides = req.get('slides')
    if not slides:
        # Build slides from markdown: every "# " starts a new slide
        slides = []
        current = None
        for block in parse_markdown(req.get('content', '')):
            if block[0] == 'heading' and block[1] <= 2:
                current = {'title': block[2], 'bullets': []}
                slides.append(current)
            elif current is not None:
                if block[0] == 'bullet' or block[0] == 'numbered':
                    current['bullets'].extend(block[1])
                elif block[0] == 'para':
                    current['bullets'].append(block[1])
                elif block[0] == 'heading':
                    current['bullets'].append(block[2])

    if req.get('title'):
        slide = prs.slides.add_slide(prs.slide_layouts[0])
        slide.shapes.title.text = strip_inline(req['title'])
        if len(slide.placeholders) > 1 and req.get('subtitle'):
            slide.placeholders[1].text = strip_inline(req['subtitle'])

    for spec in slides or []:
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = strip_inline(str(spec.get('title', '')))
        body = slide.placeholders[1].text_frame
        body.clear()
        bullets = spec.get('bullets') or []
        for idx, item in enumerate(bullets):
            para = body.paragraphs[0] if idx == 0 else body.add_paragraph()
            para.text = strip_inline(str(item))
            para.font.size = Pt(18)

    prs.save(req['path'])
    return {'path': req['path'], 'slides': len(prs.slides), 'bytes': os.path.getsize(req['path'])}


ACTIONS = {
    'read_pdf': read_pdf, 'read_docx': read_docx, 'read_xlsx': read_xlsx, 'read_pptx': read_pptx,
    'read_zip': read_zip, 'read_rar': read_rar,
    'create_pdf': create_pdf, 'create_docx': create_docx, 'create_pptx': create_pptx,
}


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stdin.reconfigure(encoding='utf-8')
    except Exception:
        pass
    req = json.loads(sys.stdin.read() or '{}')
    action = req.get('action')
    handler = ACTIONS.get(action)
    if not handler:
        print(json.dumps({'error': 'unknown action: %s' % action}, ensure_ascii=False))
        return
    try:
        print(json.dumps(handler(req), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'error': '%s: %s' % (type(exc).__name__, exc)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
