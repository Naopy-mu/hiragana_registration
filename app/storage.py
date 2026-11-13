import os
from pathlib import Path
from urllib.parse import quote

import httpx


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

    def put(self, key, data, content_type):
        r = self.client.post(self.url(key), content=data, headers={'Content-Type': content_type, 'x-upsert': 'false'})
        r.raise_for_status()

    def get(self, key):
        r = self.client.get(self.url(key))
        r.raise_for_status()
        return r.content

    def delete(self, key):
        r = self.client.request('DELETE', self.base + '/object/' + quote(self.bucket, safe=''), json={'prefixes': [key]})
        r.raise_for_status()
