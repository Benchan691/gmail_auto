import unittest
from unittest.mock import patch

from email_store import email_ids, merge_new_emails
from zimbra import (
    actionable_flag_for_record,
    has_it_support_recipient,
    is_actionable_candidate,
    is_closed_record,
    is_trustcsi_reply_subject,
    record_for_json,
    scan_closed_folder_records,
    scan_folder_records,
    sync_folder_emails,
)


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


class TestActionableMatchers(unittest.TestCase):
    def test_trustcsi_reply_subject(self):
        self.assertTrue(
            is_trustcsi_reply_subject(
                "Re: TrustCSI Security Incident Notification (Case Number: 50095)"
            )
        )
        self.assertTrue(
            is_trustcsi_reply_subject(
                "Re: Re: TrustCSI Security Incident Notification (Case Number: 50095)"
            )
        )
        self.assertFalse(
            is_trustcsi_reply_subject(
                "TrustCSI Security Incident Notification (Case Number: 50095)"
            )
        )
        self.assertFalse(is_trustcsi_reply_subject("Re: Something else"))

    def test_it_support_recipient(self):
        self.assertTrue(
            has_it_support_recipient(
                [{"name": "it support", "email": "it.support@kaitaksportspark.com.hk"}]
            )
        )
        self.assertTrue(
            has_it_support_recipient(
                [{"name": "IT Support", "email": "IT.Support@KaiTakSportsPark.com.hk"}]
            )
        )
        self.assertFalse(
            has_it_support_recipient([{"name": "Other", "email": "other@example.com"}])
        )
        self.assertFalse(has_it_support_recipient([]))

    def test_actionable_candidate_non_closed_only(self):
        base = {
            "case_number": "500952026070510025940",
            "case_status": "Open",
            "subject": "Re: TrustCSI Security Incident Notification (Case Number: 50095)",
            "to": [{"name": "it support", "email": "it.support@kaitaksportspark.com.hk"}],
        }
        self.assertTrue(is_actionable_candidate(base))
        self.assertEqual(actionable_flag_for_record(base), "Yes")
        self.assertEqual(actionable_flag_for_record({**base, "to": []}), "No")
        self.assertEqual(
            actionable_flag_for_record(
                {**base, "to": [{"name": "Other", "email": "other@example.com"}]}
            ),
            "No",
        )
        self.assertIsNone(actionable_flag_for_record({**base, "case_status": "Closed"}))
        self.assertIsNone(actionable_flag_for_record({**base, "case_number": None}))
        self.assertFalse(is_actionable_candidate({**base, "case_status": "Closed"}))
        self.assertFalse(is_actionable_candidate({**base, "case_number": None}))

    def test_record_for_json_strips_to(self):
        record = {
            "id": "1",
            "subject": "s",
            "case_number": "1",
            "case_status": "Closed",
            "resolution": "r",
            "to": [{"email": "x@y.com"}],
        }
        self.assertEqual(
            record_for_json(record),
            {
                "id": "1",
                "subject": "s",
                "case_number": "1",
                "case_status": "Closed",
                "resolution": "r",
            },
        )


class TestScanClosedFolderRecords(unittest.TestCase):
    def _fake_record(self, hit):
        status = "Closed" if "closed" in hit["id"] else "Open"
        record = {"id": hit["id"], "case_status": status, "case_number": "500952026070510025940"}
        if hit.get("actionable"):
            record["subject"] = "Re: TrustCSI Security Incident Notification (Case Number: 50095)"
            record["case_number"] = hit.get("case_number", "500952026070510025940")
            if hit.get("actionable") == "No":
                record["to"] = []
            else:
                record["to"] = [{"name": "it support", "email": "it.support@kaitaksportspark.com.hk"}]
        else:
            record["subject"] = "Other"
            record["to"] = []
        return record

    def test_keeps_closed_within_total_message_limit(self):
        # limit=4 examines open1, closed1, open2, closed2 — keeps 2 closed (not closed3)
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
                result = scan_closed_folder_records("h", "t", "373", 4, scan_batch=10)

        self.assertEqual([r["id"] for r in result], ["closed1", "closed2"])

    def test_collects_actionable_yes_and_no_in_same_pass(self):
        hits = [
            {"id": "open-yes", "actionable": "Yes", "case_number": "500952026070510025941"},
            {"id": "open-no", "actionable": "No", "case_number": "500952026070510025942"},
            {"id": "closed1"},
            {"id": "open2"},
        ]

        def search(host, token, query, limit=50, offset=0):
            return hits[offset : offset + limit]

        import zimbra as zimbra_module

        with patch.object(zimbra_module, "zimbra_search", side_effect=search):
            with patch.object(zimbra_module, "message_to_record", side_effect=lambda h, t, hit: self._fake_record(hit)):
                closed, actionable = scan_folder_records("h", "t", "373", 4, scan_batch=10)

        self.assertEqual([r["id"] for r in closed], ["closed1"])
        self.assertEqual(
            [(r["id"], r["actionable"]) for r in actionable],
            [("open-yes", "Yes"), ("open-no", "No")],
        )

    def test_stops_at_known_id(self):
        hits = [{"id": "closed-new"}, {"id": "closed-known"}, {"id": "closed-old"}]

        def search(host, token, query, limit=50, offset=0):
            return hits[offset : offset + limit]

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
                )

        self.assertEqual([r["id"] for r in result], ["closed-new"])

    def test_paginates_until_total_message_limit(self):
        batch1 = [{"id": f"open{i}"} for i in range(3)]
        batch2 = [{"id": "closed1"}, {"id": "closed2"}]
        calls = {"n": 0}

        def search(host, token, query, limit=50, offset=0):
            calls["n"] += 1
            if offset == 0:
                return batch1[:limit]
            if offset == 3:
                return batch2[:limit]
            return []

        import zimbra as zimbra_module

        with patch.object(zimbra_module, "zimbra_search", side_effect=search):
            with patch.object(zimbra_module, "message_to_record", side_effect=lambda h, t, hit: self._fake_record(hit)):
                # total 5 messages: 3 open + closed1 + closed2
                result = scan_closed_folder_records("h", "t", "373", 5, scan_batch=3)

        self.assertEqual(calls["n"], 2)
        self.assertEqual([r["id"] for r in result], ["closed1", "closed2"])


