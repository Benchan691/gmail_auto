import argparse

from common import CONFIG_PATH, load_config
from splunk_lookup import reorder_splunk_lookup, run_self_test, update_splunk_from_folder
from zimbra import (
    find_cust_g50095,
    list_folder_emails,
    sync_folder_emails,
    test_imap_login,
    watch_folder_emails,
    zimbra_get_info,
    zimbra_soap_login,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--method",
        choices=["imap", "soap", "both", "find", "list", "watch", "sync", "update-splunk", "reorder-splunk"],
        default="both",
        help="Login/test, find, list/watch/sync emails, update/reorder Splunk lookup",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run local parser/SPL self-checks and exit",
    )
    parser.add_argument("--config", default=CONFIG_PATH, help="Path to config.json")
    parser.add_argument("--folder-path", type=str, help="Folder path to read emails from (overrides config folder_path)")
    parser.add_argument("--limit", type=int, help="Number of recent emails to list (overrides config limit)")
    parser.add_argument("--lookup-name", type=str, help="Splunk lookup CSV filename for --method reorder-splunk")

    parser.add_argument("--output", default="output", help="Output directory for --method sync or watch")

    args = parser.parse_args()
    if args.self_test:
        run_self_test()
        return

    config = load_config(args.config)
    host = config["host"]
    email = config["email"]
    password = config["password"]
    folder_path = str(args.folder_path if args.folder_path is not None else config.get("folder_path", "Inbox"))
    limit = args.limit if args.limit is not None else int(config.get("limit", 10))

    if args.method in ["imap", "both"]:
        try:
            test_imap_login(host, email, password)
        except Exception as e:
            print("[-] IMAP login failed:", e)

    if args.method == "find":
        try:
            find_cust_g50095(host, email, password)
        except Exception as e:
            print("[-] Search failed:", e)
        return

    if args.method == "list":
        try:
            list_folder_emails(host, email, password, folder_path, limit)
        except Exception as e:
            print("[-] List failed:", e)
        return

    if args.method == "watch":
        try:
            watch_folder_emails(host, email, password, folder_path, limit, args.output)
        except Exception as e:
            print("[-] Watch failed:", e)
        return

    if args.method == "sync":
        try:
            sync_folder_emails(host, email, password, folder_path, limit, args.output, config)
        except Exception as e:
            print("[-] Sync failed:", e)
        return

    if args.method == "update-splunk":
        try:
            update_splunk_from_folder(host, email, password, folder_path, limit, config)
        except Exception as e:
            print("[-] Splunk update failed:", e)
        return

    if args.method == "reorder-splunk":
        if not args.lookup_name:
            print("[-] --lookup-name is required for --method reorder-splunk (e.g. G50095_Ticket_Status.csv)")
            return
        try:
            reorder_splunk_lookup(args.lookup_name, config)
        except Exception as e:
            print("[-] Splunk reorder failed:", e)
        return

    if args.method in ["soap", "both"]:
        try:
            token = zimbra_soap_login(host, email, password)
            zimbra_get_info(host, token)
        except Exception as e:
            print("[-] SOAP login failed:", e)


if __name__ == "__main__":
    main()
