"""Researcher-managed immutable worksheet revisions and participant pinning."""
import io
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.main import create_app
from app.revisions import RevisionNotProduction, StudyRegistry
from app.security import hash_password
from app.settings import ROOT, Settings
from app.storage import LocalStorage
from scripts.generate_fixtures import generate

PASSWORD = 'worksheet-revision-tests-2026'


class FailingRevisionStorage(LocalStorage):
    fail_next_put = False
    fail_next_get = False

    def put(self, key, data, content_type):
        if self.fail_next_put:
            self.fail_next_put = False
            raise OSError('Simulated worksheet storage outage')
        return super().put(key, data, content_type)

    def get(self, key):
        if self.fail_next_get:
            self.fail_next_get = False
            raise OSError('Simulated worksheet download outage')
        return super().get(key)


@pytest.fixture(scope='session')
def revision_password_hash():
    return hash_password(PASSWORD)


@pytest.fixture(scope='session')
def original_pdf():
    return (ROOT / 'resources/worksheets/hiragana_collection_a4.pdf').read_bytes()


@pytest.fixture(scope='session')
def old_form_photo(tmp_path_factory):
    return generate(tmp_path_factory.mktemp('old-revision-photos'))[2].read_bytes()


@pytest.fixture
def revision_harness(tmp_path, revision_password_hash):
    settings = Settings(data_dir=tmp_path, page_config=ROOT / 'config/pages.test.json',
                        session_secret='revision-test-session-' + 's' * 40,
                        participant_token_secret='revision-test-participant-' + 'p' * 40,
                        researcher_password_hash=revision_password_hash)
    storage = FailingRevisionStorage(tmp_path)
    app = create_app(settings, storage_override=storage)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, app, settings, storage


def register(client):
    config = client.get('/api/config').json()
    response = client.post('/api/register', json={
        'consent': True, 'consent_version': config['consent_version'],
        'idempotency_key': str(uuid.uuid4()),
    })
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer ' + response.json()['resume_token']}


def login(client):
    response = client.post('/api/researcher/login', json={'password': PASSWORD})
    assert response.status_code == 200, response.text
    return {'X-CSRF-Token': response.json()['csrf_token']}


def active(client):
    response = client.get('/api/researcher/worksheet')
    assert response.status_code == 200, response.text
    return response.json()


def inspect(client, csrf, pdf, filename='hiragana_collection_a4.pdf'):
    response = client.post('/api/researcher/worksheet/inspect', headers=csrf,
                           files={'file': (filename, pdf, 'application/pdf')})
    assert response.status_code == 200, response.text
    return response.json()


def activate(client, csrf, draft):
    return client.post('/api/researcher/worksheet/activate', headers=csrf, json={
        'draft_id': draft['draft_id'], 'expected_active_revision_id': draft['active_revision_id'],
    })


def test_researcher_revision_apis_enforce_authentication_and_csrf(revision_harness, original_pdf):
    client, _, _, _ = revision_harness
    assert client.get('/api/researcher/worksheet').status_code == 401
    assert client.get('/api/researcher/worksheet/pdf').status_code == 401
    assert client.get('/api/researcher/worksheet/drafts/unknown/pdf').status_code == 401
    assert client.post('/api/researcher/worksheet/inspect',
                       files={'file': ('form.pdf', original_pdf, 'application/pdf')}).status_code == 401
    assert client.post('/api/researcher/worksheet/activate', json={
        'draft_id': str(uuid.uuid4()), 'expected_active_revision_id': None}).status_code == 401
    csrf = login(client)
    assert client.get('/api/researcher/worksheet').status_code == 200
    assert client.post('/api/researcher/worksheet/inspect',
                       files={'file': ('form.pdf', original_pdf, 'application/pdf')}).status_code == 403
    assert client.post('/api/researcher/worksheet/activate', json={
        'draft_id': str(uuid.uuid4()), 'expected_active_revision_id': None}).status_code == 403
    draft = inspect(client, csrf, original_pdf)
    assert client.get(draft['preview_url']).content == original_pdf
    assert client.post('/api/researcher/logout', headers=csrf).status_code == 200
    assert client.get(draft['preview_url']).status_code == 401


