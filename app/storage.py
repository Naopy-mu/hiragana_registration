import os
import hashlib
import io
import json
import re
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

import httpx


PDF_PART_SIZE = 6 * 1024 * 1024
PDF_PART_THRESHOLD = 16 * 1024 * 1024
STORAGE_WORKERS = 4
PDF_PART_FORMAT = 'handwriting-pdf-parts-v1'
PDF_PART_PREFIX = b'{"format":"handwriting-pdf-parts-v1",'
PDF_PART_CONTENT_TYPE = 'application/vnd.handwriting.pdf-parts+json'


def ordered_parallel(function, items):
    """Yield in input order with at most four requests retained or in flight."""
    iterator = iter(items)
    with ThreadPoolExecutor(max_workers=STORAGE_WORKERS) as executor:
        pending = deque()
        for _ in range(STORAGE_WORKERS):
            try:
                pending.append(executor.submit(function, next(iterator)))
            except StopIteration:
                break
        while pending:
            yield pending.popleft().result()
            try:
                pending.append(executor.submit(function, next(iterator)))
            except StopIteration:
                pass


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root / 'private'
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key):
        target = (self.root / key).resolve()
        if self.root.resolve() not in target.parents:
            raise ValueError('Invalid storage key')
        return target

    def put(self, key, data, content_type):
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + '.tmp')
        try:
            with open(temp, 'xb') as f:
                f.write(data)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    def get(self, key):
        return self.path(key).read_bytes()

    def delete(self, key):
        self.path(key).unlink(missing_ok=True)


class SupabaseStorage:
    def __init__(self, settings):
        self.base = settings.supabase_url + '/storage/v1'
        self.bucket = settings.storage_bucket
        self.headers = {'apikey': settings.supabase_key}
        if not settings.supabase_key.startswith('sb_secret_'):
            self.headers['Authorization'] = 'Bearer ' + settings.supabase_key
        self.client = httpx.Client(timeout=90, headers=self.headers)

    def verify_private_bucket(self):
        r = self.client.get(self.base + '/bucket/' + quote(self.bucket, safe=''))
        r.raise_for_status()
        if r.json().get('public') is not False:
            raise ValueError('Storage bucket must be private')

    def url(self, key):
        return self.base + '/object/' + quote(self.bucket, safe='') + '/' + quote(key, safe='/')

    def _put_object(self, key, data, content_type):
        r = self.client.post(self.url(key), content=data, headers={'Content-Type': content_type, 'x-upsert': 'false'})
        r.raise_for_status()

    def put(self, key, data, content_type):
        if content_type != 'application/pdf' or key.startswith('study-worksheets/') or len(data) <= PDF_PART_THRESHOLD:
            self._put_object(key, data, content_type)
            return
        upload_id = uuid.uuid4().hex
        parts = []
        for index, offset in enumerate(range(0, len(data), PDF_PART_SIZE)):
            chunk = data[offset:offset + PDF_PART_SIZE]
            parts.append({'key': f'{key}.parts/{upload_id}/{index:06d}',
                          'size': len(chunk), 'sha256': hashlib.sha256(chunk).hexdigest()})

        def upload_part(index):
            offset = index * PDF_PART_SIZE
            self._put_object(parts[index]['key'], data[offset:offset + PDF_PART_SIZE], 'application/octet-stream')

        for _ in ordered_parallel(upload_part, range(len(parts))):
            pass
        manifest = {'format': PDF_PART_FORMAT, 'upload_id': upload_id,
                    'content_type': content_type, 'size': len(data),
                    'sha256': hashlib.sha256(data).hexdigest(), 'parts': parts}
        # Publish the index only after every part succeeds. Keep orphan parts
        # on uncertain replies: deleting them could destroy a published PDF.
        self._put_object(key, json.dumps(manifest, separators=(',', ':')).encode(), PDF_PART_CONTENT_TYPE)

    def _get_object(self, key):
        r = self.client.get(self.url(key))
        r.raise_for_status()
        return r.content

    def _manifest(self, key, data):
        if not data.startswith(PDF_PART_PREFIX):
            return None
        try:
            manifest = json.loads(data)
            upload_id = manifest['upload_id']
            parts = manifest['parts']
            digest = manifest['sha256']
            size = manifest['size']
            if (manifest['format'] != PDF_PART_FORMAT or manifest['content_type'] != 'application/pdf'
                    or not isinstance(upload_id, str) or not re.fullmatch('[0-9a-f]{32}', upload_id)
                    or not isinstance(parts, list) or not 1 <= len(parts) <= 10000
                    or type(size) is not int or size <= 0
                    or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)):
                raise ValueError
            total = 0
            for index, part in enumerate(parts):
                if (part['key'] != f'{key}.parts/{upload_id}/{index:06d}'
                        or type(part['size']) is not int or not 1 <= part['size'] <= PDF_PART_SIZE
                        or not isinstance(part['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', part['sha256'])):
                    raise ValueError
                total += part['size']
            if total != size:
                raise ValueError
            return manifest
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError('Invalid private PDF part index') from error

    def get(self, key):
        data = self._get_object(key)
        manifest = self._manifest(key, data)
        if manifest is None:
            return data

        def read_part(part):
            chunk = self._get_object(part['key'])
            if len(chunk) != part['size'] or hashlib.sha256(chunk).hexdigest() != part['sha256']:
                raise ValueError('Private PDF part is incomplete or corrupted')
            return chunk

        output = io.BytesIO()
        digest = hashlib.sha256()
        for chunk in ordered_parallel(read_part, manifest['parts']):
            output.write(chunk)
            digest.update(chunk)
        if digest.hexdigest() != manifest['sha256']:
            raise ValueError('Private PDF checksum does not match')
        return output.getvalue()

    def iter_get(self, keys):
        return ordered_parallel(self.get, keys)

    def _delete_objects(self, keys):
        r = self.client.request('DELETE', self.base + '/object/' + quote(self.bucket, safe=''),
                                json={'prefixes': keys}, timeout=10)
        if r.status_code != 404:
            r.raise_for_status()

    def _manifest_for_delete(self, key):
        # An old ordinary PDF can be tens of megabytes. Read only its prefix
        # to distinguish the index, then close without downloading the PDF.
        with self.client.stream('GET', self.url(key), timeout=10) as response:
            response.raise_for_status()
            chunks = response.iter_bytes(chunk_size=4096)
            first = next(chunks, b'')
            if not first.startswith(PDF_PART_PREFIX):
                return None
            return self._manifest(key, first + b''.join(chunks))

    def delete(self, key):
        # PDFs can be an index after a restart, so inspect their small root
        # object before deleting. Image cleanup retains its direct request.
        if key.lower().endswith('.pdf'):
            try:
                manifest = self._manifest_for_delete(key)
            except httpx.HTTPStatusError as error:
                if error.response.status_code != 404:
                    raise
                manifest = None
            if manifest:
                # Retain the index until every part deletion is acknowledged,
                # making an interrupted cleanup safe to retry.
                self._delete_objects([part['key'] for part in manifest['parts']])
        self._delete_objects([key])
