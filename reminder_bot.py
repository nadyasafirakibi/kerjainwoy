#!/usr/bin/env python3
"""
Slack Reminder Bot (GitHub Actions) — reminder dari chat ber-hashtag, tanpa tanggal.

Tulis pesan biasa di channel dengan hashtag trigger:
    Follow up proposal ke partner @andi #sehari #h-3 #h-7

Hashtag = jumlah hari SETELAH chat dikirim:
    #sehari = 1 hari, #h-3 = 3 hari, #h-5 = 5 hari, #h-7 = 7 hari, #h-14 = 14 hari

Bot dijalankan tiap pagi, membalas di thread chat tersebut dan selalu me-mention
1 user yang ditentukan di REMINDER_USER_ID.
Reminder berhenti jika chat utama atau balasan reminder diberi reaksi ✅.

Env:
  SLACK_BOT_TOKEN  xoxb-<TOKEN>  scope: chat:write, channels:history (+ groups:history)
  SLACK_CHANNEL     channel ID yang dipindai, mis. C0123ABCD
  REMINDER_USER_ID  member ID yang selalu di-mention, mis. U0123ABCD
  ACK_OWNER_ONLY    "true" = hanya ✅ dari REMINDER_USER_ID yang dihitung
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Asia/Jakarta")
API = "https://slack.com/api/"
EVT_REMINDER = "chat_reminder"
ACK_EMOJIS = {"white_check_mark", "heavy_check_mark", "ballot_box_with_check"}

HASHTAGS = {"#sehari": 1, "#h-3": 3, "#h-5": 5, "#h-7": 7, "#h-14": 14}
TAG_NAME = {v: k for k, v in HASHTAGS.items()}
EMOJI = {1: ":bell:", 3: ":hourglass_flowing_sand:", 5: ":warning:", 7: ":warning:", 14: ":rotating_light:"}
SCAN_DAYS = max(HASHTAGS.values()) + 2  # cukup pindai 16 hari terakhir

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("reminder")


# ================================================================ parsing
def normalize(text: str) -> str:
    # Slack bisa mengubah #nama jadi <#C123|nama> jika channel bernama sama ada
    return re.sub(r"<#C[A-Z0-9]+\|([^>]*)>", lambda m: f"#{m.group(1)}", text or "")


def parse_triggers(text: str) -> list[int]:
    tags = {t.lower() for t in re.findall(r"#[\w-]+", text)}
    return sorted({HASHTAGS[t] for t in tags if t in HASHTAGS})


# ================================================================ slack api
def slack(method: str, token: str, http: str = "post", **params) -> dict:
    data: dict = {}
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


def paginate(method: str, token: str, key: str, **params) -> list[dict]:
    out, cursor = [], None
    while True:
        p = dict(params, limit=200, include_all_metadata="true")
        if cursor:
            p["cursor"] = cursor
        data = slack(method, token, http="get", **p)
        out += data.get(key, [])
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return out


def has_ack(msg: dict, owner: str, owner_only: bool) -> bool:
    return any(r["name"] in ACK_EMOJIS and (not owner_only or owner in r.get("users", []))
               for r in msg.get("reactions", []))


def payload(msg: dict, event_type: str) -> dict | None:
    md = msg.get("metadata") or {}
    return md.get("event_payload") or {} if md.get("event_type") == event_type else None


# ================================================================ reply
def reminder_blocks(owner: str, day: int, triggers: list[int]) -> tuple[str, list[dict]]:
    nxt = next((t for t in triggers if t > day), None)
    follow = (f"Reminder berikutnya: {TAG_NAME[nxt]} ({nxt - day} hari lagi)." if nxt
              else "Ini reminder terakhir.")
    return (f"Reminder {TAG_NAME[day]} KERJAIN JANGAN BESOK BESOK", [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"{EMOJI[day]} <@{owner}> Reminder *{TAG_NAME[day]}* — sudah {day} hari sejak chat ini dikirim."}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"{follow} Beri reaksi :white_check_mark: jika sudah selesai."}]},
    ])


# ================================================================ main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="Simulasi tanggal hari ini (YYYY-MM-DD)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    token, channel = os.getenv("SLACK_BOT_TOKEN"), os.getenv("SLACK_CHANNEL")
    owner = (os.getenv("REMINDER_USER_ID") or "").strip().strip("<@>")
    owner_only = os.getenv("ACK_OWNER_ONLY", "").lower() == "true"
    if not token or not channel:
        log.error("Set secret SLACK_BOT_TOKEN dan variable SLACK_CHANNEL")
        return 1
    if not re.fullmatch(r"[UW][A-Z0-9]{6,}", owner):
        log.error("Variable REMINDER_USER_ID belum di-set / bukan member ID (contoh: U0123ABCD)")
        return 1

    today = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else datetime.now(TZ).date()
    oldest = datetime.combine(today - timedelta(days=SCAN_DAYS), datetime.min.time(), TZ).timestamp()
    log.info("Tanggal acuan %s", today)

    try:
        messages = paginate("conversations.history", token, "messages",
                            channel=channel, oldest=str(oldest))
    except RuntimeError as e:
        log.error("Gagal membaca channel: %s", e)
        return 1

    sent = skipped = failed = 0
    for msg in messages:
        if msg.get("bot_id") or msg.get("subtype"):
            continue
        text = normalize(msg.get("text", ""))
        triggers = parse_triggers(text)
        if not triggers:
            continue

        posted = datetime.fromtimestamp(float(msg["ts"]), TZ).date()
        day = (today - posted).days
        if day not in triggers:
            continue

        ts = msg["ts"]
        if has_ack(msg, owner, owner_only):
            skipped += 1
            continue
        bot_replies = [r for r in paginate("conversations.replies", token, "messages",
                                           channel=channel, ts=ts)[1:]
                       if payload(r, EVT_REMINDER) is not None]
        if any(has_ack(r, owner, owner_only) for r in bot_replies):
            skipped += 1
            continue
        if any(payload(r, EVT_REMINDER).get("day") == day for r in bot_replies):
            continue  # sudah terkirim (mis. workflow di-rerun)

        text_out, blocks = reminder_blocks(owner, day, triggers)
        if args.dry_run:
            print(f"[DRY RUN] thread {ts} -> {text_out}")
            continue
        try:
            slack("chat.postMessage", token, channel=channel, thread_ts=ts, text=text_out, blocks=blocks,
                  metadata={"event_type": EVT_REMINDER, "event_payload": {"day": day}})
            sent += 1
            log.info("Terkirim %s untuk thread %s", TAG_NAME[day], ts)
        except RuntimeError as e:
            failed += 1
            log.error("Gagal kirim thread %s: %s", ts, e)

    log.info("Selesai: %d terkirim, %d dilewati (sudah ✅), %d gagal", sent, skipped, failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
