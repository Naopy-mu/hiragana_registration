"""HTTP contract tests only: no live Supabase credentials or service contacted."""
import json
import threading
import time

import httpx
import pytest

from app.settings import Settings
from app.storage import LocalStorage, SupabaseStorage, PDF_PART_CONTENT_TYPE


def remote_storage(key, handler):
    storage=SupabaseStorage(Settings(supabase_url="https://example.supabase.co",
                                     supabase_key=key,storage_bucket="handwriting-private"))
    storage.client.close()
    storage.client=httpx.Client(transport=httpx.MockTransport(handler),headers=storage.headers)
    return storage


@pytest.mark.parametrize("key,bearer",[("sb_secret_example-server-only",False),
                                      ("legacy-service-role-jwt",True)])
def test_private_storage_http_contract(key,bearer):
    requests=[]
    def handler(request):
        requests.append(request)
        assert request.headers["apikey"]==key
        if bearer:
            assert request.headers["authorization"]==f"Bearer {key}"
        else:
            assert "authorization" not in request.headers
        assert not request.url.query
        assert "/object/public/" not in str(request.url)
        if "/bucket/" in request.url.path:
            return httpx.Response(200,json={"public":False})
        if request.method=="POST":
            assert request.headers["x-upsert"]=="false"
            assert request.headers["content-type"]=="image/png"
            assert request.content==b"private image"
            return httpx.Response(200,json={"Key":"stored"})
        if request.method=="GET":
            return httpx.Response(200,content=b"private image")
        assert request.method=="DELETE"
        assert b'"prefixes"' in request.content
        return httpx.Response(200,json=[])
    storage=remote_storage(key,handler)
    try:
        storage.verify_private_bucket()
        object_key="P001/submission/unique/corrected sample #1.png"
        storage.put(object_key,b"private image","image/png")
        assert storage.get(object_key)==b"private image"
        storage.delete(object_key)
        assert [r.method for r in requests]==["GET","POST","GET","DELETE"]
        object_url=str(requests[1].url)
        assert "/object/handwriting-private/P001/submission/unique/" in object_url
        assert "%20" in object_url and "%23" in object_url
        # Secrets travel in headers, never in object URLs or browser URLs.
        assert all(key not in str(r.url) for r in requests)
    finally:
        storage.client.close()


@pytest.mark.parametrize("bucket_info",[{"public":True},{},{"public":"false"}])
def test_public_or_ambiguous_bucket_prevents_startup(bucket_info):
    storage=remote_storage("sb_secret_test",lambda r:httpx.Response(200,json=bucket_info))
    try:
        with pytest.raises(ValueError,match="private"):
            storage.verify_private_bucket()
    finally:
        storage.client.close()


@pytest.mark.parametrize("operation,status",[("verify_private_bucket",401),("put",409),
                                            ("get",404),("delete",500)])
def test_storage_http_errors_propagate(operation,status):
    storage=remote_storage("sb_secret_test",lambda r:httpx.Response(status,json={"message":"simulated failure"}))
    try:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            if operation=="put":
                storage.put("P001/file.png",b"data","image/png")
            elif operation in ("get","delete"):
                getattr(storage,operation)("P001/file.png")
            else:
                storage.verify_private_bucket()
        assert caught.value.response.status_code==status
        assert "sb_secret_test" not in str(caught.value)
    finally:
        storage.client.close()


@pytest.mark.parametrize("operation",["put","get","delete"])
def test_storage_transport_failures_propagate(operation):
    def handler(request):
        raise httpx.ReadTimeout("simulated timeout",request=request)
    storage=remote_storage("sb_secret_test",handler)
    try:
        with pytest.raises(httpx.ReadTimeout):
            if operation=="put":
                storage.put("P001/file.png",b"data","image/png")
            else:
                getattr(storage,operation)("P001/file.png")
    finally:
        storage.client.close()


def test_local_storage_rejects_escape(tmp_path):
    storage=LocalStorage(tmp_path)
    for key in ("../outside.png","P001/../../outside.png",str(tmp_path/"outside.png")):
        with pytest.raises(ValueError,match="storage key"):
            storage.put(key,b"secret","image/png")
    assert not (tmp_path/"outside.png").exists()


def test_local_failed_atomic_replace_preserves_previous_file(tmp_path,monkeypatch):
    storage=LocalStorage(tmp_path)
    storage.put("P001/original.jpg",b"old","image/jpeg")
    def fail_replace(*_):
        raise OSError("simulated disk failure")
    monkeypatch.setattr("app.storage.os.replace",fail_replace)
    with pytest.raises(OSError,match="disk failure"):
        storage.put("P001/original.jpg",b"new","image/jpeg")
    assert storage.get("P001/original.jpg")==b"old"
    assert not list(storage.root.rglob("*.tmp"))


