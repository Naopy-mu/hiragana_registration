"""Researcher-managed immutable worksheet revisions and participant pinning."""
import io
import json
import uuid

import fitz
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

from app.main import create_app
from app.revisions import RevisionNotProduction, StudyRegistry
from app.security import hash_password
from app.settings import ROOT, Settings
from app.storage import LocalStorage
from scripts.generate_fixtures import generate

PASSWORD = 'worksheet-revision-tests-2026'


def assert_template_page(data, page_number, original, original_page_number=0):
    with fitz.open(stream=data, filetype='pdf') as result, fitz.open(stream=original, filetype='pdf') as template:
        actual = result[page_number].get_pixmap()
        expected = template[original_page_number].get_pixmap()
        assert (actual.width, actual.height, actual.samples) == (expected.width, expected.height, expected.samples)


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
    with pytest.raises(RevisionNotProduction):
        production.select_files([old], draft['draft_id'])
    assert active(client)['active_revision_id'] == draft['draft_id']


@pytest.fixture(scope='session')
def separate_pdfs(original_pdf):
    source = PdfReader(io.BytesIO(original_pdf))
    result = []
    for page in source.pages[:3]:
        writer, output = PdfWriter(), io.BytesIO()
        writer.add_page(page)
        writer.write(output)
        result.append(output.getvalue())
    return result


def choose(client, csrf, ids, expected):
    return client.post('/api/researcher/worksheet/selection', headers=csrf, json={
        'worksheet_ids': ids, 'expected_active_revision_id': expected,
    })


def test_multiple_pdf_library_and_pinned_individual_downloads(revision_harness, separate_pdfs):
    client, app, settings, storage = revision_harness
    old_auth = register(client)
    old = client.get('/api/participant', headers=old_auth).json()
    csrf = login(client)
    before = active(client)
    drafts = [inspect(client, csrf, data, f'form-{i}.pdf') for i, data in enumerate(separate_pdfs, 1)]
    assert active(client)['active_revision_id'] == before['active_revision_id']
    assert len(active(client)['library']) == len(before['library']) + 3
    ids = [drafts[1]['draft_id'], drafts[0]['draft_id']]
    response = choose(client, csrf, ids, before['active_revision_id'])
    assert response.status_code == 200, response.text
    current = response.json()
    assert current['selected_worksheet_ids'] == ids
    auth = register(client)
    new = client.get('/api/participant', headers=auth).json()
    assert [f['number_label'] for f in new['worksheets']] == ['①', '②']
    assert [f['id'] for f in new['worksheets']] == ids
    assert new['worksheet']['download_url'] is None
    assert client.get('/api/participant/worksheet', headers=auth).status_code == 400
    assert new['missing_pages'] == ['HIRA1-02', 'HIRA1-01']
    assert [p['page_number'] for p in new['config']['pages']] == [1, 2]
    assert [p['label'] for p in new['config']['pages']] == ['① 1ページ', '② 1ページ']
    for file, expected in zip(new['worksheets'], [separate_pdfs[1], separate_pdfs[0]]):
        assert client.get(file['download_url']).status_code == 401
        assert client.get(file['download_url'], headers=auth).content == expected
        assert client.get(file['download_url'], headers=old_auth).status_code == 404
    assert client.get('/api/participant/worksheets/' + drafts[2]['draft_id'], headers=auth).status_code == 404
    assert client.get('/api/participant', headers=old_auth).json()['worksheet_revision_id'] == old['worksheet_revision_id']
    # Selecting a single file afterwards must not affect this participant's pair.
    assert choose(client, csrf, [drafts[2]['draft_id']], current['active_revision_id']).status_code == 200
    assert len(client.get('/api/participant', headers=auth).json()['worksheets']) == 2
    with TestClient(create_app(settings, storage_override=storage)) as restarted:
        assert len(restarted.get('/api/participant', headers=auth).json()['worksheets']) == 2
        latest_auth = register(restarted)
        assert restarted.get('/api/participant', headers=latest_auth).json()['missing_pages'] == ['HIRA1-03']


def test_selection_auth_csrf_empty_duplicate_and_missing(revision_harness, separate_pdfs):
    client, _, _, _ = revision_harness
    body = {'worksheet_ids': [str(uuid.uuid4())], 'expected_active_revision_id': str(uuid.uuid4())}
    assert client.post('/api/researcher/worksheet/selection', json=body).status_code == 401
    csrf = login(client)
    assert client.post('/api/researcher/worksheet/selection', json=body).status_code == 403
    before = active(client)['active_revision_id']
    draft = inspect(client, csrf, separate_pdfs[0])['draft_id']
    assert choose(client, csrf, [], before).status_code == 422
    assert choose(client, csrf, [draft, draft], before).status_code == 422
    assert choose(client, csrf, [str(uuid.uuid4())], before).status_code == 404
    assert active(client)['active_revision_id'] == before


def test_selection_duplicate_qr_and_storage_failure_keep_old_selection(revision_harness, separate_pdfs):
    client, _, _, storage = revision_harness
    csrf = login(client)
    before = active(client)['active_revision_id']
    a, b = [inspect(client, csrf, separate_pdfs[0])['draft_id'] for _ in range(2)]
    conflict = choose(client, csrf, [a, b], before)
    assert conflict.status_code == 422
    assert conflict.json()['detail']['code'] == 'invalid_worksheet_selection'
    assert active(client)['active_revision_id'] == before
    storage.fail_next_get = True
    assert choose(client, csrf, [a], before).status_code == 503
    assert active(client)['active_revision_id'] == before


