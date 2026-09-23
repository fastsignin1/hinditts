import os
import re
import time
import uuid
import urllib.parse
import requests
from flask import Flask, render_template, request, jsonify, send_from_directory

app = Flask(__name__)

BASE_DIR  = os.path.abspath(os.path.dirname(__file__))
AUDIO_DIR = os.path.join(BASE_DIR, "static", "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

# ── Tuning knobs ─────────────────────────────────────────────────────────────
MAX_TRANSLATE_CHARS = 2500   # chars per translation chunk (cuts translation requests by ~half)
MAX_TTS_CHARS       = 180    # safe max for translate_tts (reduces round trips by ~25%)
REQUEST_DELAY_TTS   = 0.12   # faster synthesis without triggering limits
REQUEST_DELAY_TRANS = 0.25   # faster translation
MAX_RETRIES         = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://translate.google.com/",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def cleanup_old_audio(max_age_hours: int = 2) -> None:
    """Delete MP3s in AUDIO_DIR that are older than max_age_hours."""
    cutoff = time.time() - max_age_hours * 3600
    try:
        for name in os.listdir(AUDIO_DIR):
            if not (name.startswith("novel_") and name.endswith(".mp3")):
                continue
            fpath = os.path.join(AUDIO_DIR, name)
            try:
                if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                    os.remove(fpath)
            except OSError:
                pass
    except Exception:
        pass


def is_mostly_hindi(text: str) -> bool:
    """Return True when the majority of letters are Devanagari."""
    return len(re.findall(r"[\u0900-\u097f]", text)) > 5


def split_for_translate(text: str, max_chars: int = MAX_TRANSLATE_CHARS):
    """Split long English text into safe-sized chunks at sentence boundaries."""
    sentences = re.split(r"(?<=[.!?\n])\s+", text.strip())
    chunks, current = [], ""
    for s in sentences:
        if not s:
            continue
        if len(current) + len(s) + 1 <= max_chars:
            current = (current + " " + s).strip()
        else:
            if current:
                chunks.append(current)
            # Sentence itself is too long — hard-split
            if len(s) > max_chars:
                for i in range(0, len(s), max_chars):
                    chunks.append(s[i : i + max_chars])
                current = ""
            else:
                current = s
    if current:
        chunks.append(current)
    return chunks


def split_for_tts(text: str, max_chars: int = MAX_TTS_CHARS):
    """
    Split Hindi (or any) text into ≤ max_chars chunks suitable for the
    Google TTS endpoint.  Priority: sentence endings → clause markers → space.
    """
    if not text:
        return []

    # First pass: split on sentence-ending punctuation, keeping the delimiter
    raw = re.split(r"([।\.!?\n]+)", text)
    sentences = []
    for i in range(0, len(raw) - 1, 2):
        joined = (raw[i] + raw[i + 1]).strip()
        if joined:
            sentences.append(joined)
    if len(raw) % 2 == 1 and raw[-1].strip():
        sentences.append(raw[-1].strip())

    chunks, current = [], ""
    for s in sentences:
        if len(s) > max_chars:
            # Sentence too long — split further at clause markers
            clauses = re.split(r"([,;:—–\s]+)", s)
            for c in clauses:
                if not c:
                    continue
                if len(current) + len(c) <= max_chars:
                    current += c
                else:
                    if current.strip():
                        chunks.append(current.strip())
                    if len(c) > max_chars:
                        # Absolute hard split
                        for i in range(0, len(c), max_chars):
                            part = c[i : i + max_chars].strip()
                            if part:
                                chunks.append(part)
                        current = ""
                    else:
                        current = c
        elif len(current) + len(s) + 1 <= max_chars:
            current = (current + " " + s).strip() if current else s
        else:
            if current.strip():
                chunks.append(current.strip())
            current = s

    if current.strip():
        chunks.append(current.strip())
    return [c for c in chunks if c.strip()]


# ── Translation ───────────────────────────────────────────────────────────────

def _clients5_post(chunk: str) -> str | None:
    try:
        r = requests.post(
            "https://clients5.google.com/translate_a/t?client=dict-chrome-ex",
            data={"sl": "en", "tl": "hi", "q": chunk},
            headers=HEADERS,
            timeout=12,
        )
        if r.status_code == 200:
            data = r.json()
            # Response can be: [["translated", ...]] or ["translated", ...]
            if isinstance(data, list) and data:
                first = data[0]
                if isinstance(first, list) and first:
                    return first[0]
                if isinstance(first, str):
                    return " ".join(str(x) for x in data if isinstance(x, str))
    except Exception:
        pass
    return None


def _clients5_get(chunk: str) -> str | None:
    try:
        r = requests.get(
            "https://clients5.google.com/translate_a/t"
            "?client=dict-chrome-ex&sl=en&tl=hi&q=" + urllib.parse.quote(chunk),
            headers=HEADERS,
            timeout=12,
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and data:
                first = data[0]
                if isinstance(first, list) and first:
                    return first[0]
                if isinstance(first, str):
                    return " ".join(str(x) for x in data if isinstance(x, str))
    except Exception:
        pass
    return None


def _gtx_translate(chunk: str) -> str | None:
    try:
        r = requests.get(
            "https://translate.googleapis.com/translate_a/single"
            "?client=gtx&sl=en&tl=hi&dt=t&q=" + urllib.parse.quote(chunk),
            headers=HEADERS,
            timeout=12,
        )
        if r.status_code == 200:
            return "".join(seg[0] for seg in r.json()[0] if seg and seg[0])
    except Exception:
        pass
    return None


def _translate_chunk(chunk: str) -> str:
    result = _clients5_post(chunk) or _clients5_get(chunk) or _gtx_translate(chunk)
    if result:
        return result
    raise RuntimeError("All translation endpoints failed.")


def translate_text(english_text: str) -> str:
    parts = split_for_translate(english_text)
    translated = []
    for chunk in parts:
        for attempt in range(MAX_RETRIES):
            try:
                translated.append(_translate_chunk(chunk))
                break
            except Exception:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(1 + attempt)
        else:
            raise RuntimeError("Translation failed after all retries.")
        time.sleep(REQUEST_DELAY_TRANS)
    return "\n".join(translated)


# ── TTS ───────────────────────────────────────────────────────────────────────

def _fetch_tts_chunk(chunk: str) -> bytes:
    url = (
        "https://translate.google.com/translate_tts"
        "?ie=UTF-8&tl=hi&client=tw-ob&q=" + urllib.parse.quote(chunk)
    )
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200 and r.content:
                return r.content
        except Exception:
            pass
        time.sleep(1 + attempt)
    raise RuntimeError(f"TTS failed for chunk: {chunk[:30]!r}")


def synthesize_to_mp3(hindi_text: str, filepath: str) -> int:
    """
    Write a Hindi MP3 to filepath.  Uses a .tmp file so that a partial
    download never leaves a corrupt file on disk.
    """
    chunks = split_for_tts(hindi_text)
    if not chunks:
        raise ValueError("No text chunks to synthesise.")

    tmp_path = filepath + ".tmp"
    try:
        with open(tmp_path, "wb") as f:
            for chunk in chunks:
                f.write(_fetch_tts_chunk(chunk))
                time.sleep(REQUEST_DELAY_TTS)
        os.replace(tmp_path, filepath)   # atomic on same filesystem
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    return len(chunks)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    cleanup_old_audio()
    return render_template("index.html")


@app.route("/process", methods=["POST"])
def process():
    cleanup_old_audio()

    # Accept both JSON body and multipart/form-data
    if request.is_json:
        data = request.get_json(force=True, silent=True) or {}
    else:
        data = request.form.to_dict()

    input_text = (data.get("text") or "").strip()
    mode       = (data.get("mode") or "auto").strip()

    if not input_text:
        return jsonify({"status": "error", "message": "Text cannot be empty."}), 400

    try:
        if mode == "hi_direct":
            needs_translation = False
        elif mode == "en_to_hi":
            needs_translation = True
        else:  # "auto"
            needs_translation = not is_mostly_hindi(input_text)

        hindi_text = translate_text(input_text) if needs_translation else input_text

        audio_id  = uuid.uuid4().hex[:12]
        filename  = f"novel_{audio_id}.mp3"
        filepath  = os.path.join(AUDIO_DIR, filename)

        chunk_count  = synthesize_to_mp3(hindi_text, filepath)
        file_size_mb = round(os.path.getsize(filepath) / (1024 * 1024), 2)

        return jsonify({
            "status":      "success",
            "hindi_text":  hindi_text,
            "audio_url":   f"/static/audio/{filename}",
            "download_url": f"/download/{filename}",
            "filename":    filename,
            "chunk_count": chunk_count,
            "file_size_mb": file_size_mb,
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/download/<path:filename>")
def download_audio(filename):
    return send_from_directory(
        AUDIO_DIR, filename, as_attachment=True, download_name=filename
    )


@app.route("/delete/<path:filename>", methods=["POST"])
def delete_audio(filename):
    # Security: only touch files that match our naming convention
    basename = os.path.basename(filename)
    if not (basename.startswith("novel_") and basename.endswith(".mp3")):
        return jsonify({"status": "error", "message": "Invalid file."}), 400
    filepath = os.path.join(AUDIO_DIR, basename)
    try:
        if os.path.isfile(filepath):
            os.remove(filepath)
        return jsonify({"status": "deleted"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/files")
def list_files():
    """Return all stored audio files sorted newest-first."""
    try:
        entries = []
        for name in os.listdir(AUDIO_DIR):
            if not (name.startswith("novel_") and name.endswith(".mp3")):
                continue
            fpath = os.path.join(AUDIO_DIR, name)
            if not os.path.isfile(fpath):
                continue
            stat = os.stat(fpath)
            entries.append({
                "filename":     name,
                "size_kb":      round(stat.st_size / 1024),
                "mtime":        int(stat.st_mtime),
                "audio_url":    f"/static/audio/{name}",
                "download_url": f"/download/{name}",
            })
        # Newest first
        entries.sort(key=lambda x: x["mtime"], reverse=True)
        return jsonify({"status": "ok", "files": entries, "count": len(entries)})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/delete-all", methods=["POST"])
def delete_all_audio():
    """Delete every stored audio file."""
    deleted = 0
    try:
        for name in os.listdir(AUDIO_DIR):
            if not (name.startswith("novel_") and name.endswith(".mp3")):
                continue
            try:
                os.remove(os.path.join(AUDIO_DIR, name))
                deleted += 1
            except OSError:
                pass
        return jsonify({"status": "ok", "deleted": deleted})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
