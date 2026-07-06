import imaplib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from case_parser import case_fields_for_json, parse_case_fields, plain_text_body
from common import require_requests
from email_store import email_ids, emails_path, load_emails, merge_new_emails, save_emails


def test_imap_login(host: str, email: str, password: str) -> None:
    print(f"[*] Connecting to IMAP SSL: {host}:993")

    with imaplib.IMAP4_SSL(host, 993) as mail:
        mail.login(email, password)
        print("[+] IMAP login successful")

        status, folders = mail.list()
        if status == "OK":
            print("\n[+] Mail folders:")
            for folder in folders[:20]:
                print("   ", folder.decode(errors="ignore"))

        mail.logout()


def zimbra_soap_login(host: str, email: str, password: str) -> str:
    req = require_requests()
    soap_url = f"https://{host}/service/soap"

    xml_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope">
  <soap:Header>
    <context xmlns="urn:zimbra">
      <userAgent name="python-zimbra-login" version="1.0"/>
    </context>
  </soap:Header>
  <soap:Body>
    <AuthRequest xmlns="urn:zimbraAccount">
      <account by="name">{email}</account>
      <password>{password}</password>
    </AuthRequest>
  </soap:Body>
</soap:Envelope>
"""

    print(f"[*] Connecting to Zimbra SOAP API: {soap_url}")
    response = req.post(
        soap_url,
        data=xml_body.encode("utf-8"),
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
        timeout=30,
    )
    print("[*] HTTP status:", response.status_code)

    if response.status_code != 200:
        print(response.text[:1000])
        raise RuntimeError("SOAP login request failed")

    token = None
    for elem in ET.fromstring(response.text).iter():
        if elem.tag.endswith("authToken"):
            token = elem.text
            break

    if not token:
        print(response.text[:1500])
        raise RuntimeError("Login failed or authToken not found")

    print("[+] SOAP login successful")
    print(f"[+] Auth token received, length={len(token)}")
    return token


def zimbra_soap_request(host: str, auth_token: str, body_xml: str) -> ET.Element:
    req = require_requests()
    soap_url = f"https://{host}/service/soap"
    xml_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope">
  <soap:Header>
    <context xmlns="urn:zimbra">
      <authToken>{auth_token}</authToken>
    </context>
  </soap:Header>
  <soap:Body>
    {body_xml}
  </soap:Body>
</soap:Envelope>
"""
    response = req.post(
        soap_url,
        data=xml_body.encode("utf-8"),
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
        timeout=60,
    )
    if response.status_code != 200:
        raise RuntimeError(f"SOAP request failed: HTTP {response.status_code}\n{response.text[:1500]}")
    return ET.fromstring(response.text)


def zimbra_get_info(host: str, auth_token: str) -> None:
    req = require_requests()
    soap_url = f"https://{host}/service/soap"
    xml_body = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope">
  <soap:Header>
    <context xmlns="urn:zimbra">
      <authToken>{auth_token}</authToken>
    </context>
  </soap:Header>
  <soap:Body>
    <GetInfoRequest xmlns="urn:zimbraAccount"/>
  </soap:Body>
