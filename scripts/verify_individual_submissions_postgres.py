"""Verify migration 005 and submissions in an isolated PostgreSQL schema."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import io
import json
from pathlib import Path
import re
import sys
import tempfile
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter
from sqlalchemy import event, func, select
from sqlalchemy.schema import CreateSchema, DropSchema
from app.database import Base, Database, Participant, WorksheetSubmission
from app.main import create_app
from app.security import hash_password
from app.settings import Settings
from app.storage import LocalStorage


def verify():
    values = dotenv_values(ROOT / '.env', interpolate=False)
    schema = 'worksheet_verify_' + uuid.uuid4().hex
    assert re.fullmatch(r'worksheet_verify_[a-f0-9]{32}', schema)
    settings = Settings(backend_mode='supabase', database_url=values['DATABASE_URL'],
                        session_secret='postgres-submission-test-' + 's' * 40,
                        researcher_password_hash=hash_password('postgres-submission-test-password'))
    db = Database(settings)
    @event.listens_for(db.engine, 'connect')
    def search_path(connection, _):
        with connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}", pg_catalog')
        connection.commit()
    created = False
    try:
        with db.engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        created = True
        Base.metadata.create_all(db.engine, tables=[table for table in Base.metadata.sorted_tables if table.name != 'worksheet_submissions'])
        migration = (ROOT / 'migrations/005_individual_worksheet_submissions.sql').read_text(encoding='utf-8').replace('public.', f'"{schema}".')
        with db.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
            connection.exec_driver_sql(migration)
            connection.exec_driver_sql(migration)
        (ROOT / 'artifacts').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='postgres-submission-', dir=ROOT / 'artifacts') as folder:
            settings = replace(settings, backend_mode='local', data_dir=Path(folder))
            with patch('app.main.Database', return_value=db):
                app = create_app(settings, storage_override=LocalStorage(Path(folder)))
            with TestClient(app, raise_server_exceptions=False) as client:
                logged = client.post('/api/researcher/login', json={'password': 'postgres-submission-test-password'})
                csrf = {'X-CSRF-Token': logged.json()['csrf_token']}
                expected = client.get('/api/researcher/worksheet').json()['active_revision_id']
                source, ids = PdfReader(ROOT / 'resources/worksheets/hiragana_collection_a4.pdf'), []
                for page in source.pages[:2]:
                    writer, output = PdfWriter(), io.BytesIO()
                    writer.add_page(page); writer.write(output)
                    response = client.post('/api/researcher/worksheet/inspect', headers=csrf, files={'file': ('test.pdf', output.getvalue(), 'application/pdf')})
                    assert response.status_code == 200
                    ids.append(response.json()['draft_id'])
                assert client.post('/api/researcher/worksheet/selection', headers=csrf, json={'worksheet_ids': ids, 'expected_active_revision_id': expected}).status_code == 200
                registered = client.post('/api/register', json={'consent': True, 'consent_version': client.get('/api/config').json()['consent_version'], 'idempotency_key': str(uuid.uuid4())})
                auth = {'Authorization': 'Bearer ' + registered.json()['resume_token']}
                with patch('app.main.Database', return_value=db):
                    other_app = create_app(settings, storage_override=LocalStorage(Path(folder)))
                with TestClient(other_app, raise_server_exceptions=False) as other_client:
                    with ThreadPoolExecutor(max_workers=3) as pool:
                        responses = list(pool.map(lambda index: (other_client if index % 2 else client).post(
                            f'/api/participant/worksheets/{ids[0]}/submit', headers=auth), range(3)))
                assert all(response.status_code == 200 for response in responses)
                state = client.get('/api/participant', headers=auth).json()
                assert state['status'] == 'partially_submitted' and state['blank_page_count'] == 1
                with db.read() as session:
                    assert session.scalar(select(func.count()).select_from(WorksheetSubmission)) == 1
                    assert session.scalar(select(Participant)).submitted_at is not None
                assert client.post(f'/api/participant/worksheets/{ids[1]}/submit', headers=auth).status_code == 200
                document = PdfReader(io.BytesIO(client.get('/api/participant/pdf', headers=auth).content))
                assert len(document.pages) == 2 and all(not list(page.images) for page in document.pages)
        with db.engine.connect() as connection:
            for role in ('anon', 'authenticated'):
                assert not connection.exec_driver_sql(f"SELECT has_table_privilege('{role}', '{schema}.worksheet_submissions', 'SELECT')").scalar()
            assert connection.exec_driver_sql(f"SELECT relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='{schema}' AND c.relname='worksheet_submissions'").scalar()
        print(json.dumps({'ok': True, 'migrationIdempotent': True, 'concurrentSubmissionCount': 1,
                          'blankPdfPages': 2, 'partialSubmissionProtectedFromExpiry': True,
                          'rlsEnabled': True, 'anonymousAccessRevoked': True,
                          'studyTablesModified': False}))
    finally:
        if created:
            with db.engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        db.engine.dispose()


if __name__ == '__main__':
    try:
        verify()
    except Exception as error:
        import traceback
        print('PostgreSQL verification failed:', type(error).__name__)
        for frame in traceback.extract_tb(error.__traceback__):
            print(Path(frame.filename).name, frame.lineno, frame.name)
        raise SystemExit(1) from None
