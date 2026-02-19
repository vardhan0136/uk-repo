"""
UK TV EPG Image Downloader
--------------------------
Reads all JSON files from the schedule/ directory (created by scraper.py),
downloads channel logos and show logos, converts them to WebP, compresses
show logos to under 10 KB, and saves them to downloaded-images/.

Also updates the JSON files to replace the original image URLs with the
new local URLs based on the GitHub repo structure.

If any image fails to download, a placeholder image is generated using
the initials of the channel/show name.

Structure:
  downloaded-images/{Channel Name}/{channel-name}.webp     ← channel logo
  downloaded-images/{Channel Name}/{show-name}.webp        ← show logo

Base URL for JSON replacement:
  http://uk-schedule.local/wp-content/uploads/downloaded-images/{Channel Name}/{show-name}.webp

Requirements:
  pip install Pillow
  (urllib, json, os, pathlib, concurrent.futures are all stdlib)

Run:
  python download_images.py
"""

import json
import os
import re
import urllib.request
import urllib.error
import io
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image, ImageDraw, ImageFont

# ── Config ────────────────────────────────────────────────────────────────────
SCHEDULE_DIR   = Path("schedule")
IMAGES_DIR     = Path("downloaded-images")
BASE_URL       = "https://example.com/wp-content/uploads/downloaded-images"
MAX_WORKERS    = 25          # parallel download threads
SHOW_MAX_KB    = 10          # compress show logos to under this
SHOW_MAX_BYTES = SHOW_MAX_KB * 1024
REQUEST_TIMEOUT = 15         # seconds per download attempt
RETRY_ATTEMPTS  = 2          # retry failed downloads once
# ─────────────────────────────────────────────────────────────────────────────


# ── Filename helpers ──────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    """Convert a name to a lowercase hyphenated slug for filenames."""
    name = name.lower().strip()
    name = re.sub(r"[^\w\s-]", "", name)       # remove special chars
    name = re.sub(r"[\s_]+", "-", name)         # spaces → hyphens
    name = re.sub(r"-+", "-", name)             # collapse multiple hyphens
    return name.strip("-")


def get_initials(name: str, max_chars: int = 4) -> str:
    """
    Generate initials from a name.
    'Naked And Afraid' → 'NAA'
    'Teleshopping'     → 'TELE'
    """
    words = name.strip().split()
    if len(words) == 1:
        return words[0][:max_chars].upper()
    return "".join(w[0] for w in words if w).upper()[:max_chars]


# ── Image helpers ─────────────────────────────────────────────────────────────

def download_image(url: str) -> bytes | None:
    """Download image bytes from a URL with retries. Returns None on failure."""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; EPG-Image-Bot/1.0)",
        "Accept":     "image/*,*/*",
    }
    for attempt in range(RETRY_ATTEMPTS):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return resp.read()
        except Exception as e:
            if attempt < RETRY_ATTEMPTS - 1:
                time.sleep(1)
            else:
                return None
    return None


def make_placeholder(initials: str, size: int = 200) -> Image.Image:
    """
    Generate a simple square placeholder image with the given initials.
    Uses a dark background with white text.
    """
    img  = Image.new("RGB", (size, size), color=(45, 45, 55))
    draw = ImageDraw.Draw(img)

    # Try to use a basic font, fall back to default
    font_size = size // (len(initials) + 1) + 10
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except Exception:
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
        except Exception:
            font = ImageFont.load_default()

    # Centre the text
    bbox = draw.textbbox((0, 0), initials, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (size - text_w) / 2
    y = (size - text_h) / 2
    draw.text((x, y), initials, fill=(255, 255, 255), font=font)

    return img


def to_webp_channel(raw_bytes: bytes) -> bytes:
    """Convert image bytes to WebP (no compression — lossless, high quality)."""
    img = Image.open(io.BytesIO(raw_bytes)).convert("RGBA")
    buf = io.BytesIO()
    img.save(buf, format="WEBP", lossless=True, quality=100)
    return buf.getvalue()


def to_webp_show(raw_bytes: bytes) -> bytes:
    """
    Convert image bytes to WebP and compress to under SHOW_MAX_BYTES.
    Tries decreasing quality levels until the file is small enough.
    """
    img = Image.open(io.BytesIO(raw_bytes)).convert("RGBA")

    # Resize if very large (speeds up compression significantly)
    max_dim = 400
    if img.width > max_dim or img.height > max_dim:
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)

    # Try quality levels from 85 down to 10
    for quality in range(85, 5, -5):
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=quality, method=6)
        if buf.tell() <= SHOW_MAX_BYTES:
            return buf.getvalue()

    # Last resort: shrink the image dimensions further
    for scale in [0.75, 0.5, 0.35, 0.25]:
        small = img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
            Image.LANCZOS,
        )
        buf = io.BytesIO()
        small.save(buf, format="WEBP", quality=10, method=6)
        if buf.tell() <= SHOW_MAX_BYTES:
            return buf.getvalue()

    # Return whatever we have at minimum quality
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=5, method=6)
    return buf.getvalue()


