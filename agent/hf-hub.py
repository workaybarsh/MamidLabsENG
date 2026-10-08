"""Hugging Face access without writing a plaintext token into the Hub cache."""
import json
import os
import pathlib
import shutil
import sys

# The Hub/xet cache is written to the install root, not the user profile (~/.cache/huggingface);
# it is deleted together with the root on uninstall. Must be set before the import.
os.environ.setdefault('HF_HOME', str(pathlib.Path(__file__).resolve().parent.parent / 'tools' / 'hf-home'))
from huggingface_hub import HfApi, hf_hub_download
from mtp_inject import ensure_mtp, has_mtp

def emit(kind, text):
    print(json.dumps(dict(kind=kind, text=text), ensure_ascii=False), flush=True)

def safe_repo_path(name):
    # Converts Windows separators into a POSIX path inside the repository.
    name = pathlib.PureWindowsPath(str(name)).as_posix()
    parts = pathlib.PurePosixPath(name).parts
    if pathlib.PurePosixPath(name).is_absolute() or '..' in parts or ':' in name:
        raise ValueError('Enter a valid file path inside the repository.')
    return name

def prune_empty(folder, stop):
    """Removes the empty repository subfolders left behind after a download."""
    folder = pathlib.Path(folder)
    while folder != stop and folder.is_dir() and not any(folder.iterdir()):
        folder.rmdir()
        folder = folder.parent

def fetch(item, token, index, total):
    name = safe_repo_path(item['filename'])
    label = item.get('label') or pathlib.PurePosixPath(name).name
    target = pathlib.Path(item['target']).resolve()
    expected_bytes = int(item.get('bytes') or 0)
    mtp_cfg = item.get('mtp')
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not item.get('force'):
        # MTP injection grows the file, so the size check is skipped for an MTP-ready file.
        if mtp_cfg and has_mtp(str(target)):
            emit('status', f'[{index}/{total}] {label} already exists (MTP included), skipped.')
            emit('artifact', str(target))
            return
        if expected_bytes and target.stat().st_size != expected_bytes:
            raise ValueError(
                f'{label}: the file size on disk does not match the expected size '
                f'({target.stat().st_size} != {expected_bytes} bytes). It may be a partial/corrupt download; '
                'delete the file and download it again.')
        emit('status', f'[{index}/{total}] {label} already exists, skipped.')
        emit('artifact', str(target))
        return
    emit('status', f'[{index}/{total}] Downloading {label}…')
    # local_dir is the target folder itself; that way the file lands on the same disk and
    # the move completes without copying (important for 10 GB files).
    path = pathlib.Path(hf_hub_download(
        repo_id=item['repo'], filename=name, revision=item.get('revision') or 'main',
        local_dir=str(target.parent), token=token))
    if path != target:
        os.replace(path, target)
        prune_empty(path.parent, target.parent)
    if expected_bytes and target.stat().st_size != expected_bytes:
        actual = target.stat().st_size
        target.unlink(missing_ok=True)
        raise ValueError(
            f'{label}: the downloaded file size does not match the expected size ({actual} != {expected_bytes} bytes). '
            'The file was considered corrupt and deleted; download it again.')
    if mtp_cfg:
        # For models whose MTP head was dropped by the converter, it is added after download.
        ensure_mtp(str(target), mtp_cfg, token, lambda text: emit('status', f'{label}: {text}'))
    emit('artifact', str(target))

def run(job):
    token = job.get('apiKey') or os.environ.get('HF_TOKEN') or None
    if token:
        user = HfApi(token=token).whoami()
        emit('status', 'Hugging Face connection verified: ' + user.get('name', 'account'))
    else:
        # The repositories in the catalog are public; a token is only needed for gated repositories.
        emit('status', 'No token saved; only public repositories can be downloaded.')
    if job.get('action') != 'download':
        return
    files = job.get('files')
    if not files:
        # The old single-file format coming from the Hugging Face window.
        name = safe_repo_path(job['filename'])
        destination = pathlib.Path(job['destination']).resolve()
        files = [dict(repo=job['repo'], filename=name, revision=job.get('revision'),
                      target=str(destination / pathlib.PurePosixPath(name).name))]
    total = len(files)
    for index, item in enumerate(files, 1):
        fetch(item, token, index, total)
    emit('status', f'{total} files ready.')

if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stdin.reconfigure(encoding='utf-8')
    job = json.load(sys.stdin)
    try:
        run(job)
    except Exception as exc:
        message = str(exc).replace(job.get('apiKey') or '\0', '[secret]')
        emit('error', message[:800])
