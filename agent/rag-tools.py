"""Local (offline) lexical search over a bound folder - BM25.

Used so the model does not have to read every file to find the relevant parts in a
large folder. No extra packages are needed; the index is kept as JSON at the
`store` path and is not rebuilt as long as the file signature (path + size + mtime)
is unchanged.

Contract: a JSON request on stdin, a single JSON response on stdout.
  {"action": "search", "root": "...", "query": "...", "top": 6, "store": "..."}
  {"action": "index",  "root": "...", "store": "..."}
"""
import hashlib
import json
import os
import re
import sys
import time

# Directory names skipped so we do not walk a folder with thousands of files
# (venv, node_modules, .git). Only the directory name is matched, not the file name.
SKIP_DIRS = {
    '.git', '.svn', '.hg', 'node_modules', 'venv', '.venv', '__pycache__',
    '.cache', '.idea', '.vscode', 'dist', 'build', 'coverage', '.pytest_cache',
    'site-packages', 'tools',
}

# Extensions that can be searched as plain text. For PDF/Word/Excel the existing
# read_document tool is used (this tool skips binary files).
TEXT_EXT = {
    '.txt', '.md', '.markdown', '.rst', '.csv', '.tsv', '.json', '.log',
    '.ps1', '.psm1', '.psd1', '.py', '.js', '.mjs', '.cjs', '.ts', '.tsx',
    '.jsx', '.html', '.htm', '.css', '.scss', '.xml', '.yaml', '.yml',
    '.ini', '.cfg', '.conf', '.sql', '.cs', '.java', '.kt', '.go', '.rs',
    '.c', '.h', '.cpp', '.hpp', '.cc', '.sh', '.bat', '.cmd', '.toml',
    '.tex', '.env', '.properties', '.gradle', '.dockerfile',
}

MAX_FILES = 800
MAX_FILE_BYTES = 2 * 1024 * 1024
CHUNK_CHARS = 900
MAX_CHUNKS = 20000

TOKEN_RE = re.compile(r'\w+', re.UNICODE)


def tokenize(text):
    return [t for t in TOKEN_RE.findall(text.lower()) if len(t) > 1]


def _iter_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d.lower() not in SKIP_DIRS and not d.startswith('.')
        )
        for name in sorted(filenames):
            if name.startswith('.'):
                ext = os.path.splitext(name)[1].lower()
                if ext not in TEXT_EXT:
                    continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in TEXT_EXT:
                continue
            yield os.path.join(dirpath, name)


