"""
actions/email_sender.py — Gama Email Sender
=============================================
Send emails via SMTP (Gmail, Outlook, etc.).

Credentials are stored using the encrypted credential store
(security/credential_store.py) — never in plaintext JSON.

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

from utils.paths import get_base_dir as _get_base_dir

import json
import logging
import email as email_lib
import imaplib
import smtplib
import ssl
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

log = get_logger(__name__)
logger = log  # back-compat alias
# SMTP server presets
_IMAP_PRESETS = {
    "gmail":   {"server": "imap.gmail.com", "port": 993},
    "outlook": {"server": "outlook.office365.com", "port": 993},
    "yahoo":   {"server": "imap.mail.yahoo.com", "port": 993},
    "hotmail": {"server": "outlook.office365.com", "port": 993},
}

_SMTP_PRESETS = {
    "gmail": {"server": "smtp.gmail.com", "port": 587},
    "outlook": {"server": "smtp-mail.outlook.com", "port": 587},
    "yahoo": {"server": "smtp.mail.yahoo.com", "port": 587},
    "hotmail": {"server": "smtp-mail.outlook.com", "port": 587},
}

# Credential-store keys
_CRED_EMAIL = "email_address"
_CRED_PASSWORD = "email_password"
_CRED_PROVIDER = "email_provider"



BASE_DIR = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _get_email_config() -> dict:
    """Load email credentials from the encrypted credential store.
    Falls back to the plaintext config for the non-secret `email_address`
    and `email_provider` fields, which are not sensitive on their own."""
    from security.credential_store import get_secret, available as store_available
    password = get_secret(_CRED_PASSWORD) if store_available() else None

    # Non-secret meta (address + provider) are kept in the JSON config
    # since they are not passwords.  The password field there is now
    # always blank — only the encrypted store has it.
    email_addr = ""
    provider = "gmail"
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        email_addr = data.get("email_address", "")
        provider = data.get("email_provider", "gmail")
    except Exception:
        pass

    return {
        "email": email_addr,
        "password": password or "",
        "provider": provider,
    }


def email_sender(action: str = "send", **kwargs) -> str:
    """Send emails via SMTP."""
    action = (action or "send").lower().strip()

    if action == "send":
        return _send(
            kwargs.get("to", ""),
            kwargs.get("subject", ""),
            kwargs.get("body", ""),
            kwargs.get("provider", "gmail"),
        )
    if action == "setup":
        return _setup(
            kwargs.get("email", ""),
            kwargs.get("password", ""),
            kwargs.get("provider", "gmail"),
        )
    if action in ("read", "list", "unread", "inbox"):
        return _read_emails(
            limit=int(kwargs.get("limit", kwargs.get("max_results", 5)) or 5),
            unread_only=action in ("unread",) or str(kwargs.get("unread_only", "true")).lower() in ("1", "true", "yes"),
            query=kwargs.get("query") or kwargs.get("from_filter") or "",
            folder=kwargs.get("folder") or "INBOX",
        )
    if action in ("summarize", "summary"):
        return _summarize_emails(
            limit=int(kwargs.get("limit", 5) or 5),
            unread_only=str(kwargs.get("unread_only", "true")).lower() in ("1", "true", "yes"),
            query=kwargs.get("query") or "",
        )
    if action in ("read_one", "get", "show"):
        return _read_one(
            message_id=kwargs.get("message_id") or kwargs.get("id") or "",
            index=int(kwargs.get("index", 1) or 1),
            folder=kwargs.get("folder") or "INBOX",
        )
    return (
        "Unknown email action. Use: send, setup, read/list/unread, "
        "summarize, read_one."
    )


def _setup(email_addr: str, password: str, provider: str = "gmail") -> str:
    """Save email credentials.

    The password goes into the encrypted credential store (DPAPI on Windows,
    Fernet fallback elsewhere) — it is NEVER written to the JSON config file.
    Only the non-secret email address and provider name are stored in JSON.
    """
    if not email_addr or not password:
        return "Please provide both email and password."

    from security.credential_store import set_secret, available as store_available

    # 1. Store the password encrypted.
    if store_available():
        ok = set_secret(_CRED_PASSWORD, password)
        if not ok:
            return ("Secure credential store is unavailable — password not saved. "
                    "Please ensure pywin32 or the 'cryptography' package is installed.")
    else:
        return ("Secure credential store is unavailable (no DPAPI or cryptography backend). "
                "Password was not saved. Install pywin32 (Windows) or the 'cryptography' "
                "package, then try again.")

    # 2. Save the non-secret address + provider to JSON (no password field).
    try:
        try:
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        data["email_address"] = email_addr
        data["email_provider"] = provider.lower()
        # Scrub any old plaintext password that might have been written previously.
        data.pop("email_password", None)
        with open(API_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        logger.warning(f"email_sender: could not update config JSON: {exc}")

    return (f"Email credentials saved securely for {email_addr} ({provider}). "
            "The password is encrypted — it will never appear in any config file. "
            "You can now send emails. Remember to use an App Password for Gmail.")


def _send(to: str, subject: str, body: str, provider: str = "gmail") -> str:
    """Send an email."""
    if not to:
        return "Who should I send the email to?"
    if not subject:
        return "What's the email subject?"
    if not body:
        return "What's the email body?"

    config = _get_email_config()
    sender_email = config.get("email", "")
    sender_password = config.get("password", "")
    provider_name = (provider or config.get("provider", "gmail")).lower()

    if not sender_email or not sender_password:
        return ("Email not configured. Say 'setup email with my address and password'. "
                "For Gmail, use an App Password from https://myaccount.google.com/apppasswords")

    smtp = _SMTP_PRESETS.get(provider_name, _SMTP_PRESETS["gmail"])

    msg = MIMEMultipart()
    msg["From"] = sender_email
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(smtp["server"], smtp["port"]) as server:
            server.starttls(context=context)
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, to, msg.as_string())
        return f"Email sent to {to}: '{subject}'"
    except Exception as exc:
        return f"Email failed: {exc}"




def _decode_mime_header(value: str) -> str:
    if not value:
        return ""
    parts = []
    try:
        for frag, enc in decode_header(value):
            if isinstance(frag, bytes):
                parts.append(frag.decode(enc or "utf-8", errors="replace"))
            else:
                parts.append(str(frag))
    except Exception:
        return str(value)
    return " ".join(parts).strip()


def _imap_connect():
    """Return (imap, email_addr) or (None, error_string)."""
    cfg = _get_email_config()
    email_addr = (cfg.get("email") or "").strip()
    password = (cfg.get("password") or "").strip()
    provider = (cfg.get("provider") or "gmail").lower().strip()
    if not email_addr or not password:
        return None, (
            "Email not configured. Say 'setup email with my address and app password'. "
            "For Gmail use an App Password from https://myaccount.google.com/apppasswords"
        )
    preset = _IMAP_PRESETS.get(provider, _IMAP_PRESETS["gmail"])
    try:
        imap = imaplib.IMAP4_SSL(preset["server"], preset["port"])
        imap.login(email_addr, password)
        return imap, email_addr
    except Exception as exc:
        return None, f"IMAP login failed: {exc}"


def _fetch_message_summaries(imap, ids, limit: int = 5):
    out = []
    for raw_id in ids[:limit]:
        try:
            typ, data = imap.fetch(raw_id, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if typ != "OK" or not data or not data[0]:
                continue
            header_bytes = data[0][1] if isinstance(data[0], tuple) else data[0]
            msg = email_lib.message_from_bytes(header_bytes)
            out.append({
                "id": raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id),
                "from": _decode_mime_header(msg.get("From", "")),
                "subject": _decode_mime_header(msg.get("Subject", "")),
                "date": msg.get("Date", ""),
            })
        except Exception as exc:
            logger.debug("fetch header failed: %s", exc)
    return out


def _read_emails(limit: int = 5, unread_only: bool = True, query: str = "", folder: str = "INBOX") -> str:
    """List recent (optionally unread) messages via IMAP."""
    limit = max(1, min(int(limit or 5), 25))
    imap, err = _imap_connect()
    if imap is None:
        return str(err)
    try:
        imap.select(folder or "INBOX", readonly=True)
        if query:
            q = query.replace('"', "")
            criteria = f'(OR FROM "{q}" SUBJECT "{q}")'
            if unread_only:
                criteria = f'(UNSEEN OR FROM "{q}" SUBJECT "{q}")'
        else:
            criteria = "UNSEEN" if unread_only else "ALL"
        typ, data = imap.search(None, criteria)
        if typ != "OK" or not data or not data[0]:
            scope = "unread " if unread_only else ""
            return f"No {scope}messages found in {folder}."
        ids = list(reversed(data[0].split()))
        summaries = _fetch_message_summaries(imap, ids, limit=limit)
        if not summaries:
            return "No messages could be read."
        lines = [f"Showing {len(summaries)} email(s)" + (" (unread)" if unread_only else "") + ":"]
        for i, s in enumerate(summaries, 1):
            lines.append(
                f"{i}. From: {s['from']}\n"
                f"   Subject: {s['subject']}\n"
                f"   Date: {s['date']}\n"
                f"   Id: {s['id']}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"Email read failed: {exc}"
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _extract_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if ctype == "text/plain" and "attachment" not in disp:
                try:
                    payload = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
                except Exception:
                    continue
        return ""
    try:
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    except Exception:
        return str(msg.get_payload() or "")


def _read_one(message_id: str = "", index: int = 1, folder: str = "INBOX") -> str:
    imap, err = _imap_connect()
    if imap is None:
        return str(err)
    try:
        imap.select(folder or "INBOX", readonly=True)
        mid = (message_id or "").strip()
        if not mid:
            typ, data = imap.search(None, "ALL")
            if typ != "OK" or not data or not data[0]:
                return "Inbox is empty."
            ids = list(reversed(data[0].split()))
            idx = max(1, int(index or 1)) - 1
            if idx >= len(ids):
                return f"No message at index {index}."
            mid = ids[idx].decode() if isinstance(ids[idx], bytes) else str(ids[idx])
        fetch_id = mid.encode() if isinstance(mid, str) else mid
        typ, data = imap.fetch(fetch_id, "(RFC822)")
        if typ != "OK" or not data or not data[0]:
            return f"Could not fetch message id={mid}."
        raw = data[0][1] if isinstance(data[0], tuple) else data[0]
        msg = email_lib.message_from_bytes(raw)
        body = _extract_body(msg).strip()
        if len(body) > 4000:
            body = body[:4000] + "\n… [truncated]"
        return (
            f"From: {_decode_mime_header(msg.get('From', ''))}\n"
            f"Subject: {_decode_mime_header(msg.get('Subject', ''))}\n"
            f"Date: {msg.get('Date', '')}\n\n"
            f"{body or '(no plain-text body)'}"
        )
    except Exception as exc:
        return f"Read message failed: {exc}"
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _summarize_emails(limit: int = 5, unread_only: bool = True, query: str = "") -> str:
    listing = _read_emails(limit=limit, unread_only=unread_only, query=query)
    if listing.startswith(("Email not", "IMAP", "No ")):
        return listing
    try:
        api_key = ""
        try:
            import json as _json
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                api_key = _json.load(f).get("gemini_api_key", "")
        except Exception:
            pass
        if not api_key:
            return listing + "\n\n(Summary skipped — no Gemini API key configured.)"
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = (
            "Summarize these emails for a busy user in 5-8 bullet points. "
            "Call out anything urgent. Be concise.\n\n" + listing
        )
        resp = client.models.generate_content(
            model="gemini-2.0-flash-lite",
            contents=prompt,
        )
        summary = (resp.text or "").strip()
        return summary or listing
    except Exception as exc:
        return listing + f"\n\n(Summary failed: {exc})"

__all__ = ["email_sender"]