def test_selection_conflicts_retries_and_preserves_library(revision_harness, separate_pdfs):
    client, _, _, _ = revision_harness
    csrf = login(client)
    before = active(client)['active_revision_id']
    ids = [inspect(client, csrf, data)['draft_id'] for data in separate_pdfs]
    first = choose(client, csrf, ids[:2], before)
    assert first.status_code == 200
    assert choose(client, csrf, ids[:2], before).json()['active_revision_id'] == first.json()['active_revision_id']
    assert choose(client, csrf, ids[1:], before).status_code == 409
    latest = choose(client, csrf, ids[1:], first.json()['active_revision_id'])
    assert latest.status_code == 200
    assert {item['id'] for item in latest.json()['library']} >= set(ids)


@pytest.mark.parametrize('commit_first', [False, True])
def test_selection_uncertain_commit_recovers(revision_harness, separate_pdfs, monkeypatch, commit_first):
    client, app, _, _ = revision_harness
    csrf = login(client)
    before = active(client)['active_revision_id']
    draft = inspect(client, csrf, separate_pdfs[0])['draft_id']
    session_class = app.state.db.sessions.class_
    original_commit = session_class.commit
    fail_once = [True]
    def fail_commit(session):
        if fail_once[0]:
            fail_once[0] = False
            if commit_first:
                original_commit(session)
            raise OSError('Simulated lost commit response')
        return original_commit(session)
    monkeypatch.setattr(session_class, 'commit', fail_commit)
    assert choose(client, csrf, [draft], before).status_code == 503
    assert (active(client)['active_revision_id'] != before) is commit_first
    assert choose(client, csrf, [draft], before).status_code == 200


@pytest.fixture(scope='session')
def form_photos(separate_pdfs):
    import fitz
    from PIL import Image, ImageDraw
    photos = []
    for data in separate_pdfs:
        with fitz.open(stream=data, filetype='pdf') as document:
            pixmap = document[0].get_pixmap(matrix=fitz.Matrix(2, 2))
            page = Image.frombytes('RGB', (pixmap.width, pixmap.height), pixmap.samples)
        pen = ImageDraw.Draw(page)
        for y in range(250, 1350, 35):
            for x in range(180, 1000, 60):
                pen.line([(x, y), (x + 20, y + 20), (x + 40, y + 3)], fill=(75, 75, 75), width=3)
        photo = Image.new('RGB', (page.width + 200, page.height + 200), 'white')
        photo.paste(page, (100, 100))
        output = io.BytesIO()
        photo.save(output, format='JPEG', quality=95)
        photos.append(output.getvalue())
    return photos


@pytest.fixture(scope='session')
def retaken_form_photo(form_photos):
    from PIL import Image, ImageDraw
    with Image.open(io.BytesIO(form_photos[0])) as source:
        photo = source.convert('RGB')
    ImageDraw.Draw(photo).rectangle((600, 650, 800, 700), fill=(25, 25, 25))
    output = io.BytesIO()
    photo.save(output, format='JPEG', quality=95)
    return output.getvalue()


def assert_submitted_photo(client, auth, pdf, index, page):
    from PIL import Image
    response = client.get(page['preview_url'], headers=auth)
    assert response.status_code == 200
    images = list(PdfReader(io.BytesIO(pdf)).pages[index].images)
    assert len(images) == 1
    with Image.open(io.BytesIO(response.content)) as corrected:
        actual, expected = images[0].image.convert('RGB'), corrected.convert('RGB')
        assert actual.size == expected.size
        assert actual.tobytes() == expected.tobytes()


def test_selected_forms_combine_in_file_order(revision_harness, separate_pdfs, form_photos):
    client, _, _, _ = revision_harness
    csrf = login(client)
    before = active(client)['active_revision_id']
    drafts = [inspect(client, csrf, data)['draft_id'] for data in separate_pdfs[:2]]
    assert choose(client, csrf, drafts[::-1], before).status_code == 200
    auth = register(client)
    first = client.post('/api/participant/pages', headers=auth, files={'file': ('form-a.jpg', form_photos[0], 'image/jpeg')})
    assert first.status_code == 200, first.text
    assert first.json()['page']['page_number'] == 2
    assert first.json()['participant']['missing_pages'] == ['HIRA1-02']
    assert client.get('/api/participant', headers=auth).json()['status'] == 'registered'
    second = client.post('/api/participant/pages', headers=auth, files={'file': ('form-b.jpg', form_photos[1], 'image/jpeg')})
    assert second.status_code == 200, second.text
    assert second.json()['page']['page_number'] == 1
    assert not second.json()['participant']['missing_pages']
    assert client.post('/api/participant/submit', headers=auth).status_code == 200
    document = PdfReader(io.BytesIO(client.get('/api/participant/pdf', headers=auth).content))
    assert len(document.pages) == 2
    assert [entry.title for entry in document.outline] == ['1: HIRA1-02', '2: HIRA1-01']


def partial_setup(harness, separate_pdfs, *, reverse=False):
    client, _, _, _ = harness
    csrf = login(client)
    writer, output = PdfWriter(), io.BytesIO()
    for source in separate_pdfs[:2]:
        writer.add_page(PdfReader(io.BytesIO(source)).pages[0])
    writer.write(output)
    ids = [inspect(client, csrf, data)['draft_id'] for data in [output.getvalue(), separate_pdfs[2]]]
    if reverse:
        ids.reverse()
    assert choose(client, csrf, ids, active(client)['active_revision_id']).status_code == 200
    auth = register(client)
    return client, csrf, auth, ids


