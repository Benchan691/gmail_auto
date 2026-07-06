import unittest
from unittest.mock import patch

from email_store import email_ids, merge_new_emails
from zimbra import is_closed_record, scan_closed_folder_records


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


class TestIsClosedRecord(unittest.TestCase):
    def test_closed(self):
        self.assertTrue(is_closed_record({"case_status": "Closed"}))

    def test_open(self):
        self.assertFalse(is_closed_record({"case_status": "Open"}))

    def test_null(self):
        self.assertFalse(is_closed_record({"case_status": None}))


class TestScanClosedFolderRecords(unittest.TestCase):
    def _fake_record(self, hit):
        status = "Closed" if "closed" in hit["id"] else "Open"
        return {"id": hit["id"], "case_status": status}

    def test_collects_only_closed_up_to_limit(self):
        hits = [
            {"id": "open1"},
            {"id": "closed1"},
            {"id": "open2"},
            {"id": "closed2"},
            {"id": "closed3"},
        ]

        def search(host, token, query, limit=50, offset=0):
            return hits[offset : offset + limit]

        import zimbra as zimbra_module

        with patch.object(zimbra_module, "zimbra_search", side_effect=search):
            with patch.object(zimbra_module, "message_to_record", side_effect=lambda h, t, hit: self._fake_record(hit)):
                result = scan_closed_folder_records("h", "t", "373", 2, scan_batch=10, max_scan=10)

        self.assertEqual([r["id"] for r in result], ["closed1", "closed2"])

    def test_stops_at_known_id(self):
        hits = [{"id": "closed-new"}, {"id": "closed-known"}, {"id": "closed-old"}]

        def search(host, token, query, limit=50, offset=0):
            return hits

        import zimbra as zimbra_module

        with patch.object(zimbra_module, "zimbra_search", side_effect=search):
            with patch.object(zimbra_module, "message_to_record", side_effect=lambda h, t, hit: self._fake_record(hit)):
                result = scan_closed_folder_records(
                    "h",
                    "t",
                    "373",
                    10,
                    known_ids={"closed-known"},
                    stop_at_known=True,
                    scan_batch=10,
                    max_scan=10,
                )

        self.assertEqual([r["id"] for r in result], ["closed-new"])

    def test_paginates_until_limit(self):
        batch1 = [{"id": f"open{i}"} for i in range(3)]
        batch2 = [{"id": "closed1"}, {"id": "closed2"}]
        calls = {"n": 0}

        def search(host, token, query, limit=50, offset=0):
            calls["n"] += 1
            if offset == 0:
                return batch1
            if offset == 3:
                return batch2
            return []

        import zimbra as zimbra_module

        with patch.object(zimbra_module, "zimbra_search", side_effect=search):
            with patch.object(zimbra_module, "message_to_record", side_effect=lambda h, t, hit: self._fake_record(hit)):
                result = scan_closed_folder_records("h", "t", "373", 2, scan_batch=3, max_scan=10)

        self.assertEqual(calls["n"], 2)
        self.assertEqual([r["id"] for r in result], ["closed1", "closed2"])


if __name__ == "__main__":
    unittest.main()