def placeholder_webp(initials: str, is_show: bool) -> bytes:
    """Generate a placeholder and return as WebP bytes."""
    img = make_placeholder(initials)
    buf = io.BytesIO()
    if is_show:
        img.thumbnail((200, 200), Image.LANCZOS)
        img.save(buf, format="WEBP", quality=40, method=6)
    else:
        img.save(buf, format="WEBP", lossless=True)
    return buf.getvalue()


# ── Download task ─────────────────────────────────────────────────────────────

def process_image(task: dict) -> dict:
    """
    Download (or generate placeholder for) a single image.
    Returns a result dict with status info.

    task keys:
        url         – original image URL (may be empty string)
        dest_path   – Path to save the .webp file
        initials    – fallback text for placeholder
        is_channel  – True = no compression, lossless WebP
    """
    dest_path  = task["dest_path"]
    url        = task["url"]
    initials   = task["initials"]
    is_channel = task["is_channel"]

    # Already exists — skip download
    if dest_path.exists():
        return {"dest_path": dest_path, "status": "skipped"}

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    used_placeholder = False

    if url:
        raw = download_image(url)
    else:
        raw = None

    if raw:
        try:
            if is_channel:
                webp_bytes = to_webp_channel(raw)
            else:
                webp_bytes = to_webp_show(raw)
        except Exception:
            webp_bytes = None

        if webp_bytes is None:
            webp_bytes = placeholder_webp(initials, is_show=not is_channel)
            used_placeholder = True
    else:
        webp_bytes = placeholder_webp(initials, is_show=not is_channel)
        used_placeholder = True

    dest_path.write_bytes(webp_bytes)

    size_kb = len(webp_bytes) / 1024
    return {
        "dest_path":       dest_path,
        "status":          "placeholder" if used_placeholder else "ok",
        "size_kb":         size_kb,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def build_new_url(dest_path: Path) -> str:
    """Build the replacement URL for the JSON file."""
    # dest_path is like: downloaded-images/DMAX/naked-and-afraid.webp
    # We want: https://example.com/wp-content/uploads/downloaded-images/DMAX/naked-and-afraid.webp
    rel = dest_path.as_posix()   # always forward slashes
    return f"{BASE_URL.rstrip('/')}/{rel.lstrip('downloaded-images/').lstrip('/')}"


def build_url_from_parts(channel_name: str, filename: str) -> str:
    """Build replacement URL from channel folder name and filename."""
    return f"{BASE_URL.rstrip('/')}/{channel_name}/{filename}"


def main():
    print("\n🖼  UK TV EPG Image Downloader")
    print(f"   Threads: {MAX_WORKERS}  |  Show max size: {SHOW_MAX_KB} KB\n")

    if not SCHEDULE_DIR.exists():
        print(f"❌ schedule/ directory not found. Run scraper.py first.")
        return

    json_files = sorted(SCHEDULE_DIR.glob("*.json"))
    if not json_files:
        print("❌ No JSON files found in schedule/")
        return

    print(f"📂 Found {len(json_files)} channel JSON files\n")

    # ── Pass 1: Collect all tasks ─────────────────────────────────────────────
    # We track (channel_folder, slug) to avoid downloading the same image twice
    seen: set[tuple[str, str]] = set()
    tasks: list[dict]          = []

    # We'll build a mapping: original_url → new_url (for JSON patching later)
    url_map: dict[str, str] = {}

    all_data: list[tuple[Path, dict]] = []   # (json_path, parsed_data)

    for json_path in json_files:
        try:
            with open(json_path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"  ⚠  Could not read {json_path.name}: {e}")
            continue

        all_data.append((json_path, data))

        channel_name   = data.get("channel_name", json_path.stem)
        channel_logo   = data.get("channel_logo", "")
        channel_folder = channel_name          # keep original name for folder
        channel_slug   = slugify(channel_name)

        # ── Channel logo ──────────────────────────────────────────────────────
        ch_filename  = f"{channel_slug}.webp"
        ch_dest_path = IMAGES_DIR / channel_folder / ch_filename
        ch_key       = (channel_folder, channel_slug)

        if ch_key not in seen:
            seen.add(ch_key)
            tasks.append({
                "url":        channel_logo,
                "dest_path":  ch_dest_path,
                "initials":   get_initials(channel_name),
                "is_channel": True,
            })

        if channel_logo:
            url_map[channel_logo] = build_url_from_parts(channel_folder, ch_filename)

        # ── Show logos ────────────────────────────────────────────────────────
        for day in data.get("schedule", []):
            for prog in day.get("programmes", []):
                show_name = prog.get("show_name", "")
                show_logo = prog.get("show_logo", "")
                if not show_name:
                    continue

                show_slug    = slugify(show_name)
                show_filename = f"{show_slug}.webp"
                show_dest    = IMAGES_DIR / channel_folder / show_filename
                show_key     = (channel_folder, show_slug)

                if show_key not in seen:
                    seen.add(show_key)
                    tasks.append({
                        "url":        show_logo,
                        "dest_path":  show_dest,
                        "initials":   get_initials(show_name),
                        "is_channel": False,
                    })

                # Always map the original URL → new URL (even if task already seen)
                if show_logo:
                    url_map[show_logo] = build_url_from_parts(channel_folder, show_filename)

    print(f"📋 Tasks: {len(tasks)} images to process ({len(seen)} unique)\n")

    # ── Pass 2: Download / generate in parallel ───────────────────────────────
    ok = skipped = placeholder = failed = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_image, task): task for task in tasks}

        for future in as_completed(futures):
            try:
                result = future.result()
                status = result["status"]
                name   = result["dest_path"].name

                if status == "skipped":
                    skipped += 1
                elif status == "placeholder":
                    placeholder += 1
                    print(f"  🔲 placeholder  {result['dest_path'].parent.name}/{name}")
                else:
                    size_str = f"{result['size_kb']:.1f} KB"
                    ok += 1
                    print(f"  ✓  {result['dest_path'].parent.name}/{name}  ({size_str})")
            except Exception as e:
                failed += 1
                print(f"  ✗  Task failed: {e}")

    print(f"\n📊 Results: {ok} downloaded | {skipped} skipped | {placeholder} placeholders | {failed} errors\n")

    # ── Pass 3: Update JSON files with new URLs ───────────────────────────────
    print("✏️  Updating JSON files with new image URLs …")
    updated_files = 0

    for json_path, data in all_data:
        changed = False

        channel_name   = data.get("channel_name", json_path.stem)
        channel_folder = channel_name
        channel_slug   = slugify(channel_name)
        ch_filename    = f"{channel_slug}.webp"

        # Update channel logo
        old_logo = data.get("channel_logo", "")
        new_logo = build_url_from_parts(channel_folder, ch_filename)
        if data.get("channel_logo") != new_logo:
            data["channel_logo"] = new_logo
            changed = True

        # Update show logos
        for day in data.get("schedule", []):
            for prog in day.get("programmes", []):
                show_name = prog.get("show_name", "")
                show_logo = prog.get("show_logo", "")
                if not show_name:
                    continue

                show_slug     = slugify(show_name)
                show_filename = f"{show_slug}.webp"
                new_show_url  = build_url_from_parts(channel_folder, show_filename)

                if prog.get("show_logo") != new_show_url:
                    prog["show_logo"] = new_show_url
                    changed = True

        if changed:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            updated_files += 1
            print(f"  ✓  Updated {json_path.name}")

    print(f"\n✅ Done! {updated_files} JSON files updated with new image URLs.")


if __name__ == "__main__":
    main()