def test_independent_partial_submission_templates_and_other_file_remains_editable(revision_harness, separate_pdfs, form_photos):
    import csv
    from zipfile import ZipFile
    client, csrf, auth, ids = partial_setup(revision_harness, separate_pdfs)
    upload = lambda index: client.post('/api/participant/pages', headers=auth, files={'file': ('form.jpg', form_photos[index], 'image/jpeg')})
    assert upload(0).status_code == 200
    submitted = client.post(f'/api/participant/worksheets/{ids[0]}/submit', headers=auth)
    assert submitted.status_code == 200, submitted.text
    state = client.get('/api/participant', headers=auth).json()
    assert state['status'] == 'partially_submitted'
    assert [f['status'] for f in state['worksheet_submissions']] == ['submitted', 'registered']
    assert state['worksheet_submissions'][0]['blank_qr_codes'] == ['HIRA1-02']
    data = client.get(state['worksheet_submissions'][0]['pdf_url'], headers=auth).content
    document = PdfReader(io.BytesIO(data))
    assert len(document.pages) == 2
    assert len(list(document.pages[0].images)) == 1
    assert_template_page(data, 1, separate_pdfs[1])
    assert document.pages[0].images[0].image.width > 1000, 'Registered photographs retain their corrected image'
    assert client.get('/api/participant/pdf', headers=auth).status_code == 404
    export = client.post('/api/researcher/download', headers=csrf, json={'scope': 'submitted', 'types': ['pdf', 'csv']})
    assert export.status_code == 200, export.text
    with ZipFile(io.BytesIO(export.content)) as archive:
        pid = state['participant_id']
        assert f'{pid}/{pid}_01.pdf' in archive.namelist()
        assert f'{pid}/{pid}_02.pdf' not in archive.namelist()
        assert f'{pid}/{pid}.pdf' not in archive.namelist()
        assert archive.read(f'{pid}/{pid}_01.pdf') == data
        rows = list(csv.DictReader(io.StringIO(archive.read(f'{pid}/mapping.csv').decode('utf-8-sig'))))
        assert [row['page_status'] for row in rows] == ['submitted', 'blank', 'not_registered']
        assert rows[1]['original_filename'] == rows[1]['corrected_filename'] == ''
        assert rows[1]['pdf_filename'] == f'{pid}_01.pdf'
    listing = client.get('/api/researcher/participants').json()['participants'][0]
    assert listing['submitted_worksheet_count'] == 1 and listing['blank_page_count'] == 1
    # A photo can fill a previously submitted template page without resubmitting the file.
    filled = upload(1)
    assert filled.status_code == 200, filled.text
    filled_state = filled.json()['participant']
    assert filled_state['status'] == 'partially_submitted'
    assert [file['status'] for file in filled_state['worksheet_submissions']] == ['submitted', 'registered']
    assert filled_state['blank_page_count'] == 1
    assert filled_state['has_pending_changes'] and filled_state['pending_page_count'] == 1
    assert filled_state['worksheet_submissions'][0]['pending_qr_codes'] == ['HIRA1-02']
    assert filled_state['worksheet_submissions'][0]['submitted_at'] == state['worksheet_submissions'][0]['submitted_at']
    assert client.get(state['worksheet_submissions'][0]['pdf_url'], headers=auth).content == data
    export = client.post('/api/researcher/download', headers=csrf, json={'scope': 'submitted', 'types': ['pdf', 'csv']})
    assert export.status_code == 200, export.text
    with ZipFile(io.BytesIO(export.content)) as archive:
        assert archive.read(f'{pid}/{pid}_01.pdf') == data
        rows = list(csv.DictReader(io.StringIO(archive.read(f'{pid}/mapping.csv').decode('utf-8-sig'))))
        assert [row['page_status'] for row in rows] == ['submitted', 'registered', 'not_registered']
    assert client.post(f'/api/participant/worksheets/{ids[0]}/submit', headers=auth).status_code == 200
    resubmitted = client.get('/api/participant', headers=auth).json()
    assert not resubmitted['has_pending_changes'] and resubmitted['blank_page_count'] == 0
    assert resubmitted['worksheet_submissions'][0]['submitted_at'] != state['worksheet_submissions'][0]['submitted_at']
    updated = client.get(state['worksheet_submissions'][0]['pdf_url'], headers=auth).content
    assert_submitted_photo(client, auth, updated, 1, filled.json()['page'])
    assert client.get('/api/participant/pdf', headers=auth).status_code == 404
    export = client.post('/api/researcher/download', headers=csrf, json={'scope': 'submitted', 'types': ['pdf', 'csv']})
    assert export.status_code == 200, export.text
    with ZipFile(io.BytesIO(export.content)) as archive:
        assert archive.read(f'{pid}/{pid}_01.pdf') == updated
        rows = list(csv.DictReader(io.StringIO(archive.read(f'{pid}/mapping.csv').decode('utf-8-sig'))))
        assert [row['page_status'] for row in rows] == ['submitted', 'submitted', 'not_registered']
        assert rows[1]['original_filename'] and rows[1]['corrected_filename']
    listing = client.get('/api/researcher/participants').json()['participants'][0]
    assert listing['blank_page_count'] == 0
    assert upload(2).status_code == 200, 'A submitted file must not prevent registering another file'
    assert client.post(f'/api/participant/worksheets/{ids[1]}/submit', headers=auth).status_code == 200
    combined_data = client.get('/api/participant/pdf', headers=auth).content
    combined = PdfReader(io.BytesIO(combined_data))
    assert len(combined.pages) == 3
    for index in range(3):
        assert len(list(combined.pages[index].images)) == 1
        assert combined.pages[index].images[0].image.width > 1000


