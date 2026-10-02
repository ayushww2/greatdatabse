import io
import os
import tempfile
import unittest

os.environ["LIBRARY_STORE"] = "memory"
os.environ["ACCESS_PASSWORD"] = "test-password"
os.environ["SECRET_KEY"] = "test"

from main import app
from library import Library
from storage import MemoryStore


class LibraryApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIBRARY_DB"] = os.path.join(self.tmp.name, "library.sqlite")
        import main

        main.library = None
        self.client = app.test_client()
        self.headers = {"X-API-Key": "test-password"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_seed_and_search_and_separate_objects(self):
        categories = self.client.get("/api/categories", headers=self.headers).get_json()["categories"]
        names = {row["name"] for row in categories}
        self.assertEqual(names, {"Royal Family", "Space", "War"})
        royal = next(row for row in categories if row["name"] == "Royal Family")
        self.assertGreaterEqual(royal["people"], 35)
        topics = self.client.get("/api/topics?category=royal-family", headers=self.headers).get_json()["topics"]
        self.assertEqual(topics[0]["name"], "King Charles")
        self.assertEqual(topics[1]["name"], "Queen Camilla")
        self.assertEqual(royal["clips"], 0)

        denied = self.client.get("/api/search?q=charles")
        self.assertEqual(denied.status_code, 401)

        found = self.client.get("/api/search?q=charles", headers=self.headers).get_json()
        self.assertTrue(any(topic["name"] == "King Charles" for topic in found["topics"]))

        first = self.client.post(
            "/api/clips",
            headers=self.headers,
            data={
                "category": "Royal Family",
                "topic": "King Charles",
                "title": "Arrival",
                "kind": "clip",
                "file": (io.BytesIO(b"clip-one"), "arrival.mp4"),
            },
            content_type="multipart/form-data",
        )
        second = self.client.post(
            "/api/clips",
            headers=self.headers,
            data={
                "category": "royal-family",
                "topic": "king-charles",
                "kind": "image",
                "file": (io.BytesIO(b"image-bytes"), "portrait.jpg"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        clip = first.get_json()
        image = second.get_json()
        self.assertNotEqual(clip["r2_key"], image["r2_key"])
        self.assertTrue(clip["r2_key"].startswith("media/royal-family/king-charles/"))
        self.assertEqual(clip["kind"], "clip")
        self.assertEqual(image["kind"], "image")

        listed = self.client.get(
            "/api/clips?category=royal-family&topic=king-charles&kind=clip",
            headers=self.headers,
        ).get_json()["clips"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["title"], "Arrival")

        downloaded = self.client.get(clip["file_url"], headers=self.headers)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.data, b"clip-one")

        created = self.client.post(
            "/api/topics",
            headers={**self.headers, "Content-Type": "application/json"},
            json={"category": "Space", "topics": ["Launch", "Crew"]},
        )
        self.assertEqual(created.status_code, 201)
        space = self.client.get("/api/topics?category=space", headers=self.headers).get_json()["topics"]
        self.assertEqual({row["name"] for row in space}, {"Launch", "Crew"})

        page = self.client.get("/")
        self.assertEqual(page.status_code, 302)
        self.client.post("/login", data={"password": "test-password"})
        home = self.client.get("/")
        self.assertIn(b"Royal Family", home.data)
        self.assertIn(b"raw clips", home.data)

    def test_import_registers_separate_keys(self):
        self.client.post(
            "/api/categories",
            headers={**self.headers, "Content-Type": "application/json"},
            json={"name": "Ancient Egypt / Archaeology"},
        )
        created = self.client.post(
            "/api/clips/import",
            headers={**self.headers, "Content-Type": "application/json"},
            json={
                "assets": [
                    {
                        "category": "Ancient Egypt / Archaeology",
                        "topic": "Egyptian Pyramids",
                        "title": "Pyramids",
                        "filename": "pyramids.mp4",
                        "content_type": "video/mp4",
                        "size_bytes": 12,
                        "r2_key": "media/ancient-egypt-archaeology/egyptian-pyramids/a/pyramids.mp4",
                        "tags": ["source:111"],
                    }
                ]
            },
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        clip = created.get_json()["clips"][0]
        self.assertEqual(clip["topic"]["name"], "Egyptian Pyramids")
        self.assertEqual(clip["tags"], ["source:111"])

    def test_database_restores_from_store(self):
        store = MemoryStore()
        path = os.path.join(self.tmp.name, "one.sqlite")
        lib = Library(path, store)
        lib.create_category("Custom")
        os.remove(path)
        restored = Library(path, store)
        self.assertTrue(any(row["name"] == "Custom" for row in restored.categories()))


if __name__ == "__main__":
    unittest.main()
