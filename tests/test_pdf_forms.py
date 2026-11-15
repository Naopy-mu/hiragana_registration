"""Original-PDF inspection checks, including the supplied eight-page form."""
from io import BytesIO
from pathlib import Path

import fitz
import pytest
import qrcode
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.pdf_forms import PdfFormError, analyze_pdf

ROOT = Path(__file__).resolve().parents[1]


def pdf_bytes(codes, *, markers=4, size=A4, qr_mm=10, qr_border=4,
              error_correction=qrcode.constants.ERROR_CORRECT_M, extra_code=None):
    output = BytesIO()
    document = canvas.Canvas(output, pagesize=size)
    for code in codes:
        for x, y in [(10, 10), (200, 10), (200, 287), (10, 287)][:markers]:
            document.circle(x*mm, A4[1]-y*mm, 1.5*mm, stroke=0, fill=1)
        if code is not None:
            png = BytesIO()
            qrcode.make(code, box_size=12, border=qr_border, error_correction=error_correction).save(png, format='PNG')
            document.drawImage(ImageReader(png), 100*mm, 7*mm, width=qr_mm*mm, height=qr_mm*mm)
        if extra_code is not None:
            png = BytesIO()
            qrcode.make(extra_code, box_size=12, border=qr_border, error_correction=error_correction).save(png, format='PNG')
            document.drawImage(ImageReader(png), 130*mm, 7*mm, width=qr_mm*mm, height=qr_mm*mm)
        document.setFont('Helvetica', 12)
        document.drawString(50*mm, 180*mm, 'Original form inspection test')
        document.showPage()
    document.save()
    return output.getvalue()


def test_supplied_original_has_unique_ordered_qrs_and_exact_a4_marks():
    original = ROOT / 'resources/worksheets/hiragana_collection_a4.pdf'
    result = analyze_pdf(original.read_bytes())
    assert result['page_count'] == 8
    assert [p['page_number'] for p in result['pages']] == list(range(1, 9))
    assert result['pages'][0]['qr_code'] == 'HIRA1-01'
    assert len({p['qr_code'] for p in result['pages']}) == 8
    assert result['geometry']['marker_centers_mm'] == [[10, 10], [200, 10], [200, 287], [10, 287]]
    assert result['geometry']['calibrated'] is True
    assert len(result['sha256']) == 64


@pytest.mark.parametrize('data,code', [(b'not a PDF', 'invalid_pdf'),
    (b'%PDF-not-really-a-pdf', 'invalid_pdf'),
    (pdf_bytes(['SAME', 'SAME']), 'pdf_duplicate_qr'),
    (pdf_bytes(['../unsafe']), 'pdf_unsafe_qr'),
    (pdf_bytes(['VALID'], markers=3), 'pdf_markers_missing'),
    (pdf_bytes([None]), 'pdf_qr_unreadable'),
    (pdf_bytes(['VALID'], size=(A4[1], A4[0])), 'pdf_not_a4')])
def test_invalid_originals_are_rejected_with_specific_reason(data, code):
    with pytest.raises(PdfFormError) as error:
        analyze_pdf(data)
    assert error.value.code == code
    assert error.value.message


def test_encrypted_pdf_rejected_before_rendering():
    with fitz.open(stream=pdf_bytes(['VALID']), filetype='pdf') as original:
        data = original.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw='owner-pass', user_pw='participant-pass')
    with pytest.raises(PdfFormError) as error:
        analyze_pdf(data)
    assert error.value.code == 'encrypted_pdf'


@pytest.mark.parametrize('page_count,expected', [(500, 'pdf_qr_unreadable'), (501, 'invalid_page_count')])
def test_page_limit_boundary_and_qr_validation(page_count, expected):
    with fitz.open() as document:
        for _ in range(page_count):
            document.new_page(width=A4[0], height=A4[1])
        data = document.tobytes()
    with pytest.raises(PdfFormError) as error:
        analyze_pdf(data)
    assert error.value.code == expected
    if expected == 'invalid_page_count':
        assert '500' in error.value.message


def test_valid_pdf_above_previous_64_page_limit():
    codes = [f'LARGE-{number:03d}' for number in range(1, 66)]
    result = analyze_pdf(pdf_bytes(codes))
    assert result['page_count'] == 65
    assert [page['qr_code'] for page in result['pages']] == codes
    assert result['pages'][-1]['page_number'] == 65
    assert result['geometry']['calibrated'] is True


def test_rotated_print_original_rejected_with_actionable_reason():
    with fitz.open(stream=pdf_bytes(['VALID']), filetype='pdf') as document:
        document[0].set_rotation(180)
        data = document.tobytes()
    with pytest.raises(PdfFormError) as error:
        analyze_pdf(data)
    assert error.value.code == 'pdf_rotated_qr'


@pytest.mark.parametrize('qr_mm', [4, 6, 8, 10])
def test_small_dense_qr_is_read_and_calibrated_at_correct_coordinates(qr_mm):
    code = ('dense-' + 'abcdefghijklmnopqrstuvwxyz' * 3)[:80]
    result = analyze_pdf(pdf_bytes([code], qr_mm=qr_mm, error_correction=qrcode.constants.ERROR_CORRECT_H))
    assert result['pages'][0]['qr_code'] == code
    assert result['geometry']['marker_centers_mm'] == [[10, 10], [200, 10], [200, 287], [10, 287]]
    left, top, right, bottom = result['geometry']['qr_center_region']
    assert left < (100 + qr_mm / 2) / 210 < right
    assert top < (297 - 7 - qr_mm / 2) / 297 < bottom


def test_pdf_qr_that_opencv_alone_cannot_read():
    from app.imaging import ImageValidationError, _decode_qr
    from app.pdf_forms import render_page
    code = ('dense-' + 'abcdefghijklmnopqrstuvwxyz' * 3)[:80]
    data = pdf_bytes([code], qr_mm=8, error_correction=qrcode.constants.ERROR_CORRECT_H)
    with fitz.open(stream=data, filetype='pdf') as document:
        with pytest.raises(ImageValidationError):
            _decode_qr(render_page(document[0]))
    assert analyze_pdf(data)['pages'][0]['qr_code'] == code


@pytest.mark.parametrize('small', [False, True])
@pytest.mark.parametrize('same', [False, True])
def test_multiple_qrs_on_one_page_are_rejected_including_high_resolution(small, same):
    code = ('dense-' + 'abcdefghijklmnopqrstuvwxyz' * 3)[:80]
    extra = code if same else ('other-' + 'zyxwvutsrqponmlkjihgfedcba' * 3)[:80]
    data = pdf_bytes([code], qr_mm=6 if small else 14,
                     error_correction=qrcode.constants.ERROR_CORRECT_H, extra_code=extra)
    with pytest.raises(PdfFormError) as error:
        analyze_pdf(data)
    assert error.value.code == 'pdf_multiple_qr'


def test_small_rotated_qr_does_not_bypass_orientation_validation():
    code = ('dense-' + 'abcdefghijklmnopqrstuvwxyz' * 3)[:80]
    with fitz.open(stream=pdf_bytes([code], qr_mm=6, error_correction=qrcode.constants.ERROR_CORRECT_H), filetype='pdf') as document:
        document[0].set_rotation(180)
        data = document.tobytes()
    with pytest.raises(PdfFormError) as error:
        analyze_pdf(data)
    assert error.value.code == 'pdf_rotated_qr'
