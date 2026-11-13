"""Build blank A4 development sheets; these are not the study's original forms."""
from io import BytesIO
import json
from pathlib import Path

import qrcode
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas

ROOT = Path(__file__).resolve().parents[1]


def build():
    manifest = json.loads((ROOT / 'config/pages.test.json').read_text(encoding='utf-8'))
    target = ROOT / 'resources/worksheets/test-worksheet.pdf'
    target.parent.mkdir(parents=True, exist_ok=True)
    pdfmetrics.registerFont(UnicodeCIDFont('HeiseiKakuGo-W5'))
    pdf = Canvas(str(target), pagesize=A4, invariant=True)
    pdf.setTitle('筆跡収集・動作確認用紙（本番原本ではありません）')
    width, height = A4
    pages = sorted(manifest['pages'], key=lambda p: p['page_number'])
    for index, page in enumerate(pages, 1):
        qr_code = page['qr_code']
        pdf.bookmarkPage(qr_code)
        pdf.addOutlineEntry(f'{page["page_number"]}: {qr_code}', qr_code)
        pdf.setFillGray(0)
        for x, y in manifest['geometry']['marker_centers_mm']:
            pdf.circle(x * mm, height - y * mm, 1.35 * mm, stroke=0, fill=1)
        pdf.setFont('HeiseiKakuGo-W5', 11)
        pdf.drawString(30 * mm, height - 32 * mm, '動作確認用紙（本番の配布原本ではありません）')
        pdf.setFont('HeiseiKakuGo-W5', 9)
        pdf.drawString(30 * mm, height - 41 * mm, '各マスにひらがなを書いて、用紙全体を撮影してください。')
        pdf.drawString(30 * mm, height - 47 * mm, '氏名などの個人情報は書かないでください。A4・実際のサイズ（100%）で片面印刷。')
        pdf.setFont('Helvetica', 9)
        pdf.drawRightString(185 * mm, height - 55 * mm, f'{index} / {len(pages)}  |  {qr_code}')
        # Empty boxes; no synthesized handwriting is included in the distributed sheets.
        pdf.setStrokeGray(.65)
        pdf.setLineWidth(.3)
        for row in range(12):
            top = height - (65 + row * 16) * mm
            for col in range(16):
                pdf.rect((25 + col * 10) * mm, top - 10 * mm, 10 * mm, 10 * mm)
        qr = qrcode.make(qr_code, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=4)
        png = BytesIO()
        qr.save(png, format='PNG')
        pdf.drawImage(ImageReader(png), 98 * mm, 15 * mm, 14 * mm, 14 * mm)
        pdf.showPage()
    pdf.save()
    print(target)
    return target


if __name__ == '__main__':
    build()
