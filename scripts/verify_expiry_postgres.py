"""Verify cleanup in an isolated PostgreSQL schema, never in the study tables."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import threading
import uuid

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def verify():
    values = dotenv_values(ROOT / '.env', interpolate=False)
    dsn = make_url(values['DATABASE_URL']).set(drivername='postgresql').render_as_string(hide_password=False)
    schema = 'expiry_verify_' + uuid.uuid4().hex
    qualified = '"' + schema + '".'
    results = []

    def connect():
        connection = psycopg.connect(dsn, connect_timeout=15)
        connection.execute(sql.SQL('SET search_path TO {}, pg_catalog').format(sql.Identifier(schema)))
        connection.execute("SET statement_timeout = '20s'")
        return connection

    def seed(connection, hours=25, status='registered', pdf=None, submitted=None, key=None, token=None):
        key = key or hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        token = token or hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        return connection.execute('''INSERT INTO participants
            (registration_key_hash, token_hash, submission_id, consent_version, consent_text,
             registered_at, status, pdf_path, submitted_at)
            VALUES (%s,%s,%s,'expiry-test','Isolated test',%s,%s,%s,%s)
            RETURNING id, submission_id, registration_key_hash, token_hash''',
            (key, token, str(uuid.uuid4()), datetime.now(timezone.utc) - timedelta(hours=hours), status, pdf, submitted)).fetchone()

    def page(connection, participant):
        connection.execute('''INSERT INTO pages
            (id, submission_id, qr_code, page_number, original_name, original_path, corrected_path, metrics_json, version)
            VALUES (%s,%s,'TEST-01',1,'test.jpg','isolated-original','isolated-corrected','{}',%s)''',
            (str(uuid.uuid4()), participant[1], str(uuid.uuid4())))

    admin = psycopg.connect(dsn, autocommit=True, connect_timeout=15)
    created = False
    try:
        admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        created = True
        initial = (ROOT / 'migrations/001_initial.sql').read_text(encoding='utf-8')
        initial = initial[initial.index('CREATE TABLE'):initial.index('ALTER TABLE public.participants ENABLE')]
        admin.execute(initial.replace('public.', qualified))
        migration = (ROOT / 'migrations/003_expire_empty_participants.sql').read_text(encoding='utf-8')
        admin.execute(migration.replace('public.', qualified))

        with connect() as connection:
            old = seed(connection)
            fresh = seed(connection, hours=23.99)
            photo = seed(connection); page(connection, photo)
            submitted = seed(connection, status='submitted', pdf='isolated.pdf', submitted=datetime.now(timezone.utc))
            with_pdf = seed(connection, pdf='isolated.pdf')
            with_submit_time = seed(connection, submitted=datetime.now(timezone.utc))
            boundary = seed(connection, hours=24)
        with connect() as connection:
            assert connection.execute('SELECT purge_empty_participants()').fetchone()[0] == 2
            kept = {row[0] for row in connection.execute('SELECT id FROM participants')}
            assert kept == {fresh[0], photo[0], submitted[0], with_pdf[0], with_submit_time[0]}
            assert connection.execute('SELECT count(*) FROM pages').fetchone()[0] == 1
            assert connection.execute('SELECT count(*) FROM expired_registrations').fetchone()[0] == 2
            assert connection.execute('SELECT purge_empty_participants()').fetchone()[0] == 0
            new = seed(connection, hours=0)
            assert new[0] > boundary[0]
        results.append('24-hour cutoff; fresh, photographed, submitted and PDF-backed IDs protected; IDs not reused')

        for reuse in ('key', 'token'):
            try:
                with connect() as connection:
                    seed(connection, hours=0, key=old[2] if reuse == 'key' else None,
                         token=old[3] if reuse == 'token' else None)
                raise AssertionError('Expired credential was reused')
            except psycopg.errors.UniqueViolation:
                pass
        results.append('Expired registration key and bearer token cannot be resurrected')

        with connect() as connection:
            locked = seed(connection)
        with connect() as uploader:
            uploader.execute('SELECT id FROM participants WHERE id=%s FOR UPDATE', (locked[0],))
            with connect() as cleanup:
                assert cleanup.execute('SELECT purge_empty_participants()').fetchone()[0] == 0
            page(uploader, locked)
        with connect() as connection:
            assert connection.execute('SELECT purge_empty_participants()').fetchone()[0] == 0
        results.append('Upload parent-row lock is skipped; committed photo remains protected')

        with connect() as connection:
            direct = seed(connection)
        with connect() as uploader:
            page(uploader, direct)  # FK takes a key-share lock without an app row lock.
            with connect() as cleanup:
                assert cleanup.execute('SELECT purge_empty_participants()').fetchone()[0] == 0
        results.append('Concurrent FK-only photo insertion is also protected')

        with connect() as connection:
            rollback_id = seed(connection)
        with connect() as connection:
            assert connection.execute('SELECT purge_empty_participants()').fetchone()[0] == 1
            connection.rollback()
        with connect() as connection:
            assert connection.execute('SELECT 1 FROM participants WHERE id=%s', (rollback_id[0],)).fetchone()
            assert connection.execute('SELECT 1 FROM expired_registrations WHERE registration_key_hash=%s', (rollback_id[2],)).fetchone() is None
            connection.execute('SELECT purge_empty_participants()')
        results.append('Deletion and credential tombstone roll back together')

        with connect() as connection:
            raced = seed(connection)
        started = threading.Event()
        def retry_registration():
            try:
                with connect() as connection:
                    started.set()
                    seed(connection, hours=0, key=raced[2], token=raced[3])
                return 'unexpected-success'
            except psycopg.errors.UniqueViolation:
                return 'expired-rejected'
        with ThreadPoolExecutor(max_workers=1) as executor:
            with connect() as cleanup:
                assert cleanup.execute('SELECT purge_empty_participants()').fetchone()[0] == 1
                future = executor.submit(retry_registration)
                assert started.wait(10)
                # Prove INSERT is waiting on the deleted-but-uncommitted unique key.
                try:
                    future.result(timeout=0.5)
                    raise AssertionError('Expected the retry to wait for cleanup commit')
                except TimeoutError:
                    pass
            assert future.result(timeout=20) == 'expired-rejected'
        results.append('Retry racing a cleanup commit cannot revive old credentials')

        with connect() as connection:
            for role in ('anon', 'authenticated'):
                assert not connection.execute('SELECT has_function_privilege(%s,%s,%s)',
                    (role, schema + '.purge_empty_participants()', 'EXECUTE')).fetchone()[0]
                assert not connection.execute('SELECT has_table_privilege(%s,%s,%s)',
                    (role, schema + '.expired_registrations', 'SELECT')).fetchone()[0]
        results.append('Anonymous and participant roles cannot invoke cleanup or read expired hashes')
        return {'ok': True, 'checks': results, 'study_tables_modified': False}
    finally:
        if created:
            assert schema.startswith('expiry_verify_') and len(schema) == len('expiry_verify_') + 32
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        admin.close()


if __name__ == '__main__':
    try:
        result = verify()
    except Exception as error:
        # Database errors can contain connection details. Never print a DSN.
        print(json.dumps({'ok': False, 'error_type': type(error).__name__,
                          'sqlstate': getattr(error, 'sqlstate', None)}))
        sys.exit(1)
    print(json.dumps(result, indent=2))
