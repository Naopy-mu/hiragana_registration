"""Private delivery of the study's blank, printable worksheet PDF."""
from pathlib import Path
import re

from pypdf import PdfReader


WORKSHEET_ROOT = Path(__file__).resolve().parent.parent / 'resources' / 'worksheets'


def worksheet_path(config):
    path = Path(config['path'])
    if not path.is_absolute():
        path = WORKSHEET_ROOT.parent.parent / path
    path = path.resolve()
    if WORKSHEET_ROOT.resolve() not in path.parents or path.suffix.lower() != '.pdf':
        raise ValueError('配布PDFは resources/worksheets/ 内のPDFファイルを指定してください。')
    return path


def validate_worksheet(manifest, production=False):
    config = manifest.get('worksheet')
    if config is None:
        if production:
            raise ValueError('本番では worksheet に配布原本PDFを設定してください。')
        return
    if not isinstance(config, dict) or not isinstance(config.get('path'), str) or not config['path']:
        raise ValueError('worksheet.path に配布PDFのパスを指定してください。')
    config.setdefault('title', '記入用紙')
    config.setdefault('filename', 'worksheet.pdf')
    config.setdefault('test_only', False)
    if not isinstance(config['title'], str) or not config['title'].strip():
        raise ValueError('worksheet.title に用紙名を指定してください。')
    if not isinstance(config['filename'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\.pdf', config['filename']):
        raise ValueError('worksheet.filename は英数字・ハイフン・下線を用いたPDF名にしてください。')
    if type(config['test_only']) is not bool:
        raise ValueError('worksheet.test_only は true / false で指定してください。')
    if production and config['test_only']:
        raise ValueError('本番ではテスト用紙を配布できません。正式な原本PDFを設定してください。')
    path = worksheet_path(config)
    if not path.is_file():
        raise ValueError('配布PDFが見つかりません。worksheet.path と配置先を確認してください。')
    try:
        with path.open('rb') as source:
            if source.read(5) != b'%PDF-':
                raise ValueError('PDF形式ではありません。')
        pdf = PdfReader(path)
        if pdf.is_encrypted or not len(pdf.pages):
            raise ValueError('暗号化のない1ページ以上のPDFが必要です。')
    except Exception as exc:
        raise ValueError('配布PDFを開けません。暗号化のない正常なPDFを指定してください。') from exc


def worksheet_info(manifest):
    config = manifest.get('worksheet')
    if not config:
        return {'available': False}
    return {'available': True, 'title': config['title'], 'filename': config['filename'],
            'download_url': '/api/participant/worksheet',
            'is_test': manifest.get('profile') == 'test' or config['test_only']}
