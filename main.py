import os
from functools import wraps
from io import BytesIO

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for

from library import Library, LibraryError
from storage import MemoryStore, R2Store, r2_client

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "great-aw-database-dev")
library = None


def get_library():
    global library
    if library is None:
        if os.environ.get("LIBRARY_STORE") == "memory":
            store = MemoryStore()
        else:
            store = R2Store(r2_client(), os.environ.get("R2_BUCKET", "greatdatabse"))
        library = Library(os.environ.get("LIBRARY_DB", "/tmp/library.sqlite"), store)
    return library


def password():
    return os.environ.get("API_KEY") or os.environ.get("ACCESS_PASSWORD") or ""


def api_authorized():
    expected = password()
    if not expected:
        return False
    header = request.headers.get("X-API-Key", "")
    bearer = request.headers.get("Authorization", "")
    token = bearer[7:] if bearer.startswith("Bearer ") else ""
    return header == expected or token == expected or session.get("ok")


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("ok"):
            return redirect(url_for("login_page"))
        return fn(*args, **kwargs)

    return wrapper


def api_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not api_authorized():
            return jsonify({"error": "API key required."}), 401
        return fn(*args, **kwargs)

    return wrapper


@app.after_request
def cors(response):
    if request.path.startswith("/api") or request.path == "/health":
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-API-Key"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return response


def size_label(n):
    value = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"


def messages():
    return request.args.getlist("m")


def page(template, **context):
    context.setdefault("messages", messages())
    context.setdefault("section", "library")
    context.setdefault("title", "Media Library")
    return render_template(template, **context)


@app.get("/health")
def health():
    return {"ok": True, "service": "great-aw-database"}


@app.get("/login")
def login_page():
    if session.get("ok"):
        return redirect(url_for("library_home"))
    return page("login.html", title="Sign in")


@app.post("/login")
def login():
    if password() and request.form.get("password") == password():
        session["ok"] = True
        return redirect(url_for("library_home"))
    return page("login.html", title="Sign in", messages=["Wrong password."])


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


@app.get("/")
@login_required
def library_home():
    lib = get_library()
    query = request.args.get("q", "").strip()
    return page(
        "library.html",
        categories=lib.categories(),
        topics=lib.topics(query=query) if query else [],
        query=query,
    )


@app.delete("/api/topics/<category_slug>/<topic_slug>")
@api_required
def api_delete_topic(category_slug, topic_slug):
    try:
        removed = get_library().delete_topic(category_slug, topic_slug)
    except LibraryError as exc:
        return jsonify({"error": str(exc)}), exc.status
    return jsonify(removed)


@app.delete("/api/categories/<slug>")
@api_required
def api_delete_category(slug):
    try:
        removed = get_library().delete_category(slug)
    except LibraryError as exc:
        return jsonify({"error": str(exc)}), exc.status
    return jsonify(removed)


@app.post("/categories")
@login_required
def add_category():
    try:
        created = get_library().create_category(request.form.get("name", ""))
    except LibraryError as exc:
        return redirect(url_for("library_home", m=str(exc)))
    return redirect(url_for("category_page", slug=created["slug"], m=f"Added {created['name']}."))


@app.get("/c/<slug>")
@login_required
def category_page(slug):
    lib = get_library()
    try:
        category = lib.category(slug)
    except LibraryError:
        return redirect(url_for("library_home", m="Category not found."))
    query = request.args.get("q", "").strip()
    topics = lib.topics(category_slug=slug, query=query or None)
    return page(
        "category.html",
        title=category["name"],
        category=category,
        topics=topics,
        query=query,
        images=sum(topic["images"] for topic in topics),
        clips=sum(topic["clips"] for topic in topics),
    )


@app.post("/c/<slug>/topics")
@login_required
def add_topic(slug):
    lib = get_library()
    names = [line.strip() for line in request.form.get("names", "").splitlines() if line.strip()]
    try:
        for name in names:
            lib.create_topic(slug, name)
    except LibraryError as exc:
        return redirect(url_for("category_page", slug=slug, m=str(exc)))
    return redirect(url_for("category_page", slug=slug, m=f"Added {len(names)} topics."))


@app.get("/c/<category_slug>/<topic_slug>")
@login_required
def topic_page(category_slug, topic_slug):
    lib = get_library()
    try:
        topic = lib.topic(category_slug, topic_slug)
        category = lib.category(category_slug)
    except LibraryError:
        return redirect(url_for("library_home", m="Topic not found."))
    assets = lib.assets(category_slug=category_slug, topic_slug=topic_slug, limit=300, newest_first=False)
    for index, asset in enumerate(assets, start=1):
        asset["rank"] = index
        asset["size_label"] = size_label(asset["size_bytes"])
    return page(
        "topic.html",
        title=topic["name"],
        topic=topic,
        category=category,
        assets=assets,
        images=sum(1 for asset in assets if asset["kind"] == "image"),
        clips=sum(1 for asset in assets if asset["kind"] == "clip"),
    )


@app.post("/clips")
@login_required
def upload_clip():
    upload = request.files.get("file")
    category = request.form.get("category", "")
    topic = request.form.get("topic", "")
    if not upload or not upload.filename:
        return redirect(request.referrer or url_for("library_home", m="Choose a file."))
    try:
        asset = get_library().add_asset(
            category,
            topic,
            upload.stream,
            upload.filename,
            upload.mimetype,
            kind=request.form.get("kind") or None,
            title=request.form.get("title"),
            tags=request.form.get("tags", ""),
        )
    except LibraryError as exc:
        return redirect(url_for("library_home", m=str(exc)))
    return redirect(
        url_for(
            "topic_page",
            category_slug=asset["category"]["slug"],
            topic_slug=asset["topic"]["slug"],
            m=f"Stored {asset['filename']} as its own R2 object.",
        )
    )