</soap:Envelope>
""".format(auth_token=auth_token)

    response = req.post(
        soap_url,
        data=xml_body.encode("utf-8"),
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
        timeout=30,
    )
    print("[*] GetInfo HTTP status:", response.status_code)

    if response.status_code != 200:
        print(response.text[:1000])
        return
    print("[+] Auth token works. Account info response received.")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _folder_path_by_id(folders: list[dict]) -> dict[str, str]:
    by_id = {f["id"]: f for f in folders}
    paths: dict[str, str] = {}

    def build_path(folder_id: str) -> str:
        if folder_id in paths:
            return paths[folder_id]
        folder = by_id.get(folder_id)
        if not folder:
            return ""
        name = folder.get("name", "")
        parent_id = folder.get("parent_id", "")
        if not parent_id or parent_id == folder_id:
            paths[folder_id] = name
            return name
        parent_path = build_path(parent_id)
        paths[folder_id] = f"{parent_path}/{name}" if parent_path else name
        return paths[folder_id]

    for folder_id in by_id:
        build_path(folder_id)
    return paths


def zimbra_list_folders(host: str, auth_token: str) -> list[dict]:
    root = zimbra_soap_request(
        host,
        auth_token,
        '<GetFolderRequest xmlns="urn:zimbraMail" visible="1" needGrantee="1"/>',
    )

    folders: list[dict] = []

    def walk(elem, parent_id: str = "") -> None:
        tag = _local_name(elem.tag)
        if tag not in ("folder", "link"):
            for child in elem:
                walk(child, parent_id)
            return

        folder_id = elem.get("id", "")
        folders.append(
            {
                "id": folder_id,
                "name": elem.get("name", ""),
                "parent_id": parent_id,
                "abs_path": elem.get("absFolderPath", ""),
                "owner": elem.get("owner", ""),
                "zid": elem.get("zid", ""),
                "view": elem.get("view", ""),
                "remote": elem.get("remote", ""),
            }
        )
        for child in elem:
            if _local_name(child.tag) in ("folder", "link"):
                walk(child, folder_id)

    for child in root.iter():
        if _local_name(child.tag) in ("folder", "link") and child.get("id"):
            if not any(f["id"] == child.get("id") for f in folders):
                walk(child)
    return folders


def zimbra_search(host: str, auth_token: str, query: str, limit: int = 50, offset: int = 0) -> list[dict]:
    root = zimbra_soap_request(
        host,
        auth_token,
        f"""<SearchRequest xmlns="urn:zimbraMail" types="message" sortBy="dateDesc" limit="{limit}" offset="{offset}">
  <query>{query}</query>
