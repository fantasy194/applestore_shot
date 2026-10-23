"""Fetch a large build dependency in bounded, verified HTTP byte ranges."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from urllib.request import Request, urlopen


def download(url, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urlopen(Request(url, method='HEAD'), timeout=60) as response:
        size = int(response.headers['Content-Length'])
        version = response.headers.get('ETag') or response.headers.get('Last-Modified')
    if not version or size <= 0:
        raise ValueError('Build download did not supply a size and version')
    identity = {'url': url, 'size': size, 'version': version}
    metadata = destination.with_name(destination.name + '.download.json')
    if destination.is_file() and destination.stat().st_size == size and metadata.is_file():
        if json.loads(metadata.read_text()) == identity:
            print(f'Using cached {destination.name} ({size} bytes)', flush=True)
            return
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]
    parts = destination.parent / ('.download-' + key)
    parts.mkdir(exist_ok=True)
    chunk = 1024 * 1024
    ranges = [(start, min(start + chunk, size) - 1) for start in range(0, size, chunk)]

    def fetch(bounds):
        start, end = bounds
        path = parts / str(start)
        length = end - start + 1
        if path.is_file() and path.stat().st_size == length:
            return path
        for attempt in range(5):
            try:
                request = Request(url, headers={'Range': f'bytes={start}-{end}', 'If-Range': version})
                with urlopen(request, timeout=60) as response:
                    if response.status != 206 or response.headers.get('Content-Range') != f'bytes {start}-{end}/{size}':
                        raise ValueError('Build download range/version did not match')
                    data = response.read(length + 1)
                if len(data) != length:
                    raise ValueError('Build download range was truncated')
                temporary = path.with_suffix('.tmp')
                temporary.write_bytes(data)
                os.replace(temporary, path)
                return path
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(attempt + 1)

    print(f'Downloading {destination.name}: {size} bytes in {len(ranges)} ranges', flush=True)
    with ThreadPoolExecutor(max_workers=4) as executor:
        paths = list(executor.map(fetch, ranges))
    temporary = destination.with_name(destination.name + '.tmp')
    digest = hashlib.sha256()
    with temporary.open('wb') as output:
        for path in paths:
            data = path.read_bytes()
            digest.update(data)
            output.write(data)
    if temporary.stat().st_size != size:
        raise ValueError('Build download total size did not match')
    os.replace(temporary, destination)
    metadata.write_text(json.dumps(identity), encoding='utf-8')
    for path in paths:
        path.unlink()
    parts.rmdir()
    print(f'Downloaded {destination.name}: sha256={digest.hexdigest()}', flush=True)


if __name__ == '__main__':
    download(sys.argv[1], sys.argv[2])
