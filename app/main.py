import csv
import hmac
import io
import json
import logging
import re
import tempfile
import threading
import time
import uuid
import zipfile
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.database import CommitUncertain, Database, ExpiredRegistration, Page, Participant, StudyRevision, StudyState, WorksheetSubmission, now, utc_iso
from app.revisions import RevisionConflict, RevisionMissing, RevisionNotProduction, StudyRegistry, WorksheetSelectionError
from app.security import create_session, digest_token, read_session, registration_token, verify_password
from app.settings import ROOT, Settings
from app.storage import LocalStorage, SupabaseStorage
from app.worksheet import worksheet_info
from app.submissions import legacy_worksheet_pdf, pending_pages, submission_rows, worksheet_groups, worksheet_pdf_name, worksheet_states

log = logging.getLogger('handwriting')


def fail(status, code, message, **extra):
    raise HTTPException(status, detail={'code': code, 'message': message, **extra})


def registration_expired():
    fail(410, 'registration_expired', 'この参加者IDは、写真が登録されないまま24時間を過ぎたため削除されました。研究説明を確認して、新しく参加登録してください。')


class Registration(BaseModel):
    consent: bool
    consent_version: str = Field(max_length=100)
    idempotency_key: uuid.UUID


class Login(BaseModel):
    password: str = Field(max_length=1024)


class ActivateWorksheet(BaseModel):
    draft_id: uuid.UUID
    expected_active_revision_id: uuid.UUID


class SelectWorksheets(BaseModel):
    worksheet_ids: list[uuid.UUID] = Field(min_length=1, max_length=64)
    expected_active_revision_id: uuid.UUID


class Download(BaseModel):
    scope: Literal['submitted', 'selected']
    participant_ids: list[str] = Field(default_factory=list, max_length=1000)
    types: list[Literal['pdf', 'original', 'corrected', 'csv']] = Field(min_length=1, max_length=4)


