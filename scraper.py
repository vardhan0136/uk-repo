"""
UK TV EPG Scraper
-----------------
Fetches today's & tomorrow's schedules from the Freeview EPG XML feed,
converts all times to UK local time (handles GMT/BST automatically),
and saves one JSON file per channel to the schedule/ directory.

Designed to run via GitHub Actions — git commit/push is handled by the workflow.

Requirements:  Python 3.9+  (no external packages needed — all stdlib)
"""

import json
import os
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# ── Config ────────────────────────────────────────────────────────────────────
EPG_URL      = "https://raw.githubusercontent.com/dp247/Freeview-EPG/master/epg.xml"
SCHEDULE_DIR = os.path.join(os.path.dirname(__file__), "schedule")
UK_TZ        = ZoneInfo("Europe/London")   # auto-handles GMT & BST
# ─────────────────────────────────────────────────────────────────────────────


# ── Time helpers ──────────────────────────────────────────────────────────────

def parse_epg_time(raw: str) -> datetime:
    """
    Parse EPG timestamps like '20260218220000 +0000' or '20260218220000 +0100'.
    Returns a UTC-aware datetime.
    """
    raw = raw.strip()
    if " " in raw:
        dt_part, tz_part = raw.split(" ", 1)
        sign  = 1 if tz_part[0] == "+" else -1
        tz_h  = int(tz_part[1:3])
        tz_m  = int(tz_part[3:5])
        off   = timezone(timedelta(hours=sign * tz_h, minutes=sign * tz_m))
    else:
        dt_part = raw
        off     = timezone.utc

    dt = datetime.strptime(dt_part, "%Y%m%d%H%M%S").replace(tzinfo=off)
    return dt.astimezone(timezone.utc)


def to_uk(dt_utc: datetime) -> datetime:
    """Convert a UTC datetime to UK local time."""
    return dt_utc.astimezone(UK_TZ)


def fmt_time(dt_utc: datetime) -> str:
    """Return just HH:MM in UK local time, e.g. '22:00'."""
    return to_uk(dt_utc).strftime("%H:%M")


def fmt_date(dt_utc: datetime) -> str:
    """Return date as 'D Month YYYY' in UK local time, e.g. '19 February 2026'."""
    return to_uk(dt_utc).strftime("%-d %B %Y")


def uk_date_key(dt_utc: datetime) -> str:
    """Return YYYY-MM-DD in UK local time (used for grouping/filtering)."""
    return to_uk(dt_utc).strftime("%Y-%m-%d")


def get_target_dates() -> tuple[str, str]:
    """Return (today, tomorrow) as YYYY-MM-DD strings in UK local time."""
    now      = datetime.now(UK_TZ)
    today    = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    return today, tomorrow


# ── File helpers ──────────────────────────────────────────────────────────────

def safe_filename(name: str) -> str:
    """Strip characters that are invalid in filenames."""
    for ch in r'/\:*?"<>|':
        name = name.replace(ch, "-")
    return name.strip()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    today, tomorrow = get_target_dates()
    target_dates    = {today, tomorrow}

    print(f"\n📺 UK EPG Scraper")
    print(f"   Targeting: {today} and {tomorrow} (UK time)\n")

    # ── Fetch XML ─────────────────────────────────────────────────────────────
    print("⬇  Fetching EPG XML …")
    with urllib.request.urlopen(EPG_URL) as resp:
        xml_bytes = resp.read()
    print(f"   Downloaded {len(xml_bytes) / 1024:.0f} KB\n")

    # ── Parse XML ─────────────────────────────────────────────────────────────
    print("🔍 Parsing XML …")
    root     = ET.fromstring(xml_bytes)
    channels = root.findall("channel")
    programs = root.findall("programme")
    print(f"   Channels:   {len(channels)}")
    print(f"   Programmes: {len(programs)}\n")

    # ── Build channel map {id → {name, logo}} ─────────────────────────────────
    channel_map: dict[str, dict] = {}
    for ch in channels:
        ch_id = (ch.get("id") or "").strip()
        if not ch_id:
            continue
        name_el = ch.find("display-name")
        icon_el = ch.find("icon")
        channel_map[ch_id] = {
            "channel_name": (name_el.text or ch_id).strip() if name_el is not None else ch_id,
            "channel_logo": (icon_el.get("src") or "") if icon_el is not None else "",
        }

    # ── Collect programmes per channel, grouped by UK date ───────────────────
    # schedule[channel_id][date_key] = [ ...entries ]
    schedule: dict[str, dict[str, list]] = {}

    for prog in programs:
        ch_id     = (prog.get("channel") or "").strip()
        start_raw = prog.get("start") or ""
        stop_raw  = prog.get("stop")  or ""

        if not ch_id or ch_id not in channel_map or not start_raw:
            continue

        try:
            start_utc = parse_epg_time(start_raw)
            stop_utc  = parse_epg_time(stop_raw) if stop_raw else None
        except Exception:
            continue

        date_key = uk_date_key(start_utc)
        if date_key not in target_dates:
            continue

        title_el = prog.find("title")
        desc_el  = prog.find("desc")
        icon_el  = prog.find("icon")

        entry = {
            "show_name":        (title_el.text or "").strip() if title_el is not None else "",
            "show_description": (desc_el.text  or "").strip() if desc_el  is not None else "",
            "show_logo":        (icon_el.get("src") or "")    if icon_el  is not None else "",
            "start_time":       fmt_time(start_utc),
            "end_time":         fmt_time(stop_utc) if stop_utc else "",
        }

        schedule.setdefault(ch_id, {}).setdefault(date_key, []).append(entry)

    # ── Write one JSON file per channel ───────────────────────────────────────
    os.makedirs(SCHEDULE_DIR, exist_ok=True)

    saved = skipped = 0

    for ch_id, ch_info in channel_map.items():
        ch_schedule = schedule.get(ch_id, {})
        total       = sum(len(v) for v in ch_schedule.values())

        if total == 0:
            skipped += 1
            continue

        # Build sorted day list
        days = []
        for date_key in sorted(ch_schedule.keys()):
            progs = ch_schedule[date_key]
            # Use the first programme's UTC start to get the formatted date label
            days.append({
                "date":       fmt_date(parse_epg_time(
                    # re-derive from date_key since we already have entries
                    # just use date_key to build a noon UTC time for formatting
                    date_key.replace("-", "") + "120000 +0000"
                )),
                "programmes": progs,
            })

        output = {
            "channel_name": ch_info["channel_name"],
            "channel_logo": ch_info["channel_logo"],
            "schedule":     days,
        }

        file_name = f"{safe_filename(ch_info['channel_name'])}.json"
        file_path = os.path.join(SCHEDULE_DIR, file_name)

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

        print(f"  ✓  schedule/{file_name}  ({total} programmes)")
        saved += 1

    print(f"\n✅ Done! Saved {saved} channel files. Skipped {skipped} channels with no data.")


if __name__ == "__main__":
    main()
