"""Integration checks for registration, authorization, durable replacement and exports."""
from __future__ import annotations

import csv
import io
import uuid
from concurrent.futures import ThreadPoolExecutor
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import func, select

from app.database import Page, Participant
from app.main import create_app
from app.security import hash_password
from app.settings import Settings
from app.storage import LocalStorage
from scripts.generate_fixtures import generate

PASSWORD = "integration-password-2026"


class FailingStorage(LocalStorage):
    fail_content_type: str | None = None

    def put(self, key, data, content_type):
        if content_type == self.fail_content_type:
            self.fail_content_type = None
            raise OSError("Simulated storage failure")
        super().put(key, data, content_type)


@pytest.fixture(scope="session")
def password_hash():
    return hash_password(PASSWORD)


@pytest.fixture(scope="session")
def photos(tmp_path_factory):
    return {index + 1: path.read_bytes() for index, path in enumerate(generate(tmp_path_factory.mktemp("study-photos")))}


@pytest.fixture(scope="session")
def replacement_photo(photos):
    from PIL import Image, ImageDraw
    with Image.open(io.BytesIO(photos[1])) as source:
        image = source.convert("RGB")
    ImageDraw.Draw(image).rectangle((800, 850, 1000, 900), fill=(25, 25, 25))
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=95)
    return output.getvalue()


def assert_pdf_photo_matches_preview(data, index, preview):
    from PIL import Image
    embedded = list(PdfReader(io.BytesIO(data)).pages[index].images)
    assert len(embedded) == 1
    with Image.open(io.BytesIO(preview)) as corrected:
        actual, expected = embedded[0].image.convert("RGB"), corrected.convert("RGB")
        assert actual.size == expected.size
        assert actual.tobytes() == expected.tobytes()


@pytest.fixture
def harness(tmp_path, password_hash):
    settings = Settings(data_dir=tmp_path, session_secret="integration-session-secret-" + "x" * 40,
                        researcher_password_hash=password_hash)
    storage = FailingStorage(tmp_path)
    app = create_app(settings, storage_override=storage)
    with TestClient(app) as client:
        yield client, app, storage


def register(client, key=None):
    response = client.post("/api/register", json={
        "consent": True, "consent_version": client.get("/api/config").json()["consent_version"],
        "idempotency_key": key or str(uuid.uuid4()),
    })
    assert response.status_code == 200, response.text
    return response.json()


def auth(registration):
    return {"Authorization": "Bearer " + registration["resume_token"]}


def upload(client, registration, data, **form):
    return client.post("/api/participant/pages", headers=auth(registration),
                       files={"file": ("phone-photo.jpg", data, "image/jpeg")}, data=form)