def part_storage(monkeypatch):
    """An authenticated, in-memory Storage service with small test parts."""
    monkeypatch.setattr('app.storage.PDF_PART_SIZE', 16)
    monkeypatch.setattr('app.storage.PDF_PART_THRESHOLD', 32)
    objects, requests = {}, []
    faults = {'post': None, 'delete': False, 'root_reply': False}

    def handler(request):
        assert request.headers['apikey'] == 'sb_secret_test'
        assert '/object/public/' not in request.url.path
        key = request.url.path.split('/object/handwriting-private/', 1)[-1]
        requests.append((request.method, key))
        if request.method == 'POST':
            assert request.headers['x-upsert'] == 'false'
            if faults['post'] and faults['post'] in key:
                return httpx.Response(503, json={'message': 'part unavailable'})
            if key in objects:
                return httpx.Response(409, json={'message': 'already exists'})
            if '.parts/' in key:
                assert len(request.content) <= 16
            objects[key] = (request.content, request.headers['content-type'])
            if faults['root_reply'] and request.headers['content-type'] == PDF_PART_CONTENT_TYPE:
                raise httpx.ReadTimeout('Stored index but reply was lost', request=request)
            return httpx.Response(200, json={'Key': key})
        if request.method == 'GET':
            if key not in objects:
                return httpx.Response(404, json={'message': 'not found'})
            data, content_type = objects[key]
            return httpx.Response(200, content=data, headers={'content-type': content_type})
        assert request.method == 'DELETE'
        keys = json.loads(request.content)['prefixes']
        if faults['delete']:
            objects.pop(keys[0], None)
            return httpx.Response(503, json={'message': 'interrupted delete'})
        for object_key in keys:
            objects.pop(object_key, None)
        return httpx.Response(200, json=[])

    return remote_storage('sb_secret_test', handler), objects, requests, faults


def test_large_private_pdf_parts_round_trip_and_atomic_index(monkeypatch):
    storage, objects, requests, _ = part_storage(monkeypatch)
    key, data = 'P001/submission/new/P001.pdf', b'%PDF-' + bytes(range(80))
    try:
        storage.put(key, data, 'application/pdf')
        assert requests[-1] == ('POST', key), 'Publish the root only after all part uploads'
        manifest = json.loads(objects[key][0])
        assert objects[key][1] == PDF_PART_CONTENT_TYPE
        assert len(manifest['parts']) == 6
        assert all(part['key'].startswith(key + '.parts/') for part in manifest['parts'])
        assert storage.get(key) == data
        storage.delete(key)
        assert objects == {}
        assert requests[-1][0] == 'DELETE'
    finally:
        storage.client.close()


def test_small_pdf_and_large_image_keep_existing_format(monkeypatch):
    storage, objects, _, _ = part_storage(monkeypatch)
    try:
        for key, data, content_type in [('small.pdf', b'%PDF-small', 'application/pdf'),
                                        ('photo.png', b'PNG' + b'x' * 90, 'image/png'),
                                        ('study-worksheets/source/worksheet.pdf', b'%PDF-' + b'x' * 90, 'application/pdf')]:
            storage.put(key, data, content_type)
            assert objects[key] == (data, content_type)
            assert storage.get(key) == data
        assert len(objects) == 3
    finally:
        storage.client.close()


def test_part_failure_does_not_publish_index_or_delete_uncertain_parts(monkeypatch):
    storage, objects, requests, faults = part_storage(monkeypatch)
    key = 'P001/submission/new/P001.pdf'
    faults['post'] = '/000001'
    try:
        with pytest.raises(httpx.HTTPStatusError):
            storage.put(key, b'%PDF-' + b'x' * 70, 'application/pdf')
        assert key not in objects
        assert objects and all('.parts/' in name for name in objects)
        assert all(method != 'DELETE' for method, _ in requests)
        storage.delete(key)  # An unpublished root is idempotently removable.
        assert key not in objects
    finally:
        storage.client.close()


