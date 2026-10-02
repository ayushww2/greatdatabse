import os
from datetime import datetime, timezone
from functools import wraps

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template_string,
    request,
    session,
    url_for,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "great-aw-database-dev")

BUCKET = os.environ.get("R2_BUCKET", "greatdatabse")
PAGE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Great AW Database</title>
  <style>
    :root { color-scheme: dark; --bg:#0e1116; --card:#171c24; --line:#2a3340; --text:#e8eef6; --muted:#93a0b3; --accent:#3d8bfd; }
    * { box-sizing: border-box; }
    body { margin:0; font:16px/1.45 system-ui, sans-serif; background:var(--bg); color:var(--text); }
    main { max-width: 960px; margin: 0 auto; padding: 32px 20px 64px; }
    h1 { font-size: 28px; margin: 0 0 4px; }
    p.sub { color: var(--muted); margin: 0 0 24px; }
    .card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px; margin-bottom: 16px; }
    label { display:block; font-size: 13px; color: var(--muted); margin-bottom: 6px; }
    input[type=password], input[type=file], input[type=text] { width:100%; background:#0e1116; color:var(--text); border:1px solid var(--line); border-radius:8px; padding:10px 12px; }
    button, .btn { background: var(--accent); color: white; border: 0; border-radius: 8px; padding: 10px 14px; font-weight: 600; cursor: pointer; text-decoration: none; display: inline-block; }
    button.ghost, a.ghost { background: transparent; color: var(--text); border: 1px solid var(--line); }
    button.danger { background: #c24141; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid var(--line); vertical-align: middle; }
    th { color: var(--muted); font-size: 12px; letter-spacing: .04em; text-transform: uppercase; }
    .row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
    .flash { background:#3a2a12; border:1px solid #8a6420; color:#ffd89a; padding:10px 12px; border-radius:8px; margin-bottom:12px; }
    .empty { color: var(--muted); padding: 20px 0; }
    form.inline { display:inline; }
    .top { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }
  </style>
</head>
<body>
<main>
  <div class="top">
    <div>
      <h1>Great AW Database</h1>
      <p class="sub">Bucket {{ bucket }} · {{ count }} object{{ '' if count == 1 else 's' }}</p>
    </div>
    {% if authed %}
    <form method="post" action="{{ url_for('logout') }}"><button class="ghost" type="submit">Log out</button></form>
    {% endif %}
  </div>
  {% for message in messages %}
    <div class="flash">{{ message }}</div>
  {% endfor %}
  {% if not authed %}
  <form class="card" method="post" action="{{ url_for('login') }}">
    <label for="password">Password</label>
    <div class="row">
      <input id="password" name="password" type="password" autocomplete="current-password" required>
      <button type="submit">Open</button>
    </div>
  </form>
  {% else %}
  <form class="card" method="post" action="{{ url_for('upload') }}" enctype="multipart/form-data">
    <label for="file">Upload a file</label>
    <div class="row">
      <input id="file" name="file" type="file" required>
      <input name="key" type="text" placeholder="Optional object name">
      <button type="submit">Upload</button>
    </div>
  </form>
  <div class="card">
    {% if objects %}
    <table>
      <thead><tr><th>Name</th><th>Size</th><th>Updated</th><th></th></tr></thead>
      <tbody>
      {% for obj in objects %}
        <tr>
          <td>{{ obj.key }}</td>
          <td>{{ obj.size_label }}</td>
          <td>{{ obj.updated }}</td>
          <td class="row">
            <a class="btn" href="{{ url_for('download', key=obj.key) }}">Download</a>
            <form class="inline" method="post" action="{{ url_for('delete') }}" onsubmit="return confirm('Delete {{ obj.key }}?')">
              <input type="hidden" name="key" value="{{ obj.key }}">
              <button class="danger" type="submit">Delete</button>
            </form>
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    {% else %}
      <div class="empty">This bucket is empty. Upload the first file above.</div>
    {% endif %}
  </div>
  {% endif %}
</main>
</body>
</html>
"""


def s3():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("ok"):
            return redirect(url_for("index"))
        return fn(*args, **kwargs)

    return wrapper


def human_size(n):
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"


def list_objects():
    client = s3()
    token = None
    items = []
    while True:
        kwargs = {"Bucket": BUCKET, "MaxKeys": 1000}
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        for obj in page.get("Contents") or []:
            updated = obj["LastModified"]
            if isinstance(updated, datetime):
                updated = updated.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            items.append(
                {
                    "key": obj["Key"],
                    "size_label": human_size(obj["Size"]),
                    "updated": updated,
                }
            )
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
    items.sort(key=lambda item: item["key"].lower())
    return items


def page(authed, objects=None, error=None):
    messages = list(request.args.getlist("m"))
    if error:
        messages.append(error)
    return render_template_string(
        PAGE,
        bucket=BUCKET,
        authed=authed,
        objects=objects or [],
        count=len(objects or []),
        messages=messages,
    )


@app.get("/health")
def health():
    return {"ok": True, "bucket": BUCKET}


@app.get("/")
def index():
    if not session.get("ok"):
        return page(False)
    try:
        objects = list_objects()
    except ClientError as exc:
        return page(True, error=exc.response["Error"].get("Message", "Could not list the bucket."))
    return page(True, objects)


@app.post("/login")
def login():
    expected = os.environ.get("ACCESS_PASSWORD", "")
    if expected and request.form.get("password") == expected:
        session["ok"] = True
        return redirect(url_for("index"))
    return page(False, error="Wrong password.")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.post("/upload")
@login_required
def upload():
    upload_file = request.files.get("file")
    if not upload_file or not upload_file.filename:
        return redirect(url_for("index", m="Choose a file to upload."))
    key = (request.form.get("key") or upload_file.filename).strip().lstrip("/")
    if not key or ".." in key.split("/"):
        return redirect(url_for("index", m="That object name is not allowed."))
    s3().upload_fileobj(
        upload_file.stream,
        BUCKET,
        key,
        ExtraArgs={"ContentType": upload_file.mimetype or "application/octet-stream"},
    )
    return redirect(url_for("index", m=f"Uploaded {key}."))


@app.get("/download")
@login_required
def download():
    key = request.args.get("key", "")
    if not key:
        return redirect(url_for("index"))
    obj = s3().get_object(Bucket=BUCKET, Key=key)
    filename = key.rsplit("/", 1)[-1]
    return Response(
        obj["Body"].read(),
        mimetype=obj.get("ContentType") or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/delete")
@login_required
def delete():
    key = (request.form.get("key") or "").strip()
    if key:
        s3().delete_object(Bucket=BUCKET, Key=key)
    return redirect(url_for("index", m=f"Deleted {key}." if key else "Nothing to delete."))
