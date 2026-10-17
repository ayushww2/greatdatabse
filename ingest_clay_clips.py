"""Add 20 landscape clips, about 6 seconds each, to every Clay Mysteries topic.

Each file stays at 720p, or at its original size when that is already 720p or smaller.
A CLIP vision check keeps only frames that match the topic. Original stills are not touched.
"""

import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import boto3
from botocore.client import Config

from library import is_landscape_16_9, preview_object_key, safe_filename, slugify


def clay_topics():
    path = os.path.join(os.path.dirname(__file__), "generate_review_stills.py")
    with open(path) as handle:
        text = handle.read()
    start = text.index("CLAY = [")
    end = text.index("]\n", start)
    return re.findall(r'\("([^"]+)"', text[start:end])

STOCK = os.environ.get("STOCK_API", "https://stock-clips-api-production.up.railway.app")
LIBRARY = os.environ["LIBRARY_API"].rstrip("/")
LIBRARY_KEY = os.environ["LIBRARY_API_KEY"]
BUCKET = os.environ.get("R2_BUCKET", "greatdatabse")
CATEGORY = "Clay Mysteries"
TARGET = int(os.environ.get("CLAY_CLIP_TARGET", "20"))
WORKERS = int(os.environ.get("CLAY_CLIP_WORKERS", "4"))
STATE = os.environ.get("CLAY_CLIP_STATE", "/tmp/clay-clips")

os.makedirs(STATE, exist_ok=True)
sources_lock = threading.Lock()
used_sources = set()
vision_lock = threading.Lock()
vision_model = None
stats_lock = threading.Lock()
stats = {"stored": 0, "rejected": 0, "topics": 0}
s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["R2_ENDPOINT"],
    aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    config=Config(signature_version="s3v4", max_pool_connections=16),
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
    with urllib.request.urlopen(req, timeout=180) as response:
        body = response.read().decode()
        return json.loads(body) if body else {}


def load_existing_sources():
    offset = 0
    while True:
        payload = library_json("GET", f"/api/clips?limit=200&offset={offset}")
        clips = payload.get("clips") or []
        if not clips:
            break
        for clip in clips:
            for tag in clip.get("tags") or []:
                if tag.startswith("source:"):
                    used_sources.add(tag.split(":", 1)[1])
        offset += len(clips)
        if len(clips) < 200:
            break
    path = os.path.join(STATE, "rejected.txt")
    if os.path.exists(path):
        with open(path) as handle:
            used_sources.update(line.strip() for line in handle if line.strip())
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
                        return []
                    return job.get("clips") or []
                time.sleep(1.5)
            return []
        except Exception as exc:
            log(f"stock retry {attempt} {query!r} {exc}")
            time.sleep(1 + attempt)
    return []


def claim(source_id):
    with sources_lock:
        if not source_id or source_id in used_sources:
            return False
        used_sources.add(source_id)
        return True


def release(source_id):
    with sources_lock:
        used_sources.discard(source_id)


def reject(source_id, reason):
    with open(os.path.join(STATE, "rejected.txt"), "a") as handle:
        handle.write(source_id + "\n")
    log(f"skip {reason} {source_id}")


