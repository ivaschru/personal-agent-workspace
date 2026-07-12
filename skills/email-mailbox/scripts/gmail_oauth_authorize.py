#!/usr/bin/env python3
"""Одноразово получает Gmail OAuth refresh token через loopback callback.

Скрипт не печатает client secret, authorization code или токены. После обмена
он атомарно записывает refresh token в нужные секции ignored accounts.toml.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import secrets
import socketserver
import ssl
import sys
import tempfile
import threading
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = ROOT / ".local" / "email" / "accounts.toml"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = {
    "api": (
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    ),
    "mail": ("https://mail.google.com/",),
}
TARGET_SECTIONS = {
    "api": ("gmail_api_read", "gmail_api_send"),
    "mail": ("imap", "smtp"),
}


def load_account(config_path: Path, account_id: str) -> dict:
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    for account in data.get("accounts", []):
        if account.get("id") == account_id:
            return account
    raise RuntimeError(f"Неизвестный аккаунт: {account_id}")


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> str:
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    ).encode("ascii")
    request = urllib.request.Request(
        TOKEN_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=30, context=ssl.create_default_context()) as response:
        result = json.loads(response.read().decode("utf-8"))
    token = result.get("refresh_token")
    if not token:
        raise RuntimeError("Google не вернул refresh_token; повторите с prompt=consent.")
    return token


def write_refresh_token(config_path: Path, account_id: str, mode: str, token: str) -> None:
    text = config_path.read_text(encoding="utf-8")
    blocks = list(re.finditer(r"(?ms)^\[\[accounts\]\]\n.*?(?=^\[\[accounts\]\]|\Z)", text))
    selected = None
    for match in blocks:
        if re.search(rf'(?m)^id = "{re.escape(account_id)}"$', match.group(0)):
            selected = match
            break
    if selected is None:
        raise RuntimeError(f"Не найден блок аккаунта {account_id}.")

    block = selected.group(0)
    for section in TARGET_SECTIONS[mode]:
        pattern = re.compile(
            rf'(?ms)(^\[accounts\.{re.escape(section)}\]\n.*?^refresh_token = ")[^"]*(")'
        )
        block, count = pattern.subn(lambda match: match.group(1) + token + match.group(2), block, count=1)
        if count != 1:
            raise RuntimeError(f"Не найдено поле refresh_token в секции {section}.")

    updated = text[: selected.start()] + block + text[selected.end() :]
    config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix="accounts.", suffix=".tmp", dir=config_path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(updated)
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, config_path)
        os.chmod(config_path, 0o600)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--account", required=True)
    parser.add_argument("--mode", choices=sorted(SCOPES), required=True)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    account = load_account(args.config, args.account)
    section_name = "gmail_api_read" if args.mode == "api" else "imap"
    section = account.get(section_name, {})
    client_id = section.get("client_id")
    client_secret = section.get("client_secret")
    if not client_id or not client_secret:
        raise RuntimeError(f"В секции {section_name} нет OAuth client.")

    state = secrets.token_urlsafe(24)
    result: dict[str, str] = {}
    finished = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *values: object) -> None:
            return

        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path != "/oauth2callback" or query.get("state", [""])[0] != state:
                self.send_error(400)
                return
            if query.get("error"):
                result["error"] = query["error"][0]
            elif query.get("code"):
                result["code"] = query["code"][0]
            else:
                result["error"] = "missing_code"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "<!doctype html><meta charset=utf-8><title>OAuth получен</title>"
                "<p>Разрешение получено. Эту вкладку можно закрыть.</p>".encode("utf-8")
            )
            finished.set()

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        port = server.server_address[1]
        redirect_uri = f"http://127.0.0.1:{port}/oauth2callback"
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES[args.mode]),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "false",
            "login_hint": account["address"],
            "state": state,
        }
        print("AUTH_URL=" + AUTHORIZE_URL + "?" + urllib.parse.urlencode(params), flush=True)
        server.timeout = 1
        for _ in range(args.timeout):
            server.handle_request()
            if finished.is_set():
                break
        if not finished.is_set():
            raise RuntimeError("Истекло время ожидания OAuth callback.")

    if result.get("error"):
        raise RuntimeError(f"Google OAuth вернул ошибку: {result['error']}")
    refresh_token = exchange_code(client_id, client_secret, result["code"], redirect_uri)
    write_refresh_token(args.config, args.account, args.mode, refresh_token)
    print(json.dumps({"account": args.account, "mode": args.mode, "authorized": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
