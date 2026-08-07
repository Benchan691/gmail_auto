from __future__ import annotations

import json
import re
import time
from urllib.parse import quote, urlparse, urlunparse

from case_parser import parse_case_fields
from common import config_bool, debug, require_requests
from zimbra import scan_closed_folder_records, zimbra_resolve_folder_path, zimbra_soap_login


def _normalize_url(url: str) -> str:
    url = url.strip()
    if "://" not in url:
        url = f"https://{url}"
    return url.rstrip("/")


def _derive_splunk_rest_url(config: dict) -> str:
    if config.get("splunk_rest_url"):
        return _normalize_url(config["splunk_rest_url"])

    web_url = _normalize_url(config["splunk_web_url"])
    parts = urlparse(web_url)
    if not parts.hostname:
        raise ValueError(f"Invalid splunk_web_url: {config['splunk_web_url']}")

    host = parts.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return urlunparse((parts.scheme or "https", f"{host}:8089", "", "", "", "")).rstrip("/")


def _required_splunk_config(config: dict) -> dict:
    required = ["splunk_username", "splunk_password"]
    missing = [key for key in required if not config.get(key)]
    if not config.get("splunk_web_url") and not config.get("splunk_rest_url"):
        missing.append("splunk_web_url or splunk_rest_url")
    if missing:
        raise ValueError(f"Missing required Splunk config fields: {missing}")

    username = config["splunk_username"]
    return {
        "rest_url": _derive_splunk_rest_url(config),
        "username": username,
        "password": config["splunk_password"],
        "app": config.get("splunk_app") or "search",
        "owner": config.get("splunk_owner") or username,
        "verify_tls": config_bool(config, "splunk_verify_tls", False),
        "timeout": int(config.get("splunk_timeout", 180)),
    }


def lookup_name_from_case_number(case_number: str) -> str:
    digits = "".join(c for c in str(case_number).strip() if c.isdigit())
    if len(digits) < 5:
        raise ValueError(f"case_number too short for lookup name: {case_number!r}")
    return f"G{digits[:5]}_Ticket_Status.csv"


LOOKUP_CSV_COLUMNS = ("TicketNumber", "Severity", "Status", "Remark", "Matrix", "Actionable")
UPDATE_MODES = frozenset({"overwrite", "skip"})


