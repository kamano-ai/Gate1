"""Gate1: local inquiry status/assignment implementation (Python 3.10+)."""
import argparse
import json
import sqlite3
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
STATUS = {"NEW": "未対応", "IN_PROGRESS": "対応中", "PENDING": "保留", "DONE": "完了"}
TRANSITIONS = {"NEW": ("IN_PROGRESS",), "IN_PROGRESS": ("PENDING", "DONE"),
               "PENDING": ("IN_PROGRESS",), "DONE": ("IN_PROGRESS",)}
MESSAGES = {
    "transition": "このステータスへは変更できません。画面を再読み込みしてください。",
    "permission": "この操作を行う権限がありません。",
    "done": "完了済みの問い合わせは担当者を変更できません。",
    "user": "指定されたユーザーは選択できません。",
    "comment": "コメントは 200 文字以内で入力してください。",
    "missing": "指定された問い合わせは存在しません。",
    "conflict": "他のユーザーが更新しました。画面を再読み込みしてください。",
}
# Reading metadata for the existing-user fixtures; no extra database columns.
USERS = [(1, "佐藤 花子", "ADMIN", 1, "さとう はなこ"),
         (2, "鈴木 一郎", "MEMBER", 1, "すずき いちろう"),
         (3, "田中 美咲", "MEMBER", 1, "たなか みさき"),
         (4, "山田 次郎", "MEMBER", 0, "やまだ じろう")]
READINGS = {u[0]: u[4] for u in USERS}


class RuleError(Exception):
    def __init__(self, key, status=400):
        self.message, self.status = MESSAGES[key], status
        super().__init__(self.message)


def connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def initialize(path):
    db = connect(path)
    with db:
        db.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
        if not db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            db.executemany("INSERT INTO users(id,name,role,is_active) VALUES(?,?,?,?)", [u[:4] for u in USERS])
            fixtures = [
                (1, "プリンタが印刷できません", "3階の複合機で印刷しようとすると、エラーが表示されます。", "山田 太郎", "NEW", None, "MIDDLE", None),
                (2, "PCが起動しません", "電源ボタンを押しても起動しません。", "伊藤 葵", "IN_PROGRESS", 2, "HIGH", None),
                (3, "ソフトウェアのインストールについて", "利用するバージョンを確認しています。", "加藤 健", "PENDING", 3, "LOW", None),
                (4, "社内ネットワークに接続できません", "接続設定の修正が完了しました。", "渡辺 遥", "DONE", 1, "MIDDLE", "2026-07-29 11:00:00.000000"),
                (5, "共有フォルダのアクセスについて", "アクセス権の確認をお願いします。", "小林 誠", "NEW", 2, "MIDDLE", None),
            ]
            db.executemany("""INSERT INTO inquiries
                (id,title,body,requester_name,status,assignee_id,priority,closed_at,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?, '2026-07-29 10:15:00.000000', '2026-07-29 10:15:00.000000')""", fixtures)
    db.close()


def actor(db, actor_id):
    user = db.execute("SELECT * FROM users WHERE id=? AND is_active=1", (actor_id,)).fetchone()
    if user is None:
        raise RuleError("permission", 403)
    return user


def inquiry(db, inquiry_id):
    row = db.execute("SELECT * FROM inquiries WHERE id=?", (inquiry_id,)).fetchone()
    if row is None:
        raise RuleError("missing", 404)
    return row


def detail(path, inquiry_id, actor_id):
    db = connect(path)
    try:
        db.execute("BEGIN")
        current = dict(actor(db, actor_id))
        row = dict(inquiry(db, inquiry_id))
        users = [dict(u) for u in db.execute("SELECT * FROM users")]
        names = {str(u["id"]): u["name"] for u in users}
        history = [dict(h) for h in db.execute("""SELECT h.*, u.name AS changed_by_name
            FROM inquiry_histories h JOIN users u ON u.id=h.changed_by
            WHERE inquiry_id=? ORDER BY changed_at DESC,id DESC LIMIT 20""", (inquiry_id,))]
        for item in history:
            labels = STATUS if item["field_name"] == "status" else names
            item["old_label"] = labels.get(item["old_value"], "未割り当て")
            item["new_label"] = labels.get(item["new_value"], "未割り当て")
        row["assignee_name"] = names.get(str(row["assignee_id"]), "未割り当て")
        return {"inquiry": row, "actor": current, "statuses": STATUS,
                "transitions": TRANSITIONS.get(row["status"], ()), "history": history,
                "users": sorted([u for u in users if u["is_active"]], key=lambda u: READINGS.get(u["id"], u["name"]))}
    finally:
        db.close()


