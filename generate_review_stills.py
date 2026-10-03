"""Generate review stills with ElevenLabs and store them on R2, outside the website library."""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.client import Config

from clip_plan import PLAN
from image_settings import ASPECT_RATIO, MODEL_ID, QUALITY, RESOLUTION
from library import slugify

API = "https://api.elevenlabs.io/v1/flows/image"
KEY = os.environ["ELEVENLABS_API_KEY"]
BUCKET = os.environ.get("R2_BUCKET", "greatdatabse")
PREFIX = "review-stills"
STATE = os.environ.get("STILLS_STATE", "/tmp/stills-review")
WORKERS = int(os.environ.get("STILLS_WORKERS", "80"))
BATCH = 500
STYLE = (
    "Photorealistic documentary photograph, shot on a full-frame camera, natural light, "
    "sharp real-world detail, landscape framing. Real place, not a painting, not CGI, "
    "not a 3D render, not an illustration. No text, no watermark, no logo."
)
VARIANTS = [
    "wide establishing shot",
    "medium shot",
    "close detail of texture",
    "low angle",
    "high angle",
    "shot at dawn",
    "shot at dusk",
    "soft overcast daylight",
    "harsh midday sun",
    "foreground ground with the subject behind",
]

CLAY = [
    ("Sumerian Clay Tablet", 50), ("Cylinder Seals", 20), ("Archaeologist Reading Tablet", 30),
    ("Ziggurat", 35), ("Mesopotamian Ruins", 40), ("Babylon And Ishtar Gate", 30),
    ("Lamassu And Ancient Statues", 25), ("Tigris And Euphrates", 20), ("Ancient Mud Brick City", 30),
    ("Ancient Scroll", 35), ("Ancient Bible Manuscript", 30), ("Ethiopian Manuscript", 25),
    ("Dead Sea Scrolls", 20), ("Voynich Manuscript", 20), ("Monk And Monastery", 25),
    ("Ancient Church", 30), ("Prayer", 20), ("Heaven Light And Clouds", 40),
    ("Divine Light Rays", 30), ("Fire And Brimstone", 30), ("Dark Judgment Sky", 25),
    ("Sealed Stone Door", 20), ("Ancient Underground City", 40), ("Underground Tunnel", 35),
    ("Hidden Chamber", 30), ("Stairs Into The Earth", 25), ("Cave Entrance", 30),
    ("People Sheltering Underground", 25), ("Ancient Desert Crowd", 25), ("Apocalyptic Storm", 40),
    ("Dust Storm", 25), ("Meteor And Fire In Sky", 30), ("Eclipse And Red Moon", 25),
    ("Empty Ruined City", 25), ("Dark Ocean Depths", 40), ("Seafloor", 35),
    ("Underwater Cave Mouth", 30), ("Submersible Going Down", 30), ("Ocean Trench", 30),
    ("Mars Surface", 45), ("Mars From Space", 20), ("Saturn And Rings", 35),
    ("Moon Surface", 40), ("Moon From Space", 25), ("Earth From Space", 30),
    ("Night Sky And Stars", 40), ("Deep Space", 35), ("Observatory And Telescope", 25),
    ("Radio Telescope", 20), ("Comet And Asteroid", 25), ("Sun And Solar Flare", 25), ("Aurora", 20),
    ("Dogs", 35), ("Wolves", 25), ("Cats", 35), ("Cat Eyes In Dark", 15), ("Snakes", 25),
    ("Eagles", 15), ("Prehistoric Fossils", 35), ("Dinosaur Skeleton", 30), ("Giant Bones", 15),
    ("Pyramids", 45), ("Sphinx", 35), ("Easter Island Moai", 30), ("Stonehenge", 25),
    ("Gobekli Tepe", 25), ("Nazca Lines", 25), ("Cave Paintings", 35),
    ("Ancient Symbols Carved In Stone", 30), ("Megalithic Stones", 30),
    ("Puma Punku And Tiwanaku", 15), ("Petra", 15), ("Ancient Map", 15),
    ("Ancient Burial", 30), ("Open Tomb", 25), ("Mummy", 30), ("Light In Darkness", 35),
    ("Dark Stone Corridor", 30), ("Misty River", 15), ("Ancient Garden", 15),
    ("Lights In The Night Sky", 35), ("People Looking Up", 30), ("Ancient Carving Of A Craft", 20),
    ("City Lights At Night", 20), ("Scientist At Computer", 25),
]

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["R2_ENDPOINT"],
    aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    config=Config(signature_version="s3v4", max_pool_connections=128),
    region_name="auto",
)
log_lock = threading.Lock()


def log(message):
    line = f"{time.strftime('%H:%M:%S')} {message}"
    print(line, flush=True)
    os.makedirs(STATE, exist_ok=True)
    with log_lock:
        with open(os.path.join(STATE, "stills.log"), "a") as handle:
            handle.write(line + "\n")


