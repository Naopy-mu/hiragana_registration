import base64
import hashlib
import hmac
import json
import secrets
import time


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError('パスワードは12文字以上にしてください。')
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 600_000).hex()
    return f'pbkdf2_sha256$600000${salt}${digest}'


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split('$')
        if algorithm != 'pbkdf2_sha256' or not 100_000 <= int(rounds) <= 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), int(rounds)).hex()
        return hmac.compare_digest(expected, actual)
    except (ValueError, TypeError):
        return False


def digest_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def registration_token(secret: str, key: str) -> str:
    return base64.urlsafe_b64encode(hmac.digest(secret.encode(), ('participant:' + key).encode(), 'sha256')).decode().rstrip('=')


def create_session(secret: str, password_hash: str) -> tuple[str, str]:
    csrf = secrets.token_urlsafe(24)
    data = {'exp': int(time.time()) + 8 * 3600, 'csrf': csrf, 'pwd': digest_token(password_hash)}
    body = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f'{body}.{sig}', csrf


def read_session(token: str, secret: str, password_hash: str):
    try:
        body, sig = token.split('.')
        if not hmac.compare_digest(sig, hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()):
            return None
        data = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
        if data['exp'] < time.time() or not hmac.compare_digest(data['pwd'], digest_token(password_hash)):
            return None
        return data
    except (ValueError, KeyError, TypeError):
        return None