def test_submitted_retake_updates_target_and_combined_but_keeps_other_pdf(revision_harness, separate_pdfs, form_photos, retaken_form_photo):
    from app.database import Participant, WorksheetSubmission
    from sqlalchemy import select
    client, csrf, auth, ids = partial_setup(revision_harness, separate_pdfs)
    initial = client.post('/api/participant/pages', headers=auth,
                          files={'file': ('first.jpg', form_photos[0], 'image/jpeg')})
    assert initial.status_code == 200, initial.text
    initial_page = initial.json()['page']
    assert client.post('/api/participant/submit', headers=auth).status_code == 200
    state = client.get('/api/participant', headers=auth).json()
    result_urls = [file['pdf_url'] for file in state['worksheet_submissions']]
    previous_target = client.get(result_urls[0], headers=auth).content
    previous_combined = client.get('/api/participant/pdf', headers=auth).content
    other_pdf = client.get(result_urls[1], headers=auth).content
    with revision_harness[1].state.db.read() as session:
        participant = session.scalar(select(Participant))
        submitted_at = participant.submitted_at
        other_path = session.get(WorksheetSubmission, (participant.submission_id, ids[1])).pdf_path
    duplicate = client.post('/api/participant/pages', headers=auth,
                            files={'file': ('retaken.jpg', retaken_form_photo, 'image/jpeg')})
    assert duplicate.status_code == 409 and duplicate.json()['detail']['code'] == 'duplicate_page'
    replaced = client.post('/api/participant/pages', headers=auth,
                           files={'file': ('retaken.jpg', retaken_form_photo, 'image/jpeg')},
                           data={'replace': 'true', 'expected_version': initial_page['version']})
    assert replaced.status_code == 200, replaced.text
    page, updated = replaced.json()['page'], replaced.json()['participant']
    assert updated['status'] == 'submitted' and updated['submitted_worksheet_count'] == 2
    assert updated['has_pending_changes'] and updated['pending_page_count'] == 1
    assert updated['blank_page_count'] == state['blank_page_count'] == 2
    assert [file['submitted_at'] for file in updated['worksheet_submissions']] == [file['submitted_at'] for file in state['worksheet_submissions']]
    assert client.get(result_urls[0], headers=auth).content == previous_target
    assert client.get('/api/participant/pdf', headers=auth).content == previous_combined
    assert client.post(f'/api/participant/worksheets/{ids[0]}/submit', headers=auth).status_code == 200
    completed = client.get('/api/participant', headers=auth).json()
    assert not completed['has_pending_changes']
    assert completed['worksheet_submissions'][0]['submitted_at'] != state['worksheet_submissions'][0]['submitted_at']
    assert completed['worksheet_submissions'][1]['submitted_at'] == state['worksheet_submissions'][1]['submitted_at']
    target_pdf = client.get(result_urls[0], headers=auth).content
    combined = client.get('/api/participant/pdf', headers=auth).content
    assert_submitted_photo(client, auth, target_pdf, 0, page)
    assert_submitted_photo(client, auth, combined, 0, page)
    assert_template_page(target_pdf, 1, separate_pdfs[1])
    assert_template_page(combined, 1, separate_pdfs[1])
    assert_template_page(combined, 2, separate_pdfs[2])
    assert client.get(result_urls[1], headers=auth).content == other_pdf
    with revision_harness[1].state.db.read() as session:
        participant = session.scalar(select(Participant))
        assert participant.submitted_at == submitted_at
        assert session.get(WorksheetSubmission, (participant.submission_id, ids[1])).pdf_path == other_path
    export = client.post('/api/researcher/download', headers=csrf, json={'scope': 'submitted', 'types': ['pdf']})
    assert export.status_code == 200, export.text
    from zipfile import ZipFile
    with ZipFile(io.BytesIO(export.content)) as archive:
        pid = state['participant_id']
        assert archive.read(f'{pid}/{pid}.pdf') == combined
        assert archive.read(f'{pid}/{pid}_01.pdf') == target_pdf
        assert archive.read(f'{pid}/{pid}_02.pdf') == other_pdf


