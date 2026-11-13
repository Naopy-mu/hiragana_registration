"""Run a real HTTP round-trip against the running local test-profile app."""
import argparse
import csv
import io
import json
from pathlib import Path
import sys
import uuid
import zipfile

import httpx
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent.parent


def checked(response, status=200):
    if response.status_code != status:
        raise RuntimeError(f'{response.status_code}: {response.text[:500]}')
    return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample', type=Path, help='Use the supplied HIRA1-01 photograph as page 1.')
    args = parser.parse_args()
    photos = {n: ROOT / f'local-data/fixtures/photo-{n:02d}.jpg' for n in (1, 2, 3)}
    if args.sample:
        photos[1] = args.sample
    destination = ROOT / 'artifacts/live'
    destination.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url='http://127.0.0.1:8000', timeout=120) as client:
        config = checked(client.get('/api/config')).json()
        if config['profile'] != 'test':
            raise SystemExit('この検証はローカルのテスト設定だけで実行してください。')
        key = str(uuid.uuid4())
        body = {'consent': True, 'consent_version': config['consent_version'], 'idempotency_key': key}
        participant = checked(client.post('/api/register', json=body)).json()
        assert checked(client.post('/api/register', json=body)).json() == participant
        auth = {'Authorization': 'Bearer ' + participant['resume_token']}
        pid = participant['participant_id']
        assert client.get('/api/researcher/participants').status_code == 401
        checked(client.post('/api/participant/submit', headers=auth), 409)
        for n in (3, 1, 2):
            response = checked(client.post('/api/participant/pages', headers=auth,
                files={'file': (photos[n].name, photos[n].read_bytes(), 'image/jpeg')})).json()
            assert response['page']['page_number'] == n
            if n == 1:
                duplicate = checked(client.post('/api/participant/pages', headers=auth,
                    files={'file': (photos[n].name, photos[n].read_bytes(), 'image/jpeg')}), 409).json()['detail']
                assert duplicate['code'] == 'duplicate_page'
                checked(client.post('/api/participant/pages', headers=auth,
                    data={'replace': 'true', 'expected_version': duplicate['version']},
                    files={'file': (photos[n].name, photos[n].read_bytes(), 'image/jpeg')}))
        checked(client.post('/api/participant/submit', headers=auth))
        pdf_bytes = checked(client.get('/api/participant/pdf', headers=auth)).content
        pdf = PdfReader(io.BytesIO(pdf_bytes))
        assert len(pdf.pages) == 3
        assert [p.title for p in pdf.outline] == ['1: HIRA1-01', '2: TEST-02', '3: TEST-03']
        password = (ROOT / 'data/local-researcher-password.txt').read_text(encoding='utf-8').strip()
        csrf = checked(client.post('/api/researcher/login', json={'password': password})).json()['csrf_token']
        zip_bytes = checked(client.post('/api/researcher/download', headers={'X-CSRF-Token': csrf}, json={
            'scope': 'selected', 'participant_ids': [pid], 'types': ['pdf', 'corrected', 'csv']})).content
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            names = archive.namelist()
            assert len(names) == 5 and all(name.startswith(pid + '/') for name in names)
            assert not any('/original/' in name for name in names)
            rows = list(csv.DictReader(io.StringIO(archive.read(f'{pid}/mapping.csv').decode('utf-8-sig'))))
            assert [int(row['page_number']) for row in rows] == [1, 2, 3]
        (destination / f'{pid}.pdf').write_bytes(pdf_bytes)
        (destination / 'selected-pdf-corrected-csv.zip').write_bytes(zip_bytes)
        report = {'ok': True, 'participant_id': pid, 'page_1_source': 'supplied photograph' if args.sample else 'synthetic fixture',
                  'upload_order': [3, 1, 2], 'replacement_confirmed': True, 'pdf_order': ['HIRA1-01', 'TEST-02', 'TEST-03'],
                  'zip_types': ['pdf', 'corrected', 'csv'], 'zip_members': names,
                  'idempotent_registration': True, 'missing_page_submission_refused': True, 'unauthenticated_researcher_refused': True}
        (destination / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
