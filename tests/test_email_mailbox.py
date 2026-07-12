"""Проверяет почтовый CLI без подключения к реальным аккаунтам."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from unittest import mock
from email.message import EmailMessage
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills/email-mailbox/scripts/email_mailbox.py"
)
SPEC = importlib.util.spec_from_file_location("email_mailbox", SCRIPT)
assert SPEC and SPEC.loader
MAILBOX = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MAILBOX
SPEC.loader.exec_module(MAILBOX)


class EmailMailboxTests(unittest.TestCase):
    def test_example_configuration_uses_api_before_protocols(self) -> None:
        config = (
            SCRIPT.parents[1] / "references/accounts.example.toml"
        )
        accounts = MAILBOX.load_accounts(config)

        gmail = accounts["personal-gmail"]
        self.assertEqual(gmail.read_order, ("gmail-api", "imap"))
        self.assertEqual(gmail.send_order, ("gmail-api", "smtp"))
        self.assertEqual(MAILBOX.transport_config(gmail, "read")[0], "gmail-api")
        self.assertEqual(MAILBOX.transport_config(gmail, "send")[0], "gmail-api")
        self.assertIs(
            MAILBOX.transport_config(gmail, "read")[1], gmail.raw["gmail_api_read"]
        )
        self.assertIs(
            MAILBOX.transport_config(gmail, "send")[1], gmail.raw["gmail_api_send"]
        )

        yandex = accounts["yandex"]
        self.assertEqual(MAILBOX.transport_config(yandex, "read")[0], "imap")
        self.assertEqual(MAILBOX.transport_config(yandex, "send")[0], "smtp")

    def test_unconfigured_primary_transport_is_skipped_before_network_use(self) -> None:
        account = MAILBOX.Account(
            id="test",
            address="owner@example.com",
            provider="generic",
            read_order=("gmail-api", "imap"),
            send_order=("gmail-api", "smtp"),
            raw={"imap": {"host": "imap.example.com"}, "smtp": {"host": "smtp.example.com"}},
        )
        self.assertEqual(MAILBOX.transport_config(account, "read")[0], "imap")
        self.assertEqual(MAILBOX.transport_config(account, "send")[0], "smtp")

    def test_read_only_account_is_valid_for_doctor(self) -> None:
        account = MAILBOX.Account(
            id="read-only",
            address="owner@example.com",
            provider="gmail",
            read_order=("gmail-api",),
            send_order=(),
            raw={"gmail_api_read": {}},
        )
        with mock.patch.object(MAILBOX, "print_json") as output:
            self.assertEqual(MAILBOX.command_doctor(account, connect=False), 0)
        self.assertIsNone(output.call_args.args[0]["send_transport"])

    def test_message_parser_prefers_plain_text_and_sanitizes_attachment_name(self) -> None:
        message = EmailMessage()
        message["From"] = "Sender <sender@example.com>"
        message["To"] = "owner@example.com"
        message["Subject"] = "Тест"
        message.set_content("Обычный текст")
        message.add_alternative(
            "<html><body><script>secret()</script><p>HTML текст</p></body></html>",
            subtype="html",
        )
        message.add_attachment(
            b"content",
            maintype="application",
            subtype="octet-stream",
            filename="../../danger.txt",
        )

        parsed = MAILBOX.parse_message(message.as_bytes())
        self.assertEqual(MAILBOX.message_text(parsed), "Обычный текст")
        attachments = MAILBOX.message_attachments(parsed)
        self.assertEqual(attachments[0][0], "danger.txt")
        self.assertEqual(attachments[0][2], b"content")

    def test_imap_date_does_not_depend_on_system_locale(self) -> None:
        self.assertEqual(MAILBOX.imap_date(MAILBOX.date(2026, 7, 12)), "12-Jul-2026")

    def test_local_configuration_requires_private_permissions(self) -> None:
        if MAILBOX.os.name != "posix":
            self.skipTest("Проверка прав относится к POSIX")
        local_dir = MAILBOX.ROOT / ".local" / "email"
        local_dir.mkdir(parents=True, exist_ok=True)
        path = local_dir / "permissions-test.toml"

        def cleanup_local_fixture() -> None:
            path.unlink(missing_ok=True)
            # Публичный template scanner запрещает даже пустую tracked/working
            # `.local`, поэтому тест обязан полностью убрать созданный каркас.
            for directory in (local_dir, local_dir.parent):
                try:
                    directory.rmdir()
                except OSError:
                    pass

        self.addCleanup(cleanup_local_fixture)
        path.write_text(
            'schema_version = 1\n[[accounts]]\nid = "a"\n'
            'address = "a@example.com"\nread_order = []\nsend_order = []\n',
            encoding="utf-8",
        )
        path.chmod(0o644)
        with self.assertRaisesRegex(MAILBOX.MailboxError, "chmod 600"):
            MAILBOX.load_accounts(path)
        path.chmod(0o600)
        self.assertIn("a", MAILBOX.load_accounts(path))

    def test_send_requires_explicit_confirmation_before_transport_selection(self) -> None:
        account = MAILBOX.Account(
            id="test",
            address="owner@example.com",
            provider="generic",
            read_order=(),
            send_order=("smtp",),
            raw={"smtp": {"host": "smtp.example.com"}},
        )
        args = type("Args", (), {"confirm_send": False})()
        with self.assertRaisesRegex(MAILBOX.MailboxError, "подтверждения"):
            MAILBOX.command_send(account, args)

    def test_skill_does_not_implement_deprecated_download_transport(self) -> None:
        skill_root = SCRIPT.parents[1]
        for path in skill_root.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
            self.assertNotIn("pop3", text, path)


if __name__ == "__main__":
    unittest.main()
