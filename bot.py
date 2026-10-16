"""Spotify-link -> MP3 Telegram bot (track links).
Run: python3 bot.py   (token is built in; no env needed)
Pipeline (verified 2026-10-12):
  Spotify embed __NEXT_DATA__ (no auth) -> title+artists ->
  yt-dlp ytsearch1 -> mp3 (imageio-ffmpeg) -> mutagen tags + cover -> send_audio
"""
import logging
import asyncio
import os
import re
import json
import tempfile
import urllib.request
import urllib.parse

import imageio_ffmpeg
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (ApplicationBuilder, CommandHandler, MessageHandler,
                          CallbackQueryHandler, ContextTypes, filters)

FF = imageio_ffmpeg.get_ffmpeg_exe()
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

SP_RE = re.compile(r"open\.spotify\.com/(track|album|playlist)/([A-Za-z0-9]+)")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("spotibot")


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=timeout).read()


def spotify_track_info(track_id):
    """Return (title, artist, cover_url) via embed page, fallback oembed."""
    title = artist = None
    try:
        raw = http_get(f"https://open.spotify.com/embed/track/{track_id}").decode("utf-8", "ignore")
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', raw, re.S)
        if m:
            d = json.loads(m.group(1))
            ent = d["props"]["pageProps"]["state"]["data"]["entity"]
            title = ent.get("title") or ent.get("name")
            arts = ent.get("artists") or []
            artist = ", ".join(a.get("name", "") for a in arts) if arts else None
    except Exception as e:
        log.warning("embed parse failed: %s", e)
    # always get cover (and missing artist) from oembed
    cover_url = None
    try:
        raw = http_get(f"https://open.spotify.com/oembed?url=https://open.spotify.com/track/{track_id}").decode("utf-8", "ignore")
        d = json.loads(raw)
        cover_url = d.get("thumbnail_url")
        if not title:
            title = d.get("title")
        if not artist:
            artist = d.get("author_name") or "Unknown"
    except Exception as e:
        log.warning("oembed failed: %s", e)
        if not artist:
            artist = "Unknown"
    if not title:
        return None, None, None
    return title, artist, cover_url


def download_mp3(query, out_mp3):
    import subprocess
    base_cmd = ["yt-dlp", "ytsearch1:" + query, "-x", "--audio-format", "mp3",
                "--ffmpeg-location", FF, "-o", out_mp3.replace(".mp3", ".%(ext)s"),
                "--no-playlist", "-q", "--js-runtimes", "node"]
    # YouTube keeps breaking player clients; try several in order.
    client_variants = [
        ["--extractor-args", "youtube:player_client=ios,android"],
        ["--extractor-args", "youtube:player_client=android,web"],
        [],  # yt-dlp smart defaults as last resort
    ]
    last_err = ""
    for extra in client_variants:
        r = subprocess.run(base_cmd + extra, capture_output=True, text=True, timeout=300)
        if os.path.exists(out_mp3):
            return out_mp3
        # yt-dlp may append .mp3 to template already ending .mp3? check siblings
        base = os.path.dirname(out_mp3)
        try:
            cands = [os.path.join(base, f) for f in os.listdir(base) if f.endswith(".mp3")]
        except Exception:
            cands = []
        if cands:
            os.rename(cands[0], out_mp3)
            return out_mp3
        last_err = (r.stderr or r.stdout)[-500:]
        log.warning("yt-dlp attempt %s failed: %s", extra, last_err[-200:])
    raise RuntimeError("yt-dlp failed: " + last_err)


def tag_mp3(path, title, artist, cover_bytes=None):
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TIT2, TPE1, APIC
    try:
        audio = MP3(path, ID3=ID3)
    except Exception:
        audio = MP3(path)
        audio.add_tags()
    audio.tags.delall("TIT2"); audio.tags.delall("TPE1"); audio.tags.delall("APIC")
    audio.tags.add(TIT2(encoding=3, text=title))
    audio.tags.add(TPE1(encoding=3, text=artist))
    if cover_bytes:
        audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes))
    audio.save()


def get_lyrics(title, artist):
    """Fetch lyrics via LRClib (same source Spotify shows). Returns str or None."""
    q = urllib.parse.urlencode({"track_name": title, "artist_name": artist})
    # 1) exact match
    try:
        d = json.loads(http_get(f"https://lrclib.net/api/get?{q}", timeout=20))
        txt = d.get("plainLyrics") or ""
        if txt.strip():
            return txt.strip()
    except Exception as e:
        log.warning("lrclib get failed: %s", e)
    # 2) search + best pick
    try:
        d = json.loads(http_get(f"https://lrclib.net/api/search?{q}", timeout=20))
        if isinstance(d, list) and d:
            best = d[0]
            for it in d:
                if (it.get("trackName", "").lower() == title.lower()
                        and it.get("artistName", "").lower() == artist.lower()):
                    best = it
                    break
            txt = best.get("plainLyrics") or ""
            if txt.strip():
                return txt.strip()
    except Exception as e:
        log.warning("lrclib search failed: %s", e)
    return None


