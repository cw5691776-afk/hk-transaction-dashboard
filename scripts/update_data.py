#!/usr/bin/env python3
"""Deterministic, token-free HK estate transaction dashboard updater.

Runs without any LLM. It fetches public provider data, normalizes records,
deduplicates overlapping Land Registry republications, merges with the previous
successful dataset, and writes a static JSON dataset for the website.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "estates.json"
DATA_DIR = ROOT / "data"
DATASET_PATH = DATA_DIR / "transactions.json"
STATE_PATH = DATA_DIR / "source-state.json"
REPORT_PATH = DATA_DIR / "update-report.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

CENTALINE_API = "https://hk.centanet.com/findproperty/api/Transaction/Search"
HSE28_BASE = "https://www.28hse.com/estate/detail"
MIDLAND_API = "https://data.midland.com.hk/search/v2/transactions"
MIDLAND_ESTATE_API = "https://data.midland.com.hk/search/v2/estates"
MIDLAND_JS = (
    "https://www.midland.com.hk/assets/midland-transaction-history/v_92/_next/static/"
    "chunks/app/%5Blocale%5D/transaction-history/%5Bslug%5D/page.js"
)


def midland_build_token() -> str:
    """Read the public build token Midland ships in its own web bundle."""
    js = safe_get_text(MIDLAND_JS)
    match = re.search(r'"BUILD_TOKEN":"([^"]+)"', js)
    if not match:
        raise RuntimeError("Midland BUILD_TOKEN not found")
    return match.group(1)


def midland_search(token: str, **params: str) -> dict[str, Any]:
    query = {
        "hash": "false",
        "redirect": "false",
        "tx_date": "3year",
        "lang": "zh-hk",
        "tx_type": "S",
        "page": "1",
    }
    query.update(params)
    url = MIDLAND_API + "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "*/*",
            "User-Agent": USER_AGENT,
            "Referer": "https://www.midland.com.hk/",
            "Origin": "https://www.midland.com.hk",
            "Authorization": "Bearer " + token,
        },
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def midland_find_estate_id(token: str, name: str) -> str | None:
    result = midland_search_estates(token, text=name)
    for item in result.get("result", []):
        labels = {item.get("name"), item.get("news_name")}
        if name in labels:
            return item.get("id")
    return None


def midland_search_estates(token: str, **params: str) -> dict[str, Any]:
    query = {"lang": "zh-hk", "page": "1", "limit": "50"}
    query.update(params)
    url = MIDLAND_ESTATE_API + "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "*/*",
            "User-Agent": USER_AGENT,
            "Referer": "https://www.midland.com.hk/",
            "Origin": "https://www.midland.com.hk",
            "Authorization": "Bearer " + token,
        },
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_get_json(url: str, payload: dict[str, Any] | None = None, timeout: int = 45) -> dict[str, Any]:
    body = None
    headers = {
        "Accept": "application/json, text/html;q=0.9",
        "User-Agent": USER_AGENT,
        "Referer": url,
    }
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def safe_get_text(url: str, timeout: int = 45) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-HK,zh-Hant;q=0.9,en;q=0.7",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def parse_money_hkd(text: str) -> int | None:
    cleaned = html.unescape(text or "").replace(",", "").strip()
    if not cleaned:
        return None
    # 28Hse writes "$570 萬元"; the extra "元" must not be parsed as 10,000.
    million = re.search(r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*萬", cleaned)
    if million:
        return int(float(million.group(1)) * 10_000)
    plain = re.search(r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*([Mm])?", cleaned)
    if plain:
        value = float(plain.group(1))
        return int(value * 1_000_000 if plain.group(2) else value)
    return None


def parse_number(text: str) -> int | None:
    match = re.search(r"[0-9][0-9,\.]*", html.unescape(text or ""))
    if not match:
        return None
    try:
        return int(float(match.group(0).replace(",", "")))
    except ValueError:
        return None


def normalize_text(value: str) -> str:
    value = html.unescape(value or "").replace("\u3000", " ").strip()
    value = re.sub(r"\s+", " ", value)
    return value


def normalize_unit(value: str | None) -> str | None:
    if not value:
        return None
    value = normalize_text(value).upper()
    value = value.replace("第", "").replace("座", "")
    value = re.sub(r"\(.*?\)", "", value)
    value = value.replace(" ", "")
    value = re.sub(r"^0+([0-9])", r"\1", value)
    return value or None


def normalize_date(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", value):
        return value[:10]
    match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", value)
    if match:
        day, month, year = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    return None


def record_key(record: dict[str, Any]) -> str:
    """Stable key for deduplication across providers.

    Registry deals are matched on estate/block/floor/unit plus a +/- 3 day date
    window, because Land Registry filings are republished by different sites on
    slightly different dates. Market deals are matched on the same unit key and
    are kept separate from registry deals.
    """
    estate = normalize_text(record.get("estate") or "")
    block = normalize_unit(record.get("block")) or ""
    floor = normalize_unit(record.get("floor")) or ""
    unit = normalize_unit(record.get("unit")) or ""
    date = record.get("date") or ""
    price = record.get("priceHKD")
    area = record.get("saleableAreaSqft")
    if block or floor or unit:
        return f"{estate}|{block}|{floor}|{unit}"
    return f"{estate}|price={price}|area={area}"


def same_unit(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if normalize_text(a.get("estate") or "") != normalize_text(b.get("estate") or ""):
        return False
    a_flat = normalize_unit(a.get("unit"))
    b_flat = normalize_unit(b.get("unit"))
    if a_flat and b_flat and a_flat != b_flat:
        return False
    a_block = normalize_unit(a.get("block"))
    b_block = normalize_unit(b.get("block"))
    if a_block and b_block and a_block != b_block:
        return False
    a_floor = normalize_unit(a.get("floor"))
    b_floor = normalize_unit(b.get("floor"))
    if a_floor and b_floor and a_floor != b_floor:
        # One provider may omit the floor number; when both give a number they
        # must agree.
        if a_floor.isdigit() and b_floor.isdigit():
            return False
    if a.get("priceHKD") and b.get("priceHKD"):
        if abs(a["priceHKD"] - b["priceHKD"]) > max(60_000, a["priceHKD"] * 0.015):
            return False
    if a.get("saleableAreaSqft") and b.get("saleableAreaSqft"):
        if abs(a["saleableAreaSqft"] - b["saleableAreaSqft"]) > 12:
            return False
    return True


def price_area_match(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Strong reconciliation signal when unit identifiers are incomplete."""
    if not (a.get("priceHKD") and b.get("priceHKD")):
        return False
    if abs(a["priceHKD"] - b["priceHKD"]) > max(30_000, a["priceHKD"] * 0.005):
        return False
    if a.get("saleableAreaSqft") and b.get("saleableAreaSqft"):
        return abs(a["saleableAreaSqft"] - b["saleableAreaSqft"]) <= 3
    return True


