"""
Отримання лімітів використання Claude Code через локальний OAuth-токен
(~/.claude/.credentials.json) — та ж логіка, що й у
https://github.com/CodeZeno/Claude-Code-Usage-Monitor

Не публічний задокументований API, тому може змінитися без попередження.
Читає лише ВЛАСНІ дані користувача через його ж локальний токен Claude Code.
"""
import json
import os
import subprocess
import time
import urllib.request
import urllib.error

USAGE_URL = 'https://api.anthropic.com/api/oauth/usage'
CREDENTIALS_PATH = os.path.expanduser('~/.claude/.credentials.json')
CLAUDE_CLI_CANDIDATES = [
    os.path.expanduser('~/.local/bin/claude.exe'),
    os.path.expanduser('~/.local/bin/claude'),
]

# кеш останнього успішного результату — читає GUI, оновлює фоновий потік
STATE = {
    'five_hour_pct': None,
    'five_hour_resets_at': None,
    'seven_day_pct': None,
    'seven_day_resets_at': None,
    'last_fetch': None,
    'error': None,
}


def _read_credentials():
    with open(CREDENTIALS_PATH, encoding='utf-8') as f:
        data = json.load(f)
    return data['claudeAiOauth']


def _is_expired(oauth):
    expires_at = oauth.get('expiresAt') or 0
    return expires_at < int(time.time() * 1000) + 60_000  # 60с запас


def _find_claude_cli():
    for path in CLAUDE_CLI_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def _refresh_token_via_cli():
    cli = _find_claude_cli()
    if cli is None:
        return False
    env = os.environ.copy()
    env.pop('CLAUDECODE', None)
    env.pop('CLAUDE_CODE_ENTRYPOINT', None)
    try:
        subprocess.run(
            [cli, '-p', '.'], env=env, timeout=30,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        return True
    except Exception:
        return False


def _fetch_usage(token):
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            'Authorization': f'Bearer {token}',
            'anthropic-beta': 'oauth-2025-04-20',
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def get_signature():
    """Компактне представлення поточних значень — щоб можна було порівняти
    'чи змінилися ліміти відколи ми востаннє щось надсилали на клавіатуру'.
    Округлюємо до цілого відсотка, щоб не реагувати на шум незначних коливань."""
    five = STATE.get('five_hour_pct')
    seven = STATE.get('seven_day_pct')
    if five is None and seven is None:
        return None
    return (round(five) if five is not None else None, round(seven) if seven is not None else None)


def refresh_state():
    """Синхронно оновлює STATE. Викликати з фонового потоку (не з GUI-потоку!),
    бо оновлення токена (claude -p .) може зайняти кілька секунд."""
    try:
        oauth = _read_credentials()
        if _is_expired(oauth):
            _refresh_token_via_cli()
            oauth = _read_credentials()
        token = oauth['accessToken']

        body = _fetch_usage(token)
        five_hour = body.get('five_hour') or {}
        seven_day = body.get('seven_day') or {}

        STATE['five_hour_pct'] = five_hour.get('utilization')
        STATE['five_hour_resets_at'] = five_hour.get('resets_at')
        STATE['seven_day_pct'] = seven_day.get('utilization')
        STATE['seven_day_resets_at'] = seven_day.get('resets_at')
        STATE['last_fetch'] = time.time()
        STATE['error'] = None
    except FileNotFoundError:
        STATE['error'] = 'не знайдено ~/.claude/.credentials.json'
    except urllib.error.HTTPError as e:
        STATE['error'] = f'HTTP {e.code}'
    except Exception as e:
        STATE['error'] = str(e)
    return STATE
