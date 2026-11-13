"""Inspect an original PDF and prepare a reviewed study manifest."""
from pathlib import Path
import argparse
import json
import sys

import cv2
import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.pdf_forms import analyze_pdf, render_page


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf', type=Path)
    parser.add_argument('--write-study-config', action='store_true')
    args = parser.parse_args()
    data = args.pdf.read_bytes()
    analysis = analyze_pdf(data)
    output = ROOT / 'artifacts/original-analysis'
    output.mkdir(parents=True, exist_ok=True)
    (output / 'analysis.json').write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding='utf-8')
    with fitz.open(stream=data, filetype='pdf') as document:
        for index in (0, document.page_count - 1):
            # imencode handles Japanese workspace paths on Windows.
            ok, image = cv2.imencode('.png', render_page(document[index]))
            if ok:
                (output / f'page-{index+1:02d}.png').write_bytes(image.tobytes())
    if args.write_study_config:
        template = json.loads((ROOT / 'config/pages.test.json').read_text(encoding='utf-8'))
        manifest = {k: template[k] for k in ('consent_text', 'consent_version')}
        manifest.update(analysis)
        manifest.update(title='筆跡収集', description='提供された配布原本PDFの全ページからQR・順序・黒丸座標を確認済み。')
        manifest['worksheet'] = {'path': 'resources/worksheets/hiragana_collection_a4.pdf',
                                 'title': 'ひらがな筆跡収集用紙', 'filename': 'hiragana_collection_a4.pdf', 'test_only': False}
        destination = ROOT / manifest['worksheet']['path']
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        (ROOT / 'config/pages.study.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(analysis, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
