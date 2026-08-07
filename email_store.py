import json
from pathlib import Path


def emails_path(output_dir: str) -> Path:
    return Path(output_dir) / "emails.json"


def load_emails(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def save_emails(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")


def email_ids(records: list[dict]) -> set[str]:
    return {str(record["id"]) for record in records if record.get("id")}


def email_case_numbers(records: list[dict]) -> set[str]:
    """Case numbers from saved closed emails (for closed_mode=skip)."""
    cases: set[str] = set()
    for record in records:
        case = str(record.get("case_number") or "").strip()
        if case and case not in {"N/A", "unrelated"}:
            cases.add(case)
    return cases


def merge_new_emails(existing: list[dict], new_records: list[dict], limit: int) -> list[dict]:
    merged: list[dict] = []
    seen: set[str] = set()
    for record in new_records + existing:
        record_id = record.get("id")
        if record_id and record_id in seen:
            continue
        if record_id:
            seen.add(str(record_id))
        merged.append(record)
        if len(merged) >= limit:
            break
    return merged