def same_unit_strict(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Exact unit-identity check used to reconcile Centaline's coarse labels."""
    a_unit = (normalize_unit(a.get("block")), normalize_unit(a.get("floor")), normalize_unit(a.get("unit")))
    b_unit = (normalize_unit(b.get("block")), normalize_unit(b.get("floor")), normalize_unit(b.get("unit")))
    if a_unit == b_unit:
        return True
    if not all(a_unit) or not all(b_unit):
        return False
    a_block, a_floor, a_flat = a_unit
    b_block, b_floor, b_flat = b_unit
    if a_block != b_block or a_flat != b_flat:
        return False
    # One provider may report the block, the other may fold it into the room.
    return a_floor == b_floor


def date_close(a: str | None, b: str | None, days: int = 3) -> bool:
    if not a or not b:
        return True
    try:
        da = datetime.strptime(a, "%Y-%m-%d")
        db = datetime.strptime(b, "%Y-%m-%d")
    except ValueError:
        return a == b
    return abs((da - db).days) <= days


def merge_provenance(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge overlapping provider records while preserving every source."""
    merged: list[dict[str, Any]] = []
    for record in sorted(records, key=lambda item: item.get("date") or "", reverse=True):
        target = None
        for candidate in merged:
            if not same_unit(candidate, record):
                continue
            if candidate.get("transactionKind") != record.get("transactionKind"):
                # A market deal and its later registry filing are related but
                # are distinct rows; keep both with shared provenance.
                continue
            # When one side lacks the floor number, require an exact unit
            # identity on the remaining fields to avoid merging wrong units.
            a_floor = normalize_unit(candidate.get("floor"))
            b_floor = normalize_unit(record.get("floor"))
            a_block = normalize_unit(candidate.get("block"))
            b_block = normalize_unit(record.get("block"))
            a_flat = normalize_unit(candidate.get("unit"))
            b_flat = normalize_unit(record.get("unit"))
            identifiers_complete = bool(a_floor and b_floor and a_block and b_block and a_flat and b_flat)
            if not identifiers_complete:
                # Require exact agreement on every identifier both sides do
                # provide, plus price/area agreement, before merging.
                if a_block and b_block and a_block != b_block:
                    continue
                if a_flat and b_flat and a_flat != b_flat:
                    continue
                if a_floor and b_floor and a_floor != b_floor:
                    continue
                if not price_area_match(candidate, record):
                    continue
            # Registry deals are frequently republished by several sites with
            # different registration dates (contract date vs filing date).
            # Accept the match when price and area agree even if the dates
            # differ, but keep the earliest observed date.
            if not price_area_match(candidate, record):
                if not date_close(candidate.get("date"), record.get("date"), days=4):
                    continue
            target = candidate
            break
        if target is None:
            merged.append({**record, "sources": [record["source"]], "hasDiscrepancy": False})
            continue
        target["sources"] = list(dict.fromkeys(target["sources"] + [record["source"]]))
        target["providerIds"] = list(
            dict.fromkeys((target.get("providerIds") or []) + (record.get("providerIds") or []))
        )
        target["sourceUrls"] = list(
            dict.fromkeys((target.get("sourceUrls") or []) + (record.get("sourceUrls") or []))
        )
        if target.get("priceHKD") and record.get("priceHKD"):
            if abs(target["priceHKD"] - record["priceHKD"]) >= max(50_000, target["priceHKD"] * 0.01):
                target["hasDiscrepancy"] = True
        for field in ("priceHKD", "saleableAreaSqft", "saleableHkdPerSqft", "grossAreaSqft", "block", "floor", "unit"):
            if target.get(field) in (None, "") and record.get(field) not in (None, ""):
                target[field] = record[field]
        if not target.get("address") and record.get("address"):
            target["address"] = record["address"]
    return sorted(merged, key=lambda item: (item.get("date") or "", item.get("priceHKD") or 0), reverse=True)


def centaline_records(estate: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for type_code in estate["centaline"]["type_codes"]:
        payload = {
            "postType": "Sale",
            "day": "Day1095",
            "sort": "InsOrRegDate",
            "order": "Descending",
            "size": 100,
            "pageSource": "search",
            "offset": 0,
            "mtrs": [],
            "primarySchoolNets": [],
            "typeCodes": [type_code],
            "universities": [],
        }
        result = safe_get_json(CENTALINE_API, payload)
        for item in result.get("data", []):
            address = item.get("displayText", {}).get("addr", {}).get("line1", "")
            estate_name = item.get("bigEstateName") or item.get("estateName") or estate["name"]
            if not any(alias.lower() in address.lower() or alias.lower() in estate_name.lower() for alias in estate["aliases"]):
                continue
            # Centaline returns human labels such as "中層", "低層" and
            # "高層" instead of a numeric floor. Keep the label but drop the
            # block/room labels so it can match the other providers.
            floor_label = normalize_unit(item.get("yAxis"))
            if floor_label and not re.fullmatch(r"\d+", floor_label):
                floor_label = {"低層": "LOW", "中層": "MID", "高層": "HIGH", "極高層": "SUPER"}.get(floor_label, floor_label)
            block_label = normalize_text(item.get("buildingName")) or None
            if block_label:
                block_label = re.sub(r"[座期]", "", block_label).strip() or None
            if block_label and re.fullmatch(r"\d+", block_label):
                block_label = None
            address_text = normalize_text(address)
            address_block = re.search(r"([A-Z])\s*座", address_text)
            if address_block:
                block_label = address_block.group(1)
            address_floor = re.search(r"([0-9]{1,2})\s*(?:樓|层)", address_text)
            if address_floor:
                floor_label = address_floor.group(1)
            elif floor_label in {"LOW", "MID", "HIGH", "SUPER"}:
                # Centaline's coarse floor label cannot be reconciled with a
                # numeric floor from the other providers.
                floor_label = None
            unit_label = normalize_unit(item.get("xAxis"))
            if unit_label and re.fullmatch(r"[A-Z]", unit_label):
                unit_label = None
            address_flat = re.search(r"([A-Z]?[0-9]{1,3})\s*(?:室|房)", address_text)
            if address_flat:
                unit_label = address_flat.group(1).upper()
            # Centaline's address string repeats the estate name and may
            # include a phase prefix ("2期"). Keep only the physical unit.
            address_clean = re.sub(
                r"^(?:得寶花園|康華苑|新成中心|The\s*Holborn|都會軒)\s*", "", address_text, flags=re.I
            )
            address_clean = re.sub(r"^[0-9]+期\s*", "", address_clean)
            if floor_label is None:
                # Centaline's building-name field carries the phase/building
                # code rather than the physical block, so keep it out of the
                # unit key and rely on the address-derived block instead.
                building_code = normalize_text(item.get("buildingName")) or None
            else:
                building_code = None
            source_kind = "registry" if item.get("dataSource") == "Land" else "market"
            records.append(
                {
                    "estateId": estate["id"],
                    "estate": estate["name"],
                    "district": estate["district"],
                    "date": normalize_date(item.get("insDate") or item.get("regDate")),
                    "block": block_label,
                    "floor": floor_label,
                    "unit": unit_label,
                    "address": address_clean,
                    "priceHKD": item.get("transactionPrice"),
                    "saleableAreaSqft": item.get("nArea"),
                    "saleableHkdPerSqft": item.get("nUnitPrice"),
                    "grossAreaSqft": item.get("gArea"),
                    "transactionKind": source_kind,
                    "source": "中原 Centadata",
                    "providerIds": [item.get("id")] if item.get("id") else [],
                    "sourceUrls": [item.get("detailUrl")] if item.get("detailUrl") else [],
                    "buildingCode": building_code,
                    "hasDiscrepancy": False,
                }
            )
        time.sleep(0.4)
    return records


def hse28_records(estate: dict[str, Any]) -> list[dict[str, Any]]:
    estate_id = estate.get("hse28", {}).get("estate_id")
    if not estate_id:
        return []
    url = f"{HSE28_BASE}/{urllib.parse.quote(estate['name'])}-{estate_id}/transaction"
    page = safe_get_text(url)
    records: list[dict[str, Any]] = []
    # 28Hse renders each deal as a table row with unit-id.
    row_pattern = re.compile(r"<tr\s+unit-id=\"(\d+)\">(.*?)</tr>", re.S)
    for row_match in row_pattern.finditer(page):
        unit_id = row_match.group(1)
        block = row_match.group(2)
        text = normalize_text(re.sub(r"<[^>]+>", " ", block))
        date_match = re.search(r"(20\d{2}-\d{2}-\d{2})", text)
        if not date_match:
            continue
        date = date_match.group(1)
        kind = "registry" if "註冊處成交" in text else ("market" if "市場成交" in text else "unknown")
        price_match = re.search(r"\$\s*[0-9]+(?:\.[0-9]+)?\s*萬", text)
        if not price_match:
            price_match = re.search(r"\$\s*[0-9,]+", text)
        area_match = re.search(r"(實|建)\s*([0-9]+)\s*呎", text)
        unit_price_match = re.search(r"@\s*\$?\s*([0-9,]+)", text)
        address_match = re.search(
            r"(?:得寶花園|康華苑|新成中心|The\s*Holborn|都會軒)\s*([^0-9]{0,40}(?:樓|層|座)[^0-9]{0,12}(?:室|房))",
            text,
            re.I,
        )
        if not address_match:
            address_match = re.search(r"([^0-9]{0,40}(?:樓|層)[^0-9]{0,12}(?:室|房))", text)
        address = normalize_text(address_match.group(1)) if address_match else None
        if not address:
            # The table's title attribute keeps the exact unit address.
            title_match = re.search(r'title="([^"]+)"', block)
            if title_match:
                address = normalize_text(title_match.group(1))
                address = re.sub(r"^(?:得寶花園|康華苑|新成中心|The\s*Holborn|都會軒)\s*", "", address, flags=re.I)
        block_name = None
        floor = None
        unit = None
        if address:
            block_match = re.search(r"(?:第)?([A-Z])\s*座", address, re.I)
            floor_match = re.search(r"([0-9]{1,2})\s*(?:樓|層)", address)
            unit_match = re.search(r"([A-Z]?[0-9]{1,3})\s*(?:室|房)", address, re.I)
            block_name = block_match.group(1).upper() if block_match else None
            floor = floor_match.group(1) if floor_match else None
            unit = unit_match.group(1).upper() if unit_match else None
        detail_url_match = re.search(r'href="([^"]*/(?:history|transaction)[^"]*)"', block)
        records.append(
            {
                "estateId": estate["id"],
                "estate": estate["name"],
                "district": estate["district"],
                "date": date,
                "block": block_name,
                "floor": floor,
                "unit": unit,
                "address": address,
                "priceHKD": parse_money_hkd(price_match.group(0)) if price_match else None,
                "saleableAreaSqft": int(area_match.group(2)) if area_match and area_match.group(1) == "實" else None,
                "saleableHkdPerSqft": parse_number(unit_price_match.group(1)) if unit_price_match else None,
                "grossAreaSqft": int(area_match.group(2)) if area_match and area_match.group(1) == "建" else None,
                "transactionKind": kind,
                "source": "28Hse",
                "providerIds": [unit_id],
                "sourceUrls": [detail_url_match.group(1) if detail_url_match else url],
                "hasDiscrepancy": False,
            }
        )
    return records


def midland_records(estate: dict[str, Any], token: str | None = None) -> list[dict[str, Any]]:
    token = token or midland_build_token()
    estate_id = estate.get("midland", {}).get("estate_id")
    if not estate_id:
        estate_id = midland_find_estate_id(token, estate["name"])
    if not estate_id:
        return []
    result = midland_search(token, est_ids=estate_id, page="1")
    records: list[dict[str, Any]] = []
    aliases = [alias.lower() for alias in estate["midland"]["estate_names"]]
    for item in result.get("result", []):
        if item.get("tx_type") not in (None, "S"):
            continue
        estate_name = (item.get("estate") or {}).get("name") or ""
        if not any(alias in estate_name.lower() for alias in aliases):
            continue
        source = item.get("source") or item.get("original_source") or "MIDLAND"
        kind = "registry" if str(source).upper() in {"LANDREG", "LAND", "LR"} else "market"
        floor_name = item.get("floor") or (item.get("floor_level") or {}).get("name")
        records.append(
            {
                "estateId": estate["id"],
                "estate": estate["name"],
                "district": estate["district"],
                "date": normalize_date(item.get("tx_date")),
                "block": normalize_unit((item.get("building") or {}).get("name")),
                "floor": normalize_unit(floor_name),
                "unit": normalize_unit(item.get("flat")),
                "address": normalize_text(
                    " ".join(
                        part
                        for part in [
                            (item.get("estate") or {}).get("name"),
                            (item.get("building") or {}).get("name"),
                            floor_name,
                            str(item.get("flat") or ""),
                        ]
                        if part
                    )
                ),
                "priceHKD": item.get("price"),
                "saleableAreaSqft": item.get("net_area") or None,
                "saleableHkdPerSqft": item.get("unit_price_net") or None,
                "grossAreaSqft": item.get("area") or None,
                "transactionKind": kind,
                "source": "美聯 Midland",
                "providerIds": [item.get("id")] if item.get("id") else [],
                "sourceUrls": [item.get("url_desc")] if item.get("url_desc") else [],
                "hasDiscrepancy": False,
            }
        )
    return records


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Update the HK transaction dashboard dataset")
    parser.add_argument("--providers", default="centaline,hse28,midland")
    parser.add_argument("--strict", action="store_true", help="Fail if any configured provider fails")
    args = parser.parse_args()

    config = load_json(CONFIG_PATH, {"estates": []})
    estates = config["estates"]
    enabled = {name.strip() for name in args.providers.split(",") if name.strip()}
    source_status: dict[str, Any] = {}
    all_records: list[dict[str, Any]] = []
    errors: list[str] = []

    adapters = {
        "centaline": centaline_records,
        "hse28": hse28_records,
    }
    midland_token: str | None = None
    if "midland" in enabled:
        try:
            midland_token = midland_build_token()
        except Exception as exc:
            source_status["midland"] = {"status": "failed", "records": 0, "errors": [str(exc)]}
            errors.append(f"midland: {exc}")
    for provider, adapter in adapters.items():
        if provider not in enabled:
            source_status[provider] = {"status": "disabled", "records": 0}
            continue
        provider_records: list[dict[str, Any]] = []
        provider_errors: list[str] = []
        for estate in estates:
            try:
                provider_records.extend(adapter(estate))
                time.sleep(0.8)
            except Exception as exc:  # one estate/source failure must not kill the job
                provider_errors.append(f"{estate['id']}: {exc}")
        all_records.extend(provider_records)
        source_status[provider] = {
            "status": "ok" if provider_records and not provider_errors else ("warning" if provider_records else "failed"),
            "records": len(provider_records),
            "latestDate": max((r.get("date") or "" for r in provider_records), default=None),
            "errors": provider_errors,
        }
        errors.extend(f"{provider}: {message}" for message in provider_errors)

    if "midland" in enabled:
        provider_records: list[dict[str, Any]] = []
        provider_errors: list[str] = []
        if midland_token:
            for estate in estates:
                try:
                    provider_records.extend(midland_records(estate, midland_token))
                    time.sleep(0.8)
                except Exception as exc:
                    provider_errors.append(f"{estate['id']}: {exc}")
        all_records.extend(provider_records)
        source_status["midland"] = {
            "status": "ok" if provider_records and not provider_errors else ("warning" if provider_records else "failed"),
            "records": len(provider_records),
            "latestDate": max((r.get("date") or "" for r in provider_records), default=None),
            "errors": provider_errors,
        }
        errors.extend(f"midland: {message}" for message in provider_errors)

    previous = load_json(DATASET_PATH, {})
    previous_records = previous.get("records", []) if isinstance(previous, dict) else []
    combined = merge_provenance(previous_records + all_records)

    by_estate: dict[str, list[dict[str, Any]]] = {}
    for record in combined:
        by_estate.setdefault(record["estateId"], []).append(record)
    for estate in estates:
        by_estate.setdefault(estate["id"], [])

    dataset = {
        "version": 2,
        "generatedAt": now_iso(),
        "timezone": "Asia/Hong_Kong",
        "estates": [
            {
                "id": estate["id"],
                "name": estate["name"],
                "district": estate["district"],
                "aliases": estate["aliases"],
            }
            for estate in estates
        ],
        "sources": source_status,
        "records": combined,
        "estateCounts": {
            estate_id: len(records) for estate_id, records in by_estate.items()
        },
        "updateErrors": errors,
    }

    if not all_records:
        report = {
            "ok": False,
            "generatedAt": now_iso(),
            "sources": source_status,
            "errors": errors,
            "action": "retained_previous_dataset",
        }
        atomic_write(REPORT_PATH, json.dumps(report, ensure_ascii=False, indent=2))
        print("No records fetched; previous dataset retained.", file=sys.stderr)
        return 2

    atomic_write(DATASET_PATH, json.dumps(dataset, ensure_ascii=False, indent=2))
    atomic_write(
        STATE_PATH,
        json.dumps(
            {"lastSuccessAt": dataset["generatedAt"], "sources": source_status}, ensure_ascii=False, indent=2
        ),
    )
    report = {
        "ok": not errors,
        "generatedAt": dataset["generatedAt"],
        "sources": source_status,
        "errors": errors,
        "recordCount": len(combined),
        "action": "published",
    }
    atomic_write(REPORT_PATH, json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors or not args.strict else 3


if __name__ == "__main__":
    raise SystemExit(main())
