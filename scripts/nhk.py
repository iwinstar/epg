#!/usr/bin/env python3
"""
nhk.py — NHK BS8K / BSP4K EPG → XMLTV (nhk_epg.xml.gz)

Fetches 8 days of schedule data starting from yesterday (JST) for two NHK
4K/8K channels and writes a gzip-compressed XMLTV file.

Requirements: Python 3.9+  |  pip install curl-cffi
Usage:        python nhk.py [nhk_epg.xml.gz]
"""

import gzip
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Optional
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from curl_cffi import requests

# ── Configuration ──────────────────────────────────────────────────────────────

CHANNELS: dict[str, dict] = {
    "nhk-bs8k.jp": {
        "service_id": "s6",
        "area_id": "130",
        "display_name": "NHK BS8K JP",
        "url": "https://www.nhk.or.jp",
    },
    "nhk-bsp4k.jp": {
        "service_id": "s5",
        "area_id": "130",
        "display_name": "NHK BSP4K JP",
        "url": "https://www.nhk.or.jp",
    },
}

BASE_URL = "https://api.nhk.jp/r8/pg/date/{service_id}/{area_id}/{date}.json"
DAYS = 8
OUTPUT_FILE = sys.argv[1] if len(sys.argv) > 1 else "nhk_epg.xml.gz"

# 16 total tasks (2 channels × 8 days); 8 workers = 2 concurrent rounds.
MAX_WORKERS = 8

JST = ZoneInfo("Asia/Tokyo")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Helpers ────────────────────────────────────────────────────────────────────

def xmltv_dt(iso_str: str) -> str:
    """Convert ISO 8601 timestamp to XMLTV format: '20260701055500 +0900'."""
    dt = datetime.fromisoformat(iso_str).astimezone(JST)
    return dt.strftime("%Y%m%d%H%M%S +0900")