def chunk(text, n=4000):
    """Split text into <=n-char pieces on line boundaries."""
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > n:
            out.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        out.append(cur)
    return out or [""]


async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("لینک آهنگ اسپاتیفای رو بفرست تا mp3ش رو بدم 🎵")


async def on_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    m = SP_RE.search(text)
    if not m:
        await update.message.reply_text("لینک اسپاتیفای معتبر بفرست عشقم 🥹")
        return
    kind, sid = m.group(1), m.group(2)
    if kind != "track":
        await update.message.reply_text("فعلاً فقط لینک تک‌آهنگ (track) ساپورت میشه، album/playlist رو بعداً اضافه می‌کنم 🥺")
        return
    status = await update.message.reply_text("دارم پیداش می‌کنم… 🎧")
    try:
        title, artist, cover_url = spotify_track_info(sid)
        if not title:
            await status.edit_text("نتونستم اطلاعات آهنگ رو بگیرم 😕 لینک رو چک کن")
            return
        await status.edit_text(f"🎵 {artist} – {title}\nدارم دانلودش می‌کنم…")
        cover_bytes = None
        if cover_url:
            try:
                cover_bytes = http_get(cover_url)
            except Exception:
                pass
        tmpd = tempfile.mkdtemp(prefix="spot_", dir="/tmp")
        out = os.path.join(tmpd, "song.mp3")
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: download_mp3(f"{artist} {title}", out))
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: tag_mp3(out, title, artist, cover_bytes))
        await status.edit_text("می‌فرستمش… 📤")
        btn = InlineKeyboardMarkup(
            [[InlineKeyboardButton("متن آهنگ 📝", callback_data=f"lyr:{sid}")]])
        # write cover to disk so PTB can send it as thumbnail
        thumb_path = None
        if cover_bytes:
            try:
                thumb_path = os.path.join(tmpd, "cover.jpg")
                with open(thumb_path, "wb") as fh:
                    fh.write(cover_bytes)
            except Exception:
                thumb_path = None
        with open(out, "rb") as f:
            kwargs = dict(audio=f, performer=artist, title=title,
                          caption=f"🎵 {artist} – {title}", reply_markup=btn)
            if thumb_path:
                with open(thumb_path, "rb") as tf:
                    kwargs["thumbnail"] = tf
                    await update.message.reply_audio(**kwargs)
            else:
                await update.message.reply_audio(**kwargs)
        await status.delete()
        # cleanup (disk guard)
        try:
            for p in (out, thumb_path):
                if p and os.path.exists(p):
                    os.remove(p)
            os.rmdir(tmpd)
        except Exception:
            pass
    except Exception as e:
        log.exception("failed")
        try:
            await status.edit_text(f"نشد عشقم 😞 ({str(e)[:150]})")
        except Exception:
            pass


async def on_lyrics(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle the 'متن آهنگ' button press."""
    q = update.callback_query
    await q.answer()
    sid = (q.data or "").split(":", 1)[-1]
    await q.message.reply_text("دارم متن رو پیدا می‌کنم… 📝")
    try:
        title, artist, _ = await asyncio.get_running_loop().run_in_executor(
            None, spotify_track_info, sid)
        if not title:
            await q.message.reply_text("نتونستم اسم آهنگ رو بگیرم 😕")
            return
        txt = await asyncio.get_running_loop().run_in_executor(
            None, get_lyrics, title, artist)
        if not txt:
            await q.message.reply_text(
                f"متن «{title}» پیدا نشد 😞\n"
                "LRClib این یکی رو نداره.")
            return
        head = f"🎵 {artist} – {title}\n\n"
        parts = chunk(head + txt, 4000)
        for p in parts:
            await q.message.reply_text(p)
    except Exception as e:
        log.exception("lyrics failed")
        try:
            await q.message.reply_text(f"نشد 😞 ({str(e)[:150]})")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Bot token. Hard-coded on purpose so the bot runs with zero configuration
# (just `python3 bot.py`, or deploy the repo to Railway as-is).
# You can still override it from the environment: BOT_TOKEN=xxx python3 bot.py
# ---------------------------------------------------------------------------
BOT_TOKEN = "8908201002:AAHe7_iCisckWv8kT1gAIAjgsy_e9Di9XSU"


def main():
    token = (os.environ.get("BOT_TOKEN") or BOT_TOKEN).strip()
    if not token:
        raise SystemExit("BOT_TOKEN missing.")
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(on_lyrics, pattern=r"^lyr:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_link))
    log.info("spotibot running, ffmpeg=%s", FF)
    app.run_polling()


if __name__ == "__main__":
    main()