def test_individual_pdf_download_recovers_when_resubmission_removes_old_pdf(revision_harness, separate_pdfs, form_photos, retaken_form_photo, monkeypatch):
    from app.database import Participant, WorksheetSubmission
    from sqlalchemy import select
    client, _, auth, ids = partial_setup(revision_harness, separate_pdfs)
    uploaded = client.post('/api/participant/pages', headers=auth,
                           files={'file': ('first.jpg', form_photos[0], 'image/jpeg')})
    assert uploaded.status_code == 200, uploaded.text
    old_page = uploaded.json()['page']
    submit_url = f'/api/participant/worksheets/{ids[0]}/submit'
    result_url = f'/api/participant/worksheets/{ids[0]}/submission'
    assert client.post(submit_url, headers=auth).status_code == 200
    app, storage = revision_harness[1], revision_harness[3]
    with app.state.db.read() as session:
        participant = session.scalar(select(Participant))
        submission_id = participant.submission_id
        old_path = session.get(WorksheetSubmission, (submission_id, ids[0])).pdf_path
    original_get, reads = storage.get, []
    replaced = False

    def replace_before_reading_old_pdf(key):
        nonlocal replaced
        reads.append(key)
        if key == old_path and not replaced:
            replaced = True
            replacement = client.post('/api/participant/pages', headers=auth,
                                      files={'file': ('retaken.jpg', retaken_form_photo, 'image/jpeg')},
                                      data={'replace': 'true', 'expected_version': old_page['version']})
            assert replacement.status_code == 200, replacement.text
            assert client.post(submit_url, headers=auth).status_code == 200
            assert not storage.path(old_path).exists()
        return original_get(key)

    monkeypatch.setattr(storage, 'get', replace_before_reading_old_pdf)
    response = client.get(result_url, headers=auth)
    assert response.status_code == 200, response.text
    assert replaced
    with app.state.db.read() as session:
        latest_path = session.get(WorksheetSubmission, (submission_id, ids[0])).pdf_path
    assert latest_path != old_path
    assert old_path in reads and reads[-1] == latest_path
    assert response.content == original_get(latest_path)
    assert_submitted_photo(client, auth, response.content, 0, old_page)
    assert_template_page(response.content, 1, separate_pdfs[1])


def test_individual_resubmission_keeps_other_worksheet_draft_out_of_combined_pdf(revision_harness, separate_pdfs, form_photos):
    import csv
    from zipfile import ZipFile
    client, csrf, auth, ids = partial_setup(revision_harness, separate_pdfs)
    assert client.post('/api/participant/submit', headers=auth).status_code == 200
    before = client.get('/api/participant', headers=auth).json()
    other_url = before['worksheet_submissions'][1]['pdf_url']
    other_pdf = client.get(other_url, headers=auth).content
    staged = []
    for index in (0, 2):
        response = client.post('/api/participant/pages', headers=auth,
                               files={'file': ('additional.jpg', form_photos[index], 'image/jpeg')})
        assert response.status_code == 200, response.text
        staged.append(response.json()['page'])
    draft = client.get('/api/participant', headers=auth).json()
    assert draft['pending_page_count'] == 2 and draft['blank_page_count'] == 3
    assert client.post(f'/api/participant/worksheets/{ids[0]}/submit', headers=auth).status_code == 200
    state = client.get('/api/participant', headers=auth).json()
    assert state['has_pending_changes'] and state['pending_page_count'] == 1
    assert [file['has_pending_changes'] for file in state['worksheet_submissions']] == [False, True]
    assert state['blank_page_count'] == 2
    assert client.get(other_url, headers=auth).content == other_pdf
    combined = client.get('/api/participant/pdf', headers=auth).content
    assert_submitted_photo(client, auth, combined, 0, staged[0])
    assert_template_page(combined, 1, separate_pdfs[1])
    assert_template_page(combined, 2, separate_pdfs[2])
    export = client.post('/api/researcher/download', headers=csrf, json={'scope': 'submitted', 'types': ['pdf', 'csv']})
    assert export.status_code == 200, export.text
    with ZipFile(io.BytesIO(export.content)) as archive:
        pid = state['participant_id']
        assert archive.read(f'{pid}/{pid}.pdf') == combined
        rows = list(csv.DictReader(io.StringIO(archive.read(f'{pid}/mapping.csv').decode('utf-8-sig'))))
        assert [row['page_status'] for row in rows] == ['submitted', 'blank', 'registered']
    assert client.post(f'/api/participant/worksheets/{ids[1]}/submit', headers=auth).status_code == 200
    completed = client.get('/api/participant', headers=auth).json()
    assert not completed['has_pending_changes'] and completed['blank_page_count'] == 1
    updated_combined = client.get('/api/participant/pdf', headers=auth).content
    assert_submitted_photo(client, auth, updated_combined, 0, staged[0])
    assert_submitted_photo(client, auth, updated_combined, 2, staged[1])
    assert_template_page(updated_combined, 1, separate_pdfs[1])


