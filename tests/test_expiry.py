"""Expired credentials cannot revive an abandoned participant or bearer token."""
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import ExpiredRegistration, Participant, now
from app.main import create_app
from app.security import hash_password
from app.settings import Settings


@pytest.fixture
def expiry_app(tmp_path):
    settings = Settings(data_dir=tmp_path, session_secret='expiry-test-' + 'x' * 40,
                        researcher_password_hash=hash_password('expiry-test-password'))
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, app


def register(client, key=None):
    return client.post('/api/register', json={
        'consent': True, 'consent_version': client.get('/api/config').json()['consent_version'],
        'idempotency_key': key or str(uuid.uuid4()),
    })


def expire(app):
    with app.state.db.write() as session:
        participant = session.scalar(select(Participant))
        participant.registered_at = now() - timedelta(hours=25)
        session.add(ExpiredRegistration(registration_key_hash=participant.registration_key_hash,
                                        token_hash=participant.token_hash))
        session.delete(participant)


def test_expired_link_is_gone_and_id_is_not_reused(expiry_app):
    client, app = expiry_app
    old = register(client).json()
    expire(app)
    headers = {'Authorization': 'Bearer ' + old['resume_token']}
    for path in ('/api/participant', '/api/participant/worksheet', '/api/participant/pdf'):
        response = client.get(path, headers=headers)
        assert response.status_code == 410
        assert response.json()['detail']['code'] == 'registration_expired'
    new = register(client).json()
    assert new['participant_id'] != old['participant_id']
    assert new['resume_token'] != old['resume_token']
    assert client.get('/api/participant', headers=headers).status_code == 410


def test_same_registration_key_cannot_restore_expired_token(expiry_app):
    client, app = expiry_app
    key = str(uuid.uuid4())
    assert register(client, key).status_code == 200
    expire(app)
    response = register(client, key)
    assert response.status_code == 410
    with app.state.db.read() as session:
        assert session.scalar(select(Participant)) is None


def test_unrecognized_token_still_returns_unauthorized(expiry_app):
    client, _ = expiry_app
    assert client.get('/api/participant', headers={'Authorization': 'Bearer ' + 'z' * 43}).status_code == 401


def test_cleanup_during_photo_processing_returns_expired_without_storage_writes(expiry_app, monkeypatch):
    client, app = expiry_app
    participant = register(client).json()
    def process(data, manifest):
        expire(app)
        return SimpleNamespace(qr_code='HIRA1-01', page_number=1, png_bytes=b'not-written', metrics={})
    monkeypatch.setattr('app.imaging.process_image', process)
    def unexpected_write(*args, **kwargs):
        pytest.fail('An expired participant must not create storage objects')
    monkeypatch.setattr(app.state.storage, 'put', unexpected_write)
    response = client.post('/api/participant/pages',
                           headers={'Authorization': 'Bearer ' + participant['resume_token']},
                           files={'file': ('test.jpg', b'photo', 'image/jpeg')})
    assert response.status_code == 410
    assert response.json()['detail']['code'] == 'registration_expired'