def _read_text(path):
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None
        with open(path, 'rb') as fh:
            raw = fh.read()
    except OSError:
        return None
    if b'\x00' in raw[:4096]:
        return None
    for encoding in ('utf-8', 'cp1252', 'latin-1'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _chunk_text(text):
    """Produces ~CHUNK_CHARS pieces while keeping line boundaries."""
    lines = text.splitlines()
    chunks = []
    current = []
    size = 0
    start_line = 1
    for index, line in enumerate(lines, start=1):
        if not current:
            start_line = index
        current.append(line)
        size += len(line) + 1
        if size >= CHUNK_CHARS:
            chunks.append((start_line, '\n'.join(current)))
            # Carry the last two lines into the next piece (so context is not cut).
            tail = current[-2:]
            current = list(tail)
            size = sum(len(t) + 1 for t in tail)
            start_line = max(1, index - len(tail) + 1)
    if current and any(t.strip() for t in current):
        chunks.append((start_line, '\n'.join(current)))
    return [(line, chunk.strip()) for line, chunk in chunks if chunk.strip()]


def _signature(files):
    parts = []
    for path, mtime, size in files:
        parts.append('%s|%d|%d' % (path, int(mtime), size))
    return hashlib.sha256('\n'.join(parts).encode('utf-8', 'replace')).hexdigest()


def build_index(root, store=None):
    files = []
    for path in _iter_files(root):
        if len(files) >= MAX_FILES:
            break
        try:
            stat = os.stat(path)
        except OSError:
            continue
        files.append((path, stat.st_mtime, stat.st_size))

    signature = _signature(files)
    cached = _load_store(store)
    if cached and cached.get('signature') == signature and cached.get('root') == root:
        return cached, True

    chunks = []
    for path, _mtime, _size in files:
        if len(chunks) >= MAX_CHUNKS:
            break
        text = _read_text(path)
        if not text:
            continue
        rel = os.path.relpath(path, root).replace('\\', '/')
        for line, body in _chunk_text(text):
            chunks.append({'path': rel, 'line': line, 'text': body})
            if len(chunks) >= MAX_CHUNKS:
                break

    index = {
        'root': root,
        'signature': signature,
        'built': int(time.time()),
        'files': len(files),
        'chunks': chunks,
    }
    if store:
        try:
            os.makedirs(os.path.dirname(store), exist_ok=True)
            tmp = store + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(index, fh, ensure_ascii=False)
            os.replace(tmp, store)
        except OSError:
            pass
    return index, False


def _load_store(store):
    if not store or not os.path.isfile(store):
        return None
    try:
        with open(store, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or 'chunks' not in data:
        return None
    return data


def search(root, query, top=6, store=None):
    index, reused = build_index(root, store)
    chunks = index.get('chunks') or []
    terms = tokenize(query)
    if not chunks:
        return {'root': root, 'files': index.get('files', 0), 'chunks': 0,
                'hits': [], 'message': 'No searchable text file in the folder.'}
    if not terms:
        return {'root': root, 'files': index.get('files', 0), 'chunks': len(chunks),
                'hits': [], 'message': 'Empty search term.'}

    # Tokens are computed once: the index stores text, the weights are derived at query
    # time. This keeps the index file small.
    docs = []
    df = {}
    for chunk in chunks:
        counts = {}
        for token in tokenize(chunk['text']):
            counts[token] = counts.get(token, 0) + 1
        docs.append(counts)
        for token in counts:
            df[token] = df.get(token, 0) + 1

    total = len(docs)
    avgdl = sum(sum(d.values()) for d in docs) / float(total)
    k1, b = 1.2, 0.75
    phrase = ' '.join(query.lower().split())
    scored = []
    for position, counts in enumerate(docs):
        length = sum(counts.values())
        score = 0.0
        for term in terms:
            freq = counts.get(term, 0)
            if not freq:
                continue
            idf = _idf(total, df.get(term, 0))
            score += idf * (freq * (k1 + 1)) / (
                freq + k1 * (1 - b + b * length / max(avgdl, 1e-9))
            )
        if score <= 0:
            continue
        text_lower = chunks[position]['text'].lower()
        if len(phrase) > 3 and phrase in text_lower:
            score *= 1.6
        scored.append((score, position))

    scored.sort(key=lambda item: item[0], reverse=True)
    hits = []
    for score, position in scored[:max(1, min(int(top or 6), 20))]:
        chunk = chunks[position]
        body = chunk['text']
        if len(body) > 1200:
            body = body[:1200] + '...'
        hits.append({
            'path': chunk['path'],
            'line': chunk['line'],
            'score': round(score, 3),
            'text': body,
        })
    return {
        'root': root,
        'files': index.get('files', 0),
        'chunks': total,
        'cached': reused,
        'hits': hits,
    }


def _idf(total, doc_freq):
    return 1.0 + (total - doc_freq + 0.5) / (doc_freq + 0.5)


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stdin.reconfigure(encoding='utf-8')
    except Exception:
        pass
    try:
        request = json.loads(sys.stdin.read() or '{}')
    except ValueError:
        print(json.dumps({'error': 'invalid JSON request'}, ensure_ascii=False))
        return

    root = request.get('root') or ''
    if not root or not os.path.isdir(root):
        print(json.dumps({'error': 'folder not found: %s' % root}, ensure_ascii=False))
        return
    store = request.get('store') or None
    action = request.get('action') or 'search'

    if action == 'index':
        index, reused = build_index(root, store)
        print(json.dumps({'root': root, 'files': index.get('files', 0),
                          'chunks': len(index.get('chunks') or []),
                          'cached': reused}, ensure_ascii=False))
        return
    if action == 'search':
        query = (request.get('query') or '').strip()
        print(json.dumps(search(root, query, request.get('top') or 6, store),
                         ensure_ascii=False))
        return
    print(json.dumps({'error': 'unknown action: %s' % action}, ensure_ascii=False))


if __name__ == '__main__':
    main()
