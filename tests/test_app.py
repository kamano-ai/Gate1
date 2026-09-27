import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
        app.initialize(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def state(self, id=1, user=1):
        return app.detail(self.path, id, user)

    def change(self, operation, user=1, id=1, **values):
        values.setdefault("updated_at", self.state(id)["inquiry"]["updated_at"])
        return app.update(self.path, id, user, operation, values)

    def sql(self, query, parameters=()):
        db = app.connect(self.path)
        try:
            with db:
                return db.execute(query, parameters).fetchall()
        finally:
            db.close()

    def assert_rule(self, key, call):
        with self.assertRaises(app.RuleError) as result:
            call()
        self.assertEqual(result.exception.message, app.MESSAGES[key])

    def test_all_16_transition_pairs(self):
        allowed = {("NEW", "IN_PROGRESS"), ("IN_PROGRESS", "PENDING"),
                   ("IN_PROGRESS", "DONE"), ("PENDING", "IN_PROGRESS"), ("DONE", "IN_PROGRESS")}
        for before in ("NEW", "IN_PROGRESS", "PENDING", "DONE"):
            for after in ("NEW", "IN_PROGRESS", "PENDING", "DONE"):
                with self.subTest(before=before, after=after):
                    self.sql("UPDATE inquiries SET status=?,assignee_id=1,closed_at=?,updated_at='2026-07-29 10:15:00.000000' WHERE id=1",
                             (before, "2026-07-29 11:00:00.000000" if before == "DONE" else None))
                    self.sql("DELETE FROM inquiry_histories")
                    old = self.state()
                    if (before, after) in allowed:
                        result = self.change("status", status=after)
                        new = self.state()
                        self.assertEqual(new["inquiry"]["status"], after)
                        self.assertEqual(len(new["history"]), 1)
                        self.assertEqual(result["message"], f"ステータスを「{app.STATUS[after]}」に変更しました。")
                        self.assertNotEqual(old["inquiry"]["updated_at"], new["inquiry"]["updated_at"])
                        if after == "DONE":
                            self.assertEqual(new["inquiry"]["closed_at"], new["inquiry"]["updated_at"])
                        if before == "DONE":
                            self.assertIsNone(new["inquiry"]["closed_at"])
                    else:
                        self.assert_rule("transition", lambda: self.change("status", status=after))
                        self.assertEqual(old, self.state())

    def test_auto_assignment_and_two_histories(self):
        self.change("status", user=2, status="IN_PROGRESS")
        state = self.state()
        self.assertEqual(state["inquiry"]["assignee_id"], 2)
        self.assertEqual({h["field_name"] for h in state["history"]}, {"status", "assignee"})
        self.assertEqual(len(state["history"]), 2)
        self.assertTrue(all(h["changed_by"] == 2 for h in state["history"]))

    def test_existing_assignee_preserved(self):
        self.change("status", id=5, user=1, status="IN_PROGRESS")
        self.assertEqual(self.state(5)["inquiry"]["assignee_id"], 2)
        self.assertEqual(len(self.state(5)["history"]), 1)

    def test_member_assignment_matrix(self):
        for target in (None, 1, 3, 4):
            self.assert_rule("permission", lambda: self.change("assignee", user=2, assignee_id=target))
        self.change("assignee", user=2, assignee_id=2)
        self.assertEqual(self.state()["inquiry"]["assignee_id"], 2)
        for id in (1, 2, 3, 5):
            self.assert_rule("permission", lambda: self.change("assignee", user=2, id=id, assignee_id=2))

    def test_admin_and_null_rules(self):
        for id in (1, 2, 3, 5):
            self.change("assignee", id=id, assignee_id=3)
            self.assertEqual(self.state(id)["inquiry"]["assignee_id"], 3)
        self.assertEqual(self.change("assignee", assignee_id=None)["message"], "担当者を未割り当てに戻しました。")
        self.assertIsNone(self.state()["inquiry"]["assignee_id"])
        for id in (2, 3):
            self.assert_rule("permission", lambda: self.change("assignee", id=id, assignee_id=None))

    def test_done_assignment_rejected_for_every_role(self):
        for user in (1, 2):
            for target in (None, 1, 2, 3):
                self.assert_rule("done", lambda: self.change("assignee", user=user, id=4, assignee_id=target))

    def test_invalid_assignees_and_sorting(self):
        for target in (4, 999, "2", True, [2]):
            self.assert_rule("user", lambda: self.change("assignee", assignee_id=target))
        self.assertEqual([u["name"] for u in self.state()["users"]], ["佐藤 花子", "鈴木 一郎", "田中 美咲"])

    def test_comment_boundary(self):
        for value in ("あ" * 201, "😀" * 201):
            self.assert_rule("comment", lambda: self.change("status", status="IN_PROGRESS", comment=value))
            self.assertEqual(self.state()["inquiry"]["status"], "NEW")
        self.change("status", status="IN_PROGRESS", comment="😀" * 200)

    def test_conflict_for_both_operations(self):
        old = self.state()["inquiry"]["updated_at"]
        self.change("assignee", assignee_id=2)
        for operation, payload in (("status", {"status": "IN_PROGRESS"}), ("assignee", {"assignee_id": 3})):
            before = self.state()
            self.assert_rule("conflict", lambda: self.change(operation, updated_at=old, **payload))
            self.assertEqual(before, self.state())

    def test_history_failure_rolls_back_status_and_auto_assignment(self):
        # Fail the second history insert so the first must also be undone.
        self.sql("""CREATE TRIGGER fail_history BEFORE INSERT ON inquiry_histories
            WHEN NEW.field_name='assignee' BEGIN SELECT RAISE(ABORT, 'intentional test failure'); END""")
        before = self.state()
        with self.assertRaises(sqlite3.IntegrityError):
            self.change("status", status="IN_PROGRESS")
        self.assertEqual(before, self.state())

    def test_history_failure_rolls_back_assignment(self):
        self.sql("""CREATE TRIGGER fail_history BEFORE INSERT ON inquiry_histories
            BEGIN SELECT RAISE(ABORT, 'intentional test failure'); END""")
        before = self.state()
        with self.assertRaises(sqlite3.IntegrityError):
            self.change("assignee", assignee_id=2)
        self.assertEqual(before, self.state())

    def test_history_values_limit_and_order(self):
        self.assertEqual(self.change("assignee", assignee_id=1)["message"], "担当者を「佐藤 花子」に変更しました。")
        history = self.state()["history"][0]
        self.assertEqual((history["old_value"], history["new_value"]), (None, "1"))
        self.assertEqual((history["old_label"], history["new_label"]), ("未割り当て", "佐藤 花子"))
        for index in range(23):
            self.change("assignee", assignee_id=2 if index % 2 == 0 else 3)
        histories = self.state()["history"]
        self.assertEqual(len(histories), 20)
        self.assertEqual([h["id"] for h in histories], list(range(24, 4, -1)))

    def test_missing_and_inactive_actor(self):
        self.assert_rule("missing", lambda: app.detail(self.path, 999, 1))
        self.assert_rule("missing", lambda: app.update(self.path, 999, 1, "status", {}))
        self.assert_rule("permission", lambda: self.change("status", user=4, status="IN_PROGRESS"))

    def test_noop_does_not_create_change_history(self):
        before = self.state(5)
        self.change("assignee", id=5, assignee_id=2)
        self.assertEqual(before, self.state(5))

    def test_http_rejects_spoofed_identity(self):
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.handler(self.path, 2))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            root = f"http://127.0.0.1:{server.server_port}"
            with urlopen(root + "/api/inquiries/1") as response:
                state = json.load(response)
            self.assertEqual(state["actor"]["id"], 2)
            data = json.dumps({"assignee_id": 3, "actor_id": 1, "role": "ADMIN", "updated_at": state["inquiry"]["updated_at"]}).encode()
            request = Request(root + "/api/inquiries/1/assignee", data=data, headers={"Content-Type": "application/json"})
            with self.assertRaises(HTTPError) as error:
                urlopen(request)
            self.assertEqual(error.exception.code, 403)
            self.assertEqual(json.load(error.exception)["message"], "この操作を行う権限がありません。")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main(verbosity=2)
