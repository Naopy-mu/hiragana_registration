import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    backend_mode: str = 'local'
    app_env: str = 'development'
    database_url: str = ''
    data_dir: Path = ROOT / 'data'
    page_config: Path = ROOT / 'config/pages.test.json'
    session_secret: str = ''
    participant_token_secret: str = ''
    researcher_password_hash: str = ''
    app_base_url: str = 'http://127.0.0.1:8000'
    cookie_secure: bool = False
    supabase_url: str = ''
    supabase_key: str = ''
    storage_bucket: str = 'handwriting-private'
    max_upload_mb: int = 25
    allowed_hosts: list[str] = field(default_factory=lambda: ['localhost', '127.0.0.1', 'testserver'])

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / '.env', override=False)
        return cls(
            backend_mode=os.getenv('BACKEND_MODE', 'local'), app_env=os.getenv('APP_ENV', 'development'),
            database_url=os.getenv('DATABASE_URL', ''), data_dir=Path(os.getenv('DATA_DIR', str(ROOT / 'data'))),
            page_config=Path(os.getenv('PAGE_CONFIG', str(ROOT / 'config/pages.study.json'))),
            session_secret=os.getenv('SESSION_SECRET', ''), researcher_password_hash=os.getenv('RESEARCHER_PASSWORD_HASH', ''),
            participant_token_secret=os.getenv('PARTICIPANT_TOKEN_SECRET', ''),
            app_base_url=os.getenv('APP_BASE_URL', 'http://127.0.0.1:8000').rstrip('/'),
            cookie_secure=os.getenv('COOKIE_SECURE', 'false').lower() == 'true',
            supabase_url=os.getenv('SUPABASE_URL', '').rstrip('/'),
            supabase_key=os.getenv('SUPABASE_SECRET_KEY') or os.getenv('SUPABASE_SERVICE_ROLE_KEY', ''),
            storage_bucket=os.getenv('SUPABASE_STORAGE_BUCKET', 'handwriting-private'),
            max_upload_mb=int(os.getenv('MAX_UPLOAD_MB', '25')),
            allowed_hosts=[h.strip() for h in os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1,testserver').split(',') if h.strip()],
        )

    def load_manifest(self):
        manifest = json.loads(self.page_config.read_text(encoding='utf-8-sig'))
        pages = manifest['pages']
        import re
        if not pages or len({p['qr_code'] for p in pages}) != len(pages) or len({p['page_number'] for p in pages}) != len(pages):
            raise ValueError('ページ設定のQRとページ番号は一意にしてください。')
        for p in pages:
            if not isinstance(p['qr_code'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', p['qr_code']) or type(p['page_number']) is not int or p['page_number'] < 1:
                raise ValueError('QRは英数字・ハイフン・下線、ページ番号は正の整数にしてください。')
            p.setdefault('required', True)
            if type(p['required']) is not bool:
                raise ValueError('required はJSONの true / false で指定してください。')
            p.setdefault('label', f"{p['page_number']}ページ")
        if not any(p['required'] for p in pages):
            raise ValueError('必須ページが必要です。')
        manifest['pages'] = sorted(pages, key=lambda p: p['page_number'])
        manifest.setdefault('title', '筆跡研究へのご協力')
        manifest.setdefault('consent_version', 'test-v1')
        manifest.setdefault('consent_text', 'これは動作確認用です。提供画像はこの環境で研究用データとして保存されます。本番では研究責任者・目的・撤回方法・保管期限を記載した研究説明に置き換えてください。')
        if not isinstance(manifest['consent_version'], str) or not 1 <= len(manifest['consent_version']) <= 100 or not isinstance(manifest['consent_text'], str) or not manifest['consent_text'].strip():
            raise ValueError('同意本文と100文字以内の版が必要です。')
        g = manifest.get('geometry', {})
        def numbers(value, count):
            return isinstance(value, list) and len(value) == count and all(type(x) in (int, float) and math.isfinite(x) for x in value)
        if not numbers(g.get('paper_mm'), 2) or g['paper_mm'] != [210, 297]:
            raise ValueError('用紙寸法はA4縦 [210,297] mmを指定してください。')
        marks = g.get('marker_centers_mm')
        if not isinstance(marks, list) or len(marks) != 4 or not all(numbers(p, 2) for p in marks):
            raise ValueError('黒丸中心座標を左上→右上→右下→左下の4点で指定してください。')
        if not all(0 < x < 210 and 0 < y < 297 for x, y in marks):
            raise ValueError('黒丸中心は用紙の内側にしてください。')
        if not (marks[0][0] < 105 < marks[1][0] and marks[3][0] < 105 < marks[2][0] and marks[0][1] < 148.5 < marks[3][1] and marks[1][1] < 148.5 < marks[2][1]):
            raise ValueError('黒丸中心の順序と用紙四隅の位置を確認してください。')
        g.setdefault('output_width', 1654)
        g.setdefault('min_focus', 80)
        g.setdefault('calibrated', False)
        g.setdefault('qr_center_region', [.36, .83, .64, .985])
        g.setdefault('qr_size_mm', [2, 25])
        if type(g['calibrated']) is not bool or type(g['output_width']) is not int or not 1000 <= g['output_width'] <= 2480:
            raise ValueError('calibratedは真偽値、output_widthは1000〜2480の整数にしてください。')
        if type(g['min_focus']) not in (int, float) or not math.isfinite(g['min_focus']) or not 1 <= g['min_focus'] <= 10000:
            raise ValueError('min_focusを1〜10000の数値で設定してください。')
        region, qr_size = g['qr_center_region'], g['qr_size_mm']
        if not numbers(region, 4) or not (0 <= region[0] < region[2] <= 1 and 0 <= region[1] < region[3] <= 1):
            raise ValueError('QR位置の範囲を用紙内の比率 [左,上,右,下] で指定してください。')
        if not numbers(qr_size, 2) or not 0 < qr_size[0] < qr_size[1] <= 100:
            raise ValueError('QRの一辺の範囲をmmで指定してください。')
        if len(self.session_secret) < 32 or not self.researcher_password_hash:
            raise ValueError('先に python scripts/bootstrap.py を実行し、SESSION_SECRET と RESEARCHER_PASSWORD_HASH を設定してください。')
        if self.backend_mode not in ('local', 'supabase'):
            raise ValueError('BACKEND_MODE must be local or supabase')
        if self.backend_mode == 'supabase' and not all([self.database_url, self.supabase_url, self.supabase_key]):
            raise ValueError('SupabaseのDB接続URL・URL・サーバー秘密鍵が必要です。')
        from app.worksheet import validate_worksheet
        validate_worksheet(manifest, production=self.app_env == 'production')
        if self.app_env == 'production':
            if len(self.participant_token_secret) < 32:
                raise ValueError('本番では32文字以上のPARTICIPANT_TOKEN_SECRETを設定してください。収集期間中は維持してください。')
            if self.backend_mode != 'supabase' or not self.cookie_secure or urlparse(self.app_base_url).scheme != 'https':
                raise ValueError('本番ではSupabase、HTTPS、COOKIE_SECURE=trueが必須です。')
            if manifest.get('profile') != 'production' or manifest.get('geometry', {}).get('calibrated') is not True:
                raise ValueError('原本で校正した本番用ページ設定が必要です。')
            if not manifest.get('consent_version') or manifest['consent_version'] == 'test-v1' or '動作確認用' in manifest['consent_text']:
                raise ValueError('本番の同意文と版を設定してください。')
            if '*' in self.allowed_hosts:
                raise ValueError('本番ALLOWED_HOSTSに公開ホストを指定してください。')
        return manifest