def test_inspection_does_not_publish_and_rejects_invalid_pdf(revision_harness, original_pdf):
    client, _, _, _ = revision_harness
    csrf = login(client)
    before = active(client)
    before_pdf = client.get(before['download_url']).content
    invalid = client.post('/api/researcher/worksheet/inspect', headers=csrf,
                          files={'file': ('bad.pdf', b'not a PDF', 'application/pdf')})
    assert 400 <= invalid.status_code < 500
    assert active(client)['active_revision_id'] == before['active_revision_id']
    draft = inspect(client, csrf, original_pdf)
    assert draft['page_count'] == len(PdfReader(io.BytesIO(original_pdf)).pages) == 8
    assert len({p['qr_code'] for p in draft['pages']}) == draft['page_count']
    assert [p['page_number'] for p in draft['pages']] == list(range(1, 9))
    assert active(client)['active_revision_id'] == before['active_revision_id']
    assert client.get(before['download_url']).content == before_pdf


def test_activation_pins_old_and_new_participants_to_their_forms(revision_harness, original_pdf, old_form_photo):
    client, _, _, _ = revision_harness
    old_auth = register(client)
    old_state = client.get('/api/participant', headers=old_auth).json()
    old_pdf = client.get('/api/participant/worksheet', headers=old_auth).content
    csrf = login(client)
    draft = inspect(client, csrf, original_pdf)
    response = activate(client, csrf, draft)
    assert response.status_code == 200, response.text
    now = active(client)
    assert now['active_revision_id'] == draft['draft_id']
    assert now['page_count'] == 8
    assert client.get(now['download_url']).content == original_pdf
    assert [p['qr_code'] for p in client.get('/api/config').json()['pages']] == [p['qr_code'] for p in draft['pages']]
    # Returning participants keep both their worksheet bytes and their required list.
    returned = client.get('/api/participant', headers=old_auth).json()
    assert client.get('/api/participant/worksheet', headers=old_auth).content == old_pdf
    assert returned['missing_pages'] == old_state['missing_pages']
    assert [p['qr_code'] for p in returned['config']['pages']] == old_state['missing_pages']
    new_auth = register(client)
    new_state = client.get('/api/participant', headers=new_auth).json()
    assert client.get('/api/participant/worksheet', headers=new_auth).content == original_pdf
    assert new_state['missing_pages'] == [p['qr_code'] for p in draft['pages']]
    assert len(new_state['config']['pages']) == 8
    old_upload = client.post('/api/participant/pages', headers=old_auth,
                             files={'file': ('old-form.jpg', old_form_photo, 'image/jpeg')})
    assert old_upload.status_code == 200, old_upload.text
    assert old_upload.json()['page']['qr_code'] == 'TEST-03'
    new_upload = client.post('/api/participant/pages', headers=new_auth,
                             files={'file': ('old-form.jpg', old_form_photo, 'image/jpeg')})
    assert new_upload.status_code == 422, new_upload.text
    assert new_upload.json()['detail']['code'] == 'unknown_page'
    # A lost activation response can be retried without creating another revision.
    retried = activate(client, csrf, draft)
    assert retried.status_code == 200, retried.text
    assert active(client)['active_revision_id'] == draft['draft_id']


def test_stale_activation_cannot_overwrite_a_newer_form(revision_harness, original_pdf):
    client, _, _, _ = revision_harness
    csrf = login(client)
    first = inspect(client, csrf, original_pdf)
    second = inspect(client, csrf, original_pdf)
    assert first['draft_id'] != second['draft_id']
    assert activate(client, csrf, first).status_code == 200
    conflict = activate(client, csrf, second)
    assert conflict.status_code == 409, conflict.text
    assert active(client)['active_revision_id'] == first['draft_id']


def test_storage_failure_does_not_change_active_revision(revision_harness, original_pdf):
    client, _, _, storage = revision_harness
    csrf = login(client)
    before = active(client)
    old_pdf = client.get(before['download_url']).content
    storage.fail_next_put = True
    failed = client.post('/api/researcher/worksheet/inspect', headers=csrf,
                         files={'file': ('form.pdf', original_pdf, 'application/pdf')})
    assert failed.status_code == 503, failed.text
    assert active(client)['active_revision_id'] == before['active_revision_id']
    assert client.get(before['download_url']).content == old_pdf


