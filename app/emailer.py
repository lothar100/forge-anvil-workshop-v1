from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v


def send_email(*, to_addr: str, subject: str, html_body: str, text_body: str | None = None) -> None:
    host = _env("SMTP_HOST")
    port = int(_env("SMTP_PORT", "587"))
    user = _env("SMTP_USER")
    password = _env("SMTP_PASS")
    from_addr = _env("SMTP_FROM", user or "noreply@localhost")

    if not host:
        raise RuntimeError("SMTP_HOST not configured")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    if text_body:
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(host, port, timeout=20) as s:
        s.ehlo()
        try:
            s.starttls()
            s.ehlo()
        except Exception:
            pass
        if user and password:
            s.login(user, password)
        s.sendmail(from_addr, [to_addr], msg.as_string())
