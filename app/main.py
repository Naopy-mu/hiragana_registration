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
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.database import CommitUncertain, Database, ExpiredRegistration, Page, Participant, StudyRevision, StudyState, now, utc_iso
from app.revisions import RevisionConflict, RevisionMissing, RevisionNotProduction, StudyRegistry, WorksheetSelectionError
from app.security import create_session, digest_token, read_session, registration_token, verify_password
from app.settings import ROOT, Settings
from app.storage import LocalStorage, SupabaseStorage
from app.worksheet import worksheet_info

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


def make_pdf(participant, pages, storage):
    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=A4, pageCompression=1)
    pdf.setTitle(participant.participant_id)
    for page in sorted(pages, key=lambda p: p.page_number):
        pdf.bookmarkPage(page.qr_code)
        pdf.addOutlineEntry(f'{page.page_number}: {page.qr_code}', page.qr_code, level=0)
        pdf.drawImage(ImageReader(io.BytesIO(storage.get(page.corrected_path))), 0, 0, width=A4[0], height=A4[1])
        pdf.showPage()
    pdf.save()
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

    def participant_data(participant, pages):
        study_manifest = registry.manifest_for(participant)
        present = {p.qr_code for p in pages}
        return {'participant_id': participant.participant_id, 'registered_at': utc_iso(participant.registered_at),
                'status': participant.status, 'pages': [page_data(p, study_manifest) for p in pages],
                'missing_pages': [p['qr_code'] for p in study_manifest['pages'] if p['required'] and p['qr_code'] not in present],
                'worksheet': worksheet_info(study_manifest), 'config': public_config(study_manifest),
                'worksheets': registry.participant_files(participant),
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
        if participant.status == 'submitted':
            fail(409, 'already_submitted', '提出済みのため変更できません。')
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
                if current.status == 'submitted':
                    fail(409, 'already_submitted', '提出済みのため変更できません。')
                existing = s.scalar(select(Page).where(Page.submission_id == current.submission_id, Page.qr_code == processed.qr_code))
                if existing and not replace:
                    fail(409, 'duplicate_page', 'このページは登録済みです。新しい写真に差し替えますか？', qr_code=processed.qr_code, page_number=processed.page_number, version=existing.version)
                if existing and expected_version and existing.version != expected_version:
                    fail(409, 'version_conflict', '別の操作で画像が変更されました。一覧を更新して確認してください。')
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
                page.metrics_json = json.dumps(processed.metrics)
                page.version = object_id
                page.created_at = now()
                s.flush()
                result = {'page': page_data(page, study_manifest), 'participant': participant_data(current, get_pages(s, current))}
        except CommitUncertain:
            log.warning('Page commit uncertain; retain storage objects and retry from participant view.')
            fail(503, 'commit_uncertain', '通信が中断されました。一覧を更新して登録状況を確認してください。')
        except HTTPException:
            clean(new_keys)
            raise
        except Exception:
            clean(new_keys)
            log.exception('Page persistence failed')
            fail(503, 'storage_failed', '保存できませんでした。元の登録画像は維持されています。もう一度お試しください。')
        clean(old_keys)
        return result

    @app.get('/api/participant/pages/{qr_code}/preview')
    def preview(qr_code: str, participant=Depends(participant_auth)):
        with db.read() as s:
            page = s.scalar(select(Page).where(Page.submission_id == participant.submission_id, Page.qr_code == qr_code))
            if not page:
                fail(404, 'page_missing', '画像が見つかりません。')
            return Response(storage.get(page.corrected_path), media_type='image/png')

    @app.post('/api/participant/submit')
    def submit(participant=Depends(participant_auth)):
        new_key = None
        try:
            with db.write() as s:
                current = s.scalar(select(Participant).where(Participant.id == participant.id).with_for_update())
                if current is None:
                    registration_expired()
                pages = get_pages(s, current)
                missing = participant_data(current, pages)['missing_pages']
                if missing:
                    fail(409, 'missing_pages', '未登録の必須ページがあります。', missing_pages=missing)
                if current.status != 'submitted':
                    pdf = make_pdf(current, pages, storage)
                    new_key = f'{current.participant_id}/{current.submission_id}/{uuid.uuid4()}/{current.participant_id}.pdf'
                    storage.put(new_key, pdf, 'application/pdf')
                    current.pdf_path, current.status, current.submitted_at = new_key, 'submitted', now()
        except CommitUncertain:
            log.warning('Submission commit uncertain; retain PDF and retry submission safely.')
            fail(503, 'commit_uncertain', '提出状況を確認できませんでした。「提出する」で安全に再試行できます。')
        except HTTPException:
            raise
        except Exception:
            clean([new_key])
            log.exception('PDF submission failed')
            fail(503, 'submission_failed', 'PDFを保存できませんでした。提出は完了していません。「提出する」で再試行できます。')
        return {'participant_id': current.participant_id, 'status': 'submitted', 'pdf_url': '/api/participant/pdf'}

    @app.get('/api/participant/pdf')
    def participant_pdf(participant=Depends(participant_auth)):
        if participant.status != 'submitted' or not participant.pdf_path:
            fail(404, 'pdf_missing', '提出後にPDFを取得できます。')
        return Response(storage.get(participant.pdf_path), media_type='application/pdf', headers={'Content-Disposition': f'attachment; filename="{participant.participant_id}.pdf"'})

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
            revisions = {r.id: json.loads(r.manifest_json) for r in s.scalars(select(StudyRevision))}
            return {'participants': [{'participant_id': p.participant_id, 'registered_at': utc_iso(p.registered_at), 'status': p.status, 'page_count': count,
                                     'total_pages': len(revisions[p.worksheet_revision_id]['pages']), 'worksheet_revision_id': p.worksheet_revision_id} for p, count in rows]}

    @app.post('/api/researcher/download')
    def download(body: Download, session=Depends(researcher_auth)):
        with db.read() as s:
            if body.scope == 'submitted':
                participants = list(s.scalars(select(Participant).where(Participant.status == 'submitted').order_by(Participant.id)))
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
            if 'pdf' in body.types and any(p.status != 'submitted' for p in participants):
                fail(409, 'pdf_missing', '未提出の参加者にはPDFがありません。対象者か種類を変更してください。')
            output = tempfile.TemporaryFile()
            try:
                with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                    for p in participants:
                        pid = p.participant_id
                        rows = []
                        for page in get_pages(s, p):
                            stem = f'{page.page_number:03d}_{page.qr_code}'
                            original = f'original/{stem}{Path(page.original_path).suffix}'
                            corrected = f'corrected/{stem}.png'
                            if 'original' in body.types:
                                archive.writestr(f'{pid}/{original}', storage.get(page.original_path))
                            if 'corrected' in body.types:
                                archive.writestr(f'{pid}/{corrected}', storage.get(page.corrected_path))
                            rows.append([pid, page.qr_code, page.page_number, original, corrected, f'{pid}.pdf' if p.status == 'submitted' else ''])
                        if 'pdf' in body.types:
                            archive.writestr(f'{pid}/{pid}.pdf', storage.get(p.pdf_path))
                        if 'csv' in body.types:
                            csv_output = io.StringIO(newline='')
                            writer = csv.writer(csv_output)
                            writer.writerow(['participant_id', 'qr_code', 'page_number', 'original_filename', 'corrected_filename', 'pdf_filename'])
                            writer.writerows(rows)
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