class TestSyncFolderEmails(unittest.TestCase):
    def _record(self, record_id: str) -> dict:
        return {
            "id": record_id,
            "subject": f"Case {record_id}",
            "case_number": "500952026070510025940",
            "case_status": "Closed",
            "resolution": "Resolved by test",
        }

    def _actionable(self, record_id: str, flag: str = "Yes") -> dict:
        record = {
            "id": record_id,
            "subject": "Re: TrustCSI Security Incident Notification (Case Number: 50095)",
            "case_number": "500952026070510025941",
            "case_status": "Open",
            "resolution": None,
            "actionable": flag,
            "to": [{"name": "it support", "email": "it.support@kaitaksportspark.com.hk"}],
        }
        if flag == "No":
            record["to"] = []
        return record

    @patch("splunk_lookup.update_splunk_actionable_from_records")
    @patch("splunk_lookup.update_splunk_from_records")
    @patch("zimbra.save_new_closed_records")
    @patch("zimbra.collect_sync_records")
    @patch("zimbra.zimbra_resolve_folder_path")
    @patch("zimbra.zimbra_soap_login")
    def test_sync_saves_and_updates_splunk_for_new_records(
        self, mock_login, mock_resolve, mock_collect, mock_save, mock_splunk, mock_actionable
    ):
        new_records = [self._record("new-1"), self._record("new-2")]
        mock_login.return_value = "token"
        mock_resolve.return_value = {"id": "373", "name": "Inbox", "abs_path": "/Inbox"}
        mock_collect.return_value = (new_records, [])
        mock_save.return_value = 2
        mock_splunk.return_value = 2
        mock_actionable.return_value = 0

        sync_folder_emails("host", "user@example.com", "pass", "373", 10, "output", {})

        mock_collect.assert_called_once_with("host", "token", "373", "output", 10)
        mock_save.assert_called_once_with("output", new_records, 10)
        mock_splunk.assert_called_once_with(new_records, {})
        mock_actionable.assert_not_called()

    @patch("splunk_lookup.update_splunk_actionable_from_records")
    @patch("splunk_lookup.update_splunk_from_records")
    @patch("zimbra.save_new_closed_records")
    @patch("zimbra.collect_sync_records")
    @patch("zimbra.zimbra_resolve_folder_path")
    @patch("zimbra.zimbra_soap_login")
    def test_sync_updates_actionable_without_closed(
        self, mock_login, mock_resolve, mock_collect, mock_save, mock_splunk, mock_actionable
    ):
        actionable = [self._actionable("open-1")]
        mock_login.return_value = "token"
        mock_resolve.return_value = {"id": "373", "name": "Inbox", "abs_path": "/Inbox"}
        mock_collect.return_value = ([], actionable)
        mock_actionable.return_value = 1

        sync_folder_emails("host", "user@example.com", "pass", "373", 10, "output", {})

        mock_save.assert_not_called()
        mock_splunk.assert_not_called()
        mock_actionable.assert_called_once_with(actionable, {})

    @patch("splunk_lookup.update_splunk_actionable_from_records")
    @patch("splunk_lookup.update_splunk_from_records")
    @patch("zimbra.save_new_closed_records")
    @patch("zimbra.collect_sync_records")
    @patch("zimbra.zimbra_resolve_folder_path")
    @patch("zimbra.zimbra_soap_login")
    def test_sync_skips_save_and_splunk_when_no_new_records(
        self, mock_login, mock_resolve, mock_collect, mock_save, mock_splunk, mock_actionable
    ):
        mock_login.return_value = "token"
        mock_resolve.return_value = {"id": "373", "name": "Inbox", "abs_path": "/Inbox"}
        mock_collect.return_value = ([], [])

        sync_folder_emails("host", "user@example.com", "pass", "373", 10, "output", {})

        mock_save.assert_not_called()
        mock_splunk.assert_not_called()
        mock_actionable.assert_not_called()


if __name__ == "__main__":
    unittest.main()
