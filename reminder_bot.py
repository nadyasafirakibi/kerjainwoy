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
  REMIND_DAILY      "false" = matikan pengingat ulang harian (default: aktif)
  MAX_DAYS          batas hari pengingat ulang sejak chat dikirim (default 30)
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

# ======================================================================
#  ✏️  TEKS REMINDER — UBAH DI SINI SAJA
#  Placeholder yang bisa dipakai (tulis persis dengan kurung kurawal):
#    {mention}  -> tag user di REMINDER_USER_ID
#    {tag}      -> hashtag, mis. #h-3
#    {hari}     -> angka hari, mis. 3
#  Format Slack: *tebal*  _miring_  ~coret~  :emoji:  <https://url|teks link>
#  Baris baru: tulis \n
# ======================================================================
PESAN = {
    1:  ":bell: {mention} Reminder *{tag}* — sudah {hari} hari sejak chat ini dikirim.",
    3:  ":hourglass_flowing_sand: {mention} Reminder *{tag}* — sudah {hari} hari sejak chat ini dikirim.",
    5:  ":warning: {mention} Reminder *{tag}* — sudah {hari} hari sejak chat ini dikirim.",
    7:  ":warning: {mention} Reminder *{tag}* — sudah {hari} hari sejak chat ini dikirim.",
    14: ":rotating_light: {mention} Reminder *{tag}* — sudah {hari} hari sejak chat ini dikirim.",
}

# Teks kecil abu-abu di bawah pesan. Kosongkan ("") jika tidak perlu.
#   {lanjut} -> diisi PESAN_LANJUT atau PESAN_TERAKHIR
PESAN_BAWAH = "{lanjut} Beri reaksi :white_check_mark: jika sudah selesai."
PESAN_LANJUT = "Reminder berikutnya: {tag_berikut} ({sisa} hari lagi)."
PESAN_TERAKHIR = "Ini reminder terakhir."

# Pengingat ulang harian: dikirim setiap hari setelah reminder hashtag pertama
# selama chat belum diberi ✅ (bisa dimatikan dengan variable REMIND_DAILY=false)
PESAN_ULANG = ":repeat: {mention} Belum ada :white_check_mark: — sudah {hari} hari sejak chat ini dikirim."
PESAN_LANJUT_HARIAN = "Akan diingatkan lagi besok jika belum ada :white_check_mark:."

# Teks di notifikasi HP/desktop
PESAN_NOTIF = "Reminder {tag} untuk chat ini"
# ======================================================================

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
def fill(template: str, **vals) -> str:
    """Isi {placeholder} tanpa error walau teks mengandung kurung kurawal lain."""
    for k, v in vals.items():
        template = template.replace("{" + k + "}", str(v))
    return template


def reminder_blocks(owner: str, day: int, triggers: list[int],
                    daily: bool, max_days: int) -> tuple[str, list[dict]]:
    vals = {"mention": f"<@{owner}>", "tag": TAG_NAME.get(day, f"hari ke-{day}"), "hari": day}
    nxt = next((t for t in triggers if t > day), None)
    if nxt and (not daily or nxt == day + 1):
        lanjut = fill(PESAN_LANJUT, tag_berikut=TAG_NAME[nxt], sisa=nxt - day)
    elif daily and day < max_days:
        lanjut = PESAN_LANJUT_HARIAN
    else:
        lanjut = PESAN_TERAKHIR
    utama = PESAN[day] if day in triggers else PESAN_ULANG
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": fill(utama, **vals)}}]
    bawah = fill(PESAN_BAWAH, lanjut=lanjut, **vals).strip()
    if bawah:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": bawah}]})
    return fill(PESAN_NOTIF, **vals), blocks


# ================================================================ main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="Simulasi tanggal hari ini (YYYY-MM-DD)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    token, channel = os.getenv("SLACK_BOT_TOKEN"), os.getenv("SLACK_CHANNEL")
    owner = (os.getenv("REMINDER_USER_ID") or "").strip().strip("<@>")
    owner_only = os.getenv("ACK_OWNER_ONLY", "").lower() == "true"
    daily = os.getenv("REMIND_DAILY", "").lower() != "false"
    max_days = int(os.getenv("MAX_DAYS") or 30)
    scan_days = (max_days if daily else max(HASHTAGS.values())) + 2
    if not token or not channel:
        log.error("Set secret SLACK_BOT_TOKEN dan variable SLACK_CHANNEL")
        return 1
    if not re.fullmatch(r"[UW][A-Z0-9]{6,}", owner):
        log.error("Variable REMINDER_USER_ID belum di-set / bukan member ID (contoh: U0123ABCD)")
        return 1

    today = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else datetime.now(TZ).date()
    oldest = datetime.combine(today - timedelta(days=scan_days), datetime.min.time(), TZ).timestamp()
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
        is_trigger = day in triggers
        is_repeat = daily and triggers[0] < day <= max_days
        if not (is_trigger or is_repeat):
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

        text_out, blocks = reminder_blocks(owner, day, triggers, daily, max_days)
        if args.dry_run:
            print(f"[DRY RUN] thread {ts} -> {text_out}")
            continue
        try:
            slack("chat.postMessage", token, channel=channel, thread_ts=ts, text=text_out, blocks=blocks,
                  metadata={"event_type": EVT_REMINDER, "event_payload": {"day": day}})
            sent += 1
            log.info("Terkirim hari ke-%d untuk thread %s", day, ts)
        except RuntimeError as e:
            failed += 1
            log.error("Gagal kirim thread %s: %s", ts, e)

    log.info("Selesai: %d terkirim, %d dilewati (sudah ✅), %d gagal", sent, skipped, failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
