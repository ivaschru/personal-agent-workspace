#!/usr/bin/env python3
"""Точечно работает с почтой через Gmail API, IMAP и SMTP.

Скрипт сознательно не синхронизирует и не экспортирует ящик целиком. Он выбирает
один заранее настроенный транспорт для операции, работает с небольшим числом
писем и не печатает конфигурацию с секретами.
"""

from __future__ import annotations

import argparse
import base64
import email
import html
import imaplib
import json
import mimetypes
import os
import re
import smtplib
import ssl
import stat
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = ROOT / ".local" / "email" / "accounts.toml"
DEFAULT_MAX_MESSAGE_BYTES = 25 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30
GMAIL_API_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
HEADER_NAMES = ("From", "To", "Cc", "Date", "Subject", "Message-ID")
IMAP_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


class MailboxError(RuntimeError):
    """Ожидаемая безопасная ошибка, которую можно показать без traceback."""


@dataclass(frozen=True)
class Account:
    """Нормализованный аккаунт без копирования секретов в строковое представление."""

    id: str
    address: str
    provider: str
    read_order: tuple[str, ...]
    send_order: tuple[str, ...]
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class SearchFilters:
    sender: str | None
    recipient: str | None
    subject: str | None
    since: date | None
    before: date | None
    unread: bool
    query: str | None


class TextExtractor(HTMLParser):
    """Извлекает читаемый текст из HTML, пропуская скрипты и стили."""

    BLOCK_TAGS = {
        "address",
        "article",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "p",
        "section",
        "table",
        "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style"}:
            self._ignored_depth += 1
        elif not self._ignored_depth and tag in self.BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif not self._ignored_depth and tag in self.BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self._parts.append(data)

    def text(self) -> str:
        value = html.unescape("".join(self._parts))
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r"\n[ \t]+", "\n", value)
        return re.sub(r"\n{3,}", "\n\n", value).strip()


def decode_value(value: str | None) -> str:
    """Декодирует RFC 2047, сохраняя исходную строку при редкой ошибке кодировки."""

    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeError):
        return value


def secret_value(config: Mapping[str, Any], name: str) -> str | None:
    """Читает секрет напрямую или через переменную окружения без его логирования."""

    direct = config.get(name)
    if isinstance(direct, str) and direct:
        return direct
    env_name = config.get(f"{name}_env")
    if isinstance(env_name, str) and env_name:
        value = os.environ.get(env_name)
        if not value:
            raise MailboxError(f"Не задана переменная окружения {env_name}.")
        return value
    return None


def require_secret(config: Mapping[str, Any], name: str) -> str:
    value = secret_value(config, name)
    if not value:
        raise MailboxError(f"В локальной конфигурации отсутствует поле {name}.")
    return value


def load_accounts(config_path: Path) -> dict[str, Account]:
    if not config_path.exists():
        raise MailboxError(
            f"Нет локальной конфигурации {config_path}. "
            "Скопируйте references/accounts.example.toml и установите права 600."
        )
    # В публичном примере права обычные, но реальная конфигурация внутри .local
    # содержит действующие токены и пароли. На POSIX не продолжаем, если её может
    # прочитать группа или любой другой локальный пользователь.
    local_root = (ROOT / ".local").resolve()
    resolved_path = config_path.resolve()
    if os.name == "posix" and resolved_path.is_relative_to(local_root):
        mode = stat.S_IMODE(resolved_path.stat().st_mode)
        if mode & 0o077:
            raise MailboxError(
                f"Слишком широкие права {oct(mode)} у {config_path}; выполните chmod 600."
            )
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise MailboxError(f"Не удалось прочитать локальную конфигурацию: {exc}") from exc

    if data.get("schema_version") != 1:
        raise MailboxError("Поддерживается только schema_version = 1.")
    raw_accounts = data.get("accounts")
    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise MailboxError("В локальной конфигурации нет [[accounts]].")

    accounts: dict[str, Account] = {}
    for raw in raw_accounts:
        if not isinstance(raw, dict):
            raise MailboxError("Каждая запись [[accounts]] должна быть таблицей TOML.")
        account_id = raw.get("id")
        address = raw.get("address")
        if not isinstance(account_id, str) or not account_id:
            raise MailboxError("У каждого аккаунта нужен непустой id.")
        if account_id in accounts:
            raise MailboxError(f"Повторяется id аккаунта: {account_id}.")
        if not isinstance(address, str) or "@" not in address:
            raise MailboxError(f"У аккаунта {account_id} некорректный address.")

        provider = str(raw.get("provider", "generic")).lower()
        has_gmail_read = "gmail_api_read" in raw or "gmail_api" in raw
        has_gmail_send = "gmail_api_send" in raw or "gmail_api" in raw
        default_read = ["gmail-api", "imap"] if has_gmail_read else ["imap"]
        default_send = ["gmail-api", "smtp"] if has_gmail_send else ["smtp"]
        read_order = _transport_order(raw.get("read_order", default_read), account_id)
        send_order = _transport_order(raw.get("send_order", default_send), account_id)
        accounts[account_id] = Account(
            id=account_id,
            address=address,
            provider=provider,
            read_order=read_order,
            send_order=send_order,
            raw=raw,
        )
    return accounts


