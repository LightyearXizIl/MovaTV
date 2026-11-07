#!/usr/bin/env python3
"""Fill missing release assets, preserving the original component bytes."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

REPOSITORY = 'LightyearXizIl/MovaTV'
TAG = 'depth-runtime-v1'
BASE = f'https://github.com/{REPOSITORY}/releases/download/{TAG}/'
UPSTREAM = 'https://github.com/glanderness/BeefTV/releases/download/depth-runtime-mirrors-v1/'


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme != 'https':
            raise ValueError('Only HTTPS redirects are permitted')
        result = super().redirect_request(req, fp, code, msg, headers, newurl)
        if result and urllib.parse.urlparse(newurl).netloc != urllib.parse.urlparse(req.full_url).netloc:
            result.remove_header('Authorization')
        return result


OPENER = urllib.request.build_opener(SafeRedirect())


def api(path, data=None, method='GET', upload=None):
    url = path if path.startswith('https://') else 'https://api.github.com' + path
    if urllib.parse.urlparse(url).hostname not in {'api.github.com', 'uploads.github.com'}:
        raise ValueError('Unexpected GitHub API host')
    headers = {'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json', 'User-Agent': 'MovaTV-component-mirror'}
    body = None
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    if upload:
        body = upload.open('rb')
        headers['Content-Type'] = 'application/octet-stream'
        headers['Content-Length'] = str(upload.stat().st_size)
    try:
        with OPENER.open(urllib.request.Request(url, data=body, headers=headers, method=method), timeout=1800) as response:
            return None if response.status == 204 else json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404 and method == 'GET':
            return None
        raise RuntimeError(f'GitHub API {method} failed: HTTP {error.code}') from None
    finally:
        if upload and body:
            body.close()


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


def read_manifest(url, path):
    subprocess.run(['curl', '--fail', '--location', '--retry', '3', '--silent', '--show-error', '--max-time', '120', '--output', str(path), url], check=True)
    raw = json.loads(path.read_bytes())
    return json.loads(base64.b64decode(raw['payload'], validate=True)) if 'payload' in raw else raw


def make_plan(mac, windows):
    records = {}

    def artifact(original):
        copied = dict(original)
        if original.get('parts'):
            copied['parts'] = [artifact(part) for part in original['parts']]
            copied['urls'] = []
            return copied
        urls = original.get('urls', [])
        if not urls or not re.fullmatch(r'[a-fA-F0-9]{64}', original['sha256']) or original['size'] <= 0:
            raise ValueError('Invalid upstream artifact')
        name = Path(urllib.parse.urlparse(urls[0]).path).name.replace('beeftv-', 'movatv-', 1)
        if not re.fullmatch(r'(movatv-depth-runtime-v1-(darwin-arm64|windows-amd64-(cpu|cuda))\.zip(\.part-\d{3})?|video_depth_anything_vits\.pth)', name):
            raise ValueError('Unexpected component file name')
        for url in urls:
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme != 'https' or parsed.username or parsed.password:
                raise ValueError('Component sources must use HTTPS')
        record = {'name': name, 'size': original['size'], 'sha256': original['sha256'].lower(), 'sources': urls}
        if name in records and (records[name]['size'], records[name]['sha256']) != (record['size'], record['sha256']):
            raise ValueError('Conflicting component metadata')
        records[name] = record
        copied['urls'] = [BASE + name]
        return copied

    if mac.get('version') != 1 or windows.get('version') != 2:
        raise ValueError('Unexpected component manifest version')
    mac = {'version': 1, 'runtime': artifact(mac['runtime']), 'model': artifact(mac['model'])}
    windows = {'version': 2, 'runtimes': {key: artifact(value) for key, value in windows['runtimes'].items()}, 'model': artifact(windows['model'])}
    if set(windows['runtimes']) != {'windows-amd64/cpu', 'windows-amd64/cuda'}:
        raise ValueError('Windows CPU and CUDA are both required')
    return mac, windows, records


def verified_remote(asset, record):
    return asset and asset.get('state') == 'uploaded' and asset['size'] == record['size'] and asset.get('digest') == 'sha256:' + record['sha256']


def download(record, output):
    path = output / record['name']
    if path.is_file() and path.stat().st_size == record['size'] and digest(path) == record['sha256']:
        return path
    sources = sorted(record['sources'], key=lambda url: urllib.parse.urlparse(url).hostname != 'github.com')
    for source in sources:
        print('Downloading ' + record['name'], flush=True)
        result = subprocess.run(['curl', '--fail', '--location', '--retry', '3', '--retry-all-errors', '--silent', '--show-error', '--connect-timeout', '30', '--max-time', '1800', '--output', str(path), source])
        if result.returncode == 0 and path.stat().st_size == record['size'] and digest(path) == record['sha256']:
            return path
    raise RuntimeError('Component download/hash verification failed: ' + record['name'])


def main():
    if os.environ.get('GITHUB_REPOSITORY', REPOSITORY) != REPOSITORY:
        raise ValueError('This mirror publishes only to the MovaTV repository')
    output = Path('dist/depth-components')
    output.mkdir(parents=True, exist_ok=True)
    mac = read_manifest(UPSTREAM + 'depth-runtime-manifest.json', output / 'upstream-mac.json')
    windows = read_manifest(UPSTREAM + 'depth-runtime-manifest.v2.json', output / 'upstream-windows.json')
    mac, windows, records = make_plan(mac, windows)
    prefix = '/repos/' + REPOSITORY
    release = api(prefix + '/releases/tags/' + TAG)
    if not release:
        release = api(prefix + '/releases', {'tag_name': TAG, 'target_commitish': os.environ.get('GITHUB_SHA', 'main'), 'name': 'MovaTV 深度捕捉组件 v1', 'body': '本仓库托管的 CPU、CUDA、Apple Silicon 运行包与模型。客户端读取无签名 JSON 清单并校验文件大小和 SHA-256。第三方组件的来源与许可见 COMPONENTS.md。', 'draft': True, 'prerelease': True, 'make_latest': 'false'}, 'POST')
    existing = {asset['name']: asset for asset in release['assets']}

    def ensure(record):
        current = existing.get(record['name'])
        if current:
            if current.get('state') != 'uploaded':
                api(prefix + '/releases/assets/' + str(current['id']), method='DELETE')
                current = None
            elif not verified_remote(current, record):
                raise RuntimeError('Existing release asset differs; refusing to overwrite: ' + record['name'])
            else:
                print('Verified existing ' + record['name'], flush=True)
                return
        path = download(record, output)
        url = release['upload_url'].split('{', 1)[0] + '?' + urllib.parse.urlencode({'name': path.name})
        print('Uploading ' + path.name, flush=True)
        uploaded = api(url, method='POST', upload=path)
        if not verified_remote(uploaded, record):
            raise RuntimeError('Uploaded asset verification failed: ' + path.name)
        print('Verified uploaded ' + path.name, flush=True)

    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(ensure, records.values()))

    cuda = windows['runtimes']['windows-amd64/cuda']
    parts = [output / Path(urllib.parse.urlparse(part['urls'][0]).path).name for part in cuda.get('parts', [])]
    if parts and all(path.is_file() for path in parts):
        full_hash = hashlib.sha256()
        total = 0
        for path in parts:
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    total += len(chunk)
                    full_hash.update(chunk)
        if total != cuda['size'] or full_hash.hexdigest() != cuda['sha256']:
            raise RuntimeError('Combined CUDA archive verification failed')
        print('Verified complete CUDA archive digest', flush=True)

    metadata = {'depth-runtime-manifest.json': canonical(mac), 'depth-runtime-manifest.v2.json': canonical(windows)}
    metadata['COMPONENTS.md'] = ('# MovaTV 深度捕捉组件\n\n组件由本仓库托管，下载清单仅引用本仓库的 Release 文件。\n\n运行包与模型的字节保持不变，仅重命名发布资产。原始来源为 https://github.com/glanderness/BeefTV/releases ，模型来自 Video Depth Anything。运行包内原有许可证、署名和组件清单完整保留；该镜像不改变第三方许可，也不代表重新完成所有平台推理验收。\n').encode('utf-8')
    sums = {name: record['sha256'] for name, record in records.items()}
    sums.update({name: hashlib.sha256(body).hexdigest() for name, body in metadata.items()})
    metadata['SHA256SUMS.txt'] = ''.join(f'{value}  {name}\n' for name, value in sorted(sums.items())).encode('utf-8')
    for name, body in metadata.items():
        path = output / name
        path.write_bytes(body)
        ensure({'name': name, 'size': len(body), 'sha256': hashlib.sha256(body).hexdigest(), 'sources': []})
    final = api(prefix + '/releases/' + str(release['id']))
    required = set(records) | set(metadata)
    if required != {asset['name'] for asset in final['assets'] if asset['state'] == 'uploaded'}:
        raise RuntimeError('Release assets are incomplete or unexpected')
    if final['draft']:
        final = api(prefix + '/releases/' + str(final['id']), {'draft': False, 'prerelease': True, 'make_latest': 'false'}, 'PATCH')
    latest = api(prefix + '/releases/latest')
    if latest and latest['tag_name'] == TAG:
        raise RuntimeError('Component assets must not replace the desktop latest release')
    print(json.dumps({'release': final['html_url'], 'assets': len(required), 'desktopLatest': (latest or {}).get('tag_name')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
