"""Immutable worksheet versions pinned to participants, with a shared active pointer."""
from copy import deepcopy
import json
import re
from pathlib import Path
import uuid

from sqlalchemy import select, text, update

from app.database import Participant, StudyRevision, StudyState, now, utc_iso
from app.worksheet import worksheet_info, worksheet_path


class RevisionConflict(ValueError):
    pass


class RevisionMissing(ValueError):
    pass


class RevisionNotProduction(ValueError):
    pass


class WorksheetSelectionError(ValueError):
    pass


def number_label(number):
    if number <= 20:
        return chr(0x2460 + number - 1)
    if number <= 35:
        return chr(0x3251 + number - 21)
    if number <= 50:
        return chr(0x32b1 + number - 36)
    return f'({number})'


class StudyRegistry:
    def __init__(self, database, storage, base_manifest, production=False):
        self.db, self.storage = database, storage
        self.production = production
        self.ensure_baseline(base_manifest)
        if production:
            self.require_production(self.active_manifest())

    @staticmethod
    def require_production(manifest):
        if manifest.get('profile') != 'production' or manifest.get('geometry', {}).get('calibrated') is not True or manifest.get('worksheet', {}).get('test_only') is not False:
            raise RevisionNotProduction('DBで配布中の用紙がテスト設定です。開発モードの研究者画面で正式原本を確認・差し替えしてから、本番モードで起動してください。')

    def ensure_baseline(self, manifest):
        with self.db.write() as session:
            if not self.db.sqlite:
                session.execute(text('SELECT pg_advisory_xact_lock(741091232)'))
            state = session.get(StudyState, 1)
            if state is not None:
                # Preserve the version of any rows imported from the pre-versioned app.
                session.execute(update(Participant).where(Participant.worksheet_revision_id.is_(None)).values(worksheet_revision_id=state.active_revision_id))
                return
            revision_id = str(uuid.uuid4())
            worksheet = manifest.get('worksheet')
            key, original_name = None, ''
            if worksheet:
                source = worksheet_path(worksheet)
                original_name = source.name
                key = f'study-worksheets/{revision_id}/worksheet.pdf'
                self.storage.put(key, source.read_bytes(), 'application/pdf')
            revision = StudyRevision(id=revision_id, manifest_json=json.dumps(manifest, ensure_ascii=False),
                                     pdf_path=key, original_name=original_name)
            session.add(revision)
            session.flush()
            session.add(StudyState(id=1, active_revision_id=revision_id))
            session.execute(update(Participant).where(Participant.worksheet_revision_id.is_(None)).values(worksheet_revision_id=revision_id))

    def current(self, session=None):
        if session is None:
            with self.db.read() as reader:
                return self.current(reader)
        state = session.get(StudyState, 1)
        return session.get(StudyRevision, state.active_revision_id)

    def for_participant(self, participant):
        with self.db.read() as session:
            if participant.worksheet_revision_id:
                revision = session.get(StudyRevision, participant.worksheet_revision_id)
            else:
                revision = self.current(session)
            if revision is None:
                raise RevisionMissing('Participant worksheet version is missing')
            return revision

    def manifest_for(self, participant):
        return json.loads(self.for_participant(participant).manifest_json)

    def active_manifest(self):
        return json.loads(self.current().manifest_json)

    def summary(self):
        with self.db.read() as session:
            state = session.get(StudyState, 1)
            revision = session.get(StudyRevision, state.active_revision_id)
            manifest = json.loads(revision.manifest_json)
            files = self.files(revision)
            selected = [item['id'] for item in files]
            library = []
            for saved in session.scalars(select(StudyRevision).where(StudyRevision.pdf_path.is_not(None)).order_by(StudyRevision.created_at, StudyRevision.id)):
                item = self.files(saved)[0]
                item['download_url'] = f'/api/researcher/worksheet/drafts/{saved.id}/pdf'
                item['selected'] = saved.id in selected
                library.append(item)
            return {'active_revision_id': revision.id, 'original_name': revision.original_name,
                    'title': manifest.get('worksheet', {}).get('title', '記入用紙'),
                    'page_count': len(manifest['pages']), 'pages': manifest['pages'],
                    'activated_at': utc_iso(state.activated_at),
                    'download_url': '/api/researcher/worksheet/pdf' if len(files) == 1 else None,
                    'selected_worksheet_ids': selected, 'library': library,
                    'worksheets': [dict(item, download_url=f"/api/researcher/worksheet/drafts/{item['id']}/pdf") for item in files],
                    'is_test': worksheet_info(manifest).get('is_test', True)}

    @staticmethod
    def files(revision):
        manifest = json.loads(revision.manifest_json)
        if 'worksheets' in manifest:
            return deepcopy(manifest['worksheets'])
        if revision.pdf_path is None:
            return []
        worksheet = manifest.get('worksheet', {})
        return [{'id': revision.id, 'number': 1, 'number_label': number_label(1),
                 'title': worksheet.get('title', '記入用紙'), 'original_name': revision.original_name,
                 'filename': worksheet.get('filename', 'worksheet.pdf'),
                 'page_count': len(manifest['pages']), 'is_test': worksheet_info(manifest).get('is_test', True)}]

    def participant_files(self, participant):
        return [dict(item, download_url=f"/api/participant/worksheets/{item['id']}") for item in self.files(self.for_participant(participant))]

    def select_files(self, worksheet_ids, expected_active_revision_id):
        if not worksheet_ids or len(worksheet_ids) != len(set(worksheet_ids)):
            raise WorksheetSelectionError('配布するPDFを重複なく1件以上選んでください。')
        with self.db.write() as session:
            state = session.scalar(select(StudyState).where(StudyState.id == 1).with_for_update())
            current = session.get(StudyRevision, state.active_revision_id)
            # A lost successful response may be retried with its original expected ID.
            if [item['id'] for item in self.files(current)] == worksheet_ids:
                return
            if state.active_revision_id != expected_active_revision_id:
                raise RevisionConflict('Active worksheet selection changed')
            manifest = deepcopy(json.loads(current.manifest_json))
            pages, files, geometries, seen = [], [], {}, set()
            for number, revision_id in enumerate(worksheet_ids, 1):
                revision = session.get(StudyRevision, revision_id)
                if revision is None or revision.pdf_path is None:
                    raise RevisionMissing('Worksheet PDF not found')
                source = json.loads(revision.manifest_json)
                if self.production:
                    self.require_production(source)
                if not self.storage.get(revision.pdf_path).startswith(b'%PDF-'):
                    raise RevisionMissing('Worksheet file is unavailable')
                item = self.files(revision)[0]
                item.update(number=number, number_label=number_label(number))
                files.append(item)
                for page in source['pages']:
                    code = page['qr_code']
                    if code in seen:
                        raise WorksheetSelectionError(f'QR「{code}」が選択したPDF間で重複しています。ページごとに異なるQRのPDFを選んでください。')
                    seen.add(code)
                    pages.append(dict(page, page_number=len(pages) + 1,
                                      label=f"{number_label(number)} {page.get('label', str(page['page_number']) + 'ページ')}",
                                      worksheet_id=revision.id, worksheet_number=number,
                                      file_page_number=page['page_number']))
                    geometries[code] = source['geometry']
            manifest.update(pages=pages, page_count=len(pages), worksheets=files, page_geometries=geometries,
                            geometry=geometries[pages[0]['qr_code']],
                            profile='test' if any(item['is_test'] for item in files) else 'production',
                            worksheet={'title': f'記入用紙（{len(files)}ファイル）', 'filename': 'worksheets.pdf',
                                       'test_only': any(item['is_test'] for item in files)})
            revision_id = str(uuid.uuid4())
            manifest['version'] = 'selection-' + revision_id
            # The selection itself is immutable, so returning participants keep it.
            session.add(StudyRevision(id=revision_id, manifest_json=json.dumps(manifest, ensure_ascii=False),
                                      pdf_path=None, original_name=' / '.join(item['original_name'] for item in files)[:255]))
            session.flush()
            state.active_revision_id, state.activated_at = revision_id, now()

    def inspect(self, data, original_name):
        from app.pdf_forms import analyze_pdf
        analysis = analyze_pdf(data)
        revision_id = str(uuid.uuid4())
        current = self.current()
        manifest = deepcopy(json.loads(current.manifest_json))
        manifest.pop('worksheets', None)
        manifest.pop('page_geometries', None)
        manifest.update({key: analysis[key] for key in ('profile', 'version', 'pages', 'geometry', 'page_count', 'sha256')})
        manifest['title'] = '筆跡収集'
        filename = Path(original_name or 'worksheet.pdf').name[:255]
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\.pdf', filename):
            filename = 'worksheet.pdf'
        manifest['worksheet'] = {'title': '記入用紙', 'filename': filename, 'test_only': False}
        key = f'study-worksheets/{revision_id}/worksheet.pdf'
        # Never overwrite an object referenced by any participant or active version.
        self.storage.put(key, data, 'application/pdf')
        with self.db.write() as session:
            session.add(StudyRevision(id=revision_id, manifest_json=json.dumps(manifest, ensure_ascii=False),
                                     pdf_path=key, original_name=Path(original_name or 'worksheet.pdf').name[:255]))
            session.flush()
        return {'draft_id': revision_id, 'original_name': original_name[:255],
                'page_count': analysis['page_count'], 'pages': analysis['pages'], 'geometry': analysis['geometry'],
                'sha256': analysis['sha256'], 'active_revision_id': current.id,
                'preview_url': f'/api/researcher/worksheet/drafts/{revision_id}/pdf'}

    def activate(self, draft_id, expected_active_revision_id):
        with self.db.write() as session:
            state = session.scalar(select(StudyState).where(StudyState.id == 1).with_for_update())
            if state.active_revision_id == draft_id:
                return
            if state.active_revision_id != expected_active_revision_id:
                raise RevisionConflict('Active worksheet changed')
            revision = session.get(StudyRevision, draft_id)
            if revision is None or revision.pdf_path is None:
                raise RevisionMissing('Worksheet draft not found')
            if self.production:
                self.require_production(json.loads(revision.manifest_json))
            # A storage failure must keep the old active version.
            if not self.storage.get(revision.pdf_path).startswith(b'%PDF-'):
                raise RevisionMissing('Worksheet file is unavailable')
            state.active_revision_id, state.activated_at = revision.id, now()

    def pdf(self, revision_id=None):
        with self.db.read() as session:
            revision = session.get(StudyRevision, revision_id) if revision_id else self.current(session)
            if revision is not None and revision.pdf_path is None:
                files = self.files(revision)
                if len(files) == 1:
                    revision = session.get(StudyRevision, files[0]['id'])
            if revision is None or revision.pdf_path is None:
                raise RevisionMissing('Worksheet PDF not found')
            manifest = json.loads(revision.manifest_json)
            return self.storage.get(revision.pdf_path), manifest['worksheet'].get('filename', 'worksheet.pdf')
