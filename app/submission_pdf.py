"""Build submissions with native PDF copies and lossless photo embedding."""
from contextlib import ExitStack, closing

import fitz

from app.pdf_engine import pdf_operation


A4_WIDTH = 210 * 72 / 25.4
A4_HEIGHT = 297 * 72 / 25.4


def _finish(document, participant_id, configured_pages):
    document.set_metadata({'title': participant_id})
    document.set_toc([[1, f"{page['page_number']}: {page['qr_code']}", index + 1]
                      for index, page in enumerate(configured_pages)])
    # Drop replaced objects and compress losslessly. Avoid re-encoding the
    # photographs or rebuilding the source worksheet's vector content.
    return document.tobytes(garbage=1, deflate=True, compression_effort=30)


@pdf_operation
def make_pdf(participant, pages, storage, configured_pages, registry, previous_pdf=None, updated_qrs=None):
    present = {page.qr_code: page for page in pages}
    configured_pages = sorted(configured_pages, key=lambda item: item['page_number'])
    with ExitStack() as stack:
        document = stack.enter_context(fitz.open())
        previous = stack.enter_context(fitz.open(stream=previous_pdf, filetype='pdf')) if previous_pdf is not None else None
        if previous is not None and len(previous) != len(configured_pages):
            raise ValueError('Previous submission page count does not match the worksheet')
        keys = [present[item['qr_code']].corrected_path for item in configured_pages
                if item['qr_code'] in present and (previous is None or updated_qrs is None or item['qr_code'] in updated_qrs)]
        photo_data = (storage.iter_get(keys) if hasattr(storage, 'iter_get')
                      else (storage.get(key) for key in keys))
        stack.enter_context(closing(photo_data))
        templates = {}
        for index, configured in enumerate(configured_pages):
            code = configured['qr_code']
            photo = present.get(code)
            if photo is not None and (previous is None or updated_qrs is None or code in updated_qrs):
                page = document.new_page(width=A4_WIDTH, height=A4_HEIGHT)
                page.insert_image(page.rect, stream=next(photo_data),
                                  keep_proportion=False, alpha=0)
            elif previous is not None:
                document.insert_pdf(previous, from_page=index, to_page=index, final=False)
            else:
                worksheet_id = configured.get('worksheet_id', participant.worksheet_revision_id)
                if worksheet_id not in templates:
                    data, _ = registry.pdf(worksheet_id)
                    templates[worksheet_id] = stack.enter_context(fitz.open(stream=data, filetype='pdf'))
                source_page = configured.get('file_page_number', configured['page_number']) - 1
                document.insert_pdf(templates[worksheet_id], from_page=source_page, to_page=source_page, final=False)
        return _finish(document, participant.participant_id, configured_pages)


@pdf_operation
def combine_pdfs(participant_id, documents):
    """Consume one saved worksheet at a time, preserving the submission order."""
    with fitz.open() as combined:
        pages = []
        for configured_pages, data in documents:
            with fitz.open(stream=data, filetype='pdf') as source:
                if len(source) != len(configured_pages):
                    raise ValueError('Submitted PDF page count does not match the worksheet')
                combined.insert_pdf(source)
            pages.extend(configured_pages)
        return _finish(combined, participant_id, pages)
