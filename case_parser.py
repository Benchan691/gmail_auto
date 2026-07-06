import re
from html import unescape


def html_to_text(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|tr|li|h[1-6])>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def plain_text_body(text_body: str, html_body: str) -> str:
    if text_body and text_body.strip():
        return text_body.strip()
    if html_body and html_body.strip():
        return html_to_text(html_body)
    return ""


def parse_case_fields(subject: str, body: str) -> dict:
    subject = subject or ""
    body = body or ""

    unrelated = {
        "case_number": "unrelated",
        "case_status": "unrelated",
        "resolution": "unrelated",
    }

    case_number = None
    number_match = re.search(r"Case Number:\s*(\d+)", subject, re.I)
    if not number_match:
        number_match = re.search(r"Case Number\s*\n+\s*(\d+)", body, re.I)
    if number_match:
        case_number = number_match.group(1)

    is_related = bool(case_number) or bool(
        re.search(
            r"TrustCSI Security Incident|Correlation event summary|Case Status:",
            f"{subject}\n{body}",
            re.I,
        )
    )
    if not is_related:
        return unrelated

    case_status = None
    resolution = None

    halo_block = re.search(
        r"#{4,}\s*\nCase Status:\s*(.+?)\nResolution:\s*(.+?)\n#{4,}",
        body,
        re.I | re.S,
    )
    if halo_block:
        case_status = halo_block.group(1).strip()
        resolution = halo_block.group(2).strip()
    else:
        status_match = re.search(r"Case Status:\s*(.+?)(?:\n|$)", body, re.I)
        if status_match:
            case_status = status_match.group(1).strip()
        else:
            status_match = re.search(r"Case Status\s*\n+\s*([A-Za-z][^\n]*)", body, re.I)
            if status_match:
                case_status = status_match.group(1).strip()

        resolution_match = re.search(
            r"Resolution:\s*(.+?)(?:\n#{4,}|\n\nregards,|\n\nThank you\.|$)",
            body,
            re.I | re.S,
        )
        if resolution_match:
            resolution = resolution_match.group(1).strip()

    if case_status:
        case_status = re.sub(r"\s+", " ", case_status).strip()
    if resolution:
        resolution = resolution.replace("\r\n", "\n").replace("\r", "\n").strip()

    return {
        "case_number": case_number or "N/A",
        "case_status": case_status or "N/A",
        "resolution": resolution or "N/A",
    }


def case_fields_for_json(case_fields: dict, *, message_id: str = "", subject: str = "") -> dict:
    def null_if_missing(value):
        if value is None:
            return None
        text = str(value).strip()
        if not text or text in {"N/A", "unrelated"}:
            return None
        return text

    subject_text = (subject or "").strip()
    if subject_text == "(no subject)":
        subject_text = ""

    return {
        "id": (message_id or "").strip() or None,
        "subject": subject_text or None,
        "case_number": null_if_missing(case_fields.get("case_number")),
        "case_status": null_if_missing(case_fields.get("case_status")),
        "resolution": null_if_missing(case_fields.get("resolution")),
    }
