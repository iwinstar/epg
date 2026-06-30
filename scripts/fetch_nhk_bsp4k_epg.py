#!/usr/bin/env python3
"""Fetch NHK BSP4K schedules from bangumi.org and emit XMLTV."""

from __future__ import annotations

import argparse
import gzip
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo


BASE_URL = "https://bangumi.org/epg/bs4k?broad_cast_date={date}"
CHANNEL_ID = "nhk-bsp4k.jp"
CHANNEL_NAME = "NHK BSP4K JP"
CHANNEL_LINE_ID = "program_line_1"
JST = ZoneInfo("Asia/Tokyo")
BROADCAST_SYMBOL_RANGES = (
    (0x1F100, 0x1F1FF),  # Enclosed Alphanumeric Supplement, e.g. 🆞 and 🆠.
    (0x1F200, 0x1F2FF),  # Enclosed Ideographic Supplement, e.g. 🈖, 🈑, and 🈞.
)


@dataclass
class Programme:
    start: str
    stop: str
    title: str
    desc: str = ""
    url: str = ""
    pid: str = ""
    event_id: str = ""


class BangumiProgramParser(HTMLParser):
    def __init__(self, line_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.line_id = line_id
        self.in_target_ul = False
        self.ul_depth = 0
        self.li_depth = 0
        self.current: dict[str, str] | None = None
        self.current_field: str | None = None
        self.programmes: list[Programme] = []

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {key: value or "" for key, value in attrs_list}

        if tag == "ul" and attrs.get("id") == self.line_id:
            self.in_target_ul = True
            self.ul_depth = 1
            return

        if self.in_target_ul and tag == "ul":
            self.ul_depth += 1

        if not self.in_target_ul:
            return

        if tag == "li" and attrs.get("s") and attrs.get("e"):
            self.li_depth = 1
            self.current = {
                "start": attrs["s"],
                "stop": attrs["e"],
                "pid": attrs.get("pid", ""),
                "event_id": attrs.get("se-id", ""),
                "title": "",
                "desc": "",
                "url": "",
            }
            return

        if self.current is None:
            return

        if tag == "li":
            self.li_depth += 1
        elif tag == "a" and "href" in attrs and not self.current["url"]:
            href = attrs["href"]
            self.current["url"] = href if href.startswith("http") else f"https://bangumi.org{href}"
        elif tag == "p":
            classes = set(attrs.get("class", "").split())
            if "program_title" in classes:
                self.current_field = "title"
            elif "program_detail" in classes:
                self.current_field = "desc"

    def handle_data(self, data: str) -> None:
        if self.current is not None and self.current_field:
            existing = self.current[self.current_field]
            self.current[self.current_field] = f"{existing}{data}"

    def handle_endtag(self, tag: str) -> None:
        if not self.in_target_ul:
            return

        if tag == "p":
            self.current_field = None

        if self.current is not None and tag == "li":
            self.li_depth -= 1
            if self.li_depth <= 0:
                self.programmes.append(
                    Programme(
                        start=to_xmltv_time(self.current["start"]),
                        stop=to_xmltv_time(self.current["stop"]),
                        title=clean_text(self.current["title"]),
                        desc=clean_text(self.current["desc"]),
                        url=self.current["url"],
                        pid=self.current["pid"],
                        event_id=self.current["event_id"],
                    )
                )
                self.current = None
                self.current_field = None

        if tag == "ul":
            self.ul_depth -= 1
            if self.ul_depth <= 0:
                self.in_target_ul = False


def clean_text(value: str) -> str:
    return " ".join(strip_broadcast_symbols(value).split())


def strip_broadcast_symbols(value: str) -> str:
    return "".join(char for char in value if not is_broadcast_symbol(char))


def is_broadcast_symbol(char: str) -> bool:
    codepoint = ord(char)
    return any(start <= codepoint <= end for start, end in BROADCAST_SYMBOL_RANGES)


def to_xmltv_time(value: str) -> str:
    return f"{value}00 +0900"


def fetch_html(date: str, retries: int = 3) -> str:
    request = urllib.request.Request(
        BASE_URL.format(date=date),
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; nhk-bsp4k-xmltv/1.0)",
            "Accept-Language": "ja,en;q=0.8",
        },
    )

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error
            if attempt < retries:
                time.sleep(2 * attempt)

    raise RuntimeError(f"failed to fetch {date}: {last_error}") from last_error


