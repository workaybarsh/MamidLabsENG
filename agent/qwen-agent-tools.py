import json
import ipaddress
import re
import socket
import sys
import threading
import urllib.request
from urllib.parse import urlparse


MAX_PAGE_BYTES = 3_000_000
MAX_REFERENCE_BYTES = 20 * 1024 * 1024
MAX_REFERENCE_PIXELS = 40_000_000

# On a slow DNS, socket.getaddrinfo can block; resolution runs in a separate
# thread bounded with join(timeout). If that time is exceeded, _pin_resolution
# raises OSError; the caller turns it into the existing clean error-JSON flow
# (no exception leaks out).
DNS_TIMEOUT_SECONDS = 5
# ddgs tries several search-engine backends internally, so the total time of
# search_web could be backend x timeout (measured: ~60 s). A single overall
# upper bound (deadline) is applied; when exceeded, the remaining backends are
# not awaited, collected results are returned if any, otherwise clean error JSON.
SEARCH_DEADLINE_SECONDS = 25

# DNS rebinding protection: _validate_remote_url resolved and validated the host once,
# but the real connection (urllib/http.client) resolved it again with a separate DNS
# query. A malicious DNS record with a low TTL could exploit the gap between these two
# resolutions (TOCTOU) by returning a public IP at validation time and a private/local IP
# at the connection right after. Fix: while validating each host, record exactly which
# addresses it resolved to, and pin socket.getaddrinfo for that host for the lifetime of
# this script; that way the real connection is forced to use the same, already validated
# address. Since this script is a one-shot process that handles a single request and exits
# (main() reads a single JSON request directly), a process-wide monkeypatch is safe;
# it does not race with a separate request/thread.
_pinned_addresses: dict = {}
_real_getaddrinfo = socket.getaddrinfo


def _pin_resolution(host, port):
    # The resolution runs in a separate thread bounded with join(timeout), so a
    # slow/stuck DNS query cannot block. The chosen addresses (pinning) are
    # unchanged; only a time limit is added.
    outcome = {}

    def _resolve():
        try:
            outcome['results'] = _real_getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except Exception as exc:  # passed on to the caller as-is
            outcome['error'] = exc

    worker = threading.Thread(target=_resolve, daemon=True)
    worker.start()
    worker.join(DNS_TIMEOUT_SECONDS)
    if worker.is_alive():
        raise OSError('DNS resolution timed out.')
    if 'error' in outcome:
        raise outcome['error']
    results = outcome['results']
    _pinned_addresses[host] = results
    return [ipaddress.ip_address(item[4][0]) for item in results]


def _patched_getaddrinfo(host, *args, **kwargs):
    pinned = _pinned_addresses.get(host)
    return pinned if pinned is not None else _real_getaddrinfo(host, *args, **kwargs)


socket.getaddrinfo = _patched_getaddrinfo


def _validate_remote_url(value):
    """Reject local/reserved destinations before urllib can connect to them."""
    parsed = urlparse(str(value))
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('Only valid http/https addresses can be read.')
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError('The URL cannot contain credentials or a fragment.')
    if parsed.port not in (None, 80, 443):
        raise ValueError('Only ports 80 and 443 can be used.')

    host = parsed.hostname.rstrip('.').lower()
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = _pin_resolution(host, port)
        except (OSError, ValueError):
            raise ValueError('The URL host could not be resolved.')
    if not addresses or any(not address.is_global for address in addresses):
        _pinned_addresses.pop(host, None)
        raise ValueError('Access to local, private or reserved network addresses is blocked.')
    return parsed.geturl()


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Pins the redirected host the same way; that way every step in the redirect
        # chain connects to its own validated address.
        _validate_remote_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_remote_url(url, headers, timeout):
    safe_url = _validate_remote_url(url)
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    return opener.open(urllib.request.Request(safe_url, headers=headers), timeout=timeout)