def login(client):
    response = client.post("/api/researcher/login", json={"password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def zipped(client, headers, *, types, ids=None, scope="selected"):
    response = client.post("/api/researcher/download", headers=headers, json={
        "scope": scope, "participant_ids": ids or [], "types": types,
    })
    assert response.status_code == 200, response.text
    return ZipFile(io.BytesIO(response.content))


def test_consent_and_registration_persist_without_personal_fields(harness):
    client, app, _ = harness
    config = client.get("/api/config").json()
    for consent, version in [(False, config["consent_version"]), (True, "old-consent")]:
        response = client.post("/api/register", json={"consent": consent, "consent_version": version,
                                                     "idempotency_key": str(uuid.uuid4())})
        assert response.status_code == 400
    participant = register(client)
    assert participant["participant_id"] == "P001"
    assert len(participant["resume_token"]) >= 32
    assert participant["resume_url"].endswith("/#" + participant["resume_token"])
    with app.state.db.read() as session:
        row = session.scalar(select(Participant))
        assert row.registered_at is not None
        assert row.consent_version == config["consent_version"]
        assert row.consent_text == config["consent_text"]
        assert row.token_hash != participant["resume_token"]
        assert row.registration_key_hash != participant["resume_token"]
    assert client.get("/api/participant", headers={"Authorization": "Bearer P001"}).status_code == 401


def test_simultaneous_registration_and_retry_are_unique_and_idempotent(harness):
    client, app, _ = harness
    common = str(uuid.uuid4())
    keys = [common] * 8 + [str(uuid.uuid4()) for _ in range(12)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(lambda key: register(client, key), keys))
    assert len({row["participant_id"] for row in records[:8]}) == 1
    assert len({row["resume_token"] for row in records[:8]}) == 1
    assert len({row["participant_id"] for row in records}) == 13
    assert len({row["resume_token"] for row in records}) == 13
    with app.state.db.read() as session:
        assert session.scalar(select(func.count()).select_from(Participant)) == 13


def test_participant_and_researcher_endpoints_reject_unauthorized_access(harness):
    client, _, _ = harness
    for endpoint in ["/api/participant", "/api/participant/pdf", "/api/participant/pages/HIRA1-01/preview",
                     "/api/researcher/participants", "/api/researcher/session"]:
        assert client.get(endpoint).status_code == 401
    assert client.post("/api/participant/submit").status_code == 401
    download_body = {"scope": "submitted", "types": ["pdf"]}
    assert client.post("/api/researcher/download", json=download_body).status_code == 401
    assert client.post("/api/researcher/login", json={"password": "incorrect"}).status_code == 401
    csrf = login(client)
    assert client.get("/api/researcher/participants").status_code == 200
    assert client.post("/api/researcher/download", json=download_body).status_code == 403
    assert client.post("/api/researcher/logout").status_code == 403
    assert client.post("/api/researcher/logout", headers=csrf).status_code == 200
    assert client.get("/api/researcher/participants").status_code == 401
    assert client.post("/api/register", headers={"Origin": "https://other.invalid"}, json={
        "consent": True, "consent_version": "test-v1", "idempotency_key": str(uuid.uuid4()),
    }).status_code == 403


def test_unreadable_qr_does_not_add_page(harness):
    from PIL import Image
    output = io.BytesIO()
    Image.new("RGB", (1000, 1400), "white").save(output, format="PNG")
    client, _, _ = harness
    participant = register(client)
    response = upload(client, participant, output.getvalue())
    assert response.status_code == 422
    assert response.json()["detail"]["code"] in {"qr_unreadable", "qr_not_found"}
    assert client.get("/api/participant", headers=auth(participant)).json()["pages"] == []
    original = client.get('/api/participant/worksheet', headers=auth(participant)).content
    assert client.post("/api/participant/submit", headers=auth(participant)).status_code == 200
    data = client.get('/api/participant/pdf', headers=auth(participant)).content
    document = PdfReader(io.BytesIO(data))
    assert len(document.pages) == 3
    import fitz
    with fitz.open(stream=data, filetype='pdf') as result, fitz.open(stream=original, filetype='pdf') as template:
        for index in range(3):
            actual, expected = result[index].get_pixmap(), template[index].get_pixmap()
            assert (actual.width, actual.height, actual.samples) == (expected.width, expected.height, expected.samples)


def test_unordered_photos_produce_ordered_pdf_and_selected_zip(harness, photos):
    client, app, storage = harness
    participant = register(client)
    other = register(client)
    pid = participant["participant_id"]
    for number in (3, 1, 2):
        response = upload(client, participant, photos[number])
        assert response.status_code == 200, response.text
        assert response.json()["page"]["page_number"] == number
        if number != 2:
            assert client.get('/api/participant', headers=auth(participant)).json()['status'] == 'registered'
    state = client.get("/api/participant", headers=auth(participant)).json()
    assert [page["page_number"] for page in state["pages"]] == [1, 2, 3]
    assert not state["missing_pages"]
    preview = client.get(state["pages"][0]["preview_url"], headers=auth(participant))
    assert preview.status_code == 200 and preview.content.startswith(b"\x89PNG")
    # A second participant's key does not gain access to the first participant's page.
    assert client.get(state["pages"][0]["preview_url"], headers=auth(other)).status_code == 404
    submitted = client.post("/api/participant/submit", headers=auth(participant))
    assert submitted.status_code == 200, submitted.text
    pdf_response = client.get("/api/participant/pdf", headers=auth(participant))
    assert pdf_response.headers["content-disposition"].endswith(f'"{pid}.pdf"')
    pdf = PdfReader(io.BytesIO(pdf_response.content))
    assert len(pdf.pages) == 3
    assert [item.title for item in pdf.outline] == ["1: HIRA1-01", "2: TEST-02", "3: TEST-03"]
    for index, item in enumerate(pdf.outline):
        assert pdf.get_destination_page_number(item) == index
    # Compare actual page pixels as well as navigation labels: upload order
    # must not survive in the images behind correctly ordered outline text.
    from PIL import Image
    with app.state.db.read() as session:
        submission = session.scalar(select(Participant.submission_id).where(Participant.id == int(pid[1:])))
        corrected_pages = list(session.scalars(select(Page).where(Page.submission_id == submission).order_by(Page.page_number)))
    for pdf_page, corrected_page in zip(pdf.pages, corrected_pages, strict=True):
        embedded = list(pdf_page.images)
        assert len(embedded) == 1
        with Image.open(io.BytesIO(storage.get(corrected_page.corrected_path))) as corrected:
            assert embedded[0].image.convert("RGB").size == corrected.size
            assert embedded[0].image.convert("RGB").tobytes() == corrected.convert("RGB").tobytes()
    assert client.post("/api/participant/submit", headers=auth(participant)).status_code == 200
    duplicate = upload(client, participant, photos[1])
    assert duplicate.status_code == 409 and duplicate.json()["detail"]["code"] == "duplicate_page"
    csrf = login(client)
    listing = client.get("/api/researcher/participants").json()["participants"]
    assert [(row["status"], row["page_count"]) for row in listing] == [("submitted", 3), ("registered", 0)]
    with zipped(client, csrf, types=["corrected", "csv"], ids=[pid]) as archive:
        names = archive.namelist()
        assert len(names) == 4
        assert all(name.startswith(pid + "/") for name in names)
        assert sum("/corrected/" in name for name in names) == 3
        assert not any("/original/" in name or name.endswith(".pdf") for name in names)
        rows = list(csv.DictReader(io.StringIO(archive.read(f"{pid}/mapping.csv").decode("utf-8-sig"))))
        assert [row["qr_code"] for row in rows] == ["HIRA1-01", "TEST-02", "TEST-03"]
        assert [int(row["page_number"]) for row in rows] == [1, 2, 3]
        for row in rows:
            assert row["participant_id"] == pid
            assert f'{pid}/{row["corrected_filename"]}' in names
    with zipped(client, csrf, types=["pdf"], scope="submitted") as archive:
        assert archive.namelist() == [f"{pid}/{pid}.pdf"]
        assert archive.read(f"{pid}/{pid}.pdf") == pdf_response.content
    with zipped(client, csrf, types=["original"], ids=[pid]) as archive:
        assert len(archive.namelist()) == 3
        assert all("/original/" in name and name.endswith(".jpg") for name in archive.namelist())
        assert sorted(archive.read(name) for name in archive.namelist()) == sorted(photos.values())
    with zipped(client, csrf, types=["csv"], ids=[other["participant_id"]]) as archive:
        assert archive.namelist() == [f'{other["participant_id"]}/mapping.csv']
    assert client.post("/api/researcher/download", headers=csrf, json={
        "scope": "selected", "participant_ids": [other["participant_id"]], "types": ["pdf"],
    }).status_code == 409


@pytest.mark.parametrize("submitted", [False, True])
def test_duplicate_replacement_preserves_old_data_until_new_save_succeeds(harness, photos, replacement_photo, submitted):
    client, app, storage = harness
    participant = register(client)
    first = upload(client, participant, photos[1])
    assert first.status_code == 200, first.text
    old_page = first.json()["page"]
    old_preview = client.get(old_page["preview_url"], headers=auth(participant)).content
    with app.state.db.read() as session:
        before = session.scalar(select(Page))
        old_paths = (before.original_path, before.corrected_path)
    if submitted:
        assert client.post("/api/participant/submit", headers=auth(participant)).status_code == 200
        old_pdf = client.get("/api/participant/pdf", headers=auth(participant)).content
        old_state = client.get("/api/participant", headers=auth(participant)).json()
    duplicate = upload(client, participant, photos[1])
    assert duplicate.status_code == 409 and duplicate.json()["detail"]["code"] == "duplicate_page"
    bad = upload(client, participant, b"not a photograph", replace="true", expected_version=old_page["version"])
    assert bad.status_code == 422
    storage.fail_content_type = "image/png"
    failed = upload(client, participant, photos[1], replace="true", expected_version=old_page["version"])
    assert failed.status_code == 503
    state = client.get("/api/participant", headers=auth(participant)).json()
    assert state["pages"][0]["version"] == old_page["version"]
    assert client.get(old_page["preview_url"], headers=auth(participant)).content == old_preview
    assert all(storage.path(path).is_file() for path in old_paths)
    if submitted:
        assert client.get("/api/participant/pdf", headers=auth(participant)).content == old_pdf
        storage.fail_content_type = "application/pdf"
    successful = upload(client, participant, replacement_photo, replace="true", expected_version=old_page["version"])
    assert successful.status_code == 200, successful.text
    assert successful.json()["page"]["version"] != old_page["version"]
    assert len(successful.json()["participant"]["pages"]) == 1
    assert all(not storage.path(path).exists() for path in old_paths)
    if submitted:
        state = successful.json()["participant"]
        assert state["status"] == "submitted" and state["blank_page_count"] == 2
        assert state["has_pending_changes"] and state["pending_page_count"] == 1
        assert state["worksheet_submissions"][0]["pending_qr_codes"] == ["HIRA1-01"]
        assert state["worksheet_submissions"][0]["submitted_at"] == old_state["worksheet_submissions"][0]["submitted_at"]
        assert storage.fail_content_type == "application/pdf", "Registering a draft must not write a PDF"
        assert client.get("/api/participant/pdf", headers=auth(participant)).content == old_pdf
        assert client.get("/api/participant", headers=auth(participant)).json() == state
        failed_pdf = client.post("/api/participant/submit", headers=auth(participant))
        assert failed_pdf.status_code == 503
        assert client.get("/api/participant", headers=auth(participant)).json() == state
        assert client.get("/api/participant/pdf", headers=auth(participant)).content == old_pdf
        assert client.post("/api/participant/submit", headers=auth(participant)).status_code == 200
        completed = client.get("/api/participant", headers=auth(participant)).json()
        assert not completed["has_pending_changes"] and completed["pending_page_count"] == 0
        assert completed["worksheet_submissions"][0]["submitted_at"] != old_state["worksheet_submissions"][0]["submitted_at"]
        updated_pdf = client.get("/api/participant/pdf", headers=auth(participant)).content
        preview = client.get(old_page["preview_url"], headers=auth(participant)).content
        assert preview != old_preview
        assert_pdf_photo_matches_preview(updated_pdf, 0, preview)
        assert client.get(state["worksheet_submissions"][0]["pdf_url"], headers=auth(participant)).content == updated_pdf
        csrf = login(client)
        pid = participant["participant_id"]
        with zipped(client, csrf, types=["pdf", "original", "corrected"], scope="submitted") as archive:
            assert archive.read(f"{pid}/{pid}.pdf") == updated_pdf
            assert archive.read(f"{pid}/original/001_HIRA1-01.jpg") == replacement_photo
            assert archive.read(f"{pid}/corrected/001_HIRA1-01.png") == preview
    stale = upload(client, participant, photos[1], replace="true", expected_version=old_page["version"])
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "version_conflict"


def test_concurrent_submitted_replacement_accepts_only_one_expected_version(harness, photos, replacement_photo):
    client, app, _ = harness
    participant = register(client)
    initial = upload(client, participant, photos[1]).json()["page"]
    assert client.post("/api/participant/submit", headers=auth(participant)).status_code == 200
    previous_pdf = client.get("/api/participant/pdf", headers=auth(participant)).content
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: upload(client, participant, replacement_photo,
                                                   replace="true", expected_version=initial["version"]), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    rejected = next(response for response in responses if response.status_code == 409)
    assert rejected.json()["detail"]["code"] == "version_conflict"
    state = client.get("/api/participant", headers=auth(participant)).json()
    assert state["status"] == "submitted" and len(state["pages"]) == 1
    assert state["has_pending_changes"] and state["pending_page_count"] == 1
    assert client.get("/api/participant/pdf", headers=auth(participant)).content == previous_pdf
    with app.state.db.read() as session:
        assert session.scalar(select(func.count()).select_from(Page)) == 1
    assert client.post("/api/participant/submit", headers=auth(participant)).status_code == 200
    data = client.get("/api/participant/pdf", headers=auth(participant)).content
    preview = client.get(state["pages"][0]["preview_url"], headers=auth(participant)).content
    assert_pdf_photo_matches_preview(data, 0, preview)


def test_draft_registration_skips_pdf_work_and_resubmission_reuses_previous_pages(harness, photos, replacement_photo, monkeypatch):
    client, app, storage = harness
    participant = register(client)
    for number in (1, 2, 3):
        assert upload(client, participant, photos[number]).status_code == 200
    assert client.post("/api/participant/submit", headers=auth(participant)).status_code == 200
    state = client.get("/api/participant", headers=auth(participant)).json()
    old_pdf = client.get("/api/participant/pdf", headers=auth(participant)).content
    with app.state.db.read() as session:
        old_pngs = {page.corrected_path for page in session.scalars(select(Page))}
    original_get, reads = storage.get, []

    def track_reads(key):
        reads.append(key)
        return original_get(key)

    monkeypatch.setattr(storage, "get", track_reads)
    staged = upload(client, participant, replacement_photo, replace="true", expected_version=state["pages"][0]["version"])
    assert staged.status_code == 200, staged.text
    assert reads == [], "Registering a photo must not read earlier PDFs or corrected photographs"
    assert staged.json()["participant"]["has_pending_changes"]
    with app.state.db.read() as session:
        new_png = session.scalar(select(Page.corrected_path).where(Page.page_number == 1))
    assert client.post("/api/participant/submit", headers=auth(participant)).status_code == 200
    assert not old_pngs.intersection(reads), "Unchanged pages must come from the previous PDF"
    assert reads.count(new_png) == 1
    updated_pdf = client.get("/api/participant/pdf", headers=auth(participant)).content
    import fitz
    with fitz.open(stream=old_pdf, filetype="pdf") as before, fitz.open(stream=updated_pdf, filetype="pdf") as after:
        for index in (1, 2):
            assert before[index].get_pixmap().samples == after[index].get_pixmap().samples
        assert before[0].get_pixmap().samples != after[0].get_pixmap().samples
    no_changes = client.get("/api/participant", headers=auth(participant)).json()
    assert not no_changes["has_pending_changes"]
    reads.clear()
    assert client.post("/api/participant/submit", headers=auth(participant)).status_code == 200
    assert reads == [], "Retrying without new changes must not rebuild a PDF"
    assert client.get("/api/participant/pdf", headers=auth(participant)).content == updated_pdf


def test_pdf_download_recovers_when_replacement_removes_authenticated_pdf(harness, photos, replacement_photo, monkeypatch):
    client, app, storage = harness
    participant = register(client)
    first = upload(client, participant, photos[1])
    assert first.status_code == 200, first.text
    old_page = first.json()["page"]
    assert client.post("/api/participant/submit", headers=auth(participant)).status_code == 200
    with app.state.db.read() as session:
        old_pdf_key = session.scalar(select(Participant.pdf_path))
    original_get = storage.get
    reads = []
    replaced = False

    def replace_before_reading_old_pdf(key):
        nonlocal replaced
        reads.append(key)
        if key == old_pdf_key and not replaced:
            replaced = True
            response = upload(client, participant, replacement_photo,
                              replace="true", expected_version=old_page["version"])
            assert response.status_code == 200, response.text
            assert client.post("/api/participant/submit", headers=auth(participant)).status_code == 200
            assert not storage.path(old_pdf_key).exists()
        return original_get(key)

    monkeypatch.setattr(storage, "get", replace_before_reading_old_pdf)
    response = client.get("/api/participant/pdf", headers=auth(participant))
    assert response.status_code == 200, response.text
    assert replaced
    with app.state.db.read() as session:
        latest_pdf_key = session.scalar(select(Participant.pdf_path))
    assert latest_pdf_key != old_pdf_key
    assert old_pdf_key in reads and reads[-1] == latest_pdf_key
    assert response.content == original_get(latest_pdf_key)
    preview = client.get(old_page["preview_url"], headers=auth(participant))
    assert preview.status_code == 200, preview.text
    assert_pdf_photo_matches_preview(response.content, 0, preview.content)


def test_pdf_download_storage_failure_returns_retryable_error(harness, monkeypatch):
    client, app, storage = harness
    participant = register(client)
    assert client.post("/api/participant/submit", headers=auth(participant)).status_code == 200
    with app.state.db.read() as session:
        pdf_key = session.scalar(select(Participant.pdf_path))

    def unavailable_storage(key):
        raise OSError("Simulated storage read failure")

    monkeypatch.setattr(storage, "get", unavailable_storage)
    response = client.get("/api/participant/pdf", headers=auth(participant))
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "storage_failed"
    assert storage.path(pdf_key).is_file()
    assert client.get("/api/participant", headers=auth(participant)).json()["status"] == "submitted"


def test_pdf_storage_failure_keeps_unsubmitted_and_allows_retry(harness, photos):
    client, app, storage = harness
    participant = register(client)
    for number in (1, 2, 3):
        response = upload(client, participant, photos[number])
        assert response.status_code == 200, response.text
    storage.fail_content_type = "application/pdf"
    failed = client.post("/api/participant/submit", headers=auth(participant))
    assert failed.status_code == 503
    assert failed.json()["detail"]["code"] == "submission_failed"
    assert client.get("/api/participant", headers=auth(participant)).json()["status"] == "registered"
    assert client.get("/api/participant/pdf", headers=auth(participant)).status_code == 404
    with app.state.db.read() as session:
        row = session.scalar(select(Participant))
        assert row.pdf_path is None and row.submitted_at is None
    assert client.post("/api/participant/submit", headers=auth(participant)).status_code == 200
    assert client.get("/api/participant/pdf", headers=auth(participant)).status_code == 200


def test_lost_commit_acknowledgement_preserves_committed_page_and_pdf(harness, photos, replacement_photo, monkeypatch):
    client, app, storage = harness
    participant = register(client)
    session_class = app.state.db.sessions.class_
    original_commit = session_class.commit
    lose_acknowledgement = {"next": True}

    def commit_then_drop_reply(session):
        original_commit(session)
        if lose_acknowledgement["next"]:
            lose_acknowledgement["next"] = False
            raise OSError("Database committed but its acknowledgement was lost")

    monkeypatch.setattr(session_class, "commit", commit_then_drop_reply)
    uncertain_upload = upload(client, participant, photos[1])
    assert uncertain_upload.status_code == 503
    assert uncertain_upload.json()["detail"]["code"] == "commit_uncertain"
    state = client.get("/api/participant", headers=auth(participant)).json()
    assert len(state["pages"]) == 1
    assert client.get(state["pages"][0]["preview_url"], headers=auth(participant)).status_code == 200
    with app.state.db.read() as session:
        page = session.scalar(select(Page))
        assert storage.get(page.original_path) == photos[1]
        assert storage.get(page.corrected_path).startswith(b"\x89PNG")
    assert upload(client, participant, photos[1]).status_code == 409
    for number in (2, 3):
        assert upload(client, participant, photos[number]).status_code == 200
    lose_acknowledgement["next"] = True
    uncertain_submit = client.post("/api/participant/submit", headers=auth(participant))
    assert uncertain_submit.status_code == 503
    assert uncertain_submit.json()["detail"]["code"] == "commit_uncertain"
    assert client.get("/api/participant", headers=auth(participant)).json()["status"] == "submitted"
    with app.state.db.read() as session:
        row = session.scalar(select(Participant))
        committed_pdf_path = row.pdf_path
        committed_pdf = storage.get(committed_pdf_path)
    assert committed_pdf.startswith(b"%PDF")
    assert client.post("/api/participant/submit", headers=auth(participant)).status_code == 200
    assert client.get("/api/participant/pdf", headers=auth(participant)).content == committed_pdf
    with app.state.db.read() as session:
        assert session.scalar(select(Participant)).pdf_path == committed_pdf_path
        old_page = session.scalar(select(Page).where(Page.page_number == 1))
        old_paths = [old_page.original_path, old_page.corrected_path, committed_pdf_path]
        old_version = old_page.version
    lose_acknowledgement["next"] = True
    uncertain_replacement = upload(client, participant, replacement_photo, replace="true", expected_version=old_version)
    assert uncertain_replacement.status_code == 503
    assert uncertain_replacement.json()["detail"]["code"] == "commit_uncertain"
    state = client.get("/api/participant", headers=auth(participant)).json()
    assert state["status"] == "submitted" and state["pages"][0]["version"] != old_version
    assert state["has_pending_changes"]
    assert all(storage.path(path).is_file() for path in old_paths)
    assert client.get("/api/participant/pdf", headers=auth(participant)).content == committed_pdf
    lose_acknowledgement["next"] = True
    uncertain_resubmission = client.post("/api/participant/submit", headers=auth(participant))
    assert uncertain_resubmission.status_code == 503
    assert uncertain_resubmission.json()["detail"]["code"] == "commit_uncertain"
    assert not client.get("/api/participant", headers=auth(participant)).json()["has_pending_changes"]
    updated_pdf = client.get("/api/participant/pdf", headers=auth(participant)).content
    updated_preview = client.get(state["pages"][0]["preview_url"], headers=auth(participant)).content
    assert_pdf_photo_matches_preview(updated_pdf, 0, updated_preview)
    with app.state.db.read() as session:
        row = session.scalar(select(Participant))
        assert row.pdf_path != committed_pdf_path
        assert storage.get(row.pdf_path) == updated_pdf
    stale = upload(client, participant, replacement_photo, replace="true", expected_version=old_version)
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "version_conflict"


def test_researcher_session_secret_rotation_keeps_registration_retry_stable(tmp_path, password_hash):
    key = str(uuid.uuid4())
    shared = {"data_dir": tmp_path, "researcher_password_hash": password_hash,
              "participant_token_secret": "study-participant-secret-" + "p" * 40}
    first = Settings(**shared, session_secret="old-researcher-secret-" + "a" * 40)
    with TestClient(create_app(first)) as client:
        before = register(client, key)
    rotated = Settings(**shared, session_secret="new-researcher-secret-" + "b" * 40)
    with TestClient(create_app(rotated)) as client:
        after = register(client, key)
        assert after == before
        assert client.get("/api/participant", headers=auth(after)).status_code == 200
