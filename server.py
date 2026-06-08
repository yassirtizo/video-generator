"""
server.py  –  Flask backend for the Video Generator
Run: python server.py
"""

import os
import json
import asyncio
import threading
import traceback
from pathlib import Path

import ffmpeg
import edge_tts
from flask import Flask, request, jsonify, send_file, render_template_string
from flask import send_from_directory

app = Flask(__name__, static_folder=".", template_folder=".")

# ── Global job state ────────────────────────────────────────────────────────
job = {
    "running": False,
    "done":    False,
    "error":   None,
    "progress": 0,
    "message": "جاهز",
    "log":     "",
}
OUTPUT_FILE = "final_output.mp4"


# ────────────────────────────────────────────────────────────────────────────
# Helper: hex ASS color is already passed from the frontend (e.g. &H00FFFFFF)
# ────────────────────────────────────────────────────────────────────────────

def to_ass_time(secs):
    h   = int(secs // 3600)
    m   = int((secs % 3600) // 60)
    s   = int(secs % 60)
    cs  = int(round((secs % 1) * 100))
    if cs == 100:
        s += 1; cs = 0
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


async def generate_voice_and_subtitles(
    text, voice, audio_file, ass_file,
    font_name, font_size, outline_size, alignment,
    active_color, default_color, outline_color,
    voice_delay
):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(audio_file)

    probe    = ffmpeg.probe(audio_file)
    duration = float(probe["format"]["duration"])

    words          = text.split()
    total_words    = len(words)
    time_per_word  = duration / total_words

    # ASS alignment: 2=bottom-center, 5=middle-center, 8=top-center
    ass_header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},{font_size},"
        f"{default_color},{active_color},{outline_color},&H00000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline_size},0,{alignment},10,10,0,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    events       = []
    current_time = float(voice_delay)

    for i in range(0, total_words, 2):
        chunk = words[i:i+2]
        if not chunk:
            break

        word1 = chunk[0].upper()
        word2 = chunk[1].upper() if len(chunk) > 1 else ""

        t_start = current_time
        t_mid   = current_time + time_per_word
        t_end   = t_mid + time_per_word if word2 else t_mid

        s_start = to_ass_time(t_start)
        s_mid   = to_ass_time(t_mid)
        s_end   = to_ass_time(t_end)

        if word2:
            line1 = (
                f"Dialogue: 0,{s_start},{s_mid},Default,,0,0,0,,"
                f"{{\\c{active_color}}}{word1}"
                f" {{\\c{default_color}}}{word2}"
            )
            line2 = (
                f"Dialogue: 0,{s_mid},{s_end},Default,,0,0,0,,"
                f"{{\\c{default_color}}}{word1}"
                f" {{\\c{active_color}}}{word2}"
            )
            events.append(line1)
            events.append(line2)
        else:
            line = (
                f"Dialogue: 0,{s_start},{s_mid},Default,,0,0,0,,"
                f"{{\\c{active_color}}}{word1}"
            )
            events.append(line)

        current_time = t_end

    with open(ass_file, "w", encoding="utf-8") as f:
        f.write(ass_header + "\n".join(events))


def create_video(
    video_file, music_file, voice_file, ass_file,
    output_file, voice_delay, music_volume
):
    video_probe    = ffmpeg.probe(video_file)
    video_duration = float(video_probe["format"]["duration"])

    voice_probe    = ffmpeg.probe(voice_file)
    voice_duration = float(voice_probe["format"]["duration"])

    logo_start = voice_duration + float(voice_delay)

    video_input = ffmpeg.input(video_file)
    music_input = ffmpeg.input(music_file)
    voice_input = ffmpeg.input(voice_file)

    blurred      = video_input.video.filter("boxblur", luma_radius=5, luma_power=1)
    ass_fixed    = ass_file.replace("\\", "/").replace(":", "\\:")
    with_subs    = blurred.filter("subtitles", ass_fixed)

    video_stream = with_subs

    # logo.png overlay (optional — only if file exists)
    if os.path.exists("logo.png"):
        logo_input  = ffmpeg.input("logo.png", loop=1, t=video_duration)
        scaled_logo = logo_input.filter("scale", 1000, -1)
        faded_logo  = scaled_logo.filter(
            "fade", type="in",
            start_time=logo_start, duration=1.0, alpha=1
        )
        video_stream = ffmpeg.overlay(
            with_subs, faded_logo,
            x="(W-w)/2", y="(H-h)/2",
            enable=f"between(t,{logo_start},{video_duration})"
        )

    delay_ms      = int(float(voice_delay) * 1000)
    delayed_voice = voice_input.audio.filter("adelay", f"{delay_ms}|{delay_ms}")
    scaled_music  = music_input.audio.filter("volume", music_volume)
    mixed_audio   = ffmpeg.filter([scaled_music, delayed_voice], "amix", duration="first")

    stream = ffmpeg.output(
        video_stream, mixed_audio, output_file,
        vcodec="libx264", acodec="aac",
        shortest=None
    )
    ffmpeg.run(stream, overwrite_output=True)


def run_job(video_path, music_path, settings):
    global job
    voice_file = "temp_voice.mp3"
    ass_file   = "temp_subs.ass"
    try:
        job.update(progress=20, message="توليد الصوت…", log="🎙️ جارٍ توليد الصوت وملف الترجمة…")

        asyncio.run(generate_voice_and_subtitles(
            text          = settings["quote"],
            voice         = settings["voice"],
            audio_file    = voice_file,
            ass_file      = ass_file,
            font_name     = settings["font_name"],
            font_size     = settings["font_size"],
            outline_size  = settings["outline_size"],
            alignment     = settings["alignment"],
            active_color  = settings["active_color"],
            default_color = settings["default_color"],
            outline_color = settings["outline_color"],
            voice_delay   = settings["voice_delay"],
        ))

        job.update(progress=50, message="معالجة الفيديو…", log="🎬 جارٍ دمج الفيديو والصوت والترجمات…")

        create_video(
            video_file   = video_path,
            music_file   = music_path,
            voice_file   = voice_file,
            ass_file     = ass_file,
            output_file  = OUTPUT_FILE,
            voice_delay  = settings["voice_delay"],
            music_volume = settings["music_volume"],
        )

        job.update(progress=100, message="اكتمل!", log="✅ تم إنشاء الفيديو!", done=True)

    except Exception as exc:
        job.update(error=str(exc), log="❌ خطأ: " + traceback.format_exc())
    finally:
        for f in [voice_file, ass_file, video_path, music_path]:
            try:
                if f and os.path.exists(f):
                    os.remove(f)
            except Exception:
                pass
        job["running"] = False


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/generate", methods=["POST"])
def generate():
    global job
    if job["running"]:
        return "هناك مهمة قيد التنفيذ. انتظر حتى تنتهي.", 429

    bg_file    = request.files.get("bg_file")
    music_file = request.files.get("music_file")
    settings   = json.loads(request.form.get("settings", "{}"))

    if not bg_file or not music_file:
        return "يجب رفع ملف الفيديو وملف الموسيقى.", 400
    if not settings.get("quote", "").strip():
        return "الاقتباس فارغ.", 400

    # Save uploads
    video_path = f"upload_bg_{bg_file.filename}"
    music_path = f"upload_music_{music_file.filename}"
    bg_file.save(video_path)
    music_file.save(music_path)

    # Reset state and launch thread
    job.update(running=True, done=False, error=None,
               progress=5, message="رفع الملفات…", log="")

    t = threading.Thread(target=run_job, args=(video_path, music_path, settings), daemon=True)
    t.start()

    return jsonify({"ok": True})


@app.route("/status")
def status():
    return jsonify({
        "progress": job["progress"],
        "message":  job["message"],
        "log":      job.get("log", ""),
        "done":     job["done"],
        "error":    job["error"],
    })


@app.route("/download")
def download():
    if not os.path.exists(OUTPUT_FILE):
        return "الملف غير موجود.", 404
    return send_file(OUTPUT_FILE, as_attachment=True, download_name="final_output.mp4")


if __name__ == "__main__":
    print("🚀 الخادم يعمل على: http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)