#!/usr/bin/env python3
"""
Slack Reminder Bot untuk GitHub Actions.

- Trigger per reminder via hashtag: #h-14 #h-7 #h-5 #h-3 #sehari (kosong = semua)
- Mention 1 orang (owner)
- Berhenti mengingatkan jika salah satu pesan reminder sudah diberi reaksi ✅

Env:
  SLACK_BOT_TOKEN   xoxb-<TOKEN>  scope: chat:write, channels:history (+ groups:history utk private)
  SLACK_CHANNEL     channel ID default, mis. C0123ABCD
  ACK_OWNER_ONLY    "true" = hanya ✅ dari owner yang dihitung (default: ✅ dari siapa pun)
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import yaml

TZ = ZoneInfo("Asia/Jakarta")
API = "https://slack.com/api/"
EVENT_TYPE = "deadline_reminder"
ACK_EMOJIS = {"white_check_mark", "heavy_check_mark", "ballot_box_with_check"}

HASHTAGS = {"#h-14": 14, "#h-7": 7, "#h-5": 5, "#h-3": 3, "#sehari": 1, "#h-1": 1}
ALL_TRIGGERS = [14, 7, 5, 3, 1]
STYLE = {
    14: (":calendar:", "2 minggu lagi"),
    7: (":spiral_calendar_pad:", "1 minggu lagi"),
    5: (":hourglass_flowing_sand:", "5 hari lagi"),
    3: (":warning:", "3 hari lagi"),
    1: (":rotating_light:", "BESOK"),
}

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("reminder")


# ------------------------------------------------------------------ config
def parse_triggers(value) -> list[int]:
    if not value:
        return ALL_TRIGGERS
    tags = re.findall(r"#[\w-]+", str(value).lower())
    unknown = [t for t in tags if t not in HASHTAGS]
    if unknown or not tags:
        raise ValueError(f"hashtag tidak dikenal: {unknown or value}. "
                         f"Pilihan: #h-14, #h-7, #h-5, #h-3, #sehari")
    return sorted({HASHTAGS[t] for t in tags}, reverse=True)


def parse_owner(value) -> str | None:
    """Terima 'U0123ABCD' atau '<@U0123ABCD>' -> 'U0123ABCD'."""
    if not value:
        return None
    m = re.fullmatch(r"<?@?([UW][A-Z0-9]{6,})>?", str(value).strip())
    if not m:
        raise ValueError(f"owner harus Slack member ID (mis. U0123ABCD), bukan '{value}'")
    return m.group(1)


def load_reminders(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        items = (yaml.safe_load(f) or {}).get("reminders", [])
    errors, ids = [], set()
    for i, r in enumerate(items, 1):
        name = r.get("title", f"item #{i}")
        if not r.get("title") or not r.get("due_date"):
            errors.append(f"{name}: wajib ada title dan due_date")
            continue
        if not isinstance(r["due_date"], date):
            try:
                r["due_date"] = datetime.strptime(str(r["due_date"]), "%Y-%m-%d").date()
            except ValueError:
                errors.append(f"{name}: due_date harus format YYYY-MM-DD")
                continue
        try:
            r["_triggers"] = parse_triggers(r.get("trigger"))
            r["_owner"] = parse_owner(r.get("owner"))
        except ValueError as e:
            errors.append(f"{name}: {e}")
        r.setdefault("id", re.sub(r"\W+", "-", r["title"].lower()).strip("-"))
        r["_key"] = f"{r['id']}@{r['due_date']}"  # deadline baru = siklus reminder baru
        if r["_key"] in ids:
            errors.append(f"{name}: id duplikat '{r['id']}'")
        ids.add(r["_key"])
    if errors:
        raise SystemExit("Config tidak valid:\n- " + "\n- ".join(errors))
    return items


# ------------------------------------------------------------------ slack
def slack(method: str, token: str, http: str = "post", **params) -> dict:
    for attempt in range(1, 4):
        kw = {"json": params} if http == "post" else {"params": params}
        resp = requests.request(http, API + method, timeout=20,
                                headers={"Authorization": f"Bearer {token}"}, **kw)
        if resp.status_code == 429:
            time.sleep(int(resp.headers.get("Retry-After", 5)))
            continue
        data = resp.json()
        if data.get("ok"):
            return data
        if data.get("error") in {"channel_not_found", "not_in_channel", "invalid_auth",
                                 "missing_scope", "not_authed"}:
            raise RuntimeError(data["error"])
        time.sleep(2 * attempt)
    raise RuntimeError(data.get("error", "unknown"))


def acknowledged_keys(token: str, channel: str, today: date, owner_only: bool,
                      owners: dict[str, str | None]) -> set[str]:
    """Cari pesan reminder bot (16 hari terakhir) yang sudah diberi ✅."""
    oldest = datetime.combine(today - timedelta(days=16), datetime.min.time(), TZ).timestamp()
    acked, cursor = set(), None
    while True:
        params = {"channel": channel, "oldest": str(oldest), "limit": 200, "include_all_metadata": "true"}
        if cursor:
            params["cursor"] = cursor
        data = slack("conversations.history", token, http="get", **params)
        for msg in data.get("messages", []):
            meta = msg.get("metadata") or {}
            if meta.get("event_type") != EVENT_TYPE:
                continue
            key = meta.get("event_payload", {}).get("key")
            for react in msg.get("reactions", []):
                if react["name"] not in ACK_EMOJIS:
                    continue
                owner = owners.get(key)
                if not owner_only or not owner or owner in react.get("users", []):
                    acked.add(key)
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return acked


def build_message(r: dict, days_left: int) -> tuple[str, list[dict]]:
    emoji, label = STYLE[days_left]
    due = r["due_date"].strftime("%d %b %Y")
    lines = [f"*Deadline:* {due} — *{label}*"]
    if r["_owner"]:
        lines.insert(0, f"<@{r['_owner']}>")
    if r.get("note"):
        lines.append(f"*Catatan:* {r['note']}")
    if r.get("link"):
        lines.append(f"<{r['link']}|Buka detail>")
    blocks = [
        {"type": "header", "text": {"type": "plain_text",
                                    "text": f"{emoji} H-{days_left} · {r['title']}"[:150], "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        {"type": "context", "elements": [{"type": "mrkdwn",
                                          "text": "Beri reaksi :white_check_mark: jika sudah selesai, reminder berikutnya tidak akan dikirim."}]},
    ]
    return f"Reminder H-{days_left}: {r['title']} (deadline {due})", blocks


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="reminders.yaml")
    ap.add_argument("--date", help="Simulasi tanggal hari ini (YYYY-MM-DD)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--validate", action="store_true")
    args = ap.parse_args()

    reminders = [r for r in load_reminders(args.config) if r.get("active") is not False]
    if args.validate:
        log.info("Config valid: %d reminder aktif", len(reminders))
        return 0

    token = os.getenv("SLACK_BOT_TOKEN")
    default_channel = os.getenv("SLACK_CHANNEL")
    owner_only = os.getenv("ACK_OWNER_ONLY", "").lower() == "true"
    if not token and not args.dry_run:
        log.error("Secret SLACK_BOT_TOKEN belum di-set")
        return 1

    today = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else datetime.now(TZ).date()
    log.info("Tanggal acuan: %s", today)

    due_today = []
    for r in reminders:
        r["_channel"] = r.get("channel") or default_channel
        days_left = (r["due_date"] - today).days
        if days_left in r["_triggers"]:
            if not r["_channel"]:
                log.error("%s: channel kosong (isi var SLACK_CHANNEL)", r["id"])
                return 1
            due_today.append((r, days_left))
    if not due_today:
        log.info("Tidak ada reminder hari ini")
        return 0

    # Cek ✅ per channel
    acked: set[str] = set()
    if token:
        owners = {r["_key"]: r["_owner"] for r, _ in due_today}
        for ch in {r["_channel"] for r, _ in due_today}:
            try:
                acked |= acknowledged_keys(token, ch, today, owner_only, owners)
            except RuntimeError as e:
                log.error("Gagal baca channel %s: %s", ch, e)
                return 1

    sent = failed = 0
    for r, days_left in due_today:
        if r["_key"] in acked:
            log.info("Lewati %s: sudah di-✅", r["id"])
            continue
        text, blocks = build_message(r, days_left)
        if args.dry_run:
            print(f"[DRY RUN] {r['_channel']} -> {text}")
            continue
        try:
            slack("chat.postMessage", token, channel=r["_channel"], text=text, blocks=blocks,
                  unfurl_links=False,
                  metadata={"event_type": EVENT_TYPE,
                            "event_payload": {"key": r["_key"], "days_left": days_left}})
            sent += 1
            log.info("Terkirim: %s H-%d", r["id"], days_left)
        except RuntimeError as e:
            failed += 1
            log.error("%s: gagal kirim (%s)", r["id"], e)
    log.info("Selesai: %d terkirim, %d gagal", sent, failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