def test_activation_commit_failure_keeps_the_previous_revision(revision_harness, original_pdf, monkeypatch):
    client, app, _, _ = revision_harness
    csrf = login(client)
    before = active(client)
    draft = inspect(client, csrf, original_pdf)
    session_class = app.state.db.sessions.class_
    original_commit = session_class.commit
    fail_next = {'value': True}

    def fail_before_commit(session):
        if fail_next['value']:
            fail_next['value'] = False
            raise OSError('Simulated database commit failure')
        return original_commit(session)

    monkeypatch.setattr(session_class, 'commit', fail_before_commit)
    failed = activate(client, csrf, draft)
    assert failed.status_code == 503, failed.text
    assert active(client)['active_revision_id'] == before['active_revision_id']
    assert activate(client, csrf, draft).status_code == 200


def test_activation_checks_stored_pdf_before_switching(revision_harness, original_pdf):
    client, _, _, storage = revision_harness
    csrf = login(client)
    before = active(client)
    draft = inspect(client, csrf, original_pdf)
    storage.fail_next_get = True
    failed = activate(client, csrf, draft)
    assert failed.status_code == 503, failed.text
    assert active(client)['active_revision_id'] == before['active_revision_id']
    assert activate(client, csrf, draft).status_code == 200
    assert client.get('/api/researcher/worksheet/pdf').content == original_pdf


def test_lost_activation_acknowledgement_can_be_retried(revision_harness, original_pdf, monkeypatch):
    client, app, _, _ = revision_harness
    csrf = login(client)
    draft = inspect(client, csrf, original_pdf)
    session_class = app.state.db.sessions.class_
    original_commit = session_class.commit
    lose_reply = {'value': True}

    def commit_then_lose_reply(session):
        original_commit(session)
        if lose_reply['value']:
            lose_reply['value'] = False
            raise OSError('Database committed, but reply was lost')

    monkeypatch.setattr(session_class, 'commit', commit_then_lose_reply)
    uncertain = activate(client, csrf, draft)
    assert uncertain.status_code == 503, uncertain.text
    assert active(client)['active_revision_id'] == draft['draft_id']
    assert client.get('/api/researcher/worksheet/pdf').content == original_pdf
    assert activate(client, csrf, draft).status_code == 200
    assert active(client)['active_revision_id'] == draft['draft_id']


def test_active_revision_survives_application_restart(tmp_path, revision_password_hash, original_pdf):
    settings = Settings(data_dir=tmp_path, page_config=ROOT / 'config/pages.test.json',
                        session_secret='restart-revision-session-' + 's' * 40,
                        researcher_password_hash=revision_password_hash)
    with TestClient(create_app(settings)) as client:
        auth = register(client)
        csrf = login(client)
        draft = inspect(client, csrf, original_pdf)
        assert activate(client, csrf, draft).status_code == 200
    # The startup manifest remains the old test configuration; persisted active state wins.
    with TestClient(create_app(settings)) as client:
        csrf = login(client)
        assert active(client)['active_revision_id'] == draft['draft_id']
        assert len(client.get('/api/config').json()['pages']) == 8
        assert client.get('/api/researcher/worksheet/pdf').content == original_pdf
        assert len(client.get('/api/participant', headers=auth).json()['config']['pages']) == 3


def test_production_boot_rejects_persisted_test_revision(revision_harness):
    _, app, _, storage = revision_harness
    actual_manifest = json.loads((ROOT / 'config/pages.study.json').read_text(encoding='utf-8'))
    # A valid new bootstrap file must not mask an old test version already in the DB.
    with pytest.raises(RevisionNotProduction):
        StudyRegistry(app.state.db, storage, actual_manifest, production=True)


def test_production_registry_accepts_real_revision_and_cannot_reactivate_test(revision_harness, original_pdf):
    client, app, _, storage = revision_harness
    csrf = login(client)
    old = active(client)['active_revision_id']
    draft = inspect(client, csrf, original_pdf)
    assert activate(client, csrf, draft).status_code == 200
    actual_manifest = json.loads((ROOT / 'config/pages.study.json').read_text(encoding='utf-8'))
    production = StudyRegistry(app.state.db, storage, actual_manifest, production=True)
    assert production.summary()['active_revision_id'] == draft['draft_id']
    with pytest.raises(RevisionNotProduction):
        production.activate(old, draft['draft_id'])
    assert active(client)['active_revision_id'] == draft['draft_id']