def frame_size(path):
    raw = subprocess.check_output(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,sample_aspect_ratio", "-of", "json", path,
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


def load_vision():
    global vision_model
    with vision_lock:
        if vision_model is not None:
            return vision_model
        import torch
        from transformers import CLIPModel, CLIPTokenizer

        model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
        tokenizer = CLIPTokenizer.from_pretrained("openai/clip-vit-base-patch32")
        model.eval()
        vision_model = (model, tokenizer, torch)
        log("vision model ready")
        return vision_model


def clip_image(path):
    from PIL import Image

    image = Image.open(path).convert("RGB")
    image = image.resize((224, 224), Image.Resampling.BICUBIC)
    pixels = list(image.getdata())
    mean = (0.48145466, 0.4578275, 0.40821073)
    std = (0.26862954, 0.26130258, 0.27577711)
    plane = [[], [], []]
    for red, green, blue in pixels:
        plane[0].append(((red / 255) - mean[0]) / std[0])
        plane[1].append(((green / 255) - mean[1]) / std[1])
        plane[2].append(((blue / 255) - mean[2]) / std[2])
    model, _tokenizer, torch = load_vision()
    tensor = torch.tensor(plane, dtype=torch.float32).view(1, 3, 224, 224)
    return model, tensor


OTHER_SUBJECTS = (
    "aztec", "mayan", "maya", "egypt", "egyptian", "hieroglyph", "templar",
    "indonesia", "roman", "greek", "inca", "norse", "viking", "chinese", "japanese",
)


def title_conflicts(topic, title):
    text = (title or "").lower()
    topic_text = topic.lower()
    if any(word in text and word not in topic_text for word in OTHER_SUBJECTS):
        return True
    if any(word in text for word in ("mortar", "bricklaying", "construction site")):
        return True
    return "book" in text and "tablet" in topic_text


def subject_prompt(topic):
    if "tablet" in topic.lower() or "cuneiform" in topic.lower():
        return "a clay tablet covered in cuneiform wedge-shaped writing"
    return f"close-up documentary footage of {topic}"


def frame_matches(path, topic):
    model, tensor = clip_image(path)
    _model, tokenizer, torch = load_vision()
    prompts = [
        subject_prompt(topic),
        "modern red construction bricks with wet mortar",
        "an Aztec or Mayan carved stone calendar",
        "Egyptian hieroglyphs on a temple wall",
        "a printed book page",
        "a completely different subject",
    ]
    tokens = tokenizer(prompts, padding=True, return_tensors="pt")
    with vision_lock:
        with torch.no_grad():
            output = model(pixel_values=tensor, **tokens)
            probs = output.logits_per_image.softmax(dim=-1)[0]
    score = float(probs[0])
    return int(probs.argmax()) == 0 and score >= 0.45, score


def prepare_clip(topic, clip):
    source_id = str(clip["sourceId"])
    filename = safe_filename(f"{slugify(topic)}-{source_id}.mp4")
    with tempfile.TemporaryDirectory() as folder:
        raw = os.path.join(folder, "raw.mp4")
        cut = os.path.join(folder, filename)
        frame = os.path.join(folder, "frame.jpg")
        request = urllib.request.Request(clip["url"], headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=120) as response, open(raw, "wb") as handle:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
        width, height = frame_size(raw)
        if not is_landscape_16_9(width, height):
            raise RuntimeError(f"aspect {width}x{height}")
        encoded = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-i", raw, "-t", "6",
                "-vf", "scale=-2:'min(720,ih)'",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-threads", "1",
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", cut,
            ],
            capture_output=True,
        )
        if encoded.returncode != 0:
            encoded = subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error", "-i", raw, "-t", "6", "-an",
                    "-vf", "scale=-2:'min(720,ih)'",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-threads", "1",
                    "-movflags", "+faststart", cut,
                ],
                capture_output=True,
            )
        if encoded.returncode != 0 or not os.path.exists(cut):
            raise RuntimeError(encoded.stderr.decode()[-240:])
        for mark in ("0.5", "3"):
            grabbed = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-ss", mark, "-i", cut, "-frames:v", "1", frame],
                capture_output=True,
            )
            if grabbed.returncode != 0 or not os.path.exists(frame):
                raise RuntimeError("frame")
            matched, score = frame_matches(frame, topic)
            if not matched:
                raise RuntimeError(f"vision {score:.2f}")
        out_w, out_h = frame_size(cut)
        asset_id = str(uuid.uuid4())
        key = f"media/{slugify(CATEGORY)}/{slugify(topic)}/{asset_id}/{filename}"
        s3.upload_file(cut, BUCKET, key, ExtraArgs={"ContentType": "video/mp4"})
        s3.upload_file(frame, BUCKET, preview_object_key(key), ExtraArgs={"ContentType": "image/jpeg"})
        size = os.path.getsize(cut)
    title = (clip.get("title") or topic).strip()
    return {
        "id": asset_id,
        "category": CATEGORY,
        "topic": topic,
        "kind": "clip",
        "title": title[:140],
        "filename": filename,
        "content_type": "video/mp4",
        "size_bytes": size,
        "r2_key": key,
        "tags": [f"source:{source_id}", "vision:match", f"{out_w}x{out_h}"],
        "created_at": "",
    }