</SearchRequest>""",
    )

    results: list[dict] = []
    for elem in root.iter():
        if _local_name(elem.tag) == "m":
            results.append(
                {
                    "id": elem.get("id", ""),
                    "folder_id": elem.get("l", ""),
                    "subject": elem.get("su", ""),
                    "fragment": elem.get("fr", ""),
                    "date": elem.get("d", ""),
                    "sender": elem.get("e", ""),
                }
            )
    return results


def format_date(ms: str) -> str:
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return ms


def zimbra_get_folder(host: str, auth_token: str, folder_id: str):
    root = zimbra_soap_request(
        host,
        auth_token,
        f'<GetFolderRequest xmlns="urn:zimbraMail" visible="1" traverse="1"><folder l="{folder_id}"/></GetFolderRequest>',
    )
    for elem in root.iter():
        if _local_name(elem.tag) in ("folder", "link") and elem.get("id") == folder_id:
            return {
                "id": elem.get("id", ""),
                "name": elem.get("name", ""),
                "abs_path": elem.get("absFolderPath", ""),
                "owner": elem.get("owner", ""),
            }
    return None


def zimbra_resolve_folder_path(host: str, auth_token: str, folder_path: str) -> dict:
    wanted = (folder_path or "").strip().strip("/")
    if not wanted:
        raise ValueError("folder_path is required")
    if wanted.isdigit():
        folder = zimbra_get_folder(host, auth_token, wanted)
        if folder:
            return folder

    folders = zimbra_list_folders(host, auth_token)
    paths = _folder_path_by_id(folders)

    def clean(value: str) -> str:
        text = (value or "").strip().strip("/").lower()
        return text.replace("\u2019", "'").replace("\u2018", "'")

    exact_hits = [
        folder
        for folder in folders
        if clean(folder.get("abs_path", "")) == clean(wanted) or clean(paths.get(folder["id"], "")) == clean(wanted)
    ]
    if len(exact_hits) == 1:
        return exact_hits[0]
    if len(exact_hits) > 1:
        raise RuntimeError(f"Folder path '{folder_path}' matched multiple folders; use the full path")

    name_hits = [folder for folder in folders if clean(folder.get("name", "")) == clean(wanted)]
    if len(name_hits) == 1:
        return name_hits[0]
    if len(name_hits) > 1:
        choices = ", ".join(paths.get(folder["id"], folder.get("abs_path", "")) for folder in name_hits[:10])
        raise RuntimeError(f"Folder name '{folder_path}' is ambiguous; use one full path: {choices}")

    raise RuntimeError(f"Folder path not found: {folder_path}")


def _extract_message_content(elem) -> dict:
    text_body = ""
    html_body = ""
    for part in elem.iter():
        if _local_name(part.tag) != "mp":
            continue
        content_elem = next((child for child in part if _local_name(child.tag) == "content" and child.text), None)
        if content_elem is None:
            continue
        if part.get("ct", "") == "text/plain" and not text_body:
            text_body = content_elem.text
        elif part.get("ct", "") == "text/html" and not html_body:
            html_body = content_elem.text

    addresses = [
        {"type": addr.get("t", ""), "name": addr.get("p", ""), "email": addr.get("a", "")}
        for addr in elem.iter()
        if _local_name(addr.tag) == "e"
    ]

    from_name = ""
    from_email = ""
    to = []
    cc = []
    for addr in addresses:
        entry = {"name": addr["name"], "email": addr["email"]}
        if addr["type"] == "f":
            from_name = addr["name"] or addr["email"]
            from_email = addr["email"]
        elif addr["type"] == "t":
            to.append(entry)
        elif addr["type"] == "c":
            cc.append(entry)

    return {
        "text_body": text_body,
        "html_body": html_body,
        "body": text_body or html_body,
        "from_name": from_name,
        "from_email": from_email,
        "to": to,
        "cc": cc,
        "addresses": addresses,
    }


def zimbra_get_message(host: str, auth_token: str, message_id: str, include_body: bool = False):
    def load_message(html_mode: str):
        root = zimbra_soap_request(
            host,
            auth_token,
            f'<GetMsgRequest xmlns="urn:zimbraMail"><m id="{message_id}" html="{html_mode}" needExp="1"/></GetMsgRequest>',
        )
        return next((elem for elem in root.iter() if _local_name(elem.tag) == "m" and elem.get("id") == message_id), None)

    elem = load_message("0")
    if elem is None:
        return None

    subject_elem = elem.find(".//{*}su")
    fragment_elem = elem.find(".//{*}fr")
    content = _extract_message_content(elem) if include_body else {}

    if include_body:
        html_elem = load_message("1")
        if html_elem is not None:
            html_content = _extract_message_content(html_elem)
            if html_content.get("html_body"):
                content["html_body"] = html_content["html_body"]
            if html_content.get("text_body") and not content.get("text_body"):
                content["text_body"] = html_content["text_body"]
            for field in ("from_name", "from_email", "to", "cc", "addresses"):
                if not content.get(field) and html_content.get(field):
                    content[field] = html_content[field]

    return {
        "id": message_id,
        "folder_id": elem.get("l", ""),
        "date": format_date(elem.get("d", "")),
        "sender": content.get("from_name", ""),
        "sender_email": content.get("from_email", ""),
        "to": content.get("to", []),
        "cc": content.get("cc", []),
        "addresses": content.get("addresses", []),
        "subject": (subject_elem.text if subject_elem is not None else "") or "(no subject)",
        "preview": fragment_elem.text if fragment_elem is not None else "",
        "body": plain_text_body(content.get("text_body", ""), content.get("html_body", "")),
    }


def message_to_record(host: str, token: str, hit: dict) -> dict:
    details = zimbra_get_message(host, token, hit["id"], include_body=True) or hit
    subject = details.get("subject", hit.get("subject", ""))
    case_fields = parse_case_fields(subject, details.get("body", ""))
    return case_fields_for_json(
        case_fields,
        message_id=details.get("id", hit["id"]),
        subject=subject,
    )


def is_closed_record(record: dict) -> bool:
    return str(record.get("case_status") or "").lower() == "closed"


def scan_closed_folder_records(
    host: str,
    token: str,
    folder_id: str,
    limit: int,
    *,
    known_ids=None,
    stop_at_known: bool = False,
    scan_batch: int = 50,
    max_scan: int = 500,
) -> list[dict]:
    known = known_ids or set()
    closed: list[dict] = []
    seen: set[str] = set()
    offset = 0
    query = f"inid:{folder_id}"

    while offset < max_scan:
        hits = zimbra_search(host, token, query, limit=scan_batch, offset=offset)
        if not hits:
            break

        for hit in hits:
            if stop_at_known and hit["id"] in known:
                return closed

            record = message_to_record(host, token, hit)
            if not is_closed_record(record):
                continue

            record_id = record.get("id")
            if record_id and record_id in seen:
                continue
            if record_id:
                seen.add(str(record_id))

            closed.append(record)
            if not stop_at_known and len(closed) >= limit:
                return closed

        offset += len(hits)
        if len(hits) < scan_batch:
            break

    return closed


def fetch_folder_records(host: str, token: str, folder_id: str, limit: int) -> list[dict]:
    return scan_closed_folder_records(host, token, folder_id, limit)


def fetch_new_closed_folder_records(
    host: str, token: str, folder_id: str, known_ids: set[str], limit: int
) -> list[dict]:
    return scan_closed_folder_records(
        host, token, folder_id, limit, known_ids=known_ids, stop_at_known=True
    )


def _print_record(index: int, record: dict) -> None:
    print(f"\n{index}. id={record['id']}")
    print(f"   subject:   {record['subject']}")
    print(f"   case:      {record['case_number']} | status={record['case_status']}")
    if record["resolution"]:
        print(f"   resolution: {record['resolution'][:120]}")


def collect_new_closed_records(
    host: str, token: str, folder_id: str, output_dir: str, limit: int
) -> list[dict]:
    summary_path = emails_path(output_dir)
    existing = load_emails(summary_path)
    if not existing:
        return fetch_folder_records(host, token, folder_id, limit)

    known_ids = email_ids(existing)
    return fetch_new_closed_folder_records(host, token, folder_id, known_ids, limit)


def save_new_closed_records(output_dir: str, new_records: list[dict], limit: int) -> int:
    summary_path = emails_path(output_dir)
    existing = load_emails(summary_path)
    if not existing:
        save_emails(summary_path, new_records)
        return len(new_records)

    merged = merge_new_emails(existing, new_records, limit)
    save_emails(summary_path, merged)
    return len(merged)


def watch_folder_emails(host: str, email: str, password: str, folder_path: str, limit: int, output_dir: str = "output") -> None:
    token = zimbra_soap_login(host, email, password)
    folder = zimbra_resolve_folder_path(host, token, folder_path)
    folder_id = folder["id"]
    folder_label = f"{folder['name']} ({folder['abs_path']})" if folder else f"id={folder_id}"
    summary_path = emails_path(output_dir)

    print(f"\n[*] Watching folder path={folder_path} (id={folder_id}, {folder_label})")
    print(f"    closed limit: {limit}")
    print(f"    output: {summary_path.resolve()}")

    existing = load_emails(summary_path)
    if not existing:
        print("[*] No emails.json yet — fetching up to limit closed message(s)")

    new_records = collect_new_closed_records(host, token, folder_id, output_dir, limit)
    if not new_records:
        if existing:
            print("[+] 0 new closed message(s)")
        else:
            print("[-] No closed messages found in this folder.")
        return

    total = save_new_closed_records(output_dir, new_records, limit)
    if existing:
        print(f"[+] {len(new_records)} new closed message(s), {total} total in {summary_path.resolve()}")
    else:
        print(f"[+] Saved {len(new_records)} closed email(s) to {summary_path.resolve()}")

    for index, record in enumerate(new_records, start=1):
        _print_record(index, record)


def sync_folder_emails(
    host: str,
    email: str,
    password: str,
    folder_path: str,
    limit: int,
    output_dir: str,
    config: dict,
) -> None:
    from splunk_lookup import update_splunk_from_records

    token = zimbra_soap_login(host, email, password)
    folder = zimbra_resolve_folder_path(host, token, folder_path)
    folder_id = folder["id"]
    folder_label = f"{folder['name']} ({folder['abs_path']})" if folder else f"id={folder_id}"
    summary_path = emails_path(output_dir)

    print(f"\n[*] Syncing folder path={folder_path} (id={folder_id}, {folder_label})")
    print(f"    closed limit: {limit}")
    print(f"    output: {summary_path.resolve()}")

    existing = load_emails(summary_path)
    if not existing:
        print("[*] No emails.json yet — fetching up to limit closed message(s)")

    new_records = collect_new_closed_records(host, token, folder_id, output_dir, limit)
    if not new_records:
        print("[+] 0 new closed message(s)")
        return

    total = save_new_closed_records(output_dir, new_records, limit)
    for index, record in enumerate(new_records, start=1):
        _print_record(index, record)

    splunk_rows = update_splunk_from_records(new_records, config)
    print(
        f"\n[+] Sync complete: {len(new_records)} new closed message(s), "
        f"{total} total in {summary_path.resolve()}, {splunk_rows} Splunk lookup row(s) updated"
    )


def list_folder_emails(host: str, email: str, password: str, folder_path: str, limit: int) -> None:
    token = zimbra_soap_login(host, email, password)
    folder = zimbra_resolve_folder_path(host, token, folder_path)
    folder_id = folder["id"]
    folder_label = f"{folder['name']} ({folder['abs_path']})" if folder else f"id={folder_id}"
    print(f"\n[*] Last {limit} email(s) in folder path={folder_path} (id={folder_id}, {folder_label}), newest first")

    hits = zimbra_search(host, token, f"inid:{folder_id}", limit=limit)
    if not hits:
        print("[-] No messages found in this folder.")
        if folder and folder.get("name") == "USER_ROOT":
            print("    (folder id=1 is the mailbox root; emails live in subfolders like Inbox=2, Sent=5)")
            message_folders = [f for f in zimbra_list_folders(host, token) if f.get("view") == "message" and f.get("id") != "1"]
            if message_folders:
                print("\n    Message folders you can set in config.json folder_path:")
                for f in message_folders:
                    print(f"      id={f['id']:>2}  {f.get('abs_path') or f.get('name')}")
        return

    for index, hit in enumerate(hits, start=1):
        details = zimbra_get_message(host, token, hit["id"]) or hit
        print(f"\n{index}. id={details.get('id', hit['id'])}  date={details.get('date', format_date(hit.get('date', '')))}")
        print(f"   folder_id: {details.get('folder_id', hit.get('folder_id', ''))}")
        print(f"   from:      {details.get('sender', '')}")
        print(f"   subject:   {details.get('subject', hit.get('subject', ''))}")
        preview = details.get("preview") or hit.get("fragment", "")
        if preview:
            print(f"   preview:   {preview}")


def find_cust_g50095(host: str, email: str, password: str) -> None:
    token = zimbra_soap_login(host, email, password)
    print("\n[*] Listing folders (including shared/mounted)...")
    folders = zimbra_list_folders(host, token)
    paths = _folder_path_by_id(folders)

    keywords = ["cust_g50095", "kai tak sports", "jack ng"]
    print(f"\n[+] Found {len(folders)} folders. Matching names:")
    folder_hits = []
    for folder in folders:
        haystack = " ".join([folder.get("name", ""), folder.get("abs_path", ""), folder.get("owner", ""), paths.get(folder["id"], "")]).lower()
        if any(k in haystack for k in keywords):
            folder_hits.append(folder)

    if folder_hits:
        for folder in folder_hits:
            print(f"   folder id={folder['id']}")
            print(f"      name: {folder['name']}")
            print(f"      path: {paths.get(folder['id'], folder.get('abs_path', ''))}")
            if folder.get("owner"):
                print(f"      owner: {folder['owner']}")
            if folder.get("zid"):
                print(f"      shared from zid: {folder['zid']}")
    else:
        print("   (no folder name/path matched keywords)")

    search_queries = ['subject:"Cust_G50095"', "Cust_G50095", '"Kai Tak Sports"', "from:jack", "jack ng"]
    all_hits: dict[str, dict] = {}
    print("\n[*] Searching messages...")
    for query in search_queries:
        try:
            hits = zimbra_search(host, token, query, limit=30)
        except Exception as e:
            print(f"   [-] query '{query}' failed: {e}")
            continue
        print(f"   query '{query}': {len(hits)} hit(s)")
        for hit in hits:
            all_hits[hit["id"]] = hit

    if not all_hits:
        print("\n[-] No matching messages found.")
        return

    print(f"\n[+] {len(all_hits)} unique message(s) found:")
    for hit in all_hits.values():
        folder_id = hit["folder_id"]
        folder_name = paths.get(folder_id, "(unknown folder)")
        for folder in folders:
            if folder["id"] == folder_id:
                folder_name = folder.get("abs_path") or folder_name
                if folder.get("owner"):
                    folder_name = f"{folder_name} (shared by {folder['owner']})"
                break

        print(f"\n   message id: {hit['id']}")
        print(f"   folder id:  {folder_id}")
        print(f"   location:   {folder_name}")
        print(f"   subject:    {hit['subject']}")
        if hit.get("fragment"):
            print(f"   snippet:    {hit['fragment'][:200]}")
        if hit.get("date"):
            print(f"   date:       {hit['date']}")
