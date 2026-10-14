import argparse
import csv
import hashlib
import html
import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import gspread
import psycopg2
import requests
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials

PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?84|0)(?:[\s().-]*\d){8,10}(?!\d)")
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TELEGRAM_MAX_ATTEMPTS = 3
TELEGRAM_TIMEOUT_SECONDS = 120
TELEGRAM_MESSAGE_LIMIT = 3800


def normalize_text(value: str) -> str:
    value = (value or "").replace("đ", "d").replace("Đ", "D")
    value = unicodedata.normalize("NFD", value)
    value = "".join(char for char in value if unicodedata.category(char) != "Mn")
    return value.lower()


def extract_phones(value: str) -> list[str]:
    return sorted({match.group(0) for match in PHONE_PATTERN.finditer(value or "")})


def read_config(path: str) -> dict:
    with open(path, encoding="utf-8") as config_file:
        config = json.load(config_file)
    if not config.get("sources"):
        raise RuntimeError(
            f"No sources configured in {path}. Add permitted RSS/Atom or public HTML URLs."
        )
    return config


def fetch_with_crawlbase(url: str) -> str:
    token = os.environ.get("CRAWLBASE_TOKEN")
    if not token:
        raise RuntimeError("CRAWLBASE_TOKEN is not set.")

    response = requests.get(
        "https://api.crawlbase.com/",
        params={"token": token, "url": url, "format": "html"},
        headers={"Accept-Encoding": "gzip", "User-Agent": "TransportLeadMonitor/1.0"},
        timeout=90,
    )
    response.raise_for_status()
    if response.headers.get("cb_status") != "200":
        raise RuntimeError(
            "Crawlbase failed: cb_status={0}, original_status={1}".format(
                response.headers.get("cb_status"),
                response.headers.get("original_status"),
            )
        )
    return response.text


def parse_feed(source: dict) -> list[dict]:
    parsed = feedparser.parse(source["url"])
    records = []
    for entry in parsed.entries:
        content = entry.get("summary", entry.get("description", ""))
        records.append(
            {
                "source": source["name"],
                "source_url": source["url"],
                "title": entry.get("title", "").strip(),
                "content": BeautifulSoup(content, "html.parser").get_text(" ", strip=True),
                "url": entry.get("link", source["url"]),
                "published": entry.get("published", entry.get("updated", "")),
            }
        )
    return records


def parse_xecont(source: dict, html: str) -> list[dict]:
    markers = [r"\"orders\":", '"orders":']
    encoded_orders = None
    for marker in markers:
        for match in re.finditer(re.escape(marker), html):
            candidate = html[match.end() :].lstrip()
            if candidate.startswith("["):
                encoded_orders = candidate.replace('\\"', '"')
                break
        if encoded_orders is not None:
            break
    if encoded_orders is None:
        raise RuntimeError("XECONT public orders data was not found.")

    try:
        orders, _ = json.JSONDecoder().raw_decode(encoded_orders)
    except json.JSONDecodeError as error:
        raise RuntimeError("XECONT public orders data is invalid.") from error

    records = []
    for order in orders:
        contact = order.get("contact") or order.get("shipper") or {}
        pickup = order.get("pickup") or order.get("from") or {}
        dropoff = order.get("dropoff") or order.get("to") or {}
        route = f"{order.get('route_from', pickup.get('province', ''))} - "
        route += order.get("route_to", dropoff.get("province", ""))
        container_type = (
            order.get("container_type")
            or order.get("equipment")
            or order.get("vehicle_type")
            or ""
        )
        cargo_type = order.get("cargo_type") or order.get("cargo") or ""
        quantity = order.get("cont_total") or order.get("quantity") or order.get("container_count") or ""
        pickup_date = (
            order.get("pickup_date")
            or order.get("pickupAt")
            or order.get("loading_date")
            or order.get("start_date")
            or ""
        )
        temperature = order.get("temperature") or ""
        content = (
            f"Route: {route}. "
            f"Pickup: {pickup.get('address', '')}. "
            f"Drop-off: {dropoff.get('address', '')}. "
            f"Cargo: {cargo_type}. "
            f"Container: {container_type}. "
            f"Quantity: {quantity}. "
            f"Temperature: {temperature}. "
            f"Pickup date: {pickup_date}."
        )
        order_code = order.get("code") or order.get("id", "order")
        records.append(
            {
                "source": source["name"],
                "source_url": source["url"],
                "title": f"{order_code} - {route}",
                "content": content,
                "url": f"{source['url']}#order-{order_code}",
                "published": order.get("posted_at") or order.get("published_at", ""),
                "phones": [contact["phone"]] if contact.get("phone") else [],
                "requires_route": source.get("require_route", False),
            }
        )
    return records