def update(path, inquiry_id, actor_id, operation, payload):
    db = connect(path)
    try:
        # Lock before reading; stale version checking and both writes are atomic.
        db.execute("BEGIN IMMEDIATE")
        user = actor(db, actor_id)
        row = inquiry(db, inquiry_id)
        if payload.get("updated_at") != row["updated_at"]:
            raise RuleError("conflict", 409)
        now = max(datetime.now(), datetime.fromisoformat(row["updated_at"]) + timedelta(microseconds=1)).isoformat(sep=" ", timespec="microseconds")

        def history(field, old, new):
            db.execute("""INSERT INTO inquiry_histories
                (inquiry_id,changed_by,field_name,old_value,new_value,changed_at) VALUES(?,?,?,?,?,?)""",
                       (inquiry_id, actor_id, field, None if old is None else str(old), None if new is None else str(new), now))

        if operation == "status":
            target = payload.get("status")
            if not isinstance(target, str) or target not in TRANSITIONS.get(row["status"], ()):
                raise RuleError("transition")
            comment = payload.get("comment", "")
            if not isinstance(comment, str) or len(comment) > 200:
                raise RuleError("comment")
            assignee = row["assignee_id"]
            if row["status"] == "NEW" and target == "IN_PROGRESS" and assignee is None:
                assignee = actor_id
            closed = now if target == "DONE" else (None if row["status"] == "DONE" else row["closed_at"])
            db.execute("UPDATE inquiries SET status=?,assignee_id=?,closed_at=?,updated_at=? WHERE id=?",
                       (target, assignee, closed, now, inquiry_id))
            history("status", row["status"], target)
            if assignee != row["assignee_id"]:
                history("assignee", row["assignee_id"], assignee)
            message = f"ステータスを「{STATUS[target]}」に変更しました。"
        elif operation == "assignee":
            target = payload.get("assignee_id")
            if row["status"] == "DONE":
                raise RuleError("done")
            if user["role"] != "ADMIN" and (user["role"] != "MEMBER" or row["assignee_id"] is not None or type(target) is not int or target != actor_id):
                raise RuleError("permission", 403)
            if target is None:
                if user["role"] != "ADMIN" or row["status"] != "NEW":
                    raise RuleError("permission", 403)
                message = "担当者を未割り当てに戻しました。"
            else:
                selected = db.execute("SELECT name FROM users WHERE id=? AND is_active=1", (target,)).fetchone() if type(target) is int else None
                if selected is None:
                    raise RuleError("user")
                message = f"担当者を「{selected['name']}」に変更しました。"
            if target != row["assignee_id"]:
                db.execute("UPDATE inquiries SET assignee_id=?,updated_at=? WHERE id=?", (target, now, inquiry_id))
                history("assignee", row["assignee_id"], target)
        else:
            raise RuleError("transition")
        db.commit()
        return {"message": message}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def handler(database, actor_id):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, code, value, mime="application/json; charset=utf-8"):
            data = json.dumps(value, ensure_ascii=False).encode("utf-8") if mime.startswith("application/json") else value
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlsplit(self.path).path
            try:
                if path == "/api/inquiries":
                    db = connect(database)
                    try:
                        actor(db, actor_id)
                        rows = [dict(r) for r in db.execute("SELECT id,title,status FROM inquiries ORDER BY id")]
                    finally:
                        db.close()
                    return self.respond(200, rows)
                if path.startswith("/api/inquiries/"):
                    return self.respond(200, detail(database, int(path.rsplit("/", 1)[1]), actor_id))
                files = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
                if path in files:
                    name, mime = files[path]
                    return self.respond(200, (ROOT / "static" / name).read_bytes(), mime)
                self.respond(404, {"message": MESSAGES["missing"]})
            except RuleError as error:
                self.respond(error.status, {"message": error.message})
            except ValueError:
                self.respond(404, {"message": MESSAGES["missing"]})

        def do_POST(self):
            try:
                # JSON-only requests prevent cross-site HTML form submissions.
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    return self.respond(415, {"message": "JSON形式で送信してください。"})
                parts = urlsplit(self.path).path.strip("/").split("/")
                if len(parts) != 4 or parts[:2] != ["api", "inquiries"] or parts[3] not in ("status", "assignee"):
                    return self.respond(404, {"message": MESSAGES["missing"]})
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16384:
                    return self.respond(400, {"message": "リクエストの形式が正しくありません。"})
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError()
                self.respond(200, update(database, int(parts[2]), actor_id, parts[3], payload))
            except RuleError as error:
                self.respond(error.status, {"message": error.message})
            except (ValueError, UnicodeDecodeError):
                self.respond(400, {"message": "リクエストの形式が正しくありません。"})
            except sqlite3.Error:
                self.respond(500, {"message": "更新に失敗しました。画面を再読み込みしてください。"})
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--user", type=int, default=1, help="既存認証の代わりにサーバー起動時に固定するユーザーID")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "gate1.sqlite3")
    args = parser.parse_args()
    args.db.parent.mkdir(parents=True, exist_ok=True)
    initialize(args.db)
    db = connect(args.db)
    try:
        actor(db, args.user)
    finally:
        db.close()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(args.db, args.user))
    print(f"Gate1: http://127.0.0.1:{args.port}/?id=1 (user={args.user})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