def parse_programmes(html: str, line_id: str = CHANNEL_LINE_ID) -> list[Programme]:
    parser = BangumiProgramParser(line_id=line_id)
    parser.feed(html)
    return [programme for programme in parser.programmes if programme.title]


def build_xml(programmes: Iterable[Programme]) -> ET.ElementTree:
    tv = ET.Element(
        "tv",
        {
            "generator-info-name": "nhk-bsp4k-bangumi-scraper",
            "generator-info-url": "https://bangumi.org/epg/bs4k",
        },
    )
    channel = ET.SubElement(tv, "channel", {"id": CHANNEL_ID})
    ET.SubElement(channel, "display-name", {"lang": "ja"}).text = CHANNEL_NAME

    seen: set[tuple[str, str, str]] = set()
    for programme in sorted(programmes, key=lambda item: (item.start, item.stop, item.title)):
        key = (programme.start, programme.stop, programme.title)
        if key in seen:
            continue
        seen.add(key)

        node = ET.SubElement(
            tv,
            "programme",
            {
                "start": programme.start,
                "stop": programme.stop,
                "channel": CHANNEL_ID,
            },
        )
        ET.SubElement(node, "title", {"lang": "ja"}).text = programme.title
        if programme.desc:
            ET.SubElement(node, "desc", {"lang": "ja"}).text = programme.desc
        if programme.url:
            ET.SubElement(node, "url").text = programme.url
        if programme.pid or programme.event_id:
            ET.SubElement(node, "episode-num", {"system": "bangumi.org"}).text = ".".join(
                part for part in (programme.pid, programme.event_id) if part
            )

    ET.indent(tv, space="  ")
    return ET.ElementTree(tv)


def write_outputs(tree: ET.ElementTree, output: Path, gzip_output: Path | None) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output, encoding="utf-8", xml_declaration=True)

    if gzip_output is not None:
        gzip_output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("rb") as source, gzip.open(gzip_output, "wb", compresslevel=9) as target:
            target.writelines(source)


def date_range(start_date: str, days: int) -> list[str]:
    start = datetime.strptime(start_date, "%Y%m%d").date()
    return [(start + timedelta(days=offset)).strftime("%Y%m%d") for offset in range(days)]


def main() -> int:
    yesterday_jst = (datetime.now(JST)-timedelta(1)).strftime("%Y%m%d")
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", default=yesterday_jst, help="Start date in YYYYMMDD, defaults to yesterday in JST.")
    parser.add_argument("--days", type=int, default=8, help="Number of days to fetch, defaults to 8.")
    parser.add_argument("--output", default="dist/nhk-bsp4k.xml", help="XMLTV output path.")
    parser.add_argument("--gzip-output", default="dist/nhk-bsp4k.xml.gz", help="Gzip output path.")
    parser.add_argument("--line-id", default=CHANNEL_LINE_ID, help="bangumi.org program line id for the channel.")
    args = parser.parse_args()

    if args.days < 1:
        raise SystemExit("--days must be >= 1")

    all_programmes: list[Programme] = []
    for date in date_range(args.start_date, args.days):
        print(f"Fetching {date}...", file=sys.stderr)
        html = fetch_html(date)
        programmes = parse_programmes(html, line_id=args.line_id)
        if not programmes:
            raise RuntimeError(f"no programmes parsed for {date}; page structure may have changed")
        print(f"Parsed {len(programmes)} programmes for {date}.", file=sys.stderr)
        all_programmes.extend(programmes)

    output = Path(args.output)
    gzip_output = Path(args.gzip_output) if args.gzip_output else None
    write_outputs(build_xml(all_programmes), output, gzip_output)
    print(f"Wrote {output}", file=sys.stderr)
    if gzip_output is not None:
        print(f"Wrote {gzip_output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