def parse_html(source: dict, html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    selector = source.get("item_selector", "article, .post, .item, .news-item")
    records = []
    for item in soup.select(selector):
        title_node = item.select_one(source.get("title_selector", "h1, h2, h3, .title"))
        link_node = item.select_one("a[href]")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        content = item.get_text(" ", strip=True)
        link = urljoin(source["url"], link_node.get("href")) if link_node else source["url"]
        records.append(
            {
                "source": source["name"],
                "source_url": source["url"],
                "title": title,
                "content": content[:4000],
                "url": link,
                "published": "",
            }
        )
    return records


def collect_source(source: dict) -> list[dict]:
    if source.get("type", "rss").lower() == "rss":
        return parse_feed(source)
    html = requests.get(
        source["url"],
        headers={"User-Agent": "TransportLeadMonitor/1.0"},
        timeout=60,
    )
    html.raise_for_status()
    if source.get("type", "").lower() == "xecont":
        return parse_xecont(source, html.text)
    return parse_html(source, fetch_with_crawlbase(source["url"]))


def filter_records(records: list[dict], config: dict) -> list[dict]:
    vehicle_terms = [normalize_text(term) for term in config["vehicle_terms"]]
    route_terms = [normalize_text(term) for term in config["route_terms"]]
    filtered = []
    seen = set()

    for record in records:
        searchable = normalize_text(f"{record['title']} {record['content']}")
        has_vehicle = (
            re.search(r"45\s*rf|48\s*rf", searchable) is not None
            or any(term in searchable for term in vehicle_terms)
        )
        requires_route = record.get("requires_route", True)
        has_route = any(term in searchable for term in route_terms)
        if not has_vehicle or (requires_route and not has_route):
            continue

        key = record["url"] or hashlib.sha256(searchable.encode()).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        record["phones"] = sorted(
            set(record.get("phones", []))
            | set(extract_phones(f"{record['title']} {record['content']}"))
        )
        record["collected_at"] = datetime.now(timezone.utc).isoformat()
        filtered.append(record)
    return filtered


def write_csv(records: list[dict], path: str) -> None:
    fields = ["collected_at", "published", "source", "title", "phones", "url", "content"]
    with open(path, "w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "collected_at": record["collected_at"],
                    "published": record.get("published", ""),
                    "source": record["source"],
                    "title": record["title"],
                    "phones": "; ".join(record.get("phones", [])),
                    "url": record["url"],
                    "content": record["content"],
                }
            )


def append_to_sheet(records: list[dict]) -> None:
    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    spreadsheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not service_account_json or not spreadsheet_id:
        print("Google Sheets skipped: required secrets are not set.")
        return

    credentials = Credentials.from_service_account_info(
        json.loads(service_account_json), scopes=SHEETS_SCOPES
    )
    sheet = gspread.authorize(credentials).open_by_key(spreadsheet_id).worksheet(
        os.environ.get("GOOGLE_SHEET_WORKSHEET", "Leads")
    )
    if not records:
        return
    sheet.append_rows(
        [
            [
                record["collected_at"],
                record.get("published", ""),
                record["source"],
                record["title"],
                "; ".join(record.get("phones", [])),
                record["url"],
                record["content"],
            ]
            for record in records
        ],
        value_input_option="USER_ENTERED",
    )


