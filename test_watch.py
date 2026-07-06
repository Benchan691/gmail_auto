import unittest

from email_store import email_ids, merge_new_emails
from zimbra import fetch_new_folder_records


class TestEmailStore(unittest.TestCase):
    def test_email_ids(self):
        records = [{"id": "a", "subject": "x"}, {"id": None, "subject": "y"}, {"subject": "z"}]
        self.assertEqual(email_ids(records), {"a"})

    def test_merge_new_emails_prepends_and_caps(self):
        existing = [{"id": "2"}, {"id": "3"}]
        new_records = [{"id": "1"}]
        merged = merge_new_emails(existing, new_records, limit=2)
        self.assertEqual([r["id"] for r in merged], ["1", "2"])

    def test_merge_new_emails_dedupes(self):
        existing = [{"id": "1"}, {"id": "2"}]
        new_records = [{"id": "1"}]
        merged = merge_new_emails(existing, new_records, limit=10)
        self.assertEqual([r["id"] for r in merged], ["1", "2"])


class TestFetchNewFolderRecords(unittest.TestCase):
    def test_stops_at_known_id(self):
        hits = [{"id": "new1"}, {"id": "known"}, {"id": "old"}]
        known_ids = {"known", "old"}

        def fake_record(host, token, hit):
            return {"id": hit["id"]}

        original = None
        import zimbra as zimbra_module

        original = zimbra_module.message_to_record
        zimbra_module.message_to_record = fake_record
        try:
            result = fetch_new_folder_records("h", "t", hits, known_ids, limit=10)
        finally:
            zimbra_module.message_to_record = original

        self.assertEqual([r["id"] for r in result], ["new1"])

    def test_stops_at_limit(self):
        hits = [{"id": f"new{i}"} for i in range(5)]
        known_ids = set()

        import zimbra as zimbra_module

        original = zimbra_module.message_to_record
        zimbra_module.message_to_record = lambda host, token, hit: {"id": hit["id"]}
        try:
            result = fetch_new_folder_records("h", "t", hits, known_ids, limit=2)
        finally:
            zimbra_module.message_to_record = original

        self.assertEqual([r["id"] for r in result], ["new0", "new1"])


if __name__ == "__main__":
    unittest.main()
