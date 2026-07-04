import imaplib
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from case_parser import parse_case_fields, plain_text_body
from common import require_requests


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
        if _local_name(elem.tag) != "folder":
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
            if _local_name(child.tag) == "folder":
                walk(child, folder_id)

    for child in root.iter():
        if _local_name(child.tag) == "folder" and child.get("id"):
            if not any(f["id"] == child.get("id") for f in folders):
                walk(child)
    return folders


def zimbra_search(host: str, auth_token: str, query: str, limit: int = 50) -> list[dict]:
    root = zimbra_soap_request(
        host,
        auth_token,
        f"""<SearchRequest xmlns="urn:zimbraMail" types="message" sortBy="dateDesc" limit="{limit}">
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


def _safe_filename(value: str, max_len: int = 80) -> str:
    cleaned = "".join(c if c.isalnum() or c in "._- " else "_" for c in value).strip()
    return (cleaned or "email")[:max_len]


def extract_folder_emails(host: str, email: str, password: str, folder_id: str, limit: int, output_dir: str = "output") -> None:
    token = zimbra_soap_login(host, email, password)
    folder = zimbra_get_folder(host, token, folder_id)
    folder_label = f"{folder['name']} ({folder['abs_path']})" if folder else f"id={folder_id}"
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    print(f"\n[*] Extracting last {limit} email(s) from folder id={folder_id}")
    print(f"    folder: {folder_label}")
    print(f"    output: {out_path.resolve()}")

    hits = zimbra_search(host, token, f"inid:{folder_id}", limit=limit)
    if not hits:
        print("[-] No messages found in this folder.")
        return

    extracted = []
    for index, hit in enumerate(hits, start=1):
        details = zimbra_get_message(host, token, hit["id"], include_body=True) or hit
        subject = details.get("subject", hit.get("subject", ""))
        case_fields = parse_case_fields(subject, details.get("body", ""))
        record = {
            "index": index,
            "id": details.get("id", hit["id"]),
            "folder_id": details.get("folder_id", hit.get("folder_id", folder_id)),
            "date": details.get("date", format_date(hit.get("date", ""))),
            "from": details.get("sender", ""),
            "from_email": details.get("sender_email", ""),
            "to": details.get("to", []),
            "cc": details.get("cc", []),
            "subject": subject,
            "case_number": case_fields["case_number"],
            "case_status": case_fields["case_status"],
            "resolution": case_fields["resolution"],
            "body": details.get("body", ""),
        }
        extracted.append(record)

        filename = f"{index:02d}_{_safe_filename(record['date'])}_{_safe_filename(record['subject'])}.txt"
        file_path = out_path / filename
        file_path.write_text(record["body"] or "", encoding="utf-8")

        print(f"\n{index}. saved {file_path.name}")
        print(f"   id={record['id']}  date={record['date']}")
        print(f"   from:    {record['from']}")
        print(f"   subject: {record['subject']}")
        print(f"   case:    {record['case_number']} | status={record['case_status']}")
        if record["resolution"] not in ("N/A", "unrelated"):
            print(f"   resolution: {record['resolution'][:120]}")

    summary_path = out_path / "emails.json"
    summary_path.write_text(json.dumps(extracted, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[+] Extracted {len(extracted)} email(s)")
    print(f"[+] Summary: {summary_path.resolve()}")


def list_folder_emails(host: str, email: str, password: str, folder_id: str, limit: int) -> None:
    token = zimbra_soap_login(host, email, password)
    folder = zimbra_get_folder(host, token, folder_id)
    folder_label = f"{folder['name']} ({folder['abs_path']})" if folder else f"id={folder_id}"
    print(f"\n[*] Last {limit} email(s) in folder id={folder_id} ({folder_label}), newest first")

    hits = zimbra_search(host, token, f"inid:{folder_id}", limit=limit)
    if not hits:
        print("[-] No messages found in this folder.")
        if folder and folder.get("name") == "USER_ROOT":
            print("    (folder id=1 is the mailbox root; emails live in subfolders like Inbox=2, Sent=5)")
            message_folders = [f for f in zimbra_list_folders(host, token) if f.get("view") == "message" and f.get("id") != "1"]
            if message_folders:
                print("\n    Message folders you can set in config.json folder_id:")
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
