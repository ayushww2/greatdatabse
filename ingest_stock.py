"""Pull one 6-second clip per stock source into the media library."""

import json
import sys
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.client import Config

from clip_plan import PLAN
from library import is_landscape_16_9, safe_filename, slugify

STOCK = os.environ.get("STOCK_API", "https://stock-clips-api-production.up.railway.app")
LIBRARY = os.environ["LIBRARY_API"].rstrip("/")
LIBRARY_KEY = os.environ["LIBRARY_API_KEY"]
BUCKET = os.environ.get("R2_BUCKET", "greatdatabse")
WORKERS = int(os.environ.get("INGEST_WORKERS", "6"))
STATE = os.environ.get("INGEST_STATE", "/tmp/aw-ingest")

os.makedirs(STATE, exist_ok=True)
sources_lock = threading.Lock()
used_sources = set()
stats_lock = threading.Lock()
stats = {"stored": 0, "topics": 0}
s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["R2_ENDPOINT"],
    aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    config=Config(signature_version="s3v4", max_pool_connections=32),
    region_name="auto",
)


def log(message):
    line = f"{time.strftime('%H:%M:%S')} {message}"
    print(line, flush=True)
    with open(os.path.join(STATE, "ingest.log"), "a") as handle:
        handle.write(line + "\n")


def library_json(method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        LIBRARY + path,
        data=data,
        method=method,
        headers={"X-API-Key": LIBRARY_KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        return json.load(response)


def load_rejected_sources():
    path = os.path.join(STATE, "rejected.txt")
    if not os.path.exists(path):
        return
    with open(path) as handle:
        for line in handle:
            source_id = line.strip()
            if source_id:
                used_sources.add(source_id)


def load_existing_sources():
    offset = 0
    while True:
        req = urllib.request.Request(
            f"{LIBRARY}/api/clips?limit=200&offset={offset}",
            headers={"X-API-Key": LIBRARY_KEY},
        )
        with urllib.request.urlopen(req, timeout=60) as response:
            clips = json.load(response)["clips"]
        if not clips:
            break
        for clip in clips:
            for tag in clip.get("tags") or []:
                if tag.startswith("source:"):
                    used_sources.add(tag.split(":", 1)[1])
        offset += len(clips)
        if len(clips) < 200:
            break
    load_rejected_sources()
    log(f"already stored sources {len(used_sources)}")


def stock_job(query, count):
    body = json.dumps({"query": query[:200], "count": count}).encode()
    for attempt in range(4):
        try:
            req = urllib.request.Request(
                STOCK + "/v1/clips",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=60) as response:
                job_id = json.load(response)["jobId"]
            deadline = time.time() + 180
            while time.time() < deadline:
                with urllib.request.urlopen(STOCK + f"/v1/jobs/{job_id}", timeout=60) as response:
                    job = json.load(response)
                if job["status"] in ("done", "failed", "error"):
                    if job["status"] != "done":
                        log(f"job failed {query!r} {job.get('error')}")
                        return []
                    return job.get("clips") or []
                time.sleep(2)
            log(f"job timeout {query!r}")
            return []
        except Exception as exc:
            log(f"stock retry {attempt} {query!r} {exc}")
            time.sleep(2 + attempt * 2)
    return []


def queries_for(topic, hint):
    stems = [
        topic,
        f"{topic} {hint}",
        f"{topic} footage",
        f"{topic} aerial",
        f"{topic} cinematic",
        f"{topic} close up",
        f"{topic} documentary",
        f"{topic} 4k",
        f"{hint} {topic}",
        f"{topic} wide shot",
    ]
    seen = []
    for stem in stems:
        cleaned = " ".join(stem.split())
        if cleaned.lower() not in {item.lower() for item in seen}:
            seen.append(cleaned)
    return seen


def rank(clip):
    resolution = (clip.get("resolution") or "").lower()
    if "2160" in resolution or "4k" in resolution:
        height = 2160
    elif "1080" in resolution:
        height = 1080
    elif "720" in resolution:
        height = 720
    else:
        height = 0
    duration = float(clip.get("duration") or 0)
    bitrate = (clip.get("bytes") or 0) / max(duration, 1)
    return (height, bitrate, duration)


def collect(topic, hint, target):
    chosen = []
    local_ids = set()
    stalled = 0
    for query in queries_for(topic, hint):
        if len(chosen) >= target:
            break
        clips = stock_job(query, 10)
        added = 0
        for clip in sorted(clips, key=rank, reverse=True):
            source_id = str(clip.get("sourceId") or "")
            duration = float(clip.get("duration") or 0)
            if not source_id or duration < 6 or not clip.get("url"):
                continue
            with sources_lock:
                if source_id in used_sources or source_id in local_ids:
                    continue
                used_sources.add(source_id)
                local_ids.add(source_id)
            chosen.append(clip)
            added += 1
            if len(chosen) >= target:
                break
        stalled = stalled + 1 if added == 0 else 0
        if stalled >= 3:
            break
    return chosen


class SkipClip(Exception):
    pass


def frame_size(path):
    raw = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,sample_aspect_ratio",
            "-of",
            "json",
            path,
        ],
        text=True,
        timeout=40,
    )
    stream = json.loads(raw)["streams"][0]
    width = int(stream["width"])
    height = int(stream["height"])
    sar = stream.get("sample_aspect_ratio") or "1:1"
    if sar not in ("1:1", "0:1", "N/A", ""):
        num, den = sar.split(":")
        if int(den):
            width = int(round(width * int(num) / int(den)))
    return width, height


