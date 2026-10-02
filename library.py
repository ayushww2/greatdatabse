import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

DB_OBJECT_KEY = "catalog/library.sqlite"

SEED_CATEGORIES = []

SEED_TOPICS = {}


def slugify(value):
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug or "item"


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_filename(name):
    base = os.path.basename(name or "file").replace("\x00", "")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip(".-")
    return cleaned or "file"


class LibraryError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Library:
    def __init__(self, db_path, store):
        self.db_path = db_path
        self.store = store
        self.lock = threading.Lock()
        folder = os.path.dirname(db_path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self._restore()
        self._migrate()
        if not self.categories():
            self._seed()
        self.sync_seed_order()

    def connect(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_order_columns(self, conn):
        for table in ("categories", "topics"):
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if columns and "position" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN position INTEGER NOT NULL DEFAULT 0")

    def sync_seed_order(self):
        with self.lock:
            with self.connect() as conn:
                for index, (name, _description) in enumerate(SEED_CATEGORIES):
                    conn.execute(
                        "UPDATE categories SET position = ? WHERE slug = ?",
                        (index, slugify(name)),
                    )
                for category_name, topic_names in SEED_TOPICS.items():
                    category = conn.execute(
                        "SELECT id FROM categories WHERE slug = ?",
                        (slugify(category_name),),
                    ).fetchone()
                    if not category:
                        continue
                    for index, topic_name in enumerate(topic_names):
                        conn.execute(
                            "UPDATE topics SET position = ? WHERE category_id = ? AND slug = ?",
                            (index, category["id"], slugify(topic_name)),
                        )
            self._persist()

    def _migrate(self):
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS categories (
                    id TEXT PRIMARY KEY,
                    slug TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    position INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS topics (
                    id TEXT PRIMARY KEY,
                    category_id TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    name TEXT NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(category_id, slug),
                    FOREIGN KEY(category_id) REFERENCES categories(id)
                );
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY,
                    topic_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    r2_key TEXT NOT NULL UNIQUE,
                    tags TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(topic_id) REFERENCES topics(id)
                );
                """
            )
            self._ensure_order_columns(conn)

    def _restore(self):
        if os.path.exists(self.db_path) and os.path.getsize(self.db_path) > 0:
            return
        data = self.store.get_bytes(DB_OBJECT_KEY)
        if data:
            with open(self.db_path, "wb") as handle:
                handle.write(data)

    def _persist(self):
        with open(self.db_path, "rb") as handle:
            self.store.put_bytes(DB_OBJECT_KEY, handle.read(), "application/x-sqlite3")

    def _seed(self):
        for name, description in SEED_CATEGORIES:
            category = self.create_category(name, description, persist=False)
            for topic_name in SEED_TOPICS.get(name, []):
                self.create_topic(category["slug"], topic_name, persist=False)
        self._persist()

    def categories(self):
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT c.*,
                    (SELECT COUNT(*) FROM topics t WHERE t.category_id = c.id) AS people,
                    (SELECT COUNT(*) FROM assets a JOIN topics t ON t.id = a.topic_id
                        WHERE t.category_id = c.id AND a.kind = 'image') AS images,
                    (SELECT COUNT(*) FROM assets a JOIN topics t ON t.id = a.topic_id
                        WHERE t.category_id = c.id AND a.kind = 'clip') AS clips
                FROM categories c
                ORDER BY c.position, c.name
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_category(self, slug):
        category = self.category(slug)
        with self.lock:
            with self.connect() as conn:
                keys = conn.execute(
                    """
                    SELECT a.r2_key FROM assets a
                    JOIN topics t ON t.id = a.topic_id
                    WHERE t.category_id = ?
                    """,
                    (category["id"],),
                ).fetchall()
                for row in keys:
                    self.store.delete(row["r2_key"])
                conn.execute(
                    "DELETE FROM assets WHERE topic_id IN (SELECT id FROM topics WHERE category_id = ?)",
                    (category["id"],),
                )
                conn.execute("DELETE FROM topics WHERE category_id = ?", (category["id"],))
                conn.execute("DELETE FROM categories WHERE id = ?", (category["id"],))
            self._persist()
        return {"slug": category["slug"], "name": category["name"]}

    def category(self, slug):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM categories WHERE slug = ?", (slug,)).fetchone()
        if not row:
            raise LibraryError("Category not found.", 404)
        return dict(row)

    def topics(self, category_slug=None, query=None):
        sql = """
            SELECT t.*, c.slug AS category_slug, c.name AS category_name,
                (SELECT COUNT(*) FROM assets a WHERE a.topic_id = t.id AND a.kind = 'image') AS images,
                (SELECT COUNT(*) FROM assets a WHERE a.topic_id = t.id AND a.kind = 'clip') AS clips
            FROM topics t
            JOIN categories c ON c.id = t.category_id
            WHERE 1 = 1
        """
        params = []
        if category_slug:
            sql += " AND c.slug = ?"
            params.append(category_slug)
        if query:
            sql += " AND (t.name LIKE ? OR c.name LIKE ?)"
            like = f"%{query}%"
            params.extend([like, like])
        sql += " ORDER BY t.position, t.name"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def topic(self, category_slug, topic_slug):
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT t.*, c.slug AS category_slug, c.name AS category_name
                FROM topics t JOIN categories c ON c.id = t.category_id
                WHERE c.slug = ? AND t.slug = ?
                """,
                (category_slug, topic_slug),
            ).fetchone()
        if not row:
            raise LibraryError("Topic not found.", 404)
        return dict(row)

    def create_category(self, name, description="", persist=True):
        name = (name or "").strip()
        if not name:
            raise LibraryError("Category name is required.")
        slug = slugify(name)
        with self.lock:
            with self.connect() as conn:
                existing = conn.execute("SELECT * FROM categories WHERE slug = ?", (slug,)).fetchone()
                if existing:
                    return dict(existing)
                position = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM categories").fetchone()[0]
                record = {
                    "id": str(uuid.uuid4()),
                    "slug": slug,
                    "name": name,
                    "description": (description or "").strip(),
                    "position": position,
                    "created_at": now(),
                }
                conn.execute(
                    "INSERT INTO categories (id, slug, name, description, position, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (record["id"], record["slug"], record["name"], record["description"], record["position"], record["created_at"]),
                )
            if persist:
                self._persist()
        return record

    def create_topic(self, category, name, persist=True):
        name = (name or "").strip()
        if not name:
            raise LibraryError("Topic name is required.")
        category_row = self._category_lookup(category)
        slug = slugify(name)
        with self.lock:
            with self.connect() as conn:
                existing = conn.execute(
                    "SELECT * FROM topics WHERE category_id = ? AND slug = ?",
                    (category_row["id"], slug),
                ).fetchone()
                if existing:
                    found = dict(existing)
                    found["category_slug"] = category_row["slug"]
                    found["category_name"] = category_row["name"]
                    return found
                position = conn.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 FROM topics WHERE category_id = ?",
                    (category_row["id"],),
                ).fetchone()[0]
                record = {
                    "id": str(uuid.uuid4()),
                    "category_id": category_row["id"],
                    "slug": slug,
                    "name": name,
                    "position": position,
                    "created_at": now(),
                    "category_slug": category_row["slug"],
                    "category_name": category_row["name"],
                }
                conn.execute(
                    "INSERT INTO topics (id, category_id, slug, name, position, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (record["id"], record["category_id"], record["slug"], record["name"], record["position"], record["created_at"]),
                )
            if persist:
                self._persist()
        return record

    def create_topics_bulk(self, category, names):
        created = []
        for name in names:
            created.append(self.create_topic(category, name, persist=False))
        self._persist()
        return created

    def _category_lookup(self, value):
        key = (value or "").strip()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM categories WHERE slug = ? OR lower(name) = lower(?)",
                (slugify(key), key),
            ).fetchone()
        if not row:
            raise LibraryError("Category not found.", 404)
        return dict(row)

    def add_asset(self, category, topic_name, fileobj, filename, content_type, kind=None, title=None, tags=""):
        category_row = self._category_lookup(category)
        topic_row = self._topic_lookup(category_row, topic_name)
        filename = safe_filename(filename)
        content_type = content_type or "application/octet-stream"
        kind = kind if kind in ("image", "clip") else infer_kind(content_type)
        asset_id = str(uuid.uuid4())
        key = f"media/{category_row['slug']}/{topic_row['slug']}/{asset_id}/{filename}"
        size = self.store.put(key, fileobj, content_type)
        title = (title or os.path.splitext(filename)[0]).strip() or filename
        record = {
            "id": asset_id,
            "topic_id": topic_row["id"],
            "kind": kind,
            "title": title,
            "filename": filename,
            "content_type": content_type,
            "size_bytes": size,
            "r2_key": key,
            "tags": (tags or "").strip(),
            "created_at": now(),
        }
        with self.lock:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO assets
                    (id, topic_id, kind, title, filename, content_type, size_bytes, r2_key, tags, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["id"],
                        record["topic_id"],
                        record["kind"],
                        record["title"],
                        record["filename"],
                        record["content_type"],
                        record["size_bytes"],
                        record["r2_key"],
                        record["tags"],
                        record["created_at"],
                    ),
                )
            self._persist()
        return self.asset(asset_id)

    def import_assets(self, items):
        prepared = []
        for item in items:
            category_row = self._category_lookup(item.get("category", ""))
            topic_row = self._topic_lookup(category_row, item.get("topic", ""))
            tags = item.get("tags") or []
            if isinstance(tags, str):
                tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
            prepared.append(
                {
                    "id": item.get("id") or str(uuid.uuid4()),
                    "topic_id": topic_row["id"],
                    "kind": "clip" if item.get("kind") not in ("image", "clip") else item["kind"],
                    "title": (item.get("title") or item.get("filename") or "clip").strip(),
                    "filename": safe_filename(item.get("filename") or "clip.mp4"),
                    "content_type": item.get("content_type") or "video/mp4",
                    "size_bytes": int(item.get("size_bytes") or 0),
                    "r2_key": item["r2_key"],
                    "tags": ", ".join(tags),
                    "created_at": now(),
                }
            )
        saved = []
        with self.lock:
            with self.connect() as conn:
                for record in prepared:
                    existing = conn.execute("SELECT id FROM assets WHERE r2_key = ?", (record["r2_key"],)).fetchone()
                    if existing:
                        saved.append(existing["id"])
                        continue
                    conn.execute(
                        """
                        INSERT INTO assets
                        (id, topic_id, kind, title, filename, content_type, size_bytes, r2_key, tags, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record["id"],
                            record["topic_id"],
                            record["kind"],
                            record["title"],
                            record["filename"],
                            record["content_type"],
                            record["size_bytes"],
                            record["r2_key"],
                            record["tags"],
                            record["created_at"],
                        ),
                    )
                    saved.append(record["id"])
            self._persist()
        return [self.asset(asset_id) for asset_id in saved]

    def _topic_lookup(self, category_row, value):
        key = (value or "").strip()
        if not key:
            raise LibraryError("Topic is required.")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM topics WHERE category_id = ? AND (slug = ? OR lower(name) = lower(?))",
                (category_row["id"], slugify(key), key),
            ).fetchone()
        if row:
            return dict(row)
        return self.create_topic(category_row["slug"], key)

    def asset(self, asset_id):
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT a.*, t.slug AS topic_slug, t.name AS topic_name,
                       c.slug AS category_slug, c.name AS category_name
                FROM assets a
                JOIN topics t ON t.id = a.topic_id
                JOIN categories c ON c.id = t.category_id
                WHERE a.id = ?
                """,
                (asset_id,),
            ).fetchone()
        if not row:
            raise LibraryError("Clip not found.", 404)
        return self._public_asset(dict(row))

    def assets(self, category_slug=None, topic_slug=None, kind=None, query=None, limit=50, offset=0):
        sql = """
            SELECT a.*, t.slug AS topic_slug, t.name AS topic_name,
                   c.slug AS category_slug, c.name AS category_name
            FROM assets a
            JOIN topics t ON t.id = a.topic_id
            JOIN categories c ON c.id = t.category_id
            WHERE 1 = 1
        """
        params = []
        if category_slug:
            sql += " AND c.slug = ?"
            params.append(category_slug)
        if topic_slug:
            sql += " AND t.slug = ?"
            params.append(topic_slug)
        if kind in ("image", "clip"):
            sql += " AND a.kind = ?"
            params.append(kind)
        if query:
            sql += " AND (a.title LIKE ? OR a.filename LIKE ? OR a.tags LIKE ? OR t.name LIKE ? OR c.name LIKE ?)"
            like = f"%{query}%"
            params.extend([like, like, like, like, like])
        sql += " ORDER BY a.created_at DESC LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._public_asset(dict(row)) for row in rows]

    def search(self, query, limit=50):
        return {
            "query": query,
            "categories": [row for row in self.categories() if query.lower() in row["name"].lower()],
            "topics": self.topics(query=query),
            "clips": self.assets(query=query, limit=limit),
        }

    def delete_asset(self, asset_id):
        record = self.asset(asset_id)
        self.store.delete(record["r2_key"])
        with self.lock:
            with self.connect() as conn:
                conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
            self._persist()
        return record

    def open_asset(self, asset_id, range_header=None):
        record = self.asset(asset_id)
        body, status, headers = self.store.get(record["r2_key"], range_header)
        headers["Content-Type"] = record["content_type"]
        headers["Accept-Ranges"] = "bytes"
        return record, body, status, headers

    def _public_asset(self, row):
        return {
            "id": row["id"],
            "title": row["title"],
            "kind": row["kind"],
            "filename": row["filename"],
            "content_type": row["content_type"],
            "size_bytes": row["size_bytes"],
            "r2_key": row["r2_key"],
            "tags": [tag.strip() for tag in row["tags"].split(",") if tag.strip()],
            "created_at": row["created_at"],
            "category": {"slug": row["category_slug"], "name": row["category_name"]},
            "topic": {"slug": row["topic_slug"], "name": row["topic_name"]},
            "file_url": f"/api/clips/{row['id']}/file",
        }


def infer_kind(content_type):
    if (content_type or "").startswith("image/"):
        return "image"
    return "clip"