@pytest.mark.parametrize('failed_pdf', ['worksheet', 'combined'])
def test_submitted_photo_pdf_failure_keeps_all_previous_data(revision_harness, separate_pdfs, form_photos, monkeypatch, failed_pdf):
    from app.database import Page, Participant, WorksheetSubmission
    from sqlalchemy import select
    client, _, auth, ids = partial_setup(revision_harness, separate_pdfs)
    assert client.post('/api/participant/submit', headers=auth).status_code == 200
    before = client.get('/api/participant', headers=auth).json()
    urls = [file['pdf_url'] for file in before['worksheet_submissions']] + ['/api/participant/pdf']
    old_pdfs = [client.get(url, headers=auth).content for url in urls]
    storage, app = revision_harness[3], revision_harness[1]
    with app.state.db.read() as session:
        old_paths = [session.scalar(select(Participant)).pdf_path]
        old_paths.extend(row.pdf_path for row in session.scalars(select(WorksheetSubmission)))
    put, attempted = storage.put, []
    suffix = f"/{before['participant_id']}{'_01' if failed_pdf == 'worksheet' else ''}.pdf"
    def fail_put(key, data, content_type):
        attempted.append(key)
        if key.endswith(suffix):
            raise OSError('Simulated submitted-PDF update failure')
        return put(key, data, content_type)
    monkeypatch.setattr(storage, 'put', fail_put)
    staged = client.post('/api/participant/pages', headers=auth,
                         files={'file': ('added.jpg', form_photos[0], 'image/jpeg')})
    assert staged.status_code == 200, staged.text
    assert not any(key.endswith('.pdf') for key in attempted), 'Draft registration must not write a PDF'
    draft_state = client.get('/api/participant', headers=auth).json()
    assert draft_state['has_pending_changes'] and draft_state['blank_page_count'] == before['blank_page_count']
    attempted.clear()
    failed = client.post(f'/api/participant/worksheets/{ids[0]}/submit', headers=auth)
    assert failed.status_code == 503
    assert client.get('/api/participant', headers=auth).json() == draft_state
    assert [client.get(url, headers=auth).content for url in urls] == old_pdfs
    assert all(storage.path(path).is_file() for path in old_paths)
    assert all(not storage.path(path).exists() for path in attempted)
    with app.state.db.read() as session:
        assert len(list(session.scalars(select(Page)))) == 1
    monkeypatch.setattr(storage, 'put', put)
    retry = client.post(f'/api/participant/worksheets/{ids[0]}/submit', headers=auth)
    assert retry.status_code == 200, retry.text
    completed = client.get('/api/participant', headers=auth).json()
    assert completed['blank_page_count'] == before['blank_page_count'] - 1
    assert not completed['has_pending_changes']


def test_zero_photo_individual_submission_is_protected_from_expiry(revision_harness, separate_pdfs):
    from app.database import Participant, WorksheetSubmission
    from sqlalchemy import select
    client, _, auth, ids = partial_setup(revision_harness, separate_pdfs)
    assert client.post(f'/api/participant/worksheets/{ids[0]}/submit', headers=auth).status_code == 200
    state = client.get('/api/participant', headers=auth).json()
    assert state['pages'] == [] and state['status'] == 'partially_submitted'
    assert state['blank_page_count'] == 2
    app = revision_harness[1]
    with app.state.db.read() as session:
        participant = session.scalar(select(Participant))
        assert participant.status == 'registered' and participant.submitted_at is not None
        assert session.get(WorksheetSubmission, (participant.submission_id, ids[0])) is not None
    assert client.post(f'/api/participant/worksheets/{ids[1]}/submit', headers=auth).status_code == 200
    assert client.get('/api/participant', headers=auth).json()['status'] == 'submitted'
    data = client.get('/api/participant/pdf', headers=auth).content
    assert len(PdfReader(io.BytesIO(data)).pages) == 3
    for index, original in enumerate(separate_pdfs):
        assert_template_page(data, index, original)


def test_template_pages_use_pinned_files_and_local_page_numbers_in_reversed_selection(revision_harness, separate_pdfs):
    client, csrf, auth, ids = partial_setup(revision_harness, separate_pdfs, reverse=True)
    # Subsequent distribution changes must not replace this participant's original selection.
    assert choose(client, csrf, [ids[1]], active(client)['active_revision_id']).status_code == 200
    response = client.post('/api/participant/submit', headers=auth)
    assert response.status_code == 200, response.text
    first = client.get(f'/api/participant/worksheets/{ids[0]}/submission', headers=auth).content
    second = client.get(f'/api/participant/worksheets/{ids[1]}/submission', headers=auth).content
    assert len(PdfReader(io.BytesIO(first)).pages) == 1
    assert len(PdfReader(io.BytesIO(second)).pages) == 2
    assert_template_page(first, 0, separate_pdfs[2])
    assert_template_page(second, 0, separate_pdfs[0])
    assert_template_page(second, 1, separate_pdfs[1])
    combined = client.get('/api/participant/pdf', headers=auth).content
    assert len(PdfReader(io.BytesIO(combined)).pages) == 3
    for index, source_index in enumerate([2, 0, 1]):
        assert_template_page(combined, index, separate_pdfs[source_index])
    reader = PdfReader(io.BytesIO(combined))
    assert [item.title for item in reader.outline] == ['1: HIRA1-03', '2: HIRA1-01', '3: HIRA1-02']
    assert [reader.get_destination_page_number(item) for item in reader.outline] == [0, 1, 2]


def test_template_download_failure_keeps_worksheet_unsubmitted_and_allows_retry(revision_harness, separate_pdfs):
    from app.database import WorksheetSubmission
    from sqlalchemy import select
    client, _, auth, ids = partial_setup(revision_harness, separate_pdfs)
    revision_harness[3].fail_next_get = True
    url = f'/api/participant/worksheets/{ids[0]}/submit'
    response = client.post(url, headers=auth)
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'submission_failed'
    state = client.get('/api/participant', headers=auth).json()
    assert state['status'] == 'registered'
    assert all(file['status'] == 'registered' for file in state['worksheet_submissions'])
    with revision_harness[1].state.db.read() as session:
        assert list(session.scalars(select(WorksheetSubmission))) == []
    assert client.post(url, headers=auth).status_code == 200
    data = client.get(f'/api/participant/worksheets/{ids[0]}/submission', headers=auth).content
    assert_template_page(data, 0, separate_pdfs[0])
    assert_template_page(data, 1, separate_pdfs[1])