def save_to_database(records: list[dict]) -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("Database skipped: DATABASE_URL is not set.")
        return
    if not records:
        return

    with psycopg2.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                create table if not exists transport_leads (
                    id bigserial primary key,
                    source text not null,
                    source_url text,
                    title text,
                    content text,
                    url text unique,
                    phones text[],
                    published text,
                    collected_at timestamptz not null default now(),
                    status text not null default 'new'
                )
                """
            )
            for record in records:
                cursor.execute(
                    """
                    insert into transport_leads
                        (source, source_url, title, content, url, phones, published)
                    values (%s, %s, %s, %s, %s, %s, %s)
                    on conflict (url) do nothing
                    """,
                    (
                        record["source"],
                        record["source_url"],
                        record["title"],
                        record["content"],
                        record["url"],
                        record.get("phones", []),
                        record.get("published", ""),
                    ),
                )


def telegram_request(endpoint: str, data: dict, files: dict | None = None) -> None:
    for attempt in range(1, TELEGRAM_MAX_ATTEMPTS + 1):
        try:
            response = requests.post(
                endpoint,
                data=data,
                files=files,
                timeout=TELEGRAM_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            result = response.json()
            if not result.get("ok"):
                raise RuntimeError(f"Telegram rejected request: {result}")
            return
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as error:
            if attempt == TELEGRAM_MAX_ATTEMPTS:
                raise RuntimeError(
                    f"Telegram connection failed after {TELEGRAM_MAX_ATTEMPTS} attempts."
                ) from error
            print(
                f"Telegram connection attempt {attempt} failed; "
                f"retrying in {attempt * 5} seconds."
            )
            time.sleep(attempt * 5)


def build_telegram_messages(records: list[dict]) -> list[str]:
    if not records:
        return ["<b>🚚 Transport leads</b>\n\nKhông tìm thấy kết quả phù hợp trong lần quét này."]

    messages = [
        "<b>🚚 TRANSPORT LEADS</b>\n"
        f"🔎 Tìm thấy: <b>{len(records)}</b> kết quả\n"
        "📌 Nguồn công khai - vui lòng kiểm tra trước khi liên hệ."
    ]
    for index, record in enumerate(records, start=1):
        phones = "; ".join(record.get("phones", [])) or "Not found"
        title = html.escape(record["title"] or "Untitled")
        source = html.escape(record["source"])
        published = html.escape(record.get("published", "") or "Unknown")
        content = html.escape(record.get("content", "")[:1200])
        phone_text = html.escape(phones)
        link = html.escape(record["url"], quote=True)
        details = (
            f"\n\n<b>📦 Lead #{index}: {title}</b>\n"
            f"📰 <b>Nguồn:</b> {source}\n"
            f"🕒 <b>Đăng:</b> {published}\n"
            f"📞 <b>Điện thoại:</b> {phone_text}\n"
            f"📝 <b>Chi tiết:</b> {content}\n"
            f'🔗 <a href="{link}">Mở nguồn</a>'
        )
        detail_parts = [
            details[start : start + TELEGRAM_MESSAGE_LIMIT]
            for start in range(0, len(details), TELEGRAM_MESSAGE_LIMIT)
        ]
        if len(messages[-1]) + len(detail_parts[0]) <= TELEGRAM_MESSAGE_LIMIT:
            messages[-1] += detail_parts.pop(0)
        for part in detail_parts:
            messages.append(part)
    return messages


def send_telegram(path: str, records: list[dict]) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_ids = [item.strip() for item in os.environ.get("TELEGRAM_CHAT_IDS", "").split(";") if item.strip()]
    if not token or not chat_ids:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS are required.")

    message_endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    document_endpoint = f"https://api.telegram.org/bot{token}/sendDocument"
    messages = build_telegram_messages(records)
    for chat_id in chat_ids:
        for message in messages:
            telegram_request(
                message_endpoint,
                {"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            )
        with open(path, "rb") as document:
            telegram_request(
                document_endpoint,
                {
                    "chat_id": chat_id,
                    "caption": f"CSV attachment: {len(records)} record(s)",
                },
                files={"document": (Path(path).name, document, "text/csv")},
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="sources.json")
    parser.add_argument("--report", default="transport_leads.csv")
    parser.add_argument("--no-telegram", action="store_true")
    args = parser.parse_args()

    config = read_config(args.config)
    all_records = []
    for source in config["sources"]:
        try:
            records = collect_source(source)
            print(f"Read {len(records)} item(s) from {source['name']}.")
            all_records.extend(records)
        except Exception as error:
            print(f"Source failed: {source.get('name', source.get('url'))}: {error}")

    records = filter_records(all_records, config)
    if not all_records:
        raise RuntimeError("No configured source could be read.")

    write_csv(records, args.report)
    append_to_sheet(records)
    save_to_database(records)
    if not args.no_telegram:
        send_telegram(args.report, records)
    print(f"Created {args.report} with {len(records)} matching record(s).")


if __name__ == "__main__":
    main()