def test_lost_index_reply_preserves_a_readable_large_pdf(monkeypatch):
    storage, objects, requests, faults = part_storage(monkeypatch)
    key, data = 'P001/new/P001.pdf', b'%PDF-' + b'data' * 25
    faults['root_reply'] = True
    try:
        with pytest.raises(httpx.ReadTimeout):
            storage.put(key, data, 'application/pdf')
        assert key in objects
        assert storage.get(key) == data
        assert all(method != 'DELETE' for method, _ in requests)
        faults['root_reply'] = False
        with pytest.raises(httpx.HTTPStatusError) as caught:
            storage.put(key, b'%PDF-' + b'other' * 20, 'application/pdf')
        assert caught.value.response.status_code == 409
        assert storage.get(key) == data, 'A conflicting retry must retain the published PDF'
    finally:
        storage.client.close()


@pytest.mark.parametrize('corruption', ['content', 'missing', 'size', 'foreign-key', 'whole-checksum'])
def test_invalid_pdf_parts_fail_without_fetching_unrelated_objects(monkeypatch, corruption):
    storage, objects, requests, _ = part_storage(monkeypatch)
    key = 'P001/new/P001.pdf'
    try:
        storage.put(key, b'%PDF-' + bytes(range(80)), 'application/pdf')
        manifest = json.loads(objects[key][0])
        first_key = manifest['parts'][0]['key']
        if corruption == 'content':
            objects[first_key] = (b'corrupted bytes!', 'application/octet-stream')
        elif corruption == 'missing':
            del objects[first_key]
        elif corruption == 'size':
            manifest['parts'][0]['size'] += 1
        elif corruption == 'foreign-key':
            manifest['parts'][0]['key'] = 'P002/other/private.pdf'
        else:
            manifest['sha256'] = '0' * 64
        objects[key] = (json.dumps(manifest, separators=(',', ':')).encode(), PDF_PART_CONTENT_TYPE)
        requests.clear()
        with pytest.raises((ValueError, httpx.HTTPStatusError)):
            storage.get(key)
        assert all(name != 'P002/other/private.pdf' for _, name in requests)
        if corruption in ('size', 'foreign-key'):
            assert requests == [('GET', key)]
    finally:
        storage.client.close()


def test_interrupted_part_cleanup_keeps_index_for_safe_retry(monkeypatch):
    storage, objects, _, faults = part_storage(monkeypatch)
    key = 'P001/new/P001.pdf'
    try:
        storage.put(key, b'%PDF-' + b'x' * 70, 'application/pdf')
        faults['delete'] = True
        with pytest.raises(httpx.HTTPStatusError):
            storage.delete(key)
        assert key in objects, 'Retain the index until every part deletion succeeds'
        faults['delete'] = False
        storage.delete(key)
        assert not objects
    finally:
        storage.client.close()


def test_iter_get_is_ordered_with_bounded_parallel_requests():
    first_four = threading.Barrier(4)
    lock = threading.Lock()
    active, peak, requested = 0, 0, []

    def handler(request):
        nonlocal active, peak
        number = int(request.url.path.rsplit('/', 1)[1])
        with lock:
            active += 1
            peak = max(active, peak)
            requested.append(number)
        if number < 4:
            first_four.wait(timeout=5)
        time.sleep((4 - number % 4) * .002)
        with lock:
            active -= 1
        return httpx.Response(200, content=str(number).encode())

    storage = remote_storage('sb_secret_test', handler)
    try:
        results = storage.iter_get(f'images/{number}' for number in range(10))
        assert next(results) == b'0'
        assert sorted(requested) == [0, 1, 2, 3], 'Only four reads may be prefetched'
        assert list(results) == [str(number).encode() for number in range(1, 10)]
        assert peak == 4
    finally:
        storage.client.close()


def test_plain_pdf_cleanup_does_not_download_the_old_pdf():
    read_chunks, closed, removed = [], [], []

    class OldPdf(httpx.SyncByteStream):
        def __iter__(self):
            read_chunks.append(0)
            yield b'%PDF-' + b'x' * 4091
            read_chunks.append(1)
            raise AssertionError('Cleanup must not read the rest of the old PDF')

        def close(self):
            closed.append(True)

    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, stream=OldPdf())
        assert request.method == 'DELETE'
        removed.extend(json.loads(request.content)['prefixes'])
        return httpx.Response(200, json=[])

    storage = remote_storage('sb_secret_test', handler)
    try:
        storage.delete('P001/old/P001.pdf')
        assert read_chunks == [0] and closed == [True]
        assert removed == ['P001/old/P001.pdf']
    finally:
        storage.client.close()


def test_missing_pdf_cleanup_is_idempotent():
    storage = remote_storage('sb_secret_test', lambda request: httpx.Response(404))
    try:
        storage.delete('P001/unpublished/P001.pdf')
    finally:
        storage.client.close()
