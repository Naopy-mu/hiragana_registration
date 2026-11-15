"""Per-worksheet submissions and compatibility with older combined submissions."""
import io
import json

from pypdf import PdfReader, PdfWriter
from sqlalchemy import select

from app.database import WorksheetSubmission, utc_iso


def worksheet_groups(participant, registry):
    manifest = registry.manifest_for(participant)
    files = registry.participant_files(participant) or [{
        'id': participant.worksheet_revision_id, 'number': 1, 'number_label': '①',
        'title': '記入用紙', 'original_name': '記入用紙', 'page_count': len(manifest['pages']),
    }]
    for file in files:
        file['pages'] = [page for page in manifest['pages']
                         if page.get('worksheet_id', participant.worksheet_revision_id) == file['id']]
    return files


def submission_rows(session, participant):
    return {row.worksheet_id: row for row in session.scalars(
        select(WorksheetSubmission).where(WorksheetSubmission.submission_id == participant.submission_id))}


def worksheet_states(session, participant, pages, registry):
    rows = submission_rows(session, participant)
    present = {page.qr_code for page in pages}
    states = []
    for group in worksheet_groups(participant, registry):
        row = rows.get(group['id'])
        submitted = bool(row) or participant.status == 'submitted'
        missing = [page['qr_code'] for page in group['pages'] if page['qr_code'] not in present]
        blank = json.loads(row.blank_qrs_json) if row else missing if submitted else []
        states.append({key: value for key, value in group.items() if key != 'pages'} | {
            'status': 'submitted' if submitted else 'registered',
            'registered_pages': len(group['pages']) - len(missing), 'missing_page_count': len(missing),
            'blank_pages': len(blank), 'blank_qr_codes': blank,
            'submitted_at': utc_iso(row.submitted_at) if row else utc_iso(participant.submitted_at) if submitted and participant.submitted_at else None,
            'submit_url': f"/api/participant/worksheets/{group['id']}/submit",
            'pdf_url': f"/api/participant/worksheets/{group['id']}/submission" if submitted else None,
        })
    return states


def worksheet_pdf_name(participant, group, group_count):
    return f'{participant.participant_id}.pdf' if group_count == 1 else f"{participant.participant_id}_{group['number']:02d}.pdf"


def legacy_worksheet_pdf(participant, group, pages, storage):
    """Read existing submissions without changing their stored PDF or study rows."""
    combined = PdfReader(io.BytesIO(storage.get(participant.pdf_path)))
    positions = {page.qr_code: index for index, page in enumerate(sorted(pages, key=lambda item: item.page_number))}
    writer = PdfWriter()
    for configured in group['pages']:
        index = positions.get(configured['qr_code'])
        if index is None:
            writer.add_blank_page(width=595.2756, height=841.8898)
        else:
            writer.add_page(combined.pages[index])
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()
