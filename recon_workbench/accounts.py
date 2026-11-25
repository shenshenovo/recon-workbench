"""账号基础件：口令散列、会话令牌、凭据规则与登录限流。

只使用标准库，不引入额外依赖；散列参数与格式写入 docs/ACCOUNTS.md，
修改格式时需要同时提供旧格式的校验兼容。
"""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import re
import secrets
import threading
import time

from .errors import RateLimited

ITERATIONS = 210_000
SALT_BYTES = 16
SESSION_DAYS = 30
USERNAME_RULE = re.compile(r'[\w.\-]{2,32}', re.UNICODE)
USERNAME_HELP = '用户名需要 2～32 个字符，可用中文、字母、数字、下划线、点和短横线。'


def username(value):
    text = value.strip() if isinstance(value, str) else ''
    if not USERNAME_RULE.fullmatch(text):
        raise ValueError(USERNAME_HELP)
    return text


def password(value):
    if not isinstance(value, str) or not 8 <= len(value) <= 128 or not value.strip():
        raise ValueError('密码需要 8～128 个字符。')
    return value


def hash_password(value, salt=None, iterations=ITERATIONS):
    salt = salt or secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac('sha256', password(value).encode(), salt, iterations)
    return 'pbkdf2_sha256${}${}${}'.format(iterations, salt.hex(), digest.hex())


def verify_password(value, stored):
    """未知用户与错误口令都由调用方返回同一提示，这里只判断散列是否匹配。"""
    if not isinstance(value, str) or len(value) > 4096:
        return False
    try:
        algorithm, iterations, salt, digest = str(stored).split('$', 3)
        if algorithm != 'pbkdf2_sha256':
            return False
        rounds = min(int(iterations), 1_000_000)
        actual = hashlib.pbkdf2_hmac('sha256', value.encode(), bytes.fromhex(salt), rounds)
        return hmac.compare_digest(actual, bytes.fromhex(digest))
    except (ValueError, TypeError):
        return False


def new_token():
    return secrets.token_urlsafe(32)


def token_hash(token):
    return hashlib.sha256(str(token).encode()).hexdigest()


def expiry(days=SESSION_DAYS):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec='seconds')


class Throttle:
    """按用户名限制连续失败次数；状态只在当前进程内，重启服务即清空。"""

    def __init__(self, limit=8, window=300, lock=60):
        self.limit, self.window, self.lock = limit, window, lock
        self.records = {}
        self.guard = threading.Lock()

    def check(self, key):
        now = time.monotonic()
        with self.guard:
            failures, blocked = self.records.get(key, ([], 0.0))
        if blocked > now:
            raise RateLimited('登录尝试次数过多，请 {} 秒后再试。'.format(int(blocked - now) + 1))
        failures = [stamp for stamp in failures if now - stamp < self.window]
        if len(failures) >= self.limit:
            raise RateLimited('登录尝试次数过多，请 {} 秒后再试。'.format(self.lock))
        return True

    def fail(self, key):
        now = time.monotonic()
        with self.guard:
            failures, blocked = self.records.get(key, ([], 0.0))
            failures = [stamp for stamp in failures if now - stamp < self.window] + [now]
            self.records[key] = ([], now + self.lock) if len(failures) >= self.limit else (failures, blocked)

    def reset(self, key):
        with self.guard:
            self.records.pop(key, None)
