"""HTTP contract tests only: no live Supabase credentials or service contacted."""
import httpx
import pytest

from app.settings import Settings
from app.storage import LocalStorage, SupabaseStorage


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
