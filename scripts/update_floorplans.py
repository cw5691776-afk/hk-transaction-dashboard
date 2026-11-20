#!/usr/bin/env python3
"""Collect estate floor plans for the HK transaction dashboard.

Source: Midland's public estate page (`/zh-hk/estate/<estateId>`), whose
embedded JSON carries a `floorplans` array. Each entry describes one drawing:

    {"id": "...", "name": "一期 1座 1/F", "wan_doc_path": "https://wm-cdn...",
     "source": "BUILDING", "phase": {"id": "P...", "name": "一期"},
     "building": {"id": "B...", "name": "1座"},
     "floor_from": "1", "floor_to": "1", "floor_desc": "1/F"}

28Hse is kept as a second source because its drawings are rendered as
`.floorplanCard` anchors and cover some estates Midland does not.

The dataset is written to `data/floorplans.json`. This script is independent of
the transaction updater so a floor-plan failure can never invalidate the
transaction dataset.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "estates.json"
DATA_DIR = ROOT / "data"
OUTPUT_PATH = DATA_DIR / "floorplans.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

MIDLAND_ESTATE_PAGE = "https://www.midland.com.hk/zh-hk/estate/{estate_id}"
HSE28_BASE = "https://www.28hse.com/estate/detail"


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def safe_get_text(url: str, timeout: int = 45) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": USER_AGENT,
            "Referer": "https://www.midland.com.hk/" if "midland" in url else url,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def escape_json_text(raw: str) -> str:
    """Unescape the HTML-embedded JSON so the payload can be regex-parsed."""
    return (
        raw.replace("\\u0026", "&")
        .replace("\\u002F", "/")
        .replace("\\/", "/")
        .replace('\\"', '"')
    )


def split_floorplan_objects(block: str) -> list[str]:
    """Split a JSON array body into its top-level objects.

    The array contains nested objects (phase, building), so a regex for
    `\{[^{}]*\}` would miss every entry. Track brace depth instead.
    """
    objects: list[str] = []
    depth = 0
    start = 0
    in_string = False
    escaped = False
    for index, char in enumerate(block):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                objects.append(block[start : index + 1])
    return objects


def extract_json_array(page: str, key: str) -> str | None:
    """Return the body of the JSON array named `key`.

    A lazy regex stops at the first `]` inside a nested array (e.g.
    `"size":["300","600"]`), so walk the brackets by depth instead.
    """
    marker = f'"{key}":['
    start = page.find(marker)
    if start < 0:
        return None
    index = start + len(marker)
    depth = 1
    in_string = False
    escaped = False
    while index < len(page):
        char = page[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return page[start + len(marker) : index]
        index += 1
    return None


def normalize_phase_label(value: str | None) -> str | None:
    """Normalise a phase label to the short `"N期"` form used elsewhere."""
    if not value:
        return None
    match = re.search(r"([一二三四五六七八九十\d]+)\s*期", value)
    if not match:
        return None
    token = match.group(1)
    chinese = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}
    return f"{chinese.get(token, token)}期"


def normalize_block_label(value: str | None) -> str | None:
    """Normalise a block label so `1`, `1座` and `01座` are the same block."""
    if not value:
        return None
    text = value.strip()
    text = re.sub(r"\(.*?\)", "", text).strip()
    text = re.sub(r"座$", "", text).strip()
    if not text:
        return None
    if text.isdigit():
        return str(int(text))
    return text


def parse_midland_floorplans(estate: dict[str, Any]) -> list[dict[str, Any]]:
    estate_id = (estate.get("midland") or {}).get("estate_id")
    if not estate_id:
        return []
    url = MIDLAND_ESTATE_PAGE.format(estate_id=estate_id)
    try:
        page = escape_json_text(safe_get_text(url))
    except Exception as exc:  # noqa: BLE001 - a single estate must not abort the run
        print(f"  {estate['name']}: midland fetch failed: {exc}", file=sys.stderr)
        return []

    body = extract_json_array(page, "floorplans")
    if body is None:
        print(f"  {estate['name']}: no floorplans array on midland")
        return []

    plans: list[dict[str, Any]] = []
    for obj in split_floorplan_objects(body):
        path_match = re.search(r'"wan_doc_path":"([^"]+)"', obj)
        if not path_match:
            continue
        phase = re.search(r'"phase":\{"id":"([^"]*)","name":"([^"]*)"', obj)
        building = re.search(r'"building":\{"id":"([^"]*)","name":"([^"]*)"', obj)
        floor_from = re.search(r'"floor_from":"?([^",]*)"?', obj)
        floor_to = re.search(r'"floor_to":"?([^",]*)"?', obj)
        floor_desc = re.search(r'"floor_desc":"([^"]*)"', obj)
        name = re.search(r'"name":"([^"]*)"', obj)
        source_type = re.search(r'"source":"([^"]*)"', obj)
        plans.append(
            {
                "provider": "美聯 Midland",
                "estateId": estate["id"],
                "estate": estate["name"],
                "phase": normalize_phase_label(phase.group(2) if phase else None),
                "phaseId": phase.group(1) if phase else None,
                "block": normalize_block_label(building.group(2) if building else None),
                "blockId": building.group(1) if building else None,
                "floorFrom": floor_from.group(1) if floor_from else None,
                "floorTo": floor_to.group(1) if floor_to else None,
                "floorDesc": floor_desc.group(1) if floor_desc else None,
                "title": name.group(1) if name else None,
                "kind": source_type.group(1) if source_type else "BUILDING",
                "imageUrl": path_match.group(1),
                "thumbnailUrl": path_match.group(1),
                "sourceUrl": url,
            }
        )
    return plans


def block_from_title(title: str | None) -> str | None:
    """Extract the block name from a floor-plan title.

    28Hse's `attr_block` is a sequential index (1, 2, 3 …) rather than the
    block name, so read the name from the title text instead:
    `A座 (宏康閣) 1-33樓` -> `A`, `1期 第一期 2座 5樓` -> `2`.
    """
    if not title:
        return None
    match = re.search(r"([A-Za-z]?\d{0,2})\s*座", title)
    if match:
        return normalize_block_label(match.group(1))
    return None


def parse_hse28_floorplans(estate: dict[str, Any]) -> list[dict[str, Any]]:
    estate_id = (estate.get("hse28") or {}).get("estate_id")
    if not estate_id:
        return []
    url = f"{HSE28_BASE}/{urllib.parse.quote(estate['name'])}-{estate_id}"
    try:
        page = escape_json_text(safe_get_text(url))
    except Exception as exc:  # noqa: BLE001
        print(f"  {estate['name']}: 28hse fetch failed: {exc}", file=sys.stderr)
        return []

    plans: list[dict[str, Any]] = []
    cards = re.findall(r'<a\s+attr_row_count[^>]*class="floorplanCard[^"]*"(.*?)</a>', page, re.S)
    for card in cards:
        image = re.search(r'src=\\?"([^"\\]+)', card)
        if not image:
            continue
        src = image.group(1)
        thumb_src = src
        if src.endswith("_thumb.jpg"):
            thumb_src = src
            src = src[: -len("_thumb.jpg")] + "_large.jpg"
        elif src.endswith("_large.jpg"):
            # The full drawing is several megabytes; the grid uses the preview.
            thumb_src = src[: -len("_large.jpg")] + "_thumb.jpg"
        description = re.search(r'<div class="description"><p[^>]*>(.*?)</p>', card, re.S)
        title = re.sub(r"<[^>]+>", "", description.group(1)).strip() if description else None
        state = re.search(r'attr_state="([^"]*)"', card)
        block = re.search(r'attr_block="([^"]*)"', card)
        kind = re.search(r'attr_type="([^"]*)"', card)
        phase = None
        if title:
            phase = normalize_phase_label(title)
        block_label = block_from_title(title)
        floor_desc = None
        floor_from = None
        floor_to = None
        if title:
            # 28Hse titles read "A座 (宏康閣) 1-33樓" or "1期 第一期 2座 5-42樓".
            range_match = re.search(r"(\d{1,3})\s*-\s*(\d{1,3})\s*樓", title)
            single_match = re.search(r"(\d{1,3})\s*樓", title)
            if range_match:
                floor_from, floor_to = range_match.group(1), range_match.group(2)
                floor_desc = f"{floor_from}-{floor_to}F"
            elif single_match:
                floor_from = floor_to = single_match.group(1)
                floor_desc = f"{floor_from}F"
        plans.append(
            {
                "provider": "28Hse",
                "estateId": estate["id"],
                "estate": estate["name"],
                "phase": phase,
                "phaseId": state.group(1) if state else None,
                "block": block_label,
                "blockId": None,
                "floorFrom": floor_from,
                "floorTo": floor_to,
                "floorDesc": floor_desc,
                "title": title,
                "kind": kind.group(1) if kind else "FLOORPLAN",
                "imageUrl": src,
                "thumbnailUrl": thumb_src or src,
                "sourceUrl": url,
            }
        )
    return plans


def main() -> int:
    config = load_json(CONFIG_PATH, {"estates": []})
    estates = config.get("estates", [])
    all_plans: list[dict[str, Any]] = []
    errors: list[str] = []

    for estate in estates:
        print(f"{estate['name']}:")
        plans = parse_midland_floorplans(estate)
        print(f"  midland: {len(plans)} plans")
        all_plans.extend(plans)
        time.sleep(0.4)

        extra = parse_hse28_floorplans(estate)
        print(f"  28hse:   {len(extra)} plans")
        all_plans.extend(extra)
        time.sleep(0.4)

    # Deduplicate. The same drawing is published by both providers under
    # different URLs, so collapse on estate + phase + block + floor range and
    # keep the first (Midland's, which names the building explicitly).
    unique: list[dict[str, Any]] = []
    seen_drawing: set[tuple[str, str, str, str]] = set()
    seen_url: set[tuple[str, str]] = set()
    for plan in all_plans:
        url_key = (plan["provider"], plan["imageUrl"])
        if url_key in seen_url:
            continue
        seen_url.add(url_key)
        floor_key = plan["floorDesc"] or plan["floorFrom"] or ""
        drawing_key = (plan["estate"], plan["phase"] or "", plan["block"] or "", floor_key)
        if drawing_key in seen_drawing:
            continue
        seen_drawing.add(drawing_key)
        unique.append(plan)

    by_estate: dict[str, int] = {}
    blocks_by_estate: dict[str, set[str]] = {}
    for plan in unique:
        by_estate[plan["estate"]] = by_estate.get(plan["estate"], 0) + 1
        if plan["block"]:
            blocks_by_estate.setdefault(plan["estate"], set()).add(plan["block"])

    payload = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "planCount": len(unique),
        "estateCounts": by_estate,
        "blockCounts": {estate: len(blocks) for estate, blocks in blocks_by_estate.items()},
        "plans": unique,
        "errors": errors,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"ok": True, "planCount": len(unique), "estateCounts": by_estate}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
