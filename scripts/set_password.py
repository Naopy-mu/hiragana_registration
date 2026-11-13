"""Print a salted researcher password hash; the password never enters argv."""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.security import hash_password


def main() -> None:
    password = getpass.getpass("研究者用パスワード（12文字以上）: ")
    repeated = getpass.getpass("もう一度入力: ")
    if len(password) < 12:
        raise SystemExit("12文字以上のパスワードを入力してください。")
    if password != repeated:
        raise SystemExit("入力が一致しません。")
    value = hash_password(password)
    print("\n.envには次の1行を設定してください（ハッシュも公開しないでください）。")
    print(f"RESEARCHER_PASSWORD_HASH='{value}'")
    print("Azureの環境変数には = の右側の値を、外側の引用符を除いて入力してください。")


if __name__ == "__main__":
    main()