def test_individual_submission_auth_ownership_and_retry(revision_harness, separate_pdfs):
    from app.database import WorksheetSubmission
    from sqlalchemy import select
    client, _, auth, ids = partial_setup(revision_harness, separate_pdfs)
    url = f'/api/participant/worksheets/{ids[0]}/submit'
    assert client.post(url).status_code == 401
    assert client.post(f'/api/participant/worksheets/{uuid.uuid4()}/submit', headers=auth).status_code == 404
    assert client.post(url, headers=auth).status_code == 200
    result_url = f'/api/participant/worksheets/{ids[0]}/submission'
    before = client.get(result_url, headers=auth).content
    other = register(client)
    assert client.get(result_url, headers=other).status_code == 404
    assert client.get(result_url).status_code == 401
    assert client.post(url, headers=auth).status_code == 200
    assert client.get(result_url, headers=auth).content == before
    with revision_harness[1].state.db.read() as session:
        assert len(list(session.scalars(select(WorksheetSubmission)))) == 1


def test_last_file_combined_storage_failure_rolls_back_only_new_submission(revision_harness, separate_pdfs, monkeypatch):
    client, _, auth, ids = partial_setup(revision_harness, separate_pdfs)
    assert client.post(f'/api/participant/worksheets/{ids[0]}/submit', headers=auth).status_code == 200
    before = client.get(f'/api/participant/worksheets/{ids[0]}/submission', headers=auth).content
    storage = revision_harness[3]
    put = storage.put
    def fail_combined(key, data, content_type):
        if key.endswith('/P001.pdf'):
            raise OSError('Simulated combined-PDF failure after saving the individual PDF')
        return put(key, data, content_type)
    monkeypatch.setattr(storage, 'put', fail_combined)
    assert client.post(f'/api/participant/worksheets/{ids[1]}/submit', headers=auth).status_code == 503
    state = client.get('/api/participant', headers=auth).json()
    assert [f['status'] for f in state['worksheet_submissions']] == ['submitted', 'registered']
    assert client.get(f'/api/participant/worksheets/{ids[0]}/submission', headers=auth).content == before
    monkeypatch.setattr(storage, 'put', put)
    assert client.post(f'/api/participant/worksheets/{ids[1]}/submit', headers=auth).status_code == 200


def test_concurrent_individual_submission_is_idempotent(revision_harness, separate_pdfs):
    from concurrent.futures import ThreadPoolExecutor
    from app.database import WorksheetSubmission
    from sqlalchemy import select
    client, _, auth, ids = partial_setup(revision_harness, separate_pdfs)
    url = f'/api/participant/worksheets/{ids[0]}/submit'
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(lambda _: client.post(url, headers=auth), range(3)))
    assert all(response.status_code == 200 for response in responses)
    with revision_harness[1].state.db.read() as session:
        assert len(list(session.scalars(select(WorksheetSubmission)))) == 1


def test_lost_individual_commit_acknowledgement_retains_pdf(revision_harness, separate_pdfs, monkeypatch):
    client, _, auth, ids = partial_setup(revision_harness, separate_pdfs)
    session_class = revision_harness[1].state.db.sessions.class_
    commit = session_class.commit
    first = [True]
    def commit_then_fail(session):
        commit(session)
        if first[0]:
            first[0] = False
            raise OSError('Lost acknowledgement after successful individual submission')
    monkeypatch.setattr(session_class, 'commit', commit_then_fail)
    url = f'/api/participant/worksheets/{ids[0]}/submit'
    assert client.post(url, headers=auth).status_code == 503
    result_url = f'/api/participant/worksheets/{ids[0]}/submission'
    before = client.get(result_url, headers=auth).content
    assert before.startswith(b'%PDF-')
    assert client.post(url, headers=auth).status_code == 200
    assert client.get(result_url, headers=auth).content == before


