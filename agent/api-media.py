"""MamidLabs cloud image/video generation. JSON on stdin; progress JSONL on stdout."""
import base64
import io
import json
import pathlib
import sys
import time
import uuid
from urllib.parse import urlparse, quote

import requests
from PIL import Image, ImageOps

# Derive the install root from this file's own location: this file always lives under <root>/agent/.
ROOT = pathlib.Path(__file__).resolve().parent.parent
MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_VIDEO_BYTES = 512 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 40_000_000

def emit(kind, text='', data=None):
    print(json.dumps(dict(kind=kind, text=text, usage=None, data=data), ensure_ascii=False), flush=True)

def check(response):
    if response.status_code >= 400:
        try:
            error = response.json().get('error', {})
            message = error.get('message', str(error)) if isinstance(error, dict) else str(error)
        except ValueError:
            message = response.reason
        raise RuntimeError(f'API {response.status_code}: {message[:600]}')
    return response

def normalized_image(path, size=None):
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert('RGB')
        if size:
            image = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
        else:
            image.thumbnail((2048, 2048))
        out = io.BytesIO()
        image.save(out, 'PNG')
        return out.getvalue()

def decode_image_payload(value):
    if not value or len(value) > (MAX_IMAGE_BYTES * 4 // 3 + 16):
        raise ValueError('The API image response exceeded the size limit.')
    try:
        pixels = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise ValueError('The API returned an invalid image response.')
    if len(pixels) > MAX_IMAGE_BYTES:
        raise ValueError('The API image response exceeded the size limit.')
    return pixels

def image_generation(job):
    provider, key, model = job['provider'], job['apiKey'], job['model']
    source = job.get('sourceImage')
    aspect = job.get('aspect', '1:1')
    emit('status', f'Generating image with the {provider} API…')
    if provider == 'openai':
        size = '1024x1536' if aspect in ('9:16', '3:4') else '1536x1024' if aspect in ('16:9', '4:3') else '1024x1024'
        body = dict(model=model, prompt=job['prompt'], size=size, n=1)
        headers = {'Authorization': f'Bearer {key}'}
        if source and job.get('mode') != 'new':
            response = requests.post('https://api.openai.com/v1/images/edits', headers=headers,
                data=body, files={'image': ('reference.png', normalized_image(source), 'image/png')}, timeout=(30, 900))
        else:
            response = requests.post('https://api.openai.com/v1/images/generations', headers=headers, json=body, timeout=(30, 900))
        data = check(response).json()['data'][0]
        if not data.get('b64_json'):
            raise RuntimeError('The selected image model did not return base64 output. Choose a GPT Image model.')
        pixels = decode_image_payload(data['b64_json'])
    elif provider == 'gemini':
        parts = [{'text': job['prompt']}]
        if source and job.get('mode') != 'new':
            parts.append({'inlineData': {'mimeType': 'image/png', 'data': base64.b64encode(normalized_image(source)).decode()}})
        body = {'contents': [{'role': 'user', 'parts': parts}], 'generationConfig': {
            'responseModalities': ['TEXT', 'IMAGE'], 'imageConfig': {'aspectRatio': aspect}}}
        response = requests.post(f'https://generativelanguage.googleapis.com/v1beta/models/{quote(model, safe="")}:generateContent',
            headers={'x-goog-api-key': key}, json=body, timeout=(30, 900))
        result = check(response).json()
        outputs = result.get('candidates', [{}])[0].get('content', {}).get('parts', [])
        data = next((p.get('inlineData') or p.get('inline_data') for p in outputs if p.get('inlineData') or p.get('inline_data')), None)
        if not data:
            raise RuntimeError('The API did not generate an image: ' + ' '.join(p.get('text', '') for p in outputs)[:300])
        pixels = decode_image_payload(data['data'])
    else:
        raise ValueError('This provider does not support image generation.')
    directory = ROOT / 'outputs' / 'images'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'mamidlabs-{uuid.uuid4().hex[:12]}.png'
    with Image.open(io.BytesIO(pixels)) as image:
        image.save(path, 'PNG')
        width, height = image.size
    emit('image', str(path), dict(width=width, height=height, prompt=job['prompt'], mode=job.get('mode', 'new'), provider=provider))
    emit('status', 'Image ready.')

def video_generation(job):
    provider, key, model = job['provider'], job['apiKey'], job['model']
    if job.get('mode') not in ('text', 'image'):
        raise ValueError('API providers only support video generation from text or from an image.')
    aspect = job.get('aspect', '16:9')
    if aspect not in ('16:9', '9:16'):
        raise ValueError('Choose 16:9 or 9:16 for the video API.')
    source = job.get('sourceFile') if job.get('mode') == 'image' else None
    headers = {'Authorization': f'Bearer {key}'}
    prompt = job['prompt']
    seconds = float(job.get('seconds') or 4)
    if provider == 'openai':
        base = 'https://api.openai.com/v1'
        seconds = next((s for s in (4, 8, 12) if s >= seconds), 12)
        size = (720, 1280) if aspect == '9:16' else (1280, 720)
        body = dict(model=model, prompt=prompt, seconds=str(seconds), size=f'{size[0]}x{size[1]}')
        files = {'input_reference': ('reference.png', normalized_image(source, size), 'image/png')} if source else None
        multipart = {name: (None, value) for name, value in body.items()}
        multipart.update(files or {})
        response = requests.post(base + '/videos', headers=headers, files=multipart, timeout=(30, 180))
    elif provider == 'gemini':
        base = 'https://generativelanguage.googleapis.com/v1beta/openai'
        seconds = next((s for s in (4, 6, 8) if s >= seconds), 8)
        options = dict(duration_seconds=seconds, aspect_ratio=aspect)
        if source:
            options['image'] = base64.b64encode(normalized_image(source)).decode()
        response = requests.post(base + '/videos', headers=headers,
            json=dict(model=model, prompt=prompt, extra_body={'google': options}), timeout=(30, 180))
    else:
        raise ValueError('This provider does not support video generation.')
    video = check(response).json()
    video_id = video.get('id')
    if not video_id:
        raise RuntimeError('The API did not return a video job id.')
    # Saving only the public job ID makes a timeout diagnosable without logging credentials.
    emit('status', f'{provider} video job created: {video_id} · {seconds} s')
    deadline = time.monotonic() + 1800
    while video.get('status') not in ('completed', 'failed'):
        if time.monotonic() > deadline:
            raise TimeoutError(f'The video job is still running: {video_id}. Check it in your account; it was not recreated automatically.')
        time.sleep(5)
        video = check(requests.get(base + '/videos/' + quote(video_id, safe=''), headers=headers, timeout=(30, 60))).json()
        emit('status', f'Generating video · {video.get("progress", 0)}% · {video_id}')
    if video['status'] == 'failed':
        raise RuntimeError('Video API generation failed: ' + str(video.get('error', 'Unknown reason')))
    if provider == 'openai':
        url = base + '/videos/' + quote(video_id, safe='') + '/content'
        download_headers = headers
    else:
        url = video.get('url', '')
        parsed = urlparse(url)
        if parsed.scheme != 'https' or not any(parsed.hostname == h or (parsed.hostname or '').endswith('.' + h) for h in ('googleapis.com', 'googleusercontent.com')):
            raise ValueError('The API did not return the expected Google video download address.')
        download_headers = {'x-goog-api-key': key} if (parsed.hostname or '').endswith('googleapis.com') else {}
    directory = ROOT / 'outputs' / 'videos'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'mamidlabs-{uuid.uuid4().hex[:12]}.mp4'
    try:
        with check(requests.get(url, headers=download_headers, stream=True, timeout=(30, 180))) as response:
            with path.open('wb') as output:
                total = 0
                for chunk in response.iter_content(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_VIDEO_BYTES:
                        raise ValueError('The API video response exceeded the size limit.')
                    output.write(chunk)
    except Exception:
        if path.exists():
            path.unlink()
        raise
    emit('video', str(path), dict(prompt=prompt, seconds=seconds, mode=job.get('mode'), provider=provider, thumbnail=''))
    emit('status', 'Video ready.')

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stdin.reconfigure(encoding='utf-8')
    job = json.load(sys.stdin)
    try:
        if not job.get('apiKey') or not job.get('model'):
            raise ValueError('Enter the API key and model name of the generation provider in the settings.')
        (image_generation if job['kind'] == 'image' else video_generation)(job)
    except Exception as exc:
        # Provider/network exceptions must never echo credentials.
        message = str(exc).replace(job.get('apiKey', '') or '\0', '[secret]')
        emit('error', message[:900])

if __name__ == '__main__': main()