def safe_get(d, *keys, default=None):
    """Safely traverse nested dicts; return default on any missing level or None value."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def best_eyecatch(about: dict) -> Optional[str]:
    """
    Return the best available thumbnail URL from an episode's about block.
    Falls back from episode-level to series-level eyecatch.
    """
    for path in (
        ("partOfSeries", "eyecatch", "main", "url"),
    ):
        url = safe_get(about, *path)
        if isinstance(url, str) and url:
            return url
    return None


# ── Genre / Credits ────────────────────────────────────────────────────────────

def build_categories(genre_list: Optional[list]) -> list[tuple[str, str]]:
    """
    Return deduplicated (category_text, lang) pairs from an NHK genre list.
    Both name1 (broad genre) and name2 (sub-genre) are emitted as separate
    <category> elements, which is the standard XMLTV practice.
    """
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for g in genre_list or []:
        category = f"{g.get('name1')}:{g.get('name2')}".replace(":その他", "")
        result.append((category, "ja"))
    return result


# NHK role strings → XMLTV <credits> child element names.
NHK_ROLE_MAP: dict[str, str] = {
    "出演":                 "actor",
    "主演":                 "actor",
    "声の出演":             "actor",
    "出演者":               "actor",
    "ゲスト":               "guest",
    "司会":                 "presenter",
    "アナウンサー":         "presenter",
    "スタジオアナウンサー": "presenter",
    "現地実況":             "presenter",
    "解説":                 "commentator",
    "現地解説":             "commentator",
    "スタジオ解説":         "commentator",
    "ナレーション":         "narrator",
    "ナレーター":           "narrator",
    "演出":                 "director",
    "監督":                 "director",
    "脚本":                 "writer",
    "音楽":                 "composer",
    "プロデューサー":       "producer",
}

# XMLTV DTD mandates this child-element order inside <credits>.
CREDITS_ORDER = [
    "director", "actor", "writer", "producer",
    "composer", "narrator", "presenter", "commentator", "guest",
]


def build_credits_map(act_list: list) -> dict[str, list[str]]:
    """Map NHK actList entries to XMLTV credit role buckets."""
    result: dict[str, list[str]] = {}
    for act in act_list or []:
        name = (act.get("name") or "").strip()
        if not name:
            continue
        role = NHK_ROLE_MAP.get(act.get("role", ""), "actor")
        result.setdefault(role, []).append(name)
    return result


# ── XMLTV Element Builder ──────────────────────────────────────────────────────

def make_programme(prog: dict, channel_id: str) -> ET.Element:
    """Build a single <programme> element from one NHK publication entry."""
    el = ET.Element(
        "programme",
        start=xmltv_dt(prog["startDate"]),
        stop=xmltv_dt(prog["endDate"]),
        channel=channel_id,
    )

    id_grp = prog.get("identifierGroup") or {}
    misc   = prog.get("misc") or {}
    about  = prog.get("about") or {}
    dd     = prog.get("detailedDescription") or {}

    # title: epg40 is the canonical EPG display title; fall back to name.
    title = (dd.get("epg40") or prog.get("name") or "").strip()

    # sub-title: emit episode name only when it differs from both the series
    # name and the top-level title (avoids redundant repetition).
    series_name = (id_grp.get("tvSeriesName") or "").strip()
    if series_name:
        title = series_name
    ET.SubElement(el, "title", lang="ja").text = title
    ep_name     = (id_grp.get("tvEpisodeName") or "").strip()
    if ep_name and ep_name not in {title, series_name}:
        ET.SubElement(el, "sub-title", lang="ja").text = ep_name

    # desc: longest available description wins (epg200 > epg80 > description).
    desc = (
        dd.get("epg200")
        or dd.get("epg80")
        or prog.get("description")
        or ""
    ).strip()
    if desc:
        ET.SubElement(el, "desc", lang="ja").text = desc

    # credits
    act_list = misc.get("actList") or []
    if act_list:
        credits_el = ET.SubElement(el, "credits")
        cm = build_credits_map(act_list)
        for role in CREDITS_ORDER:
            for person in cm.get(role, []):
                ET.SubElement(credits_el, role).text = person

    # categories: NHK genre name1 (broad) + name2 (sub-genre), deduplicated.
    for cat_text, cat_lang in build_categories(id_grp.get("genre")):
        ET.SubElement(el, "category", lang=cat_lang).text = cat_text

    # icon: episode eyecatch preferred; falls back to series eyecatch.
    if icon_url := best_eyecatch(about):
        ET.SubElement(el, "icon", src=icon_url)

    return el


# ── XMLTV Writer ───────────────────────────────────────────────────────────────

def write_xmltv(
    all_programs: dict[str, list],
    channel_icons: dict[str, str],
    out_path: str,
) -> None:
    """
    Build the XMLTV element tree and pipe it directly into a gzip stream.

    ET.indent is intentionally omitted: the output is machine-consumed and
    gzip-compressed, so pretty-printing would only waste CPU cycles and
    inflate the uncompressed byte count without any benefit.

    ET.ElementTree.write() streams element-by-element to the gzip file
    object, avoiding the full-document bytes buffer that ET.tostring()
    would create in memory.
    """
    root = ET.Element(
        "tv",
        attrib={
            "source-info-name": "NHK",
            "generator-info-name": "nhk-epg-fetcher",
        },
    )

    # channel blocks
    for channel_id, cfg in CHANNELS.items():
        ch_el = ET.SubElement(root, "channel", id=channel_id)
        ET.SubElement(ch_el, "display-name", lang="ja").text = cfg["display_name"]
        if url := cfg.get("url"):
            ET.SubElement(ch_el, "url").text = url
        if icon_url := channel_icons.get(channel_id):
            ET.SubElement(ch_el, "icon", src=icon_url)

    # programme blocks: sort by startDate; deduplicate by broadcast event ID.
    # NHK returns early-morning programmes under the previous calendar date,
    # so cross-day duplicates with the same id are expected and filtered here.
    for channel_id in CHANNELS:
        seen_ids: set[str] = set()
        for prog in sorted(
            all_programs.get(channel_id, []),
            key=lambda p: p.get("startDate", ""),
        ):
            prog_id = prog.get("id", "")
            if prog_id and prog_id in seen_ids:
                continue
            seen_ids.add(prog_id)
            try:
                root.append(make_programme(prog, channel_id))
            except Exception as exc:
                log.warning("Skipping programme %s: %s", prog_id or "?", exc)

    ET.indent(root, space="  ")
    tree = ET.ElementTree(root)
    with gzip.open(out_path, "wb", compresslevel=9) as fh:
        tree.write(fh, encoding="utf-8", xml_declaration=True)


# ── Network ────────────────────────────────────────────────────────────────────

def fetch_day(
    channel_id: str,
    service_id: str,
    area_id: str,
    date_str: str,
) -> tuple[list, Optional[dict]]:
    """
    Fetch one channel / one date from the NHK API.

    Returns (programmes, channel_info_or_None).
    json.loads(resp.content) is used instead of resp.json() to bypass
    requests' charset-detection pass, which is unnecessary for a known
    UTF-8 JSON endpoint.
    """
    url = BASE_URL.format(service_id=service_id, area_id=area_id, date=date_str)
    kwargs = dict(timeout=30)

    try:
        resp = requests.get(url, impersonate="chrome", **kwargs)
        resp.raise_for_status()
        svc_data = json.loads(resp.content).get(service_id, {})
        programmes = svc_data.get("publication", [])
        ch_info    = (svc_data.get("publishedOn") or [None])[0]
        log.info("  %-14s  %s  %d programmes", channel_id, date_str, len(programmes))
        return programmes, ch_info
    except requests.HTTPError as exc:
        log.warning("  %-14s  %s  HTTP %d", channel_id, date_str, exc.response.status_code)
    except Exception as exc:
        log.warning("  %-14s  %s  ERROR: %s", channel_id, date_str, exc)
    return [], None


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    today     = datetime.now(JST).date()
    yesterday = today - timedelta(days=1)
    dates     = [
        (yesterday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(DAYS)
    ]

    log.info("Date range : %s … %s  (%d days)", dates[0], dates[-1], DAYS)
    log.info("Channels   : %s", ", ".join(CHANNELS))

    all_programs: dict[str, list] = {ch: [] for ch in CHANNELS}
    channel_icons: dict[str, str] = {}

    tasks = [
        (ch_id, cfg["service_id"], cfg["area_id"], d)
        for ch_id, cfg in CHANNELS.items()
        for d in dates
    ]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        future_map = {
            pool.submit(fetch_day, ch_id, svc_id, area_id, d): ch_id
            for ch_id, svc_id, area_id, d in tasks
        }
        for future in as_completed(future_map):
            ch_id = future_map[future]
            try:
                programmes, ch_info = future.result()
            except Exception as exc:
                log.error("Unhandled error for %s: %s", ch_id, exc)
                continue

            all_programs[ch_id].extend(programmes)
            # publishedOn is identical across all dates; capture it once.
            if ch_info and ch_id not in channel_icons:
                if icon := safe_get(ch_info, "eyecatch", "main", "url"):
                    if isinstance(icon, str):
                        channel_icons[ch_id] = icon

    total = sum(len(v) for v in all_programs.values())
    log.info("Programmes collected : %d", total)
    log.info("Writing %s ...", OUTPUT_FILE)

    write_xmltv(all_programs, channel_icons, OUTPUT_FILE)

    compressed_kb = os.path.getsize(OUTPUT_FILE) / 1024
    log.info("Done. Compressed size: %.1f KB  →  %s", compressed_kb, OUTPUT_FILE)


if __name__ == "__main__":
    main()