@pytest.mark.parametrize('missing_first', [False, True])
def test_legacy_combined_submission_is_read_and_can_be_retaken(revision_harness, separate_pdfs, form_photos, retaken_form_photo, missing_first):
    from app.database import Participant, WorksheetSubmission
    from sqlalchemy import delete, select
    client, _, auth, ids = partial_setup(revision_harness, separate_pdfs)
    for photo in form_photos[1:] if missing_first else form_photos:
        response = client.post('/api/participant/pages', headers=auth, files={'file': ('form.jpg', photo, 'image/jpeg')})
        assert response.status_code == 200, response.text
    assert client.post('/api/participant/submit', headers=auth).status_code == 200
    before = client.get('/api/participant/pdf', headers=auth).content
    with revision_harness[1].state.db.write() as session:
        session.execute(delete(WorksheetSubmission))
        participant = session.scalar(select(Participant))
        submitted_at = participant.submitted_at
    state = client.get('/api/participant', headers=auth).json()
    assert all(file['status'] == 'submitted' for file in state['worksheet_submissions'])
    for index, expected in enumerate([2, 1]):
        response = client.get(f'/api/participant/worksheets/{ids[index]}/submission', headers=auth)
        assert response.status_code == 200
        assert len(PdfReader(io.BytesIO(response.content)).pages) == expected
    assert client.get('/api/participant/pdf', headers=auth).content == before
    target_number = 3 if missing_first else 1
    old_page = next(page for page in state['pages'] if page['page_number'] == target_number)
    retaken = retaken_form_photo
    if missing_first:
        from PIL import Image, ImageDraw
        with Image.open(io.BytesIO(form_photos[2])) as source:
            photo = source.convert('RGB')
        ImageDraw.Draw(photo).rectangle((600, 650, 800, 700), fill=(25, 25, 25))
        output = io.BytesIO()
        photo.save(output, format='JPEG', quality=95)
        retaken = output.getvalue()
    replacement = client.post('/api/participant/pages', headers=auth,
                              files={'file': ('retaken-legacy.jpg', retaken, 'image/jpeg')},
                              data={'replace': 'true', 'expected_version': old_page['version']})
    assert replacement.status_code == 200, replacement.text
    updated = replacement.json()['participant']
    assert updated['status'] == 'submitted' and updated['submitted_worksheet_count'] == 2
    assert updated['has_pending_changes'] and updated['pending_page_count'] == 1
    assert updated['blank_page_count'] == int(missing_first)
    assert replacement.json()['page']['version'] != old_page['version']
    assert [file['submitted_at'] for file in updated['worksheet_submissions']] == [file['submitted_at'] for file in state['worksheet_submissions']]
    assert client.get('/api/participant/pdf', headers=auth).content == before
    target_id = ids[1] if missing_first else ids[0]
    submitted = client.post(f'/api/participant/worksheets/{target_id}/submit', headers=auth)
    assert submitted.status_code == 200, submitted.text
    updated = client.get('/api/participant', headers=auth).json()
    assert not updated['has_pending_changes']
    combined = client.get('/api/participant/pdf', headers=auth).content
    first = client.get(f'/api/participant/worksheets/{ids[0]}/submission', headers=auth).content
    second = client.get(f'/api/participant/worksheets/{ids[1]}/submission', headers=auth).content
    for page in updated['pages']:
        index = page['page_number'] - 1
        assert_submitted_photo(client, auth, combined, index, page)
        assert_submitted_photo(client, auth, first if index < 2 else second, index if index < 2 else 0, page)
    if missing_first:
        assert_template_page(combined, 0, separate_pdfs[0])
        assert_template_page(first, 0, separate_pdfs[0])
        assert updated['worksheet_submissions'][0]['blank_qr_codes'] == ['HIRA1-01']
    with revision_harness[1].state.db.read() as session:
        rows = list(session.scalars(select(WorksheetSubmission)))
        assert len(rows) == 2 and {row.worksheet_id for row in rows} == set(ids)
        assert next(row for row in rows if row.worksheet_id == target_id).submitted_at != submitted_at
        assert next(row for row in rows if row.worksheet_id != target_id).submitted_at == submitted_at
        assert session.scalar(select(Participant)).submitted_at == submitted_at


def test_sparse_legacy_pdf_without_outline_keeps_old_positions_when_photo_is_added(revision_harness, separate_pdfs, form_photos):
    from app.database import Participant, WorksheetSubmission
    from sqlalchemy import delete, select
    client, _, auth, ids = partial_setup(revision_harness, separate_pdfs)
    old_pages = []
    for index in (1, 2):
        uploaded = client.post('/api/participant/pages', headers=auth,
                               files={'file': ('old.jpg', form_photos[index], 'image/jpeg')})
        assert uploaded.status_code == 200, uploaded.text
        old_pages.append(uploaded.json()['page'])
    assert client.post('/api/participant/submit', headers=auth).status_code == 200
    source = PdfReader(io.BytesIO(client.get('/api/participant/pdf', headers=auth).content))
    writer, output = PdfWriter(), io.BytesIO()
    for index in (1, 2):
        writer.add_page(source.pages[index])
    writer.write(output)
    sparse_pdf = output.getvalue()
    assert len(PdfReader(io.BytesIO(sparse_pdf)).pages) == 2
    assert PdfReader(io.BytesIO(sparse_pdf)).outline == []
    app, storage = revision_harness[1], revision_harness[3]
    with app.state.db.write() as session:
        participant = session.scalar(select(Participant))
        legacy_path = f'{participant.participant_id}/{participant.submission_id}/legacy-without-outline.pdf'
        storage.put(legacy_path, sparse_pdf, 'application/pdf')
        participant.pdf_path = legacy_path
        session.execute(delete(WorksheetSubmission))
    added = client.post('/api/participant/pages', headers=auth,
                        files={'file': ('added-first-page.jpg', form_photos[0], 'image/jpeg')})
    assert added.status_code == 200, added.text
    assert added.json()['participant']['pending_page_count'] == 1
    assert client.get('/api/participant/pdf', headers=auth).content == sparse_pdf
    first = client.get(f'/api/participant/worksheets/{ids[0]}/submission', headers=auth)
    second = client.get(f'/api/participant/worksheets/{ids[1]}/submission', headers=auth)
    assert first.status_code == second.status_code == 200
    assert_submitted_photo(client, auth, first.content, 1, old_pages[0])
    assert_submitted_photo(client, auth, second.content, 0, old_pages[1])
    submitted = client.post(f'/api/participant/worksheets/{ids[0]}/submit', headers=auth)
    assert submitted.status_code == 200, submitted.text
    completed = client.get('/api/participant', headers=auth).json()
    assert not completed['has_pending_changes'] and completed['blank_page_count'] == 0
    combined = client.get('/api/participant/pdf', headers=auth).content
    assert len(PdfReader(io.BytesIO(combined)).pages) == 3
    for page in [added.json()['page'], *old_pages]:
        assert_submitted_photo(client, auth, combined, page['page_number'] - 1, page)
    assert_submitted_photo(client, auth,
                           client.get(f'/api/participant/worksheets/{ids[1]}/submission', headers=auth).content,
                           0, old_pages[1])
