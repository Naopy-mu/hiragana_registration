"""Consent-gated distribution of the printable, blank study worksheet."""
import io
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import select

from app.database import Participant, now
from app.main import create_app
from app.security import hash_password
from app.settings import ROOT, Settings
from app.worksheet import validate_worksheet


@pytest.fixture(scope='session')
def worksheet_password_hash():
    return hash_password('worksheet-tests-password-2026')


@pytest.fixture
def worksheet_app(tmp_path, worksheet_password_hash):
    manifest = json.loads((ROOT / 'config/pages.test.json').read_text(encoding='utf-8'))
    config = tmp_path / 'pages.json'
    config.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    settings = Settings(data_dir=tmp_path / 'data', page_config=config,
                        session_secret='worksheet-test-session-' + 's' * 40,
                        researcher_password_hash=worksheet_password_hash)
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, app, settings, manifest


def consent_and_register(client):
    config = client.get('/api/config').json()
    response = client.post('/api/register', json={
        'consent': True, 'consent_version': config['consent_version'],
        'idempotency_key': str(uuid.uuid4()),
    })
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer ' + response.json()['resume_token']}


def test_worksheet_requires_consent_and_valid_participant_link(worksheet_app):
    client, _, _, _ = worksheet_app
    endpoint = '/api/participant/worksheet'
    assert client.get(endpoint).status_code == 401
    assert client.get(endpoint, headers={'Authorization': 'Bearer ' + 'x' * 43}).status_code == 401
    assert client.get(endpoint, headers={'Authorization': 'Bearer P001'}).status_code == 401
    config = client.get('/api/config').json()
    assert 'worksheet' not in config
    refused = client.post('/api/register', json={
        'consent': False, 'consent_version': config['consent_version'],
        'idempotency_key': str(uuid.uuid4()),
    })
    assert refused.status_code == 400
    assert client.get(endpoint).status_code == 401


def test_consented_participant_gets_complete_pdf_and_can_resume(worksheet_app):
    client, _, _, manifest = worksheet_app
    auth = consent_and_register(client)
    worksheet = client.get('/api/participant', headers=auth).json()['worksheet']
    assert worksheet['available'] is True
    assert worksheet['is_test'] is True
    assert worksheet['title']
    assert worksheet['download_url'] == '/api/participant/worksheet'
    response = client.get(worksheet['download_url'], headers=auth)
    assert response.status_code == 200, response.text
    assert response.headers['content-type'].startswith('application/pdf')
    assert 'attachment;' in response.headers['content-disposition']
    assert worksheet['filename'] in response.headers['content-disposition']
    expected = (ROOT / manifest['worksheet']['path']).read_bytes()
    assert response.content == expected
    pdf = PdfReader(io.BytesIO(response.content))
    assert len(pdf.pages) == len(manifest['pages']) == 3
    for page in pdf.pages:
        assert float(page.mediabox.width) == pytest.approx(595.28, abs=.1)
        assert float(page.mediabox.height) == pytest.approx(841.89, abs=.1)
    # A resumed session uses the same capability, without creating a new account.
    resumed = client.get('/api/participant', headers=auth).json()['worksheet']
    assert resumed == worksheet
    assert client.get(resumed['download_url'], headers=auth).content == expected
    # Distribution never exposes the PDF through unauthenticated/static paths.
    for path in ('/' + manifest['worksheet']['path'], '/static/worksheets/test-worksheet.pdf'):
        assert client.get(path).status_code == 404


def test_submitted_participant_can_still_download_worksheet(worksheet_app):
    client, app, _, _ = worksheet_app
    auth = consent_and_register(client)
    with app.state.db.write() as session:
        participant = session.scalar(select(Participant))
        participant.status = 'submitted'
        participant.pdf_path = 'test-only/submitted.pdf'
        participant.submitted_at = now()
    response = client.get('/api/participant/worksheet', headers=auth)
    assert response.status_code == 200
    assert response.content.startswith(b'%PDF-')


def test_development_without_configured_worksheet_reports_unavailable(tmp_path, worksheet_password_hash):
    manifest = json.loads((ROOT / 'config/pages.test.json').read_text(encoding='utf-8'))
    manifest.pop('worksheet', None)
    config = tmp_path / 'pages.json'
    config.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    settings = Settings(data_dir=tmp_path / 'data', page_config=config,
                        session_secret='worksheet-unconfigured-' + 's' * 40,
                        researcher_password_hash=worksheet_password_hash)
    with TestClient(create_app(settings)) as client:
        auth = consent_and_register(client)
        worksheet = client.get('/api/participant', headers=auth).json()['worksheet']
        assert worksheet['available'] is False
        assert client.get('/api/participant/worksheet', headers=auth).status_code == 404


@pytest.mark.parametrize('configuration', [None, {'test_only': True}, {'test_only': 'false'}])
def test_production_rejects_missing_or_test_only_worksheet(configuration):
    manifest = json.loads((ROOT / 'config/pages.test.json').read_text(encoding='utf-8'))
    if configuration is None:
        manifest.pop('worksheet')
    else:
        manifest['worksheet'].update(configuration)
    with pytest.raises(ValueError):
        validate_worksheet(manifest, production=True)