def allocate(rows, total_images):
    weights = [item[2] for item in rows]
    total_weight = sum(weights)
    counts = [max(4, round(weight / total_weight * total_images)) for weight in weights]
    diff = total_images - sum(counts)
    order = sorted(range(len(rows)), key=lambda index: rows[index][2], reverse=diff > 0)
    step = 0
    while diff != 0 and step < 200000:
        index = order[step % len(order)]
        if diff > 0:
            counts[index] += 1
            diff -= 1
        elif counts[index] > 4:
            counts[index] -= 1
            diff += 1
        step += 1
    return counts


def jobs():
    library_rows = [
        (category, name, target)
        for category, _hint, topics in PLAN
        if category != "Military"
        for name, target in topics
    ]
    library_counts = allocate(library_rows, 5000)
    clay_counts = allocate([("Clay Mysteries", name, target) for name, target in CLAY], 2561)
    planned = []
    for (category, name, _target), count in zip(library_rows, library_counts):
        for index in range(1, count + 1):
            planned.append((category, name, index, count))
    for (_category, name, _target), count in zip([("Clay Mysteries", name, target) for name, target in CLAY], clay_counts):
        for index in range(1, count + 1):
            planned.append(("Clay Mysteries", name, index, count))
    return planned


def prompt_for(topic, index):
    variant = VARIANTS[(index - 1) % len(VARIANTS)]
    return f"{variant.capitalize()} of {topic}. {STYLE}"


def object_key(category, topic, index):
    return f"{PREFIX}/{slugify(category)}/{slugify(topic)}/{index:04d}.png"


def already_uploaded(done):
    return set(done)


def load_done():
    path = os.path.join(STATE, "done.txt")
    if not os.path.exists(path):
        return set()
    with open(path) as handle:
        return {line.strip() for line in handle if line.strip()}


def mark_done(key):
    with log_lock:
        with open(os.path.join(STATE, "done.txt"), "a") as handle:
            handle.write(key + "\n")
        with open(os.path.join(STATE, "manifest.jsonl"), "a") as handle:
            handle.write(json.dumps({"r2_key": key, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}) + "\n")


def api(method, url, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"xi-api-key": KEY, "Content-Type": "application/json"},
    )
    for attempt in range(6):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:300]
            if exc.code in (429, 500, 502, 503, 504):
                time.sleep(2 + attempt * 2)
                continue
            raise RuntimeError(f"{exc.code} {detail}")
        except Exception:
            time.sleep(2 + attempt)
    raise RuntimeError("api retries exhausted")


def generate_one(category, topic, index, count):
    key = object_key(category, topic, index)
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return key, "exists"
    except Exception:
        pass
    prompt = prompt_for(topic, index)
    created = api("POST", API, {
        "model_id": MODEL_ID,
        "prompt": prompt,
        "aspect_ratio": ASPECT_RATIO,
        "quality": QUALITY,
        "resolution": RESOLUTION,
    })
    generation_id = created["id"]
    deadline = time.time() + 240
    while time.time() < deadline:
        data = api("GET", f"{API}/{generation_id}")
        status = data.get("status")
        if status == "completed":
            image = urllib.request.urlopen(data["content_url"], timeout=90).read()
            s3.put_object(Bucket=BUCKET, Key=key, Body=image, ContentType="image/png")
            mark_done(key)
            return key, "stored"
        if status == "failed":
            raise RuntimeError(data.get("error_message") or data.get("failure_reason") or "failed")
        time.sleep(3)
    raise RuntimeError("timeout")


def main():
    os.makedirs(STATE, exist_ok=True)
    planned = jobs()
    done = load_done()
    pending = [item for item in planned if object_key(*item[:3]) not in done]
    log(f"plan {len(planned)} stills, already stored {len(planned) - len(pending)}, workers {WORKERS}, quality {QUALITY}, resolution {RESOLUTION}")
    stored = 0
    failed = 0
    for start in range(0, len(pending), BATCH):
        batch = pending[start:start + BATCH]
        prompts_path = os.path.join(STATE, f"prompts-{start // BATCH + 1:03d}.jsonl")
        with open(prompts_path, "w") as handle:
            for category, topic, index, count in batch:
                handle.write(json.dumps({
                    "category": category,
                    "topic": topic,
                    "index": index,
                    "of": count,
                    "prompt": prompt_for(topic, index),
                    "r2_key": object_key(category, topic, index),
                }) + "\n")
        log(f"batch {start // BATCH + 1} prompts ready: {len(batch)}")
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = [pool.submit(generate_one, *item) for item in batch]
            for future in as_completed(futures):
                try:
                    key, status = future.result()
                    stored += 1
                    if stored % 25 == 0:
                        log(f"progress stored {stored} failed {failed} last {key} {status}")
                except Exception as exc:
                    failed += 1
                    log(f"image failed {exc}")
        log(f"batch {start // BATCH + 1} finished, stored {stored}, failed {failed}")
        manifest = os.path.join(STATE, "manifest.jsonl")
        if os.path.exists(manifest):
            s3.upload_file(manifest, BUCKET, f"{PREFIX}/manifest.jsonl", ExtraArgs={"ContentType": "application/json"})
    log(f"finished stored {stored} failed {failed}")


if __name__ == "__main__":
    main()