def _transport_order(value: Any, account_id: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise MailboxError(f"Некорректный порядок транспортов у аккаунта {account_id}.")
    return tuple(value)


def select_account(accounts: Mapping[str, Account], account_id: str) -> Account:
    try:
        return accounts[account_id]
    except KeyError as exc:
        known = ", ".join(sorted(accounts))
        raise MailboxError(f"Неизвестный аккаунт {account_id}. Доступны: {known}.") from exc


def transport_config(account: Account, operation: str) -> tuple[str, Mapping[str, Any]]:
    """Выбирает первый настроенный транспорт, не выполняя сетевой fallback."""

    order = account.read_order if operation == "read" else account.send_order
    section_by_transport = {"imap": ("imap",), "smtp": ("smtp",)}
    # Раздельные секции позволяют выдать Gmail API минимальные независимые
    # scopes для чтения и отправки. `gmail_api` оставлен как компактный вариант,
    # когда владелец сознательно использует один OAuth grant для обеих операций.
    section_by_transport["gmail-api"] = (
        ("gmail_api_read", "gmail_api")
        if operation == "read"
        else ("gmail_api_send", "gmail_api")
    )
    for transport in order:
        for section_name in section_by_transport.get(transport, ()):
            section = account.raw.get(section_name)
            if isinstance(section, dict):
                return transport, section
    raise MailboxError(
        f"Для аккаунта {account.id} не настроен транспорт для операции {operation}."
    )


def oauth_access_token(config: Mapping[str, Any]) -> str:
    """Возвращает готовый access token или обновляет его по refresh token."""

    access_token = secret_value(config, "access_token")
    if access_token:
        return access_token

    client_id = require_secret(config, "client_id")
    client_secret = require_secret(config, "client_secret")
    refresh_token = require_secret(config, "refresh_token")
    token_url = str(config.get("token_url", GOOGLE_TOKEN_URL))
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("ascii")
    request = urllib.request.Request(
        token_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    response = http_json(request)
    token = response.get("access_token")
    if not isinstance(token, str) or not token:
        raise MailboxError("OAuth-сервер не вернул access_token.")
    return token


def http_json(request: urllib.request.Request) -> dict[str, Any]:
    """Выполняет HTTPS-запрос и не включает тело с токеном в сообщение ошибки."""

    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        # Тело ответа API может содержать полезное описание, но также непредсказуемые
        # данные. Для безопасного общего инструмента достаточно статуса и endpoint.
        endpoint = urllib.parse.urlsplit(request.full_url).path
        raise MailboxError(f"Почтовый API вернул HTTP {exc.code} для {endpoint}.") from exc
    except urllib.error.URLError as exc:
        raise MailboxError(f"Не удалось подключиться к почтовому API: {exc.reason}.") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MailboxError("Почтовый API вернул некорректный JSON.") from exc
    if not isinstance(value, dict):
        raise MailboxError("Почтовый API вернул неожиданный формат ответа.")
    return value


def gmail_request(
    config: Mapping[str, Any],
    method: str,
    path: str,
    *,
    query: Sequence[tuple[str, str]] = (),
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    token = oauth_access_token(config)
    url = f"{GMAIL_API_ROOT}/{path.lstrip('/')}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    return http_json(request)


def build_gmail_query(filters: SearchFilters) -> str:
    parts: list[str] = []
    if filters.sender:
        parts.append(f"from:{filters.sender}")
    if filters.recipient:
        parts.append(f"to:{filters.recipient}")
    if filters.subject:
        safe_subject = filters.subject.replace('"', "")
        parts.append(f'subject:"{safe_subject}"')
    if filters.since:
        parts.append(f"after:{filters.since.isoformat().replace('-', '/')}")
    if filters.before:
        parts.append(f"before:{filters.before.isoformat().replace('-', '/')}")
    if filters.unread:
        parts.append("is:unread")
    if filters.query:
        parts.append(filters.query)
    return " ".join(parts)


def gmail_search(
    config: Mapping[str, Any], filters: SearchFilters, limit: int
) -> list[dict[str, Any]]:
    query = [("maxResults", str(limit))]
    gmail_query = build_gmail_query(filters)
    if gmail_query:
        query.append(("q", gmail_query))
    result = gmail_request(config, "GET", "messages", query=query)
    messages = result.get("messages", [])
    if not isinstance(messages, list):
        raise MailboxError("Gmail API вернул некорректный список писем.")

    summaries: list[dict[str, Any]] = []
    for item in messages[:limit]:
        message_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(message_id, str):
            continue
        metadata_query: list[tuple[str, str]] = [("format", "metadata")]
        metadata_query.extend(("metadataHeaders", name) for name in HEADER_NAMES)
        metadata = gmail_request(
            config, "GET", f"messages/{message_id}", query=metadata_query
        )
        payload = metadata.get("payload", {})
        headers = payload.get("headers", []) if isinstance(payload, dict) else []
        summary = headers_to_dict(headers)
        summary.update(
            {
                "id": message_id,
                "thread_id": metadata.get("threadId", ""),
                "labels": metadata.get("labelIds", []),
                "snippet": metadata.get("snippet", ""),
            }
        )
        summaries.append(summary)
    return summaries


def headers_to_dict(headers: Any) -> dict[str, str]:
    result = {name.lower().replace("-", "_"): "" for name in HEADER_NAMES}
    if not isinstance(headers, list):
        return result
    for item in headers:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if isinstance(name, str) and isinstance(value, str) and name in HEADER_NAMES:
            result[name.lower().replace("-", "_")] = decode_value(value)
    return result


def gmail_raw_message(config: Mapping[str, Any], message_id: str) -> bytes:
    result = gmail_request(
        config, "GET", f"messages/{message_id}", query=[("format", "raw")]
    )
    raw = result.get("raw")
    if not isinstance(raw, str):
        raise MailboxError("Gmail API не вернул исходное письмо.")
    try:
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, UnicodeError) as exc:
        raise MailboxError("Gmail API вернул повреждённое исходное письмо.") from exc


def imap_connection(account: Account, config: Mapping[str, Any]) -> imaplib.IMAP4_SSL:
    host = str(config.get("host", ""))
    port = int(config.get("port", 993))
    if not host:
        raise MailboxError(f"У аккаунта {account.id} не указан IMAP host.")
    try:
        connection = imaplib.IMAP4_SSL(
            host,
            port,
            ssl_context=ssl.create_default_context(),
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        auth = str(config.get("auth", "password"))
        if auth == "oauth2":
            token = oauth_access_token(config)
            auth_string = f"user={account.address}\x01auth=Bearer {token}\x01\x01"
            connection.authenticate("XOAUTH2", lambda _: auth_string.encode("utf-8"))
        elif auth == "password":
            connection.login(account.address, require_secret(config, "password"))
        else:
            raise MailboxError(f"Неизвестный IMAP auth: {auth}.")
        if {"ENABLE", "UTF8=ACCEPT"}.issubset(connection.capabilities):
            # imaplib после ENABLE меняет внутреннюю кодировку с ASCII на UTF-8.
            # Это нужно для русских имён и тем без ручной работы с IMAP literals.
            connection.enable("UTF8=ACCEPT")
        return connection
    except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
        raise MailboxError(f"Не удалось подключиться к IMAP {host}:{port}: {exc}") from exc


def imap_mailbox(config: Mapping[str, Any]) -> str:
    value = config.get("mailbox", "INBOX")
    if not isinstance(value, str) or not value:
        raise MailboxError("Некорректное имя IMAP-папки.")
    return value


def imap_select_readonly(connection: imaplib.IMAP4_SSL, mailbox: str) -> None:
    status, _ = connection.select(mailbox, readonly=True)
    if status != "OK":
        raise MailboxError(f"Не удалось открыть IMAP-папку {mailbox} только для чтения.")


def imap_search_criteria(filters: SearchFilters) -> list[str]:
    criteria = ["UNSEEN"] if filters.unread else ["ALL"]
    values = (
        ("FROM", filters.sender),
        ("TO", filters.recipient),
        ("SUBJECT", filters.subject),
        ("TEXT", filters.query),
    )
    for key, value in values:
        if value:
            criteria.extend([key, _imap_quote(value)])
    if filters.since:
        criteria.extend(["SINCE", imap_date(filters.since)])
    if filters.before:
        criteria.extend(["BEFORE", imap_date(filters.before)])
    return criteria


def imap_date(value: date) -> str:
    """Формирует IMAP-дату с английским месяцем независимо от locale ОС."""

    return f"{value.day:02d}-{IMAP_MONTHS[value.month - 1]}-{value.year:04d}"


def _imap_quote(value: str) -> str:
    """Кавычит поисковую строку и не допускает внедрение IMAP-команд."""

    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def imap_search(
    account: Account,
    config: Mapping[str, Any],
    filters: SearchFilters,
    limit: int,
) -> list[dict[str, Any]]:
    connection = imap_connection(account, config)
    try:
        imap_select_readonly(connection, imap_mailbox(config))
        try:
            status, data = connection.uid("search", None, *imap_search_criteria(filters))
        except UnicodeEncodeError as exc:
            raise MailboxError(
                "IMAP-сервер не объявил UTF8=ACCEPT и не принимает русский текст поиска."
            ) from exc
        if status != "OK" or not data:
            raise MailboxError("IMAP SEARCH завершился ошибкой.")
        uids = data[0].split()[-limit:]
        summaries: list[dict[str, Any]] = []
        for uid in reversed(uids):
            raw_headers = imap_fetch_bytes(
                connection,
                uid.decode("ascii"),
                "(BODY.PEEK[HEADER.FIELDS (FROM TO CC DATE SUBJECT MESSAGE-ID)] FLAGS)",
            )
            message = email.message_from_bytes(raw_headers, policy=policy.default)
            summary = message_summary(message)
            summary["id"] = uid.decode("ascii")
            summaries.append(summary)
        return summaries
    finally:
        try:
            connection.logout()
        except imaplib.IMAP4.error:
            pass


def imap_fetch_bytes(
    connection: imaplib.IMAP4_SSL, uid: str, section: str
) -> bytes:
    if not uid.isdigit():
        raise MailboxError("IMAP id должен быть числовым UID.")
    status, data = connection.uid("fetch", uid, section)
    if status != "OK" or not data:
        raise MailboxError(f"Не удалось получить IMAP UID {uid}.")
    for item in data:
        if isinstance(item, tuple) and isinstance(item[1], bytes):
            if len(item[1]) > DEFAULT_MAX_MESSAGE_BYTES:
                raise MailboxError("Письмо превышает безопасный лимит размера.")
            return item[1]
    raise MailboxError(f"IMAP не вернул содержимое UID {uid}.")


def imap_raw_message(
    account: Account, config: Mapping[str, Any], message_id: str
) -> bytes:
    connection = imap_connection(account, config)
    try:
        imap_select_readonly(connection, imap_mailbox(config))
        return imap_fetch_bytes(connection, message_id, "(BODY.PEEK[])")
    finally:
        try:
            connection.logout()
        except imaplib.IMAP4.error:
            pass


def parse_message(raw: bytes) -> Message:
    if len(raw) > DEFAULT_MAX_MESSAGE_BYTES:
        raise MailboxError("Письмо превышает безопасный лимит размера.")
    return email.message_from_bytes(raw, policy=policy.default)


def message_summary(message: Message) -> dict[str, Any]:
    return {
        "from": decode_value(message.get("From")),
        "to": decode_value(message.get("To")),
        "cc": decode_value(message.get("Cc")),
        "date": decode_value(message.get("Date")),
        "subject": decode_value(message.get("Subject")),
        "message_id": decode_value(message.get("Message-ID")),
    }


def message_text(message: Message) -> str:
    """Предпочитает text/plain и безопасно упрощает HTML при его отсутствии."""

    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in message.walk() if message.is_multipart() else [message]:
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", errors="replace")
        if not isinstance(content, str):
            continue
        if content_type == "text/plain":
            plain_parts.append(content.strip())
        else:
            parser = TextExtractor()
            parser.feed(content)
            html_parts.append(parser.text())
    parts = plain_parts or html_parts
    return "\n\n".join(part for part in parts if part).strip()


def message_attachments(message: Message) -> list[tuple[str, str, bytes]]:
    attachments: list[tuple[str, str, bytes]] = []
    for part in message.walk():
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        if not filename and disposition != "attachment":
            continue
        payload = part.get_payload(decode=True) or b""
        safe_name = sanitize_filename(decode_value(filename) or "attachment.bin")
        attachments.append((safe_name, part.get_content_type(), payload))
    return attachments


def sanitize_filename(filename: str) -> str:
    """Удаляет путь и управляющие символы, сохраняя узнаваемое имя вложения."""

    filename = filename.replace("\\", "/").split("/")[-1]
    filename = re.sub(r"[\x00-\x1f\x7f]", "", filename).strip().strip(".")
    return filename[:180] or "attachment.bin"


def raw_message_for_account(
    account: Account, transport: str, config: Mapping[str, Any], message_id: str
) -> bytes:
    if transport == "gmail-api":
        return gmail_raw_message(config, message_id)
    if transport == "imap":
        return imap_raw_message(account, config, message_id)
    raise MailboxError(f"Транспорт {transport} не умеет читать письма.")


def smtp_connection(
    account: Account, config: Mapping[str, Any]
) -> smtplib.SMTP | smtplib.SMTP_SSL:
    host = str(config.get("host", ""))
    port = int(config.get("port", 465))
    security = str(config.get("security", "tls"))
    if not host:
        raise MailboxError(f"У аккаунта {account.id} не указан SMTP host.")
    context = ssl.create_default_context()
    try:
        if security == "tls":
            connection: smtplib.SMTP | smtplib.SMTP_SSL = smtplib.SMTP_SSL(
                host, port, context=context, timeout=DEFAULT_TIMEOUT_SECONDS
            )
        elif security == "starttls":
            connection = smtplib.SMTP(host, port, timeout=DEFAULT_TIMEOUT_SECONDS)
            connection.ehlo()
            connection.starttls(context=context)
            connection.ehlo()
        else:
            raise MailboxError("SMTP без TLS запрещён.")

        auth = str(config.get("auth", "password"))
        if auth == "oauth2":
            token = oauth_access_token(config)
            auth_string = f"user={account.address}\x01auth=Bearer {token}\x01\x01"
            encoded = base64.b64encode(auth_string.encode("utf-8")).decode("ascii")
            code, response = connection.docmd("AUTH", "XOAUTH2 " + encoded)
            if code != 235:
                raise MailboxError(f"SMTP XOAUTH2 отклонён с кодом {code}.")
        elif auth == "password":
            connection.login(account.address, require_secret(config, "password"))
        else:
            raise MailboxError(f"Неизвестный SMTP auth: {auth}.")
        return connection
    except (OSError, ssl.SSLError, smtplib.SMTPException) as exc:
        raise MailboxError(f"Не удалось подключиться к SMTP {host}:{port}: {exc}") from exc


def build_outgoing_message(args: argparse.Namespace, account: Account) -> EmailMessage:
    body = Path(args.body_file).read_text(encoding="utf-8")
    message = EmailMessage()
    message["From"] = account.address
    message["To"] = ", ".join(args.to)
    if args.cc:
        message["Cc"] = ", ".join(args.cc)
    if args.reply_to:
        message["Reply-To"] = args.reply_to
    message["Subject"] = args.subject
    message.set_content(body)

    for filename in args.attach or []:
        path = Path(filename)
        if not path.is_file():
            raise MailboxError(f"Нет файла вложения: {path}.")
        data = path.read_bytes()
        if len(data) > DEFAULT_MAX_MESSAGE_BYTES:
            raise MailboxError(f"Вложение {path.name} превышает безопасный лимит.")
        content_type, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (content_type or "application/octet-stream").split("/", 1)
        message.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype,
            filename=sanitize_filename(path.name),
        )
    return message


def gmail_send(config: Mapping[str, Any], message: EmailMessage) -> dict[str, Any]:
    encoded = base64.urlsafe_b64encode(message.as_bytes(policy=policy.SMTP)).decode("ascii")
    return gmail_request(config, "POST", "messages/send", payload={"raw": encoded})


def smtp_send(
    account: Account, config: Mapping[str, Any], message: EmailMessage
) -> dict[str, Any]:
    connection = smtp_connection(account, config)
    try:
        refused = connection.send_message(message)
        if refused:
            raise MailboxError(
                "SMTP отказал части получателей; повторная отправка без проверки запрещена."
            )
        return {"accepted": True}
    except smtplib.SMTPException as exc:
        raise MailboxError(
            "SMTP-отправка завершилась ошибкой. Сначала проверьте папку отправленных; "
            "не повторяйте через другой транспорт автоматически."
        ) from exc
    finally:
        try:
            connection.quit()
        except smtplib.SMTPException:
            connection.close()


def parse_iso_date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Дата должна быть в формате YYYY-MM-DD.") from exc


def search_filters(args: argparse.Namespace) -> SearchFilters:
    return SearchFilters(
        sender=args.sender,
        recipient=args.recipient,
        subject=args.subject,
        since=args.since,
        before=args.before,
        unread=args.unread,
        query=args.query,
    )


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def command_accounts(accounts: Mapping[str, Account]) -> int:
    output = []
    for account in accounts.values():
        output.append(
            {
                "id": account.id,
                "address": account.address,
                "provider": account.provider,
                "read_order": account.read_order,
                "send_order": account.send_order,
            }
        )
    print_json(output)
    return 0


def command_doctor(account: Account, connect: bool) -> int:
    read_transport, read_config = transport_config(account, "read")
    try:
        send_transport, send_config = transport_config(account, "send")
    except MailboxError:
        # Аккаунт только для чтения – нормальная конфигурация с минимальными
        # полномочиями, а не ошибка общей диагностики.
        send_transport, send_config = None, None
    result: dict[str, Any] = {
        "account": account.id,
        "address": account.address,
        "read_transport": read_transport,
        "send_transport": send_transport,
        "network_checked": False,
    }
    if connect:
        if read_transport == "gmail-api":
            gmail_request(read_config, "GET", "profile")
        elif read_transport == "imap":
            connection = imap_connection(account, read_config)
            connection.logout()
        if send_transport == "gmail-api" and send_config is not None:
            # Получение access token проверяет OAuth без создания внешнего объекта.
            oauth_access_token(send_config)
        elif send_transport == "smtp" and send_config is not None:
            connection = smtp_connection(account, send_config)
            connection.quit()
        result["network_checked"] = True
    print_json(result)
    return 0


def command_search(account: Account, args: argparse.Namespace) -> int:
    transport, config = transport_config(account, "read")
    filters = search_filters(args)
    if transport == "gmail-api":
        messages = gmail_search(config, filters, args.limit)
    elif transport == "imap":
        messages = imap_search(account, config, filters, args.limit)
    else:
        raise MailboxError(f"Транспорт {transport} не умеет искать письма.")
    print_json({"account": account.id, "transport": transport, "messages": messages})
    return 0


def command_read(account: Account, message_id: str) -> int:
    transport, config = transport_config(account, "read")
    message = parse_message(raw_message_for_account(account, transport, config, message_id))
    result = message_summary(message)
    result.update(
        {
            "account": account.id,
            "transport": transport,
            "id": message_id,
            "body": message_text(message),
            "attachments": [
                {"index": index, "filename": name, "content_type": content_type, "size": len(data)}
                for index, (name, content_type, data) in enumerate(
                    message_attachments(message), start=1
                )
            ],
        }
    )
    print_json(result)
    return 0


def command_attachments(account: Account, message_id: str) -> int:
    transport, config = transport_config(account, "read")
    message = parse_message(raw_message_for_account(account, transport, config, message_id))
    items = [
        {"index": index, "filename": name, "content_type": content_type, "size": len(data)}
        for index, (name, content_type, data) in enumerate(
            message_attachments(message), start=1
        )
    ]
    print_json(
        {"account": account.id, "transport": transport, "id": message_id, "attachments": items}
    )
    return 0


def command_save_attachment(
    account: Account, message_id: str, index: int, output_dir: Path
) -> int:
    transport, config = transport_config(account, "read")
    message = parse_message(raw_message_for_account(account, transport, config, message_id))
    attachments = message_attachments(message)
    if index < 1 or index > len(attachments):
        raise MailboxError(f"В письме нет вложения с индексом {index}.")
    name, content_type, data = attachments[index - 1]
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir.resolve() / name
    target.write_bytes(data)
    print_json(
        {
            "account": account.id,
            "transport": transport,
            "id": message_id,
            "path": str(target),
            "content_type": content_type,
            "size": len(data),
        }
    )
    return 0


def command_send(account: Account, args: argparse.Namespace) -> int:
    if not args.confirm_send:
        raise MailboxError(
            "Отправка требует подтверждения владельца в момент действия и флага --confirm-send."
        )
    transport, config = transport_config(account, "send")
    message = build_outgoing_message(args, account)
    if transport == "gmail-api":
        result = gmail_send(config, message)
    elif transport == "smtp":
        result = smtp_send(account, config, message)
    else:
        raise MailboxError(f"Транспорт {transport} не умеет отправлять письма.")
    print_json(
        {
            "account": account.id,
            "transport": transport,
            "to": args.to,
            "cc": args.cc or [],
            "subject": args.subject,
            "result": result,
        }
    )
    return 0


def add_account_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--account", required=True, help="id из локальной конфигурации")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="ignored TOML с аккаунтами; по умолчанию .local/email/accounts.toml",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("accounts", help="показать аккаунты без секретов")

    doctor = subparsers.add_parser("doctor", help="проверить настройку аккаунта")
    add_account_argument(doctor)
    doctor.add_argument("--connect", action="store_true", help="проверить сеть и вход")

    search = subparsers.add_parser("search", help="выполнить узкий поиск")
    add_account_argument(search)
    search.add_argument("--from", dest="sender")
    search.add_argument("--to", dest="recipient")
    search.add_argument("--subject")
    search.add_argument("--since", type=parse_iso_date)
    search.add_argument("--before", type=parse_iso_date)
    search.add_argument("--unread", action="store_true")
    search.add_argument("--query", help="дополнительный текст поиска")
    search.add_argument("--limit", type=int, default=20, choices=range(1, 101))

    read = subparsers.add_parser("read", help="прочитать выбранное письмо")
    add_account_argument(read)
    read.add_argument("--id", required=True, dest="message_id")

    attachments = subparsers.add_parser("attachments", help="перечислить вложения")
    add_account_argument(attachments)
    attachments.add_argument("--id", required=True, dest="message_id")

    save = subparsers.add_parser("save-attachment", help="сохранить выбранное вложение")
    add_account_argument(save)
    save.add_argument("--id", required=True, dest="message_id")
    save.add_argument("--index", required=True, type=int)
    save.add_argument("--output-dir", required=True, type=Path)

    send = subparsers.add_parser("send", help="отправить письмо после подтверждения")
    add_account_argument(send)
    send.add_argument("--to", required=True, action="append")
    send.add_argument("--cc", action="append")
    send.add_argument("--reply-to")
    send.add_argument("--subject", required=True)
    send.add_argument("--body-file", required=True)
    send.add_argument("--attach", action="append")
    send.add_argument("--confirm-send", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        accounts = load_accounts(args.config)
        if args.command == "accounts":
            return command_accounts(accounts)
        account = select_account(accounts, args.account)
        if args.command == "doctor":
            return command_doctor(account, args.connect)
        if args.command == "search":
            return command_search(account, args)
        if args.command == "read":
            return command_read(account, args.message_id)
        if args.command == "attachments":
            return command_attachments(account, args.message_id)
        if args.command == "save-attachment":
            return command_save_attachment(
                account, args.message_id, args.index, args.output_dir
            )
        if args.command == "send":
            return command_send(account, args)
        parser.error("неизвестная команда")
    except MailboxError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Ошибка локального файла: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