def collect(topic, need):
    short = " ".join(topic.split()[-2:])
    queries = [
        topic,
        short,
        f"ancient {short}",
        f"{short} close up",
        f"{short} landscape",
        f"{topic} documentary",
        f"real {short}",
        f"{short} museum",
    ]
    if "tablet" in topic.lower():
        queries = [
            "cuneiform clay tablet",
            "cuneiform tablet",
            "mesopotamian cuneiform tablet",
            "ancient cuneiform writing clay",
            "archaeological cuneiform tablet",
            *queries,
        ]
    chosen = []
    for query in queries:
        if len(chosen) >= need:
            break
        for clip in stock_job(query, min(10, max(need, 4))):
            source_id = str(clip.get("sourceId") or "")
            if float(clip.get("duration") or 0) < 5 or not clip.get("url"):
                continue
            if not claim(source_id):
                continue
            chosen.append(clip)
            if len(chosen) >= need:
                break
    return chosen


def fill_topic(topic, already=0):
    needed = max(0, TARGET - already)
    stored = []
    for _round in range(4):
        if len(stored) >= needed:
            break
        clips = collect(topic, needed - len(stored) + 4)
        if not clips:
            break
        for clip in clips:
            source_id = str(clip["sourceId"])
            if title_conflicts(topic, clip.get("title")):
                reject(source_id, f"{topic} title")
                continue
            try:
                item = prepare_clip(topic, clip)
            except Exception as exc:
                reject(source_id, f"{topic} {exc}")
                continue
            index = len(stored) + 1
            stamp = datetime(2026, 10, 17, tzinfo=timezone.utc) + timedelta(seconds=index)
            item["created_at"] = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
            stored.append(item)
            if len(stored) >= needed:
                break
        pending = [item for item in stored if not item.get("imported")]
        for start in range(0, len(pending), 10):
            batch = pending[start:start + 10]
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
                raise RuntimeError(f"import failed {topic}")
            for item in batch:
                item["imported"] = True
    with stats_lock:
        stats["stored"] += len(stored)
        stats["topics"] += 1
        done = stats["stored"]
        topics_done = stats["topics"]
    log(f"{topic}: {already + len(stored)}/{TARGET} total {done} across {topics_done} topics")
    with open(os.path.join(STATE, "progress.jsonl"), "a") as handle:
        handle.write(json.dumps({"topic": topic, "stored": already + len(stored), "target": TARGET}) + "\n")
    return len(stored)


def main():
    load_vision()
    load_existing_sources()
    library_json("POST", "/api/categories", {"name": CATEGORY, "description": "Clay Mysteries footage and stills"})
    names = clay_topics()
    for start in range(0, len(names), 40):
        library_json("POST", "/api/topics", {"category": CATEGORY, "topics": names[start:start + 40]})
    existing = {
        topic["name"]: topic.get("clips") or 0
        for topic in library_json("GET", f"/api/topics?category={slugify(CATEGORY)}")["topics"]
    }
    only = os.environ.get("CLAY_ONLY_TOPIC", "").strip()
    if only:
        names = [name for name in names if name.lower() == only.lower()]
    pending = [name for name in names if existing.get(name, 0) < TARGET]
    log(f"starting {len(pending)} topics, {TARGET} clips each, workers {WORKERS}")
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(fill_topic, name, existing.get(name, 0)) for name in pending]
        for future in as_completed(futures):
            future.result()
    log(f"finished stored {stats['stored']}")


if __name__ == "__main__":
    main()