def make_pdf(participant, pages, storage, configured_pages, registry, previous_pdf=None, updated_qrs=None):
    present = {page.qr_code: page for page in pages}
    configured_pages = sorted(configured_pages, key=lambda item: item['page_number'])
    previous = PdfReader(io.BytesIO(previous_pdf)) if previous_pdf is not None else None
    if previous is not None and len(previous.pages) != len(configured_pages):
        raise ValueError('Previous submission page count does not match the worksheet')
    photos = io.BytesIO()
    pdf = canvas.Canvas(photos, pagesize=A4, pageCompression=1)
    photo_positions = {}
    for configured in configured_pages:
        code = configured['qr_code']
        page = present.get(code)
        if page is not None and (previous is None or updated_qrs is None or code in updated_qrs):
            photo_positions[code] = len(photo_positions)
            pdf.drawImage(ImageReader(io.BytesIO(storage.get(page.corrected_path))), 0, 0, width=A4[0], height=A4[1])
            pdf.showPage()
    pdf.save()
    photo_pdf = PdfReader(photos) if photo_positions else None
    templates = {}
    writer = PdfWriter()
    writer.add_metadata({'/Title': participant.participant_id})
    for index, configured in enumerate(configured_pages):
        code = configured['qr_code']
        if code in photo_positions:
            writer.add_page(photo_pdf.pages[photo_positions[code]])
        elif previous is not None:
            writer.add_page(previous.pages[index])
        else:
            # Use the participant's immutable source, including file-local page order.
            worksheet_id = configured.get('worksheet_id', participant.worksheet_revision_id)
            if worksheet_id not in templates:
                data, _ = registry.pdf(worksheet_id)
                templates[worksheet_id] = PdfReader(io.BytesIO(data))
            source_page = configured.get('file_page_number', configured['page_number']) - 1
            writer.add_page(templates[worksheet_id].pages[source_page])
        writer.add_outline_item(f"{configured['page_number']}: {code}", index)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def create_app(settings: Settings | None = None, storage_override=None):
    settings = settings or Settings.from_env()
    manifest = settings.load_manifest()
    db = Database(settings)
    storage = storage_override or (LocalStorage(settings.data_dir) if settings.backend_mode == 'local' else SupabaseStorage(settings))
    if isinstance(storage, SupabaseStorage):
        storage.verify_private_bucket()
    registry = StudyRegistry(db, storage, manifest, production=settings.app_env == 'production')
    image_slots = threading.BoundedSemaphore(2)
    pdf_slots = threading.BoundedSemaphore(1)
    login_attempts = defaultdict(list)
    attempt_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        yield
        db.engine.dispose()
        if isinstance(storage, SupabaseStorage):
            storage.client.close()

    app = FastAPI(title='筆跡収集', lifespan=lifespan, docs_url='/docs' if settings.app_env != 'production' else None, redoc_url=None)
    app.state.db = db
    app.state.storage = storage
    app.state.manifest = manifest
    app.state.registry = registry
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    @app.middleware('http')
    async def protect(request: Request, call_next):
        origin = request.headers.get('origin')
        # Mutations only accept the configured origin or same-host browser requests.
        if request.method in ('POST', 'PUT', 'DELETE', 'PATCH') and origin:
            if origin != settings.app_base_url and origin != str(request.base_url).rstrip('/'):
                return Response('許可されていない送信元です。', status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site' and request.method != 'GET':
            return Response('許可されていない送信元です。', status_code=403)
        try:
            length = int(request.headers.get('content-length', '0'))
        except ValueError:
            return Response(status_code=400)
        if length > (settings.max_upload_mb + 1) * 1024 * 1024:
            return Response(json.dumps({'detail': {'code': 'file_too_large', 'message': f'{settings.max_upload_mb}MB以内の画像を選んでください。'}}), status_code=413, media_type='application/json')
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' blob:; media-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        if settings.cookie_secure:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    def participant_auth(authorization: str = Header(default='')):
        if not authorization.startswith('Bearer ') or not 32 <= len(authorization[7:]) <= 128:
            fail(401, 'unauthorized', '専用の再開リンクから開いてください。')
        with db.read() as s:
            participant = s.scalar(select(Participant).where(Participant.token_hash == digest_token(authorization[7:])))
            if not participant:
                if s.scalar(select(ExpiredRegistration).where(ExpiredRegistration.token_hash == digest_token(authorization[7:]))):
                    registration_expired()
                fail(401, 'unauthorized', '再開リンクを確認してください。')
            return participant

    def researcher_auth(request: Request):
        session = read_session(request.cookies.get('researcher_session', ''), settings.session_secret, settings.researcher_password_hash)
        if not session:
            fail(401, 'unauthorized', '研究者パスワードでログインしてください。')
        if request.method != 'GET' and not hmac.compare_digest(request.headers.get('x-csrf-token', ''), session['csrf']):
            fail(403, 'csrf', '画面を再読み込みしてから操作してください。')
        return session

    def get_pages(s, participant):
        return list(s.scalars(select(Page).where(Page.submission_id == participant.submission_id).order_by(Page.page_number)))

    def public_config(study_manifest):
        return {k: study_manifest[k] for k in ('title', 'consent_version', 'consent_text', 'pages')} | {
            'profile': study_manifest.get('profile', 'test'),
            'production_ready': study_manifest.get('profile') == 'production' and bool(study_manifest.get('geometry', {}).get('calibrated')),
            'max_upload_mb': settings.max_upload_mb}

    def page_data(page, study_manifest):
        configured = next((p for p in study_manifest['pages'] if p['qr_code'] == page.qr_code), {})
        return {'qr_code': page.qr_code, 'page_number': page.page_number, 'label': configured.get('label', f'{page.page_number}ページ'),
                'original_name': page.original_name, 'version': page.version,
                'preview_url': f'/api/participant/pages/{page.qr_code}/preview'}

    def participant_data(participant, pages, session=None):
        if session is None:
            with db.read() as reader:
                return participant_data(participant, pages, reader)
        study_manifest = registry.manifest_for(participant)
        present = {p.qr_code for p in pages}
        states = worksheet_states(session, participant, pages, registry)
        submitted_count = sum(item['status'] == 'submitted' for item in states)
        return {'participant_id': participant.participant_id, 'registered_at': utc_iso(participant.registered_at),
                'status': 'partially_submitted' if submitted_count and participant.status != 'submitted' else participant.status,
                'pages': [page_data(p, study_manifest) for p in pages],
                'missing_pages': [p['qr_code'] for p in study_manifest['pages'] if p['required'] and p['qr_code'] not in present],
                'worksheet': worksheet_info(study_manifest), 'config': public_config(study_manifest),
                'worksheets': registry.participant_files(participant),
                'worksheet_submissions': states, 'submitted_worksheet_count': submitted_count,
                'blank_page_count': sum(item['blank_pages'] for item in states),
                'has_pending_changes': any(item['has_pending_changes'] for item in states),
                'pending_page_count': sum(item['pending_page_count'] for item in states),
                'worksheet_revision_id': participant.worksheet_revision_id}

    def clean(keys):
        for key in keys:
            if key:
                try:
                    storage.delete(key)
                except Exception:
                    log.warning('Unreferenced storage object cleanup failed; run storage audit.')

    @app.get('/api/health')
    def health():
        with db.read() as s:
            from sqlalchemy import text
            s.execute(text('SELECT 1'))
        return {'status': 'ok', 'backend_mode': settings.backend_mode, 'profile': registry.active_manifest().get('profile', 'test')}

    @app.get('/api/config')
    def config():
        return public_config(registry.active_manifest())

    @app.post('/api/register')
    def register(body: Registration):
        key = str(body.idempotency_key)
        token = registration_token(settings.participant_token_secret or settings.session_secret, key)
        key_hash = digest_token(key)
        try:
            with db.write() as s:
                if s.get(ExpiredRegistration, key_hash):
                    registration_expired()
                state = s.scalar(select(StudyState).where(StudyState.id == 1).with_for_update())
                current_manifest = json.loads(s.get(StudyRevision, state.active_revision_id).manifest_json)
                if not body.consent or body.consent_version != current_manifest['consent_version']:
                    fail(400, 'consent_required', '最新の研究説明を確認し、同意してください。')
                participant = s.scalar(select(Participant).where(Participant.registration_key_hash == key_hash))
                if participant is None:
                    participant = Participant(registration_key_hash=key_hash, token_hash=digest_token(token),
                        submission_id=str(uuid.uuid4()), consent_version=body.consent_version, consent_text=current_manifest['consent_text'],
                        worksheet_revision_id=state.active_revision_id)
                    s.add(participant)
                    s.flush()
        except (IntegrityError, CommitUncertain):
            with db.read() as s:
                participant = s.scalar(select(Participant).where(Participant.registration_key_hash == key_hash))
                if participant is None:
                    if s.get(ExpiredRegistration, key_hash):
                        registration_expired()
                    raise
        if not hmac.compare_digest(participant.token_hash, digest_token(token)):
            fail(409, 'registration_secret_changed', '登録用のサーバー鍵が変更されています。保存した再開リンクから開いてください。')
        return {'participant_id': participant.participant_id, 'resume_token': token, 'resume_url': settings.app_base_url + '/#' + token}

    @app.get('/api/participant')
    def participant_get(participant=Depends(participant_auth)):
        with db.read() as s:
            return participant_data(participant, get_pages(s, participant))

    @app.get('/api/participant/worksheet')
    def participant_worksheet(participant=Depends(participant_auth)):
        revision = registry.for_participant(participant)
        if len(registry.files(revision)) > 1:
            fail(400, 'worksheet_selection_required', '①・②のボタンから、対象のPDFを個別にダウンロードしてください。')
        try:
            data, filename = registry.pdf(revision.id)
        except RevisionMissing:
            fail(404, 'worksheet_unavailable', '配布用紙は準備中です。')
        except Exception:
            fail(503, 'worksheet_unavailable', '用紙を取得できませんでした。時間をおいて再度お試しください。')
        return Response(data, media_type='application/pdf', headers={'Content-Disposition': f'attachment; filename="{filename}"'})

    @app.get('/api/participant/worksheets/{worksheet_id}')
    def participant_selected_worksheet(worksheet_id: uuid.UUID, participant=Depends(participant_auth)):
        if str(worksheet_id) not in {item['id'] for item in registry.participant_files(participant)}:
            fail(404, 'worksheet_unavailable', 'この参加者の配布対象に含まれていないPDFです。')
        return worksheet_pdf(str(worksheet_id))

    @app.post('/api/participant/pages')
    def upload(file: UploadFile = File(...), replace: bool = Form(False), expected_version: str | None = Form(None), participant=Depends(participant_auth)):
        from app.imaging import ImageValidationError, process_image
        study_manifest = registry.manifest_for(participant)
        data = file.file.read(settings.max_upload_mb * 1024 * 1024 + 1)
        if len(data) > settings.max_upload_mb * 1024 * 1024:
            fail(413, 'file_too_large', f'{settings.max_upload_mb}MB以内の画像を選んでください。')
        with image_slots:
            try:
                processed = process_image(data, study_manifest)
            except ImageValidationError as e:
                fail(422, e.code, e.message)
        new_keys, old_keys = [], []
        try:
            with db.write() as s:
                current = s.scalar(select(Participant).where(Participant.id == participant.id).with_for_update())
                if current is None:
                    registration_expired()
                existing = s.scalar(select(Page).where(Page.submission_id == current.submission_id, Page.qr_code == processed.qr_code))
                if existing and not replace:
                    fail(409, 'duplicate_page', 'このページは登録済みです。新しい写真に差し替えますか？', qr_code=processed.qr_code, page_number=processed.page_number, version=existing.version)
                if existing and expected_version and existing.version != expected_version:
                    fail(409, 'version_conflict', '別の操作で画像が変更されました。一覧を更新して確認してください。')
                metrics = dict(processed.metrics)
                # Registration stages a new photo version; only submission may
                # acknowledge it, independently of clocks on separate workers.
                metrics['submitted_version'] = None
                configured = next(page for page in study_manifest['pages'] if page['qr_code'] == processed.qr_code)
                file_id = configured.get('worksheet_id', current.worksheet_revision_id)
                if current.status == 'submitted' and s.get(WorksheetSubmission, (current.submission_id, file_id)) is None:
                    # Legacy PDFs have no saved blank list; remember whether this QR already had a submitted photo.
                    metrics['legacy_submitted_photo'] = bool(existing and (
                        not pending_pages([existing], current.submitted_at)
                        or json.loads(existing.metrics_json).get('legacy_submitted_photo', False)))
                object_id = str(uuid.uuid4())
                prefix = f'{current.participant_id}/{current.submission_id}/{object_id}'
                # Use decoded magic, never an uploaded filename, to choose original extension.
                ext = '.png' if data.startswith(b'\x89PNG') else '.jpg' if data.startswith(b'\xff\xd8') else '.img'
                original_key, corrected_key = prefix + '/original' + ext, prefix + '/corrected.png'
                new_keys.extend([original_key, corrected_key])
                storage.put(original_key, data, 'image/png' if ext == '.png' else 'image/jpeg' if ext == '.jpg' else 'application/octet-stream')
                storage.put(corrected_key, processed.png_bytes, 'image/png')
                if existing:
                    page = existing
                    old_keys = [page.original_path, page.corrected_path]
                else:
                    page = Page(id=str(uuid.uuid4()), submission_id=current.submission_id, qr_code=processed.qr_code)
                    s.add(page)
                page.page_number = processed.page_number
                page.original_name = Path(file.filename or 'photo').name[:255]
                page.original_path, page.corrected_path = original_key, corrected_key
                page.metrics_json = json.dumps(metrics)
                page.version = object_id
                page.created_at = now()
                s.flush()
                pages = get_pages(s, current)
                result = {'page': page_data(page, study_manifest), 'participant': participant_data(current, pages, s)}
        except CommitUncertain:
            log.warning('Page commit uncertain; retain storage objects and retry from participant view.')
            fail(503, 'commit_uncertain', '通信が中断されました。一覧を更新して登録状況を確認してください。')
        except HTTPException:
            clean(new_keys)
            raise
        except Exception:
            clean(new_keys)
            log.exception('Page persistence failed')
            fail(503, 'storage_failed', '保存できませんでした。元の登録画像と提出PDFは維持されています。もう一度お試しください。')
        clean(set(old_keys))
        return result

    @app.get('/api/participant/pages/{qr_code}/preview')
    def preview(qr_code: str, participant=Depends(participant_auth)):
        with db.read() as s:
            page = s.scalar(select(Page).where(Page.submission_id == participant.submission_id, Page.qr_code == qr_code))
            if not page:
                fail(404, 'page_missing', '画像が見つかりません。')
            return Response(storage.get(page.corrected_path), media_type='image/png')

    def submit_files(participant, worksheet_id=None):
        new_keys, old_keys = [], []
        try:
            with pdf_slots, db.write() as s:
                current = s.scalar(select(Participant).where(Participant.id == participant.id).with_for_update())
                if current is None:
                    registration_expired()
                groups = worksheet_groups(current, registry)
                if worksheet_id is not None and worksheet_id not in {group['id'] for group in groups}:
                    fail(404, 'worksheet_missing', 'この参加者の対象に含まれていない用紙です。')
                pages = get_pages(s, current)
                present = {page.qr_code for page in pages}
                rows = submission_rows(s, current)
                fully_submitted = current.status == 'submitted'
                changed = False
                pdf_data = {}
                submitted_pages = []
                for group in groups:
                    if worksheet_id is not None and group['id'] != worksheet_id:
                        continue
                    row = rows.get(group['id'])
                    was_submitted = row is not None or fully_submitted
                    codes = {page['qr_code'] for page in group['pages']}
                    group_pages = [page for page in pages if page.qr_code in codes]
                    updates = pending_pages(group_pages, row.submitted_at if row else current.submitted_at,
                                            json.loads(row.blank_qrs_json) if row else ()) if was_submitted else group_pages
                    if was_submitted and not updates:
                        continue
                    previous = None
                    if row is not None:
                        previous = storage.get(row.pdf_path)
                    elif was_submitted:
                        previous = legacy_worksheet_pdf(current, group, pages, storage, registry.manifest_for(current)['pages'])
                    data = make_pdf(current, group_pages, storage, group['pages'], registry,
                                    previous_pdf=previous, updated_qrs={page.qr_code for page in updates})
                    name = worksheet_pdf_name(current, group, len(groups))
                    key = f'{current.participant_id}/{current.submission_id}/{uuid.uuid4()}/{name}'
                    new_keys.append(key)
                    storage.put(key, data, 'application/pdf')
                    if row is None:
                        row = WorksheetSubmission(submission_id=current.submission_id, worksheet_id=group['id'])
                        s.add(row)
                        rows[group['id']] = row
                    else:
                        old_keys.append(row.pdf_path)
                    row.pdf_path = key
                    row.blank_qrs_json = json.dumps([page['qr_code'] for page in group['pages'] if page['qr_code'] not in present])
                    row.submitted_at = now()
                    submitted_pages.extend(group_pages)
                    pdf_data[group['id']] = data
                    changed = True
                # Freeze untouched legacy worksheets before replacing their combined PDF.
                if changed and fully_submitted:
                    old_states = {item['id']: item for item in worksheet_states(s, current, pages, registry)}
                    for group in groups:
                        if group['id'] in rows:
                            continue
                        data = legacy_worksheet_pdf(current, group, pages, storage, registry.manifest_for(current)['pages'])
                        key = f'{current.participant_id}/{current.submission_id}/{uuid.uuid4()}/{worksheet_pdf_name(current, group, len(groups))}'
                        new_keys.append(key)
                        storage.put(key, data, 'application/pdf')
                        row = WorksheetSubmission(submission_id=current.submission_id, worksheet_id=group['id'], pdf_path=key,
                              blank_qrs_json=json.dumps(old_states[group['id']]['blank_qr_codes']), submitted_at=current.submitted_at)
                        s.add(row)
                        rows[group['id']] = row
                        pdf_data[group['id']] = data
                if changed:
                    s.flush()
                    # Also protects zero-photo, partially submitted IDs from expiry.
                    current.submitted_at = current.submitted_at or now()
                    if all(group['id'] in rows for group in groups):
                        old_keys.append(current.pdf_path)
                        if len(groups) == 1:
                            current.pdf_path = rows[groups[0]['id']].pdf_path
                        else:
                            writer = PdfWriter()
                            writer.add_metadata({'/Title': current.participant_id})
                            position = 0
                            for group in groups:
                                data = pdf_data.get(group['id'])
                                document = PdfReader(io.BytesIO(data if data is not None else storage.get(rows[group['id']].pdf_path)))
                                for configured, pdf_page in zip(group['pages'], document.pages, strict=True):
                                    writer.add_page(pdf_page)
                                    writer.add_outline_item(f"{configured['page_number']}: {configured['qr_code']}", position)
                                    position += 1
                            output = io.BytesIO()
                            writer.write(output)
                            key = f'{current.participant_id}/{current.submission_id}/{uuid.uuid4()}/{current.participant_id}.pdf'
                            new_keys.append(key)
                            storage.put(key, output.getvalue(), 'application/pdf')
                            current.pdf_path = key
                        current.status = 'submitted'
                    # Legacy PDF positions still depend on the previous photo
                    # state. Acknowledge versions only after every PDF is built.
                    for page in submitted_pages:
                        metrics = json.loads(page.metrics_json)
                        metrics['submitted_version'] = page.version
                        page.metrics_json = json.dumps(metrics)
        except CommitUncertain:
            log.warning('Submission commit uncertain; retain PDF and retry submission safely.')
            fail(503, 'commit_uncertain', '提出状況を確認できませんでした。「提出する」で安全に再試行できます。')
        except HTTPException:
            clean(new_keys)
            raise
        except Exception:
            clean(new_keys)
            log.exception('PDF submission failed')
            fail(503, 'submission_failed', 'PDFを保存できませんでした。以前の提出PDFと登録済み写真は保持されています。「提出する」で再試行できます。')
        clean(set(old_keys) - set(new_keys))
        return {'participant_id': current.participant_id, 'status': current.status,
                'pdf_url': '/api/participant/pdf' if current.status == 'submitted' else None}

    @app.post('/api/participant/submit')
    def submit(participant=Depends(participant_auth)):
        return submit_files(participant)

    @app.post('/api/participant/worksheets/{worksheet_id}/submit')
    def submit_worksheet(worksheet_id: uuid.UUID, participant=Depends(participant_auth)):
        result = submit_files(participant, str(worksheet_id))
        return result | {'worksheet_id': str(worksheet_id), 'pdf_url': f'/api/participant/worksheets/{worksheet_id}/submission'}

    @app.get('/api/participant/worksheets/{worksheet_id}/submission')
    def participant_worksheet_submission(worksheet_id: uuid.UUID, participant=Depends(participant_auth)):
        groups = worksheet_groups(participant, registry)
        group = next((item for item in groups if item['id'] == str(worksheet_id)), None)
        if group is None:
            fail(404, 'worksheet_missing', 'この参加者の対象に含まれていない用紙です。')
        with db.read() as s:
            row = s.get(WorksheetSubmission, (participant.submission_id, str(worksheet_id)))
            try:
                if row:
                    data = storage.get(row.pdf_path)
                elif participant.status == 'submitted' and participant.pdf_path:
                    data = storage.get(participant.pdf_path) if len(groups) == 1 else legacy_worksheet_pdf(
                        participant, group, get_pages(s, participant), storage, registry.manifest_for(participant)['pages'])
                else:
                    fail(404, 'pdf_missing', 'この用紙を提出した後にPDFを取得できます。')
            except HTTPException:
                raise
            except Exception:
                fail(503, 'storage_failed', '提出PDFを取得できませんでした。再度お試しください。')
        name = worksheet_pdf_name(participant, group, len(groups))
        return Response(data, media_type='application/pdf', headers={'Content-Disposition': f'attachment; filename="{name}"'})

    @app.get('/api/participant/pdf')
    def participant_pdf(participant=Depends(participant_auth)):
        if participant.status != 'submitted' or not participant.pdf_path:
            fail(404, 'pdf_missing', '提出後にPDFを取得できます。')
        try:
            data = storage.get(participant.pdf_path)
        except Exception:
            # A concurrent retake may have replaced and removed the authenticated snapshot's PDF.
            with db.read() as s:
                current = s.get(Participant, participant.id)
                key = current.pdf_path if current else None
            if not key or key == participant.pdf_path:
                fail(503, 'storage_failed', '提出PDFを取得できませんでした。再度お試しください。')
            try:
                data = storage.get(key)
            except Exception:
                fail(503, 'storage_failed', '提出PDFを取得できませんでした。再度お試しください。')
        return Response(data, media_type='application/pdf', headers={'Content-Disposition': f'attachment; filename="{participant.participant_id}.pdf"'})

    @app.post('/api/researcher/login')
    def login(body: Login, request: Request, response: Response):
        host = request.client.host if request.client else 'unknown'
        clock = time.monotonic()
        with attempt_lock:
            login_attempts[host] = [t for t in login_attempts[host] if t > clock - 300]
            if len(login_attempts[host]) >= 10:
                fail(429, 'rate_limited', '試行回数が多いため5分後にお試しください。')
            login_attempts[host].append(clock)
        if not verify_password(body.password, settings.researcher_password_hash):
            fail(401, 'wrong_password', 'パスワードを確認してください。')
        cookie, csrf = create_session(settings.session_secret, settings.researcher_password_hash)
        response.set_cookie('researcher_session', cookie, httponly=True, secure=settings.cookie_secure, samesite='strict', max_age=8*3600, path='/api/researcher')
        return {'csrf_token': csrf}

    @app.get('/api/researcher/session')
    def researcher_session(session=Depends(researcher_auth)):
        return {'authenticated': True, 'csrf_token': session['csrf']}

    @app.post('/api/researcher/logout')
    def logout(response: Response, session=Depends(researcher_auth)):
        response.delete_cookie('researcher_session', path='/api/researcher')
        return {'ok': True}

    @app.get('/api/researcher/worksheet')
    def researcher_worksheet(session=Depends(researcher_auth)):
        return registry.summary()

    def worksheet_pdf(revision_id=None):
        try:
            data, filename = registry.pdf(revision_id)
        except RevisionMissing:
            fail(404, 'worksheet_missing', '用紙が見つかりません。')
        except Exception:
            fail(503, 'storage_failed', '用紙を取得できませんでした。再度お試しください。')
        return Response(data, media_type='application/pdf', headers={'Content-Disposition': f'attachment; filename="{filename}"'})

    @app.get('/api/researcher/worksheet/pdf')
    def researcher_worksheet_pdf(session=Depends(researcher_auth)):
        return worksheet_pdf()

    @app.get('/api/researcher/worksheet/drafts/{draft_id}/pdf')
    def researcher_draft_pdf(draft_id: uuid.UUID, session=Depends(researcher_auth)):
        return worksheet_pdf(str(draft_id))

    @app.post('/api/researcher/worksheet/inspect')
    def researcher_inspect(file: UploadFile = File(...), session=Depends(researcher_auth)):
        from app.pdf_forms import PdfFormError
        data = file.file.read(settings.max_upload_mb * 1024 * 1024 + 1)
        if len(data) > settings.max_upload_mb * 1024 * 1024:
            fail(413, 'file_too_large', f'{settings.max_upload_mb}MB以内のPDFを選んでください。')
        try:
            with pdf_slots:
                return registry.inspect(data, file.filename or 'worksheet.pdf')
        except PdfFormError as exc:
            fail(422, exc.code, exc.message)
        except Exception:
            log.exception('Worksheet inspection persistence failed; active revision unchanged')
            fail(503, 'worksheet_save_failed', '確認結果を保存できませんでした。配布中の用紙は変更されていません。もう一度お試しください。')

    @app.post('/api/researcher/worksheet/activate')
    def researcher_activate(body: ActivateWorksheet, session=Depends(researcher_auth)):
        try:
            registry.activate(str(body.draft_id), str(body.expected_active_revision_id))
        except RevisionConflict:
            fail(409, 'revision_conflict', '別の操作で配布用紙が変更されました。現在の用紙を確認してからやり直してください。')
        except RevisionMissing:
            fail(404, 'worksheet_missing', '確認した用紙が見つかりません。PDFをもう一度選択してください。')
        except RevisionNotProduction as exc:
            fail(422, 'test_worksheet_not_allowed', str(exc))
        except CommitUncertain:
            fail(503, 'commit_uncertain', '更新結果を確認できませんでした。現在の用紙を再読み込みし、必要なら同じ操作を再試行してください。')
        except Exception:
            log.exception('Worksheet activation failed')
            fail(503, 'worksheet_activation_failed', '配布用紙を変更できませんでした。現在の用紙を再読み込みして再度お試しください。')
        return registry.summary()

    @app.post('/api/researcher/worksheet/selection')
    def researcher_select_worksheets(body: SelectWorksheets, session=Depends(researcher_auth)):
        try:
            registry.select_files([str(item) for item in body.worksheet_ids], str(body.expected_active_revision_id))
        except RevisionConflict:
            fail(409, 'revision_conflict', '別の操作で配布するPDFが変更されました。現在の選択を確認してからやり直してください。')
        except WorksheetSelectionError as exc:
            fail(422, 'invalid_worksheet_selection', str(exc))
        except RevisionMissing:
            fail(404, 'worksheet_missing', '選択したPDFが見つかりません。一覧を更新してください。')
        except RevisionNotProduction as exc:
            fail(422, 'test_worksheet_not_allowed', str(exc))
        except CommitUncertain:
            fail(503, 'commit_uncertain', '更新結果を確認できませんでした。現在の配布対象を再読み込みして確認してください。')
        except Exception:
            log.exception('Worksheet selection failed')
            fail(503, 'worksheet_selection_failed', '配布対象を保存できませんでした。現在の選択を確認して再度お試しください。')
        return registry.summary()

    @app.get('/api/researcher/participants')
    def researcher_list(session=Depends(researcher_auth)):
        with db.read() as s:
            rows = s.execute(select(Participant, func.count(Page.id)).outerjoin(Page, Page.submission_id == Participant.submission_id).group_by(Participant.id).order_by(Participant.id)).all()
            participants = []
            for p, count in rows:
                data = participant_data(p, get_pages(s, p), s)
                participants.append({'participant_id': p.participant_id, 'registered_at': utc_iso(p.registered_at),
                    'status': data['status'], 'page_count': count, 'total_pages': len(data['config']['pages']),
                    'worksheet_revision_id': p.worksheet_revision_id, 'worksheet_submissions': data['worksheet_submissions'],
                    'submitted_worksheet_count': data['submitted_worksheet_count'], 'total_worksheets': len(data['worksheet_submissions']),
                    'has_pending_changes': data['has_pending_changes'], 'pending_page_count': data['pending_page_count'],
                    'blank_page_count': data['blank_page_count']})
            return {'participants': participants}

    @app.post('/api/researcher/download')
    def download(body: Download, session=Depends(researcher_auth)):
        with db.read() as s:
            if body.scope == 'submitted':
                participants = list(s.scalars(select(Participant).where(
                    (Participant.status == 'submitted') | Participant.submission_id.in_(select(WorksheetSubmission.submission_id))).order_by(Participant.id)))
            else:
                if not body.participant_ids:
                    fail(400, 'empty_selection', '対象者を選んでください。')
                if any(not re.fullmatch(r'P\d{3,}', p) for p in body.participant_ids):
                    fail(400, 'invalid_selection', '参加者IDを確認してください。')
                ids = {int(p[1:]) for p in body.participant_ids}
                participants = list(s.scalars(select(Participant).where(Participant.id.in_(ids)).order_by(Participant.id)))
                if len(participants) != len(ids):
                    fail(400, 'invalid_selection', '対象者が見つかりません。')
            if not participants:
                fail(400, 'empty_selection', '対象となる参加者がいません。')
            if 'pdf' in body.types and any(p.status != 'submitted' and not submission_rows(s, p) for p in participants):
                fail(409, 'pdf_missing', '未提出の参加者にはPDFがありません。対象者か種類を変更してください。')
            output = tempfile.TemporaryFile()
            try:
                with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                    for p in participants:
                        pid = p.participant_id
                        pages = get_pages(s, p)
                        saved = {page.qr_code: page for page in pages}
                        groups = worksheet_groups(p, registry)
                        states = {item['id']: item for item in worksheet_states(s, p, pages, registry)}
                        submitted = submission_rows(s, p)
                        for page in pages:
                            stem = f'{page.page_number:03d}_{page.qr_code}'
                            original = f'original/{stem}{Path(page.original_path).suffix}'
                            corrected = f'corrected/{stem}.png'
                            if 'original' in body.types:
                                archive.writestr(f'{pid}/{original}', storage.get(page.original_path))
                            if 'corrected' in body.types:
                                archive.writestr(f'{pid}/{corrected}', storage.get(page.corrected_path))
                        if 'pdf' in body.types:
                            if p.status == 'submitted':
                                archive.writestr(f'{pid}/{pid}.pdf', storage.get(p.pdf_path))
                            if len(groups) > 1:
                                for group in groups:
                                    if states[group['id']]['status'] != 'submitted':
                                        continue
                                    row = submitted.get(group['id'])
                                    data = storage.get(row.pdf_path) if row else legacy_worksheet_pdf(p, group, pages, storage, registry.manifest_for(p)['pages'])
                                    archive.writestr(f'{pid}/{worksheet_pdf_name(p, group, len(groups))}', data)
                        if 'csv' in body.types:
                            csv_output = io.StringIO(newline='')
                            writer = csv.writer(csv_output)
                            writer.writerow(['participant_id', 'qr_code', 'page_number', 'original_filename', 'corrected_filename', 'pdf_filename',
                                             'worksheet_number', 'worksheet_filename', 'file_page_number', 'page_status'])
                            for group in groups:
                                done = states[group['id']]['status'] == 'submitted'
                                for configured in group['pages']:
                                    page = saved.get(configured['qr_code'])
                                    pending = configured['qr_code'] in states[group['id']]['pending_qr_codes']
                                    stem = f"{configured['page_number']:03d}_{configured['qr_code']}"
                                    writer.writerow([pid, configured['qr_code'], configured['page_number'],
                                        f'original/{stem}{Path(page.original_path).suffix}' if page else '',
                                        f'corrected/{stem}.png' if page else '',
                                        worksheet_pdf_name(p, group, len(groups)) if done and not pending else '',
                                        group['number'], group['original_name'], configured.get('file_page_number', configured['page_number']),
                                        'submitted' if page and done and not pending else 'registered' if page else 'blank' if done else 'not_registered'])
                            archive.writestr(f'{pid}/mapping.csv', csv_output.getvalue().encode('utf-8-sig'))
            except Exception:
                output.close()
                log.exception('ZIP creation failed')
                fail(503, 'download_failed', 'ファイル取得に失敗しました。もう一度お試しください。')
        output.seek(0)
        def chunks():
            try:
                while block := output.read(1024 * 1024):
                    yield block
            finally:
                output.close()
        return StreamingResponse(chunks(), media_type='application/zip', headers={'Content-Disposition': 'attachment; filename="handwriting.zip"'})

    app.mount('/static', StaticFiles(directory=ROOT / 'app/static'), name='static')

    @app.get('/')
    @app.get('/researcher')
    def index():
        return FileResponse(ROOT / 'app/static/index.html')

    return app


# Factory remains importable for tests without loading deployment secrets.
class LazyApp:
    def __init__(self):
        self.instance = None

    async def __call__(self, scope, receive, send):
        if self.instance is None:
            self.instance = create_app()
        await self.instance(scope, receive, send)


app = LazyApp()