def trim_and_store(category, topic, clip):
    source_id = str(clip["sourceId"])
    filename = safe_filename(f"{slugify(topic)}-{source_id}.mp4")
    asset_id = str(uuid.uuid4())
    category_slug = slugify(category)
    topic_slug = slugify(topic)
    key = f"media/{category_slug}/{topic_slug}/{asset_id}/{filename}"
    with tempfile.TemporaryDirectory() as folder:
        raw = os.path.join(folder, "raw.mp4")
        cut = os.path.join(folder, filename)
        request = urllib.request.Request(
            clip["url"],
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(request, timeout=120) as response, open(raw, "wb") as handle:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
        width, height = frame_size(raw)
        if not is_landscape_16_9(width, height):
            raise SkipClip(f"{width}x{height}")
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                raw,
                "-t",
                "6",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "28",
                "-threads",
                "1",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-movflags",
                "+faststart",
                cut,
            ],
            capture_output=True,
        )
        if result.returncode != 0:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    raw,
                    "-t",
                    "6",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "28",
                    "-threads",
                    "1",
                    "-movflags",
                    "+faststart",
                    cut,
                ],
                capture_output=True,
            )
        if result.returncode != 0 or not os.path.exists(cut):
            raise RuntimeError(result.stderr.decode()[-300:])
        s3.upload_file(cut, BUCKET, key, ExtraArgs={"ContentType": "video/mp4"})
        size = os.path.getsize(cut)
    title = (clip.get("title") or topic).strip()
    if len(title) > 140:
        title = title[:137] + "..."
    return {
        "id": asset_id,
        "category": category,
        "topic": topic,
        "title": title,
        "filename": filename,
        "content_type": "video/mp4",
        "size_bytes": size,
        "r2_key": key,
        "kind": "clip",
        "tags": [f"source:{source_id}"],
    }


def release(source_id):
    with sources_lock:
        used_sources.discard(source_id)