@app.post("/clips/delete")
@login_required
def delete_clip():
    try:
        get_library().delete_asset(request.form.get("id", ""))
    except LibraryError as exc:
        return redirect(url_for("library_home", m=str(exc)))
    return redirect(request.form.get("next") or url_for("library_home"))


@app.get("/api-access")
@login_required
def api_help_page():
    return page("api.html", section="api", title="API Access", base=request.url_root.rstrip("/"))


@app.get("/api")
def api_index():
    return jsonify(
        {
            "auth": "Send X-API-Key or Authorization: Bearer with the library password.",
            "endpoints": [
                "GET /api/categories",
                "POST /api/categories",
                "GET /api/topics?category=&q=",
                "POST /api/topics",
                "GET /api/search?q=",
                "GET /api/clips?category=&topic=&kind=&q=",
                "POST /api/clips",
                "GET /api/clips/<id>",
                "GET /api/clips/<id>/file",
                "DELETE /api/clips/<id>",
            ],
        }
    )


@app.get("/api/categories")
@api_required
def api_categories():
    rows = get_library().categories()
    return jsonify({"categories": rows})


@app.post("/api/categories")
@api_required
def api_create_category():
    body = request.get_json(silent=True) or {}
    try:
        created = get_library().create_category(body.get("name") or request.form.get("name", ""), body.get("description", ""))
    except LibraryError as exc:
        return jsonify({"error": str(exc)}), exc.status
    return jsonify(created), 201


@app.get("/api/topics")
@api_required
def api_topics():
    return jsonify(
        {
            "topics": get_library().topics(
                category_slug=request.args.get("category") or None,
                query=request.args.get("q") or None,
            )
        }
    )


@app.post("/api/topics")
@api_required
def api_create_topics():
    body = request.get_json(silent=True) or {}
    category = body.get("category") or request.form.get("category", "")
    names = body.get("topics") or body.get("names") or []
    if isinstance(names, str):
        names = [names]
    if body.get("name"):
        names = [body["name"], *names]
    try:
        created = get_library().create_topics_bulk(category, names)
    except LibraryError as exc:
        return jsonify({"error": str(exc)}), exc.status
    return jsonify({"topics": created}), 201


@app.post("/api/clips/import")
@api_required
def api_import_clips():
    body = request.get_json(silent=True) or {}
    items = body.get("assets") or []
    if not items:
        return jsonify({"error": "assets is required."}), 400
    try:
        saved = get_library().import_assets(items)
    except LibraryError as exc:
        return jsonify({"error": str(exc)}), exc.status
    except KeyError as exc:
        return jsonify({"error": f"Missing {exc}."}), 400
    return jsonify({"clips": saved}), 201


@app.get("/api/search")
@api_required
def api_search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "q is required."}), 400
    return jsonify(get_library().search(query, limit=request.args.get("limit", 50)))


@app.get("/api/clips")
@api_required
def api_clips():
    return jsonify(
        {
            "clips": get_library().assets(
                category_slug=request.args.get("category") or None,
                topic_slug=request.args.get("topic") or None,
                kind=request.args.get("kind") or None,
                query=request.args.get("q") or None,
                limit=request.args.get("limit", 50),
                offset=request.args.get("offset", 0),
            )
        }
    )


@app.post("/api/clips")
@api_required
def api_upload_clip():
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"error": "file is required."}), 400
    try:
        asset = get_library().add_asset(
            request.form.get("category", ""),
            request.form.get("topic", ""),
            upload.stream,
            upload.filename,
            upload.mimetype,
            kind=request.form.get("kind") or None,
            title=request.form.get("title"),
            tags=request.form.get("tags", ""),
        )
    except LibraryError as exc:
        return jsonify({"error": str(exc)}), exc.status
    return jsonify(asset), 201


@app.get("/api/clips/<asset_id>")
@api_required
def api_clip(asset_id):
    try:
        return jsonify(get_library().asset(asset_id))
    except LibraryError as exc:
        return jsonify({"error": str(exc)}), exc.status


@app.delete("/api/clips/<asset_id>")
@api_required
def api_delete_clip(asset_id):
    try:
        return jsonify(get_library().delete_asset(asset_id))
    except LibraryError as exc:
        return jsonify({"error": str(exc)}), exc.status


@app.get("/api/clips/<asset_id>/file")
@api_required
def api_clip_file(asset_id):
    try:
        record, body, status, headers = get_library().open_asset(asset_id, request.headers.get("Range"))
    except LibraryError as exc:
        return jsonify({"error": str(exc)}), exc.status
    except KeyError:
        return jsonify({"error": "File missing from R2."}), 404
    disposition = "attachment" if request.args.get("download") else "inline"
    headers["Content-Disposition"] = f'{disposition}; filename="{record["filename"]}"'
    if isinstance(body, (bytes, bytearray)):
        stream = BytesIO(body)
    else:
        stream = body
    return Response(stream, status=status, headers=headers)


@app.route("/api/<path:unused>", methods=["OPTIONS"])
def api_options(unused):
    return "", 204
