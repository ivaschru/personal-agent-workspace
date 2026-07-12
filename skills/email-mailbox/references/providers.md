# Провайдеры и выбор транспорта

## Gmail

- Чтение и отправка: Gmail API через OAuth 2.0.
- Для чтения запрашивать `https://www.googleapis.com/auth/gmail.readonly`, для отправки – `https://www.googleapis.com/auth/gmail.send`. Не выдавать `gmail.modify`, если изменение писем не требуется.
- Резервное чтение: IMAP `imap.gmail.com:993` через XOAUTH2.
- Резервная отправка: SMTP `smtp.gmail.com:465` либо `:587` через XOAUTH2.
- Важное ограничение: IMAP/SMTP OAuth требует широкого scope `https://mail.google.com/`; не переиспользовать для него токен Gmail API с узкими правами.
- Официальная документация: <https://developers.google.com/workspace/gmail/api/auth/scopes>, <https://developers.google.com/workspace/gmail/imap/imap-smtp>.

## Яндекс Почта

- Публичного пользовательского API для чтения содержания писем нет.
- Чтение: IMAP `imap.yandex.com:993`.
- Отправка: SMTP `smtp.yandex.com:465`.
- Предпочитать OAuth XOAUTH2. Яндекс позволяет раздельно выдать `mail:imap_ro` для чтения и `mail:smtp` для отправки.
- API Яндекс 360 предназначен преимущественно для администрирования организаций, маршрутизации и общих ящиков, а не для обычного чтения переписки пользователя.
- Официальная документация: <https://yandex.ru/support/yandex-360/business/mail/ru/web/security/oauth>.

## Почта Mail и VK WorkSpace

- Общедоступного пользовательского REST API для чтения обычной переписки не заявлено.
- Чтение: IMAP `imap.mail.ru:993`.
- Отправка: SMTP `smtp.mail.ru:465`.
- Для личного ящика использовать отдельный пароль внешнего приложения.
- API VK WorkSpace относится к администрированию домена; gRPC API резервного копирования относится к On-Premises и не является обычным пользовательским почтовым API.
- Официальная документация: <https://help.mail.ru/mail/login/mailer/>, <https://biz.mail.ru/developer/api.html>.

## Рамблер/почта

- Общедоступного пользовательского API для писем не заявлено.
- Чтение: IMAP `imap.rambler.ru:993`.
- Отправка: SMTP `smtp.rambler.ru:465`.
- При двухфакторной аутентификации использовать специальный пароль приложения.
- Официальная документация: <https://help.rambler.ru/mail/mail-pochtovye-klienty/1275/>.

## Другой хостинг

Указать выданные провайдером IMAP и SMTP серверы, TLS-порты и отдельный пароль приложения либо OAuth XOAUTH2. Не включать соединение без TLS и не отключать проверку сертификата.