def _splunk_literal(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _lookup_table_clause() -> str:
    return f"| table {', '.join(LOOKUP_CSV_COLUMNS)}"


def update_mode_from_config(config: dict, key: str, default: str = "overwrite") -> str:
    """Return 'overwrite' or 'skip' for closed_mode / actionable_mode."""
    raw = config.get(key, default)
    mode = str(raw if raw is not None else default).strip().lower()
    if mode not in UPDATE_MODES:
        raise ValueError(f"{key} must be 'overwrite' or 'skip', got {raw!r}")
    return mode


def actionable_already_set(row: dict) -> bool:
    return bool(str(row.get("Actionable") or "").strip())


def select_tickets_for_update(
    case_updates: dict[str, str],
    rows: list[dict],
    mode: str,
    already_set,
) -> tuple[dict[str, str], list[str]]:
    """Return (tickets to write, tickets skipped because already set in skip mode)."""
    by_ticket = {str(row.get("TicketNumber", "")).strip(): row for row in rows}
    selected: dict[str, str] = {}
    skipped: list[str] = []
    for ticket, value in case_updates.items():
        row = by_ticket.get(ticket)
        if row is None:
            continue
        if mode == "skip" and already_set(row):
            skipped.append(ticket)
            continue
        selected[ticket] = value
    return selected, skipped


def _splunk_fetch_lookup_rows(session, settings: dict, lookup_name: str) -> list[dict]:
    search = f"| inputlookup {_splunk_literal(lookup_name)}"
    rows = _splunk_run_search(session, settings, search, f"fetch {lookup_name}", want_results=True)
    debug(f"Fetched lookup rows: lookup={lookup_name} rows={len(rows)}")
    return rows


def build_splunk_batch_update_search(lookup_name: str, case_updates: dict[str, dict[str, str]]) -> str:
    # Update field values, then pin CSV column order before outputlookup.
    lines = [f"| inputlookup {_splunk_literal(lookup_name)}"]
    for ticket, payload in case_updates.items():
        resolution = payload.get("resolution", "")
        matrix_value = payload.get("matrix") or matrix_from_resolution(resolution)
        ticket_lit = _splunk_literal(ticket)
        lines.append(f"| eval Status=if(TicketNumber={ticket_lit}, {_splunk_literal('Resolved')}, Status)")
        lines.append(f"| eval Remark=if(TicketNumber={ticket_lit}, {_splunk_literal(resolution)}, Remark)")
        lines.append(f"| eval Matrix=if(TicketNumber={ticket_lit}, {_splunk_literal(matrix_value)}, Matrix)")
    lines.append(_lookup_table_clause())
    lines.append(f"| outputlookup {_splunk_literal(lookup_name)}")
    return "\n".join(lines)


def build_splunk_actionable_update_search(lookup_name: str, case_updates: dict[str, str]) -> str:
    # Actionable only — never touch Status / Remark / Matrix.
    lines = [f"| inputlookup {_splunk_literal(lookup_name)}"]
    for ticket, value in case_updates.items():
        ticket_lit = _splunk_literal(ticket)
        lines.append(
            f"| eval Actionable=if(TicketNumber={ticket_lit}, {_splunk_literal(value)}, Actionable)"
        )
    lines.append(_lookup_table_clause())
    lines.append(f"| outputlookup {_splunk_literal(lookup_name)}")
    return "\n".join(lines)


def _splunk_write_lookup_via_spl(session, settings: dict, search: str, label: str) -> None:
    _splunk_run_search(session, settings, search, label, want_results=False)


def _splunk_update_lookup_cases(
    session, settings: dict, lookup_name: str, case_updates: dict[str, dict[str, str]]
) -> int:
    rows = _splunk_fetch_lookup_rows(session, settings, lookup_name)
    if not rows:
        print(f"[-] Lookup {lookup_name} is empty or not found.")
        return 0

    existing = {str(row.get("TicketNumber", "")).strip() for row in rows}
    matched = {ticket for ticket in case_updates if ticket in existing}
    if not matched:
        tickets = ", ".join(sorted(case_updates))
        print(f"[-] No lookup row matched TicketNumber(s) {tickets} in {lookup_name}; skipped")
        return 0

    updates = {ticket: case_updates[ticket] for ticket in matched}
    search = build_splunk_batch_update_search(lookup_name, updates)
    _splunk_write_lookup_via_spl(session, settings, search, f"update {lookup_name}")
    return len(updates)


def _splunk_update_lookup_actionable(
    session,
    settings: dict,
    lookup_name: str,
    case_updates: dict[str, str],
    mode: str = "overwrite",
) -> int:
    rows = _splunk_fetch_lookup_rows(session, settings, lookup_name)
    if not rows:
        print(f"[-] Lookup {lookup_name} is empty or not found.")
        return 0

    existing = {str(row.get("TicketNumber", "")).strip() for row in rows}
    matched = {ticket for ticket in case_updates if ticket in existing}
    if not matched:
        tickets = ", ".join(sorted(case_updates))
        print(f"[-] No lookup row matched TicketNumber(s) {tickets} in {lookup_name}; skipped")
        return 0

    matched_updates = {ticket: case_updates[ticket] for ticket in matched}
    updates, skipped = select_tickets_for_update(
        matched_updates, rows, mode, actionable_already_set
    )
    if not updates:
        return 0

    search = build_splunk_actionable_update_search(lookup_name, updates)
    _splunk_write_lookup_via_spl(session, settings, search, f"update-actionable {lookup_name}")
    return len(updates)


def _splunk_jobs_path(owner: str, app: str) -> str:
    return f"/servicesNS/{quote(owner, safe='')}/{quote(app, safe='')}/search/jobs"


def _splunk_request(session, method: str, settings: dict, path: str, **kwargs):
    response = session.request(
        method,
        f"{settings['rest_url']}{path}",
        auth=(settings["username"], settings["password"]),
        verify=settings["verify_tls"],
        timeout=60,
        **kwargs,
    )
    debug(f"Splunk {method} {path}: HTTP {response.status_code}")
    if response.status_code >= 400:
        raise RuntimeError(f"Splunk request failed: HTTP {response.status_code}\n{response.text[:1500]}")
    return response


def _splunk_json(response) -> dict:
    try:
        return response.json()
    except ValueError as e:
        raise RuntimeError(f"Splunk returned non-JSON response:\n{response.text[:1500]}") from e


def _splunk_run_search(session, settings: dict, search: str, label: str, want_results: bool) -> list[dict]:
    jobs_path = _splunk_jobs_path(settings["owner"], settings["app"])
    if label.startswith("update "):
        debug(f"Splunk search start ({label}): search_chars={len(search)} resolution omitted from log")
    else:
        debug(f"Splunk search start ({label}): {search}")

    response = _splunk_request(
        session,
        "POST",
        settings,
        jobs_path,
        data={"search": search, "output_mode": "json"},
    )
    sid = _splunk_json(response).get("sid")
    if not sid:
        raise RuntimeError(f"Splunk did not return a search sid:\n{response.text[:1500]}")

    debug(f"Splunk job created ({label}): sid={sid}")
    job_path = f"{jobs_path}/{quote(sid, safe='')}"
    deadline = time.monotonic() + settings["timeout"]

    while True:
        response = _splunk_request(session, "GET", settings, job_path, params={"output_mode": "json"})
        content = (_splunk_json(response).get("entry") or [{}])[0].get("content", {})
        state = content.get("dispatchState", "")
        done = str(content.get("isDone", "0")).lower() in {"1", "true"}
        debug(
            f"Splunk job status ({label}): state={state} done={done} "
            f"progress={content.get('doneProgress', '')} event_count={content.get('eventCount', '')}"
        )
        if done:
            break
        if state in {"FAILED", "CANCELED"}:
            raise RuntimeError(f"Splunk search {sid} ended with state={state}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"Splunk search {sid} timed out after {settings['timeout']} seconds")
        time.sleep(1)

    if not want_results:
        return []

    response = _splunk_request(
        session,
        "GET",
        settings,
        f"{job_path}/results",
        params={"output_mode": "json", "count": 0},
    )
    results = _splunk_json(response).get("results") or []
    debug(f"Splunk results ({label}): rows={len(results)}")
    return results


def matrix_from_resolution(resolution: str) -> str:
    """Matrix defaults to False Positive; use True Positive only when the resolution says so."""
    text = str(resolution or "")
    if re.search(r"(?i)\btrue\s+positive\b", text):
        return "True Positive"
    return "False Positive"


def sanitize_resolution_for_splunk(resolution: str) -> str:
    """Remove True/False positive labels from resolution text before writing Remark."""
    text = re.sub(r"(?i)\b(?:true|false)\s+positive\b", "", str(resolution or ""))
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        cleaned = re.sub(r"[ \t]{2,}", " ", line).strip(" \t,;:-–—")
        # "sentence. False positive." → "sentence. ." → collapse to one full stop
        cleaned = re.sub(r"\.(?:\s*\.)+", ".", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip(" \t,;:-–—")
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines).strip()


def case_update_from_fields(case_fields: dict) -> tuple[dict | None, str]:
    case_number = str(case_fields.get("case_number") or "").strip()
    case_status = str(case_fields.get("case_status") or "").strip()
    raw_resolution = str(case_fields.get("resolution") or "").strip()
    matrix = matrix_from_resolution(raw_resolution)
    resolution = sanitize_resolution_for_splunk(raw_resolution)

    if not case_number or case_number in {"N/A", "unrelated"}:
        return None, "no case number"
    if case_status.lower() != "closed":
        return None, f"status is {case_status or 'empty'}, not Closed"
    if not resolution or resolution in {"N/A", "unrelated"}:
        return None, "closed case has no resolution"

    return {"case_number": case_number, "resolution": resolution, "matrix": matrix}, "queued"


def build_splunk_update_search(
    lookup_name: str, case_number: str, resolution: str, matrix: str | None = None
) -> str:
    return build_splunk_batch_update_search(
        lookup_name,
        {
            case_number: {
                "resolution": resolution,
                "matrix": matrix or matrix_from_resolution(resolution),
            }
        },
    )


def _splunk_update_case(session, settings: dict, update: dict) -> int:
    case_number = update["case_number"]
    try:
        lookup_name = lookup_name_from_case_number(case_number)
    except ValueError as e:
        print(f"[-] Skip TicketNumber={case_number}: {e}")
        return 0

    return _splunk_update_lookup_cases(
        session,
        settings,
        lookup_name,
        {
            case_number: {
                "resolution": update["resolution"],
                "matrix": update.get("matrix") or matrix_from_resolution(update["resolution"]),
            }
        },
    )


def update_splunk_from_records(records: list[dict], config: dict) -> int:
    if not records:
        return 0

    req = require_requests()
    settings = _required_splunk_config(config)
    if not settings["verify_tls"]:
        req.packages.urllib3.disable_warnings()

    updates: dict[str, dict] = {}
    for index, record in enumerate(records, start=1):
        case_fields = {
            "case_number": record.get("case_number") or "N/A",
            "case_status": record.get("case_status") or "N/A",
            "resolution": record.get("resolution") or "N/A",
        }
        debug(
            f"Parsed closed message {index}/{len(records)}: id={record.get('id')} "
            f"case={case_fields['case_number']} status={case_fields['case_status']} "
            f"resolution_chars={len(case_fields['resolution'])}"
        )

        update, reason = case_update_from_fields(case_fields)
        if not update:
            debug(f"Skip message id={record.get('id')}: {reason}")
            continue
        if update["case_number"] in updates:
            debug(f"Skip duplicate closed case {update['case_number']}: newest message already queued")
            continue
        try:
            lookup_name = lookup_name_from_case_number(update["case_number"])
        except ValueError as e:
            debug(f"Skip case {update['case_number']}: {e}")
            continue
        updates[update["case_number"]] = update
        debug(
            f"Queued update: TicketNumber={update['case_number']} lookup={lookup_name} "
            f"Remark chars={len(update['resolution'])} Matrix={update['matrix']}"
        )

    if not updates:
        return 0

    session = req.Session()
    by_lookup: dict[str, dict[str, dict[str, str]]] = {}
    for update in updates.values():
        lookup_name = lookup_name_from_case_number(update["case_number"])
        by_lookup.setdefault(lookup_name, {})[update["case_number"]] = {
            "resolution": update["resolution"],
            "matrix": update["matrix"],
        }

    total_rows = 0
    for lookup_name, case_updates in by_lookup.items():
        total_rows += _splunk_update_lookup_cases(session, settings, lookup_name, case_updates)

    return total_rows


def update_splunk_actionable_from_records(records: list[dict], config: dict) -> int:
    if not records:
        return 0

    mode = update_mode_from_config(config, "actionable_mode")
    req = require_requests()
    settings = _required_splunk_config(config)
    if not settings["verify_tls"]:
        req.packages.urllib3.disable_warnings()

    by_lookup: dict[str, dict[str, str]] = {}
    for index, record in enumerate(records, start=1):
        case_number = str(record.get("case_number") or "").strip()
        if not case_number or case_number in {"N/A", "unrelated"}:
            debug(f"Skip actionable message id={record.get('id')}: no case number")
            continue
        flag = str(record.get("actionable") or "").strip()
        if flag not in {"Yes", "No"}:
            debug(f"Skip actionable message id={record.get('id')}: invalid actionable={flag!r}")
            continue
        try:
            lookup_name = lookup_name_from_case_number(case_number)
        except ValueError as e:
            debug(f"Skip actionable case {case_number}: {e}")
            continue
        by_lookup.setdefault(lookup_name, {})[case_number] = flag
        debug(
            f"Queued actionable={flag}: {index}/{len(records)} TicketNumber={case_number} "
            f"lookup={lookup_name} id={record.get('id')}"
        )

    if not by_lookup:
        return 0

    debug(
        f"Connecting to Splunk REST for actionable updates across {len(by_lookup)} lookup(s) "
        f"(actionable_mode={mode})"
    )
    session = req.Session()
    total_rows = 0
    for lookup_name, case_updates in by_lookup.items():
        total_rows += _splunk_update_lookup_actionable(
            session, settings, lookup_name, case_updates, mode=mode
        )

    return total_rows


def update_splunk_from_folder(host: str, email: str, password: str, folder_path: str, limit: int, config: dict) -> None:
    debug("Starting update-splunk")
    debug(f"Mail host={host} folder_path={folder_path} limit={limit}")

    settings = _required_splunk_config(config)
    debug(
        "Splunk target "
        f"rest_url={settings['rest_url']} app={settings['app']} owner={settings['owner']} "
        f"verify_tls={settings['verify_tls']}"
    )

    token = zimbra_soap_login(host, email, password)
    folder = zimbra_resolve_folder_path(host, token, folder_path)
    folder_id = folder["id"]
    folder_label = f"{folder['name']} ({folder['abs_path']})" if folder else f"id={folder_id}"
    debug(f"Zimbra folder resolved: {folder_label}")

    closed_records = scan_closed_folder_records(host, token, folder_id, limit)
    debug(f"Zimbra closed scan complete: records={len(closed_records)}")
    if not closed_records:
        print("[-] No closed messages found in this folder.")
        return

    update_splunk_from_records(closed_records, config)


def run_self_test() -> None:
    body = """####
Case Status: Closed
Resolution: First line
Second line "quoted"
####"""
    fields = parse_case_fields("Case Number: 1234567890", body)
    update, reason = case_update_from_fields(fields)
    assert reason == "queued"
    assert update == {
        "case_number": "1234567890",
        "resolution": 'First line\nSecond line "quoted"',
        "matrix": "False Positive",
    }

    non_closed, reason = case_update_from_fields(
        {"case_number": "1234567890", "case_status": "Open", "resolution": "x"}
    )
    assert non_closed is None
    assert "not Closed" in reason

    cleaned, reason = case_update_from_fields(
        {
            "case_number": "1234567890",
            "case_status": "Closed",
            "resolution": "False positive - confirmed by SOC review",
        }
    )
    assert reason == "queued"
    assert cleaned == {
        "case_number": "1234567890",
        "resolution": "confirmed by SOC review",
        "matrix": "False Positive",
    }

    doubled, reason = case_update_from_fields(
        {
            "case_number": "1234567890",
            "case_status": "Closed",
            "resolution": "No other security events correlate. False positive.",
        }
    )
    assert reason == "queued"
    assert doubled == {
        "case_number": "1234567890",
        "resolution": "No other security events correlate.",
        "matrix": "False Positive",
    }

    true_pos, reason = case_update_from_fields(
        {
            "case_number": "500952026080101033901",
            "case_status": "Closed",
            "resolution": (
                "Network scanning attempt on Alibaba load balancer and Infra team "
                "has been informed with firewall rules set. True Positive."
            ),
        }
    )
    assert reason == "queued"
    assert true_pos == {
        "case_number": "500952026080101033901",
        "resolution": (
            "Network scanning attempt on Alibaba load balancer and Infra team "
            "has been informed with firewall rules set."
        ),
        "matrix": "True Positive",
    }
    tp_search = build_splunk_update_search(
        "G50095_Ticket_Status.csv",
        true_pos["case_number"],
        true_pos["resolution"],
        true_pos["matrix"],
    )
    assert 'Matrix=if(TicketNumber="500952026080101033901", "True Positive", Matrix)' in tp_search
    assert "True Positive." not in tp_search or 'Matrix=if' in tp_search
    assert 'Remark=if(TicketNumber="500952026080101033901"' in tp_search
    assert "firewall rules set." in tp_search
    assert re.search(
        r'Remark=if\(TicketNumber="500952026080101033901", "[^"]*True Positive',
        tp_search,
    ) is None

    only_fp, reason = case_update_from_fields(
        {"case_number": "1234567890", "case_status": "Closed", "resolution": "False Positive"}
    )
    assert only_fp is None
    assert "no resolution" in reason

    only_tp, reason = case_update_from_fields(
        {"case_number": "1234567890", "case_status": "Closed", "resolution": "True Positive"}
    )
    assert only_tp is None
    assert "no resolution" in reason

    assert lookup_name_from_case_number("500952026070510025940") == "G50095_Ticket_Status.csv"
    lookup_name = lookup_name_from_case_number(update["case_number"])
    assert lookup_name == "G12345_Ticket_Status.csv"

    expected_table = "| table TicketNumber, Severity, Status, Remark, Matrix, Actionable"
    search = build_splunk_update_search(
        lookup_name, update["case_number"], update["resolution"], update["matrix"]
    )
    assert 'inputlookup "G12345_Ticket_Status.csv"' in search
    assert 'Status=if(TicketNumber="1234567890", "Resolved", Status)' in search
    assert 'Remark=if(TicketNumber="1234567890",' in search
    assert 'Matrix=if(TicketNumber="1234567890", "False Positive", Matrix)' in search
    assert expected_table in search
    assert search.index(expected_table) < search.index("| outputlookup")
    assert "Actionable=if" not in search
    assert 'First line\\nSecond line \\"quoted\\"' in search

    actionable = build_splunk_actionable_update_search(lookup_name, {"1234567890": "Yes"})
    assert 'Actionable=if(TicketNumber="1234567890", "Yes", Actionable)' in actionable
    assert "Status=if" not in actionable
    assert "Remark=if" not in actionable
    assert "Matrix=if" not in actionable
    assert expected_table in actionable
    assert actionable.index(expected_table) < actionable.index("| outputlookup")

    actionable_no = build_splunk_actionable_update_search(lookup_name, {"1234567890": "No"})
    assert 'Actionable=if(TicketNumber="1234567890", "No", Actionable)' in actionable_no

    assert update_mode_from_config({}, "closed_mode") == "overwrite"
    assert update_mode_from_config({"closed_mode": "SKIP"}, "closed_mode") == "skip"
    assert update_mode_from_config({"actionable_mode": "overwrite"}, "actionable_mode") == "overwrite"
    try:
        update_mode_from_config({"closed_mode": "force"}, "closed_mode")
        assert False, "expected ValueError for invalid mode"
    except ValueError:
        pass

    rows = [
        {"TicketNumber": "1", "Actionable": ""},
        {"TicketNumber": "2", "Actionable": "Yes"},
    ]
    selected, skipped = select_tickets_for_update(
        {"1": "Yes", "2": "No"}, rows, "skip", actionable_already_set
    )
    assert selected == {"1": "Yes"} and skipped == ["2"]
    print("[+] Self-test passed")
