from __future__ import annotations

import json
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


def _splunk_literal(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


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


def case_update_from_fields(case_fields: dict) -> tuple[dict | None, str]:
    case_number = str(case_fields.get("case_number") or "").strip()
    case_status = str(case_fields.get("case_status") or "").strip()
    resolution = str(case_fields.get("resolution") or "").strip()

    if not case_number or case_number in {"N/A", "unrelated"}:
        return None, "no case number"
    if case_status.lower() != "closed":
        return None, f"status is {case_status or 'empty'}, not Closed"
    if not resolution or resolution in {"N/A", "unrelated"}:
        return None, "closed case has no resolution"

    return {"case_number": case_number, "resolution": resolution}, "queued"


def build_splunk_count_search(lookup_name: str, case_number: str) -> str:
    return (
        f"| inputlookup {_splunk_literal(lookup_name)} "
        f"| search TicketNumber={_splunk_literal(case_number)} "
        "| stats count as count"
    )


def build_splunk_update_search(lookup_name: str, case_number: str, resolution: str) -> str:
    ticket = _splunk_literal(case_number)
    # ponytail: whole-lookup writes are fine for manual runs; add locking if this becomes a poller.
    return "\n".join(
        [
            f"| inputlookup {_splunk_literal(lookup_name)}",
            f"| eval Status=if(TicketNumber={ticket}, {_splunk_literal('Resolved')}, Status)",
            f"| eval Matrix=if(TicketNumber={ticket}, {_splunk_literal('False Positive')}, Matrix)",
            f"| eval Actionable=if(TicketNumber={ticket}, {_splunk_literal(resolution)}, Actionable)",
            f"| outputlookup {_splunk_literal(lookup_name)}",
        ]
    )


def _splunk_update_case(session, settings: dict, update: dict) -> int:
    case_number = update["case_number"]
    try:
        lookup_name = lookup_name_from_case_number(case_number)
    except ValueError as e:
        print(f"[-] Skip TicketNumber={case_number}: {e}")
        return 0

    rows = _splunk_run_search(
        session,
        settings,
        build_splunk_count_search(lookup_name, case_number),
        f"count {case_number}",
        want_results=True,
    )
    match_count = int((rows[0] if rows else {}).get("count") or 0)
    debug(f"Splunk lookup match count: lookup={lookup_name} TicketNumber={case_number} rows={match_count}")
    if match_count == 0:
        print(f"[-] No lookup row matched TicketNumber={case_number} in {lookup_name}; skipped")
        return 0

    _splunk_run_search(
        session,
        settings,
        build_splunk_update_search(lookup_name, case_number, update["resolution"]),
        f"update {case_number}",
        want_results=False,
    )
    print(f"[+] Updated TicketNumber={case_number} in {lookup_name}: rows={match_count}")
    return match_count


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
            f"Actionable chars={len(update['resolution'])}"
        )

    if not updates:
        print("[-] No closed cases with usable resolutions found.")
        return 0

    debug(f"Connecting to Splunk REST for {len(updates)} queued case update(s)")
    session = req.Session()
    total_rows = 0
    for update in updates.values():
        total_rows += _splunk_update_case(session, settings, update)

    print(f"[+] Done. Cases queued={len(updates)} lookup rows updated={total_rows}")
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
    }

    non_closed, reason = case_update_from_fields(
        {"case_number": "1234567890", "case_status": "Open", "resolution": "x"}
    )
    assert non_closed is None
    assert "not Closed" in reason

    assert lookup_name_from_case_number("500952026070510025940") == "G50095_Ticket_Status.csv"
    lookup_name = lookup_name_from_case_number(update["case_number"])
    assert lookup_name == "G12345_Ticket_Status.csv"

    search = build_splunk_update_search(lookup_name, update["case_number"], update["resolution"])
    assert 'inputlookup "G12345_Ticket_Status.csv"' in search
    assert 'Status=if(TicketNumber="1234567890", "Resolved", Status)' in search
    assert 'Matrix=if(TicketNumber="1234567890", "False Positive", Matrix)' in search
    assert 'First line\\nSecond line \\"quoted\\"' in search
    print("[+] Self-test passed")
