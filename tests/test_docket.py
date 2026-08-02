"""End-to-end tests: demo generation, deck discovery, the HTTP surface, and the
append-only guarantees (evidence freezing, tombstones, undo). Stdlib only --
run with:  python3 -m unittest discover -s tests
"""
import base64
import hashlib
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import docket  # noqa: E402

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQAB"
    "h6FO1AAAAABJRU5ErkJggg==")


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = docket.make_demo(Path(self.tmp.name) / "demo")
        self.cfg = docket.make_cfg([self.root], Path(self.tmp.name) / "data")

    def tearDown(self):
        self.tmp.cleanup()

    def test_demo_is_deterministic(self):
        other = docket.make_demo(Path(self.tmp.name) / "demo2")
        for rel in ("crosses/cards.json", "crosses/sheet.png", "diamonds/cluster-01.png"):
            self.assertEqual((self.root / rel).read_bytes(), (other / rel).read_bytes(), rel)

    def test_feed_shape(self):
        items, warnings = docket.build_items(self.cfg)
        self.assertEqual(warnings, [])
        self.assertEqual(len(items), 15)          # 2 sidecar decks x 6 + 3 loose images
        self.assertEqual(len({it["id"] for it in items}), 15)
        for it in items:
            self.assertRegex(it["id"], r"^[0-9a-f]{16}$")
            self.assertTrue(it["images"], it["label"])

    def test_ids_survive_reload(self):
        a, _ = docket.build_items(self.cfg)
        b, _ = docket.build_items(self.cfg)
        self.assertEqual([it["id"] for it in a], [it["id"] for it in b])


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = docket.make_demo(Path(cls.tmp.name) / "demo")
        cls.cfg = docket.make_cfg([cls.root], Path(cls.tmp.name) / "data")
        docket.Handler.cfg = cls.cfg
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), docket.Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return r.status, r.read()

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())

    def items(self):
        return json.loads(self.get("/items.json")[1])["items"]

    def test_page_and_feed(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html>", body)
        self.assertTrue(self.items())

    def test_verdict_freezes_evidence(self):
        it = self.items()[0]
        status, j = self.post("/verdict", {"id": it["id"], "verdict": "YES", "note": "clean"})
        self.assertEqual(status, 200)
        self.assertTrue(j["ok"])
        lines = [json.loads(l) for l in
                 (self.cfg["data"] / "verdicts.jsonl").read_text().splitlines()]
        ev = next(l for l in lines if l["id"] == it["id"])
        self.assertEqual(ev["verdict"], "YES")
        self.assertTrue(ev["evidence"]["images"])
        frozen = ev["evidence"]["images"][0]
        src = docket.find_under_roots(frozen["rel"], self.cfg["roots"]).read_bytes()
        self.assertEqual(hashlib.sha256(src).hexdigest(), frozen["sha256"])
        self.assertEqual((self.cfg["data"] / "evidence" / frozen["file"]).read_bytes(), src)

    def test_undo_clears_state(self):
        it = self.items()[1]
        self.post("/verdict", {"id": it["id"], "verdict": "NO"})
        self.post("/verdict", {"id": it["id"], "verdict": "UNDO"})
        state = json.loads(self.get("/verdicts.json")[1])
        self.assertNotIn(it["id"], state)

    def test_note_media_roundtrip(self):
        it = self.items()[2]
        data_url = "data:image/png;base64," + base64.b64encode(PNG_1PX).decode()
        _, j = self.post("/verdict", {"id": it["id"], "verdict": "FLAG",
                                      "note": "look again", "images": [data_url]})
        self.assertEqual(len(j["media"]), 1)
        status, raw = self.get("/note-media/" + j["media"][0])
        self.assertEqual(status, 200)
        self.assertEqual(raw, PNG_1PX)

    def test_delete_tombstones_and_undelete_restores(self):
        it = self.items()[3]
        _, j = self.post("/delete", {"id": it["id"]})
        self.assertTrue(j["ok"])
        self.assertNotIn(it["id"], {x["id"] for x in self.items()})
        docket.append_event(self.cfg["data"] / "deleted.jsonl",
                            {"id": it["id"], "op": "undelete"})
        self.assertIn(it["id"], {x["id"] for x in self.items()})

    def test_traversal_guarded(self):
        for path in ("/img/../cards.json", "/file/..%2F..%2Fetc%2Fpasswd"):
            try:
                status, _ = self.get(path)
            except urllib.error.HTTPError as e:
                status = e.code
            self.assertEqual(status, 404, path)

    def test_bad_verdict_rejected(self):
        it = self.items()[0]
        for body in ({"id": it["id"], "verdict": "MAYBE"}, {"id": "zz", "verdict": "YES"}):
            try:
                status, _ = self.post("/verdict", body)
            except urllib.error.HTTPError as e:
                status = e.code
            self.assertEqual(status, 400, body)


if __name__ == "__main__":
    unittest.main()
