"""Serve an isolated, local-only worksheet-library browser test on port 8002."""
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import uvicorn
from app.main import create_app
from app.security import hash_password
from app.settings import Settings


if __name__ == '__main__':
    (ROOT / 'artifacts').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='library-browser-data-', dir=ROOT / 'artifacts') as temporary:
        settings = Settings(data_dir=Path(temporary),
                            session_secret='local-library-test-' + 's' * 40,
                            researcher_password_hash=hash_password('library-browser-test-password'),
                            app_base_url='http://127.0.0.1:8002')
        uvicorn.run(create_app(settings), host='127.0.0.1', port=8002)