def fill_topic(category, hint, topic, target, already=0):
    target = max(0, target - already)
    if target == 0:
        log(f"{category} / {topic}: already full")
        return 0
    stored = []
    for _round in range(6):
        if len(stored) >= target:
            break
        clips = collect(topic, hint, target - len(stored))
        if not clips:
            break
        for clip in clips:
            source_id = str(clip["sourceId"])
            try:
                stored.append(trim_and_store(category, topic, clip))
            except SkipClip as exc:
                with open(os.path.join(STATE, "rejected.txt"), "a") as handle:
                    handle.write(source_id + "\n")
                log(f"skip not 16:9 {topic} {source_id} {exc}")
            except Exception as exc:
                release(source_id)
                log(f"clip failed {topic} {source_id} {exc}")
            if len(stored) >= target:
                break
    if stored:
        for start in range(0, len(stored), 20):
            batch = stored[start : start + 20]
            for attempt in range(4):
                try:
                    library_json("POST", "/api/clips/import", {"assets": batch})
                    break
                except Exception as exc:
                    log(f"import retry {attempt} {topic} {exc}")
                    time.sleep(2 + attempt)
            else:
                for item in batch:
                    release(item["tags"][0].split(":", 1)[1])
    with stats_lock:
        stats["stored"] += len(stored)
        stats["topics"] += 1
        done = stats["stored"]
        topics_done = stats["topics"]
    log(f"{category} / {topic}: {len(stored)}/{target}  total {done} across {topics_done} topics")
    with open(os.path.join(STATE, "progress.jsonl"), "a") as handle:
        handle.write(json.dumps({"category": category, "topic": topic, "stored": len(stored), "target": target}) + "\n")
    return len(stored)


def ensure_catalog():
    for category, _hint, topics in PLAN:
        library_json("POST", "/api/categories", {"name": category, "description": "Stock footage library"})
        names = [name for name, _target in topics]
        for start in range(0, len(names), 40):
            library_json("POST", "/api/topics", {"category": category, "topics": names[start : start + 40]})
        log(f"catalog ready {category} ({len(names)} topics)")


def prune_existing():
    offset = 0
    clips = []
    while True:
        page = library_json("GET", f"/api/clips?limit=200&offset={offset}")["clips"]
        if not page:
            break
        clips.extend(page)
        offset += len(page)
        if len(page) < 200:
            break
    log(f"checking aspect of {len(clips)} clips")

    def judge(clip):
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET, "Key": clip["r2_key"]},
            ExpiresIn=3600,
        )
        try:
            width, height = frame_size(url)
        except Exception as exc:
            return clip, f"probe {exc}"
        if is_landscape_16_9(width, height):
            return clip, None
        return clip, f"{width}x{height}"

    removed = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for clip, reason in pool.map(judge, clips):
            if not reason:
                continue
            try:
                library_json("DELETE", f"/api/clips/{clip['id']}")
            except Exception as exc:
                log(f"delete failed {clip['id']} {exc}")
                continue
            removed += 1
            source = next((tag.split(":", 1)[1] for tag in clip.get("tags") or [] if tag.startswith("source:")), "")
            if source:
                with open(os.path.join(STATE, "rejected.txt"), "a") as handle:
                    handle.write(source + "\n")
            log(f"removed {clip['topic']['name']} {clip['filename']} {reason}")
    log(f"removed {removed} clips that were not landscape 16:9")


def main():
    if "--prune" in sys.argv:
        prune_existing()
        return
    load_existing_sources()
    ensure_catalog()
    existing = {}
    for category, _hint, _topics in PLAN:
        payload = library_json("GET", f"/api/topics?category={slugify(category)}")
        for topic in payload["topics"]:
            existing[(category, topic["name"])] = topic.get("clips") or 0
    jobs = [
        (category, hint, topic, target, existing.get((category, topic), 0))
        for category, hint, topics in PLAN
        for topic, target in topics
    ]
    remaining = sum(max(0, target - already) for _c, _h, _t, target, already in jobs)
    log(f"starting {len(jobs)} topics, {remaining} clips still needed, workers {WORKERS}")
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(fill_topic, *job) for job in jobs]
        for future in as_completed(futures):
            future.result()
    log(f"finished stored {stats['stored']}")


if __name__ == "__main__":
    main()