def _read_limited(response, limit):
    chunks = []
    total = 0
    while True:
        chunk = response.read(min(1024 * 1024, limit - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ValueError('The remote response exceeded the size limit.')
        chunks.append(chunk)
    return b''.join(chunks)


def search_web(request):
    from ddgs import DDGS
    query = str(request.get('query', ''))[:500]
    count = max(1, min(int(request.get('count', 5)), 8))
    collected = []
    failure = {}

    def _search():
        try:
            # An unreachable search engine must not keep the job waiting for minutes.
            # The backend evaluation tries engines sequentially/in parallel, so the
            # overall time is bounded by the deadline on the outside.
            for item in DDGS(timeout=15).text(query, max_results=count):
                collected.append(item)
        except Exception as exc:
            failure['error'] = exc

    worker = threading.Thread(target=_search, daemon=True)
    worker.start()
    worker.join(SEARCH_DEADLINE_SECONDS)
    if worker.is_alive():
        # The overall time limit was exceeded: stop waiting for the remaining
        # backends. Return collected results if any, otherwise valid error JSON.
        if collected:
            return json.dumps(collected, ensure_ascii=False)
        return json.dumps({'error': 'Web search timed out: no results.'}, ensure_ascii=False)
    if 'error' in failure:
        return json.dumps({'error': f'Web search failed: {failure["error"]}'}, ensure_ascii=False)
    return json.dumps(collected, ensure_ascii=False)


def fetch_page(request):
    url = str(request.get('url', ''))
    try:
        safe_url = _validate_remote_url(url)
        with _open_remote_url(safe_url, {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) QwenLocal/1.0'}, 25) as response:
            charset = response.headers.get_content_charset() or 'utf-8'
            raw = _read_limited(response, MAX_PAGE_BYTES).decode(charset, errors='replace')
    except Exception as exc:
        return json.dumps({'error': str(exc)}, ensure_ascii=False)
    raw = re.sub(r'(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>', ' ', raw)
    raw = re.sub(r'(?s)<!--.*?-->', ' ', raw)
    text = re.sub(r'(?s)<[^>]+>', ' ', raw)
    text = text.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&quot;', '"').replace('&#39;', "'")
    text = re.sub(r'[ \t\r\f\v]+', ' ', text)
    text = re.sub(r'\n\s*\n\s*', '\n', text).strip()
    if len(text) > 20000:
        text = text[:20000] + '\n[page truncated at 20K characters]'
    return json.dumps({'url': url, 'text': text}, ensure_ascii=False)


def find_reference_image(request):
    """Searches the web for images, downloads the first suitable one and returns its local path."""
    import os
    import urllib.request
    from ddgs import DDGS

    query = str(request.get('query', ''))[:300]
    # The model only needs the application-owned reference directory. Never let
    # a tool argument turn this downloader into an arbitrary file writer.
    # Derive the install root from this file's own location: this file always lives under <root>/agent/.
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(root, 'outputs', 'refs')
    os.makedirs(out_dir, exist_ok=True)

    try:
        # Image search must also use an explicit timeout like search_web;
        # otherwise it depends on the ddgs default and can stall the job.
        results = list(DDGS(timeout=15).images(query, max_results=8))
    except Exception as exc:
        return json.dumps({'error': 'image search failed: %s' % exc}, ensure_ascii=False)

    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) QwenLocal/1.0'}
    for item in results:
        url = item.get('image') or item.get('thumbnail')
        if not url:
            continue
        try:
            _validate_remote_url(url)
            with _open_remote_url(url, headers, 20) as response:
                ctype = (response.headers.get('Content-Type') or '').lower()
                if 'image' not in ctype:
                    continue
                data = _read_limited(response, MAX_REFERENCE_BYTES)
        except Exception:
            continue

        ext = '.png' if 'png' in ctype else ('.webp' if 'webp' in ctype else '.jpg')
        safe = re.sub(r'[^a-zA-Z0-9]+', '-', query)[:40].strip('-') or 'ref'
        path = os.path.join(out_dir, 'ref-%s-%d%s' % (safe, abs(hash(url)) % 100000, ext))
        try:
            with open(path, 'wb') as handle:
                handle.write(data)
            from PIL import Image
            Image.MAX_IMAGE_PIXELS = MAX_REFERENCE_PIXELS
            with Image.open(path) as img:
                img.verify()
            with Image.open(path) as img:
                if img.width * img.height > MAX_REFERENCE_PIXELS:
                    raise ValueError('The image exceeded the pixel limit.')
                width, height = img.size
                if width < 256 or height < 256:
                    os.remove(path)
                    continue
                if ext != '.png':
                    png_path = os.path.splitext(path)[0] + '.png'
                    Image.open(path).convert('RGB').save(png_path)
                    os.remove(path)
                    path = png_path
        except Exception:
            try:
                os.remove(path)
            except Exception:
                pass
            continue

        return json.dumps({
            'path': path, 'width': width, 'height': height,
            'source': item.get('url') or url, 'title': item.get('title', ''),
        }, ensure_ascii=False)

    return json.dumps({'error': 'no suitable image found'}, ensure_ascii=False)


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stdin.reconfigure(encoding='utf-8')
    except Exception:
        pass
    request = json.loads(sys.stdin.read() or '{}')
    action = request.get('action')
    if action == 'search_web':
        print(search_web(request))
        return
    if action == 'fetch_page':
        print(fetch_page(request))
        return
    if action == 'find_reference_image':
        print(find_reference_image(request))
        return
    print(json.dumps({'error': 'unknown action'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
