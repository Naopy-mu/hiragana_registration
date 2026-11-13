"""Inspect printable A4 originals; document text is data, never instructions."""
from __future__ import annotations

import hashlib
import re

import cv2
import fitz
import numpy as np

from app.imaging import ImageValidationError, _decode_qr, _marker_candidates


class PdfFormError(ValueError):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def render_page(page: fitz.Page, dpi: int = 200) -> np.ndarray:
    pixmap = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), colorspace=fitz.csGRAY, alpha=False)
    return np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width)


def _ordered_corner_marks(candidates, width, height):
    groups = [[], [], [], []]
    for point in candidates:
        x, y = point
        if x < width * .25 and y < height * .20:
            groups[0].append(point)
        elif x > width * .75 and y < height * .20:
            groups[1].append(point)
        elif x > width * .75 and y > height * .80:
            groups[2].append(point)
        elif x < width * .25 and y > height * .80:
            groups[3].append(point)
    if any(len(group) != 1 for group in groups):
        return None
    return np.array([group[0] for group in groups], dtype=np.float64)


def _vector_marks(page):
    centers = []
    for drawing in page.get_drawings():
        fill = drawing.get('fill')
        if fill is None or max(fill) > .2 or drawing.get('fill_opacity', 1) < .9:
            continue
        curves = [item for item in drawing.get('items', []) if item[0] == 'c']
        # A filled circle is four Bezier arcs. QR squares cannot become markers.
        if len(curves) != 4:
            continue
        rect = drawing['rect']
        if not 2 <= rect.width <= 18 or not .85 < rect.width / max(rect.height, .01) < 1.15:
            continue
        centers.append([(rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2])
    return _ordered_corner_marks(centers, page.rect.width, page.rect.height)


def _page_marks(page, gray, qr, page_number):
    candidates = _marker_candidates(gray, qr)
    raster = _ordered_corner_marks([item[0] for item in candidates], gray.shape[1], gray.shape[0])
    if raster is None:
        raise PdfFormError('pdf_markers_missing', f'{page_number}ページ目の四隅の黒丸を確認できません。用紙の外枠と黒丸がそろった原本PDFを選んでください。')
    raster_mm = raster * [210 / gray.shape[1], 297 / gray.shape[0]]
    vector = _vector_marks(page)
    if vector is None:
        return raster_mm
    vector_mm = vector * [210 / page.rect.width, 297 / page.rect.height]
    if float(np.max(np.abs(vector_mm - raster_mm))) > .5:
        raise PdfFormError('pdf_marker_ambiguous', f'{page_number}ページ目の黒丸の位置を一意に確認できません。原本を確認してください。')
    return vector_mm


def analyze_pdf(data: bytes) -> dict:
    if len(data) > 25 * 1024 * 1024:
        raise PdfFormError('pdf_too_large', '用紙PDFは25MB以内にしてください。')
    if not data.startswith(b'%PDF-'):
        raise PdfFormError('invalid_pdf', 'PDF形式のファイルを選んでください。')
    try:
        document = fitz.open(stream=data, filetype='pdf')
    except Exception as exc:
        raise PdfFormError('invalid_pdf', 'PDFを開けません。正常な原本PDFを選んでください。') from exc
    try:
        if document.needs_pass or document.is_encrypted:
            raise PdfFormError('encrypted_pdf', 'パスワードで保護されていないPDFを選んでください。')
        if not 1 <= document.page_count <= 64:
            raise PdfFormError('invalid_page_count', '用紙PDFは1〜64ページにしてください。')
        pages, geometries, qr_centers, qr_sizes = [], [], [], []
        seen = set()
        for index, page in enumerate(document, 1):
            width_mm, height_mm = page.rect.width * 25.4 / 72, page.rect.height * 25.4 / 72
            if abs(width_mm - 210) > 1 or abs(height_mm - 297) > 1:
                raise PdfFormError('pdf_not_a4', f'{index}ページ目がA4縦ではありません。全ページをA4縦にした原本PDFを選んでください。')
            gray = render_page(page)
            try:
                code, qr = _decode_qr(gray)
            except ImageValidationError as exc:
                raise PdfFormError('pdf_qr_unreadable', f'{index}ページ目のQRコードを読めません。QRを含む高画質の原本PDFを選んでください。') from exc
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', code):
                raise PdfFormError('pdf_unsafe_qr', f'{index}ページ目のQRは英数字・ハイフン・下線、80文字以内にしてください。')
            ex, ey = qr[1] - qr[0], qr[3] - qr[0]
            if ex[0] <= 0 or ey[1] <= 0 or abs(ex[1]) > ex[0] * .25 or abs(ey[0]) > ey[1] * .25:
                raise PdfFormError('pdf_rotated_qr', f'{index}ページ目のQRの向きが用紙と一致しません。用紙とQRを正しい向きにそろえてください。')
            if code in seen:
                raise PdfFormError('pdf_duplicate_qr', f'QR「{code}」が複数のページで重複しています。ページごとに別のQRが必要です。')
            # Reject pages with more than one decoded symbol rather than choosing arbitrarily.
            try:
                multiple, codes, _, _ = cv2.QRCodeDetector().detectAndDecodeMulti(gray)
                if multiple and len([value for value in codes if value]) > 1:
                    raise PdfFormError('pdf_multiple_qr', f'{index}ページ目に複数のQRがあります。用紙識別用QRを1つにしてください。')
            except cv2.error:
                pass
            marks_mm = _page_marks(page, gray, qr, index)
            geometries.append(marks_mm)
            qr_mm = qr * [210 / gray.shape[1], 297 / gray.shape[0]]
            qr_centers.append(qr_mm.mean(axis=0) / [210, 297])
            qr_sizes.append(float(np.linalg.norm(qr_mm[1] - qr_mm[0])))
            pages.append({'qr_code': code, 'page_number': index, 'label': f'{index}ページ', 'required': True})
            seen.add(code)
        geometry_stack = np.array(geometries)
        if float(np.max(np.ptp(geometry_stack, axis=0))) > .5:
            raise PdfFormError('pdf_inconsistent_geometry', 'ページによって四隅の黒丸の位置が異なります。同じ位置にそろえた原本PDFを選んでください。')
        centers = np.array(qr_centers)
        if float(np.max(np.ptp(centers, axis=0))) > .08:
            raise PdfFormError('pdf_inconsistent_qr_position', 'ページによってQRの位置が大きく異なります。同じ位置にそろえた原本PDFを選んでください。')
        sha256 = hashlib.sha256(data).hexdigest()
        return {'profile': 'production', 'version': 'pdf-' + sha256[:12], 'page_count': len(pages),
                'sha256': sha256, 'pages': pages,
                'geometry': {'paper_mm': [210, 297],
                    'marker_centers_mm': np.round(geometry_stack.mean(axis=0), 3).tolist(),
                    'output_width': 1654, 'calibrated': True,
                    'calibration_note': '配布原本PDFのA4座標から取得。黒丸は200dpi画像で確認し、ベクター円がある場合は描画座標とも照合。',
                    'min_focus': 80,
                    'qr_center_region': [round(max(0, centers[:,0].min() - .05), 5),
                                         round(max(0, centers[:,1].min() - .025), 5),
                                         round(min(1, centers[:,0].max() + .05), 5),
                                         round(min(1, centers[:,1].max() + .025), 5)],
                    'qr_size_mm': [round(min(qr_sizes) * .55, 2), round(max(qr_sizes) * 1.6, 2)]}}
    except PdfFormError:
        raise
    except Exception as exc:
        raise PdfFormError('pdf_analysis_failed', 'PDFの解析に失敗しました。正常な原本PDFを選んでください。') from exc
    finally:
        document.close()
