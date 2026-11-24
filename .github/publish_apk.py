"""Verify an already signed AX12 APK and promote its release to the existing OTA URL."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

REPO = os.environ['GITHUB_REPOSITORY']
NAME = 'EGO-Pilot-AX12.apk'
SIGNER = '76c7ff6b7b1a94b0dbbb935f5b0eb4d69caea81b191d508d50e7bf4c4512ed97'
TAG = 'latest-apk'


def api(path, method='GET', data=None):
    cmd = ['gh', 'api', '--method', method, 'repos/' + REPO + '/' + path]
    if data is not None:
        cmd += ['--input', '-']
    response = subprocess.check_output(cmd, input=json.dumps(data).encode() if data is not None else None)
    return json.loads(response) if response else None


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def get_ota(release):
    match = re.search(r'<!-- ax12-ota (\{[^\n]*\}) -->', release['body'])
    if not match:
        raise ValueError('Current release has no update manifest')
    return json.loads(match[1])


def verify(apk, info):
    if apk.stat().st_size != info['bytes'] or digest(apk) != info['sha256']:
        raise ValueError('APK contents do not match the approved build')
    sdk = Path(os.environ['ANDROID_HOME']) / 'build-tools' / '34.0.0'
    certs = subprocess.check_output([sdk/'apksigner', 'verify', '--print-certs', apk], text=True)
    if re.findall(r'^Signer #\d+ certificate SHA-256 digest: ([0-9a-f]+)$', certs, re.M) != [SIGNER]:
        raise ValueError('APK is not signed with the AX12 update certificate')
    package = subprocess.check_output([sdk/'aapt2', 'dump', 'badging', apk], text=True)
    match = re.search(r"^package: name='([^']+)' versionCode='([0-9]+)' versionName='([^']+)'", package, re.M)
    if not match or (match[1], int(match[2]), match[3]) != ('com.radiomaster.egogcs', info['version_code'], info['version_name']):
        raise ValueError('Android package or manifest version mismatch')


def publish(apk, info, current, old):
    version = info['version_name']
    candidate_tag = 'candidate-' + str(info['version_code']) + '-' + info['sha256'][:12]
    body = 'AX12 地面站签名安装包。\n\nSHA-256 `' + info['sha256'] + '`\n\n<!-- ax12-ota ' + json.dumps(info, separators=(',', ':')) + ' -->'
    draft = next((r for r in api('releases?per_page=100') if r['tag_name'] == candidate_tag and r['draft']), None)
    if draft is None:
        draft = api('releases', 'POST', {'tag_name':candidate_tag, 'target_commitish':'main', 'draft':True, 'name':'EGO Pilot AX12 v' + version, 'body':body})
    subprocess.run(['gh', 'release', 'upload', candidate_tag, str(apk), '--clobber', '--repo', REPO], check=True)
    draft_path = 'releases/' + str(draft['id'])
    assets = api(draft_path)['assets']
    if len(assets) != 1 or (assets[0]['name'], assets[0]['size'], assets[0]['digest']) != (NAME, info['bytes'], 'sha256:' + info['sha256']):
        raise ValueError('Uploaded release asset failed verification')
    old_path = 'releases/' + str(current['id'])
    api(old_path, 'PATCH', {'tag_name':'v' + old['version_name'], 'make_latest':'false'})
    try:
        api(draft_path, 'PATCH', {'tag_name': TAG, 'draft':False, 'make_latest':'true', 'body':body})
    except subprocess.CalledProcessError:
        if api(draft_path)['draft']:
            api(old_path, 'PATCH', {'tag_name': TAG, 'make_latest':'true'})
        raise
    print('Published', version, info['sha256'])


def main(delivery, work):
    m = json.loads((delivery/'manifest.json').read_text())
    info = {key:m[key] for key in ('version_name','version_code','sha256','bytes')}
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+){2,3}', info['version_name']) or not re.fullmatch(r'[0-9a-f]{64}', info['sha256']):
        raise ValueError('Invalid release manifest')
    current = api('releases/tags/' + TAG)
    old = get_ota(current)
    if info['version_code'] < old['version_code'] or info['sha256'] == old['sha256']:
        print('The OTA channel already has this or a newer build')
        return
    if info['version_code'] == old['version_code']:
        raise ValueError('Refusing to reuse an Android versionCode for different content')
    apk = work/NAME
    if (delivery/NAME).is_file():
        shutil.copyfile(delivery/NAME, apk)
    else:
        base = work/'base.apk'
        with base.open('wb') as stream:
            subprocess.run(['gh','api','-H','Accept: application/octet-stream','repos/'+REPO+'/releases/assets/'+str(int(m['base_asset']))], stdout=stream, check=True)
        if digest(base) != m['base_sha256']:
            raise ValueError('The previous APK does not match the binary update')
        subprocess.run(['xdelta3','-d','-s',base,delivery/'update.vcdiff',apk], check=True)
    verify(apk, info)
    publish(apk, info, current, old)


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as scratch:
        main(Path(sys.argv[1]).resolve(), Path(scratch))
