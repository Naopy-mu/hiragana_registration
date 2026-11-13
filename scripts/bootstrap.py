"""Create local-only secrets once. Never overwrites an existing .env."""
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.security import hash_password


def main():
    env = ROOT / '.env'
    if env.exists():
        print('.env は既にあります。変更していません。')
        return
    data = ROOT / 'data'
    data.mkdir(exist_ok=True)
    password = secrets.token_urlsafe(18)
    (data / 'local-researcher-password.txt').write_text(password + '\n', encoding='utf-8')
    env.write_text('\n'.join([
        'APP_ENV=development', 'BACKEND_MODE=local', 'APP_BASE_URL=http://127.0.0.1:8000',
        'PAGE_CONFIG=config/pages.study.json', 'DATA_DIR=./data', 'COOKIE_SECURE=false',
        'ALLOWED_HOSTS=localhost,127.0.0.1,testserver', f'SESSION_SECRET={secrets.token_urlsafe(48)}',
        f'PARTICIPANT_TOKEN_SECRET={secrets.token_urlsafe(48)}',
        f'RESEARCHER_PASSWORD_HASH={hash_password(password)}', '',
    ]), encoding='utf-8')
    print('.env を作成しました。研究者パスワード: data/local-researcher-password.txt')
    print('起動: python -m uvicorn app.main:app --host 127.0.0.1 --port 8000')


if __name__ == '__main__':
    main()
