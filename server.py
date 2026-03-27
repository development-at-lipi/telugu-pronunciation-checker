import os
import tempfile
import unicodedata
import speech_recognition as sr
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from pydub import AudioSegment

app = Flask(__name__, static_folder="static")
CORS(app)

# Telugu vowels, consonants, and common words for practice
TELUGU_LETTERS = {
    "vowels": [
        "అ", "ఆ", "ఇ", "ఈ", "ఉ", "ఊ", "ఋ", "ౠ",
        "ఎ", "ఏ", "ఐ", "ఒ", "ఓ", "ఔ", "అం", "అః"
    ],
    "consonants": [
        "క", "ఖ", "గ", "ఘ", "ఙ",
        "చ", "ఛ", "జ", "ఝ", "ఞ",
        "ట", "ఠ", "డ", "ఢ", "ణ",
        "త", "థ", "ద", "ధ", "న",
        "ప", "ఫ", "బ", "భ", "మ",
        "య", "ర", "ల", "వ", "శ",
        "ష", "స", "హ", "ళ", "క్ష", "ఱ"
    ],
    "words": [
        "నమస్కారం", "ధన్యవాదాలు", "నీళ్ళు", "అమ్మ", "నాన్న",
        "పుస్తకం", "బడి", "ఇల్లు", "చెట్టు", "పువ్వు",
        "సూర్యుడు", "చంద్రుడు", "నక్షత్రం", "ఆకాశం", "భూమి"
    ]
}



# ------------------------------------------------------------------
# Phonetic equivalents: letters Google can't return directly are
# mapped to what it actually outputs for those sounds.
# Key   = what we expect the student to say (our letter)
# Value = list of strings Google may return for that sound
# ------------------------------------------------------------------
PHONETIC_EQUIVALENTS = {
    # Vocalic R  (ఋ) — Google returns రు / రి / ర
    "ఋ":  ["రు", "రి", "ర", "రూ", "ru", "ri"],
    # Vocalic RR (ౠ) — Google returns రూ / రీ
    "ౠ":  ["రూ", "రీ", "రు", "రి", "ర"],
    # Anusvara variants — అన్ / అమ్ are nasals Google may return
    "అం": ["అన్", "అమ్", "అన", "అమ"],
    # Visarga — Google rarely returns this; accept అ / అహ
    "అః": ["అహ", "అ", "aha"],
    # Consonant ఙ (nga) — very rare, Google may return న/ఞ
    "ఙ":  ["న", "ఞ", "న్గ"],
    # Consonant ఞ (nya)
    "ఞ":  ["న", "ని", "న్య"],
    # Consonant ణ vs న — Google often confuses these
    "ణ":  ["న", "నా"],
    "న":  ["ణ", "నా"],
    # Retroflexes Google sometimes swaps
    "ట":  ["త", "టా"],
    "ఠ":  ["థ", "ఠా"],
    "డ":  ["ద", "డా"],
    "ఢ":  ["ధ", "ఢా"],
    # ఱ (old Telugu ra)
    "ఱ":  ["ర", "రా"],
    # క్ష
    "క్ష": ["క్ష", "క్ష", "కష", "క్షా"],
}


def normalize_telugu(text: str) -> str:
    """Normalize Telugu text for comparison."""
    text = text.strip()
    text = unicodedata.normalize("NFC", text)
    # Remove punctuation / noise
    for ch in [" ", ".", ",", "?", "!", "।", "\u200c", "\u200d"]:
        text = text.replace(ch, "")
    return text


def compare_telugu(expected: str, recognized: str) -> dict:
    """
    Compare expected Telugu text with recognized speech.
    Returns match result with confidence score.

    Fix summary vs original:
      1. Single-letter starts-with pass — if expected (1 grapheme) starts the
         recognized word Google returns, count it as correct (score 80).
      2. Phonetic equivalents map — handles ఋ→రు, ౠ→రూ, అం→అన్, etc.
      3. Lower containment threshold — from 50% to 30% for short strings.
      4. Levenshtein threshold lowered to 55% for very short strings.
    """
    expected_norm = normalize_telugu(expected)
    recognized_norm = normalize_telugu(recognized)

    if not recognized_norm:
        return {"match": False, "score": 0, "reason": "No speech detected"}

    # ── 1. Exact match ────────────────────────────────────────────────────────
    if expected_norm == recognized_norm:
        return {"match": True, "score": 100, "reason": "Perfect match"}

    # ── 2. Phonetic equivalents (e.g. ఋ ↔ రు) ────────────────────────────────
    for phonetic in PHONETIC_EQUIVALENTS.get(expected_norm, []):
        phonetic_norm = normalize_telugu(phonetic)
        if recognized_norm == phonetic_norm or recognized_norm.startswith(phonetic_norm):
            return {"match": True, "score": 85, "reason": "Phonetically correct"}

    # ── 3. Starts-with check (FIX: Google returns full word for single letter) ─
    # e.g. student says "అ", Google returns "అమ్మ" → pass because it starts with అ
    expected_graphemes = list(_iter_graphemes(expected_norm))
    if len(expected_graphemes) <= 2:          # only for short expected letters
        if recognized_norm.startswith(expected_norm):
            return {"match": True, "score": 80, "reason": "Good pronunciation (extra syllable detected)"}

    # ── 4. Expected contained inside recognized (longer strings too) ──────────
    if expected_norm in recognized_norm:
        ratio = len(expected_norm) / len(recognized_norm)
        score = max(int(ratio * 100), 70)     # floor at 70 when contained
        return {"match": True, "score": score, "reason": "Good pronunciation"}

    # ── 5. Recognized contained inside expected ───────────────────────────────
    if recognized_norm in expected_norm:
        ratio = len(recognized_norm) / len(expected_norm)
        score = int(ratio * 100)
        threshold = 30 if len(expected_norm) <= 3 else 50
        if score >= threshold:
            return {"match": True, "score": score, "reason": "Partial match detected"}

    # ── 6. Levenshtein similarity ─────────────────────────────────────────────
    score = _similarity_score(expected_norm, recognized_norm)
    threshold = 55 if len(expected_norm) <= 3 else 60
    if score >= threshold:
        return {"match": True, "score": score, "reason": "Close pronunciation"}

    return {
        "match": False,
        "score": score,
        "reason": f"Expected '{expected}', heard '{recognized}'"
    }


def _iter_graphemes(text: str):
    """Yield Telugu grapheme clusters (rough approximation)."""
    # Telugu combining chars (matras, halant, anusvara, visarga)
    COMBINING = set(range(0x0C3E, 0x0C57)) | {0x0C4D, 0x0C02, 0x0C03, 0x0C55, 0x0C56}
    cluster = ""
    for ch in text:
        cp = ord(ch)
        if cluster and cp in COMBINING:
            cluster += ch
        else:
            if cluster:
                yield cluster
            cluster = ch
    if cluster:
        yield cluster


def _similarity_score(s1: str, s2: str) -> int:
    """Calculate similarity percentage between two strings."""
    if not s1 or not s2:
        return 0

    # Simple Levenshtein distance
    len1, len2 = len(s1), len(s2)
    matrix = [[0] * (len2 + 1) for _ in range(len1 + 1)]

    for i in range(len1 + 1):
        matrix[i][0] = i
    for j in range(len2 + 1):
        matrix[0][j] = j

    for i in range(1, len1 + 1):
        for j in range(1, len2 + 1):
            cost = 0 if s1[i - 1] == s2[j - 1] else 1
            matrix[i][j] = min(
                matrix[i - 1][j] + 1,
                matrix[i][j - 1] + 1,
                matrix[i - 1][j - 1] + cost
            )

    distance = matrix[len1][len2]
    max_len = max(len1, len2)
    return int((1 - distance / max_len) * 100)


def recognize_audio(audio_path: str) -> dict:
    """
    Recognize Telugu speech from audio file.
    Tries multiple recognition approaches for best results.
    """
    recognizer = sr.Recognizer()

    # Adjust recognizer settings for short audio (single letters)
    recognizer.energy_threshold = 200
    recognizer.dynamic_energy_threshold = False
    recognizer.pause_threshold = 0.5

    try:
        with sr.AudioFile(audio_path) as source:
            # Read with slight padding for short audio
            audio = recognizer.record(source)

        results = []

        # Try Google Speech Recognition (free, supports Telugu)
        try:
            # Get all possible results
            all_results = recognizer.recognize_google(
                audio,
                language="te-IN",
                show_all=True
            )

            if all_results and "alternative" in all_results:
                for alt in all_results["alternative"]:
                    transcript = alt.get("transcript", "")
                    confidence = alt.get("confidence", 0.5)
                    if transcript:
                        results.append({
                            "text": transcript,
                            "confidence": confidence,
                            "engine": "google"
                        })

            if not results:
                # Try single result mode
                text = recognizer.recognize_google(audio, language="te-IN")
                if text:
                    results.append({
                        "text": text,
                        "confidence": 0.7,
                        "engine": "google"
                    })

        except sr.UnknownValueError:
            pass
        except sr.RequestError as e:
            return {"error": f"Google API error: {str(e)}", "results": []}

        if not results:
            return {"error": "Could not recognize any speech. Please speak louder and clearer.", "results": []}

        return {"error": None, "results": results}

    except Exception as e:
        return {"error": f"Audio processing error: {str(e)}", "results": []}


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/letters", methods=["GET"])
def get_letters():
    """Get Telugu letters and words for practice."""
    category = request.args.get("category", "vowels")
    letters = TELUGU_LETTERS.get(category, TELUGU_LETTERS["vowels"])
    return jsonify({
        "category": category,
        "letters": letters,
        "categories": list(TELUGU_LETTERS.keys())
    })


@app.route("/api/verify", methods=["POST"])
def verify_pronunciation():
    """
    Verify pronunciation of a Telugu letter/word.
    Expects: audio file + expected text.
    """
    if "audio" not in request.files:
        return jsonify({"error": "No audio file provided"}), 400

    expected_text = request.form.get("expected", "").strip()
    if not expected_text:
        return jsonify({"error": "No expected text provided"}), 400

    audio_file = request.files["audio"]

    # Save uploaded audio to temp file
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
        tmp_path = tmp.name
        audio_file.save(tmp_path)

    wav_path = tmp_path.replace(".webm", ".wav")

    try:
        # Convert to WAV (required by SpeechRecognition)
        audio = AudioSegment.from_file(tmp_path)

        # Ensure good quality for recognition
        audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)

        # Trim silence from start and end
        # Use a simple approach: find non-silent portions
        from pydub.silence import detect_nonsilent
        nonsilent = detect_nonsilent(audio, min_silence_len=300, silence_thresh=-40)

        if nonsilent:
            start = max(0, nonsilent[0][0] - 100)  # 100ms padding
            end = min(len(audio), nonsilent[-1][1] + 100)
            audio = audio[start:end]

        # Add small padding (helps recognition of short sounds)
        silence = AudioSegment.silent(duration=300, frame_rate=16000)
        audio = silence + audio + silence

        audio.export(wav_path, format="wav")

        # Recognize
        recognition = recognize_audio(wav_path)

        if recognition["error"] and not recognition["results"]:
            return jsonify({
                "pass": False,
                "error": recognition["error"],
                "expected": expected_text,
                "recognized": None,
                "score": 0,
                "details": []
            })

        # Compare each recognition result with expected text
        best_result = None
        best_score = -1

        for result in recognition["results"]:
            comparison = compare_telugu(expected_text, result["text"])
            combined_score = comparison["score"]

            if combined_score > best_score:
                best_score = combined_score
                best_result = {
                    "recognized_text": result["text"],
                    "engine_confidence": result["confidence"],
                    "comparison": comparison
                }

        passed = best_result["comparison"]["match"] if best_result else False

        return jsonify({
            "pass": passed,
            "expected": expected_text,
            "recognized": best_result["recognized_text"] if best_result else None,
            "score": best_result["comparison"]["score"] if best_result else 0,
            "reason": best_result["comparison"]["reason"] if best_result else "No match",
            "details": [
                {
                    "text": r["text"],
                    "confidence": r["confidence"],
                    "match": compare_telugu(expected_text, r["text"])
                }
                for r in recognition["results"]
            ]
        })

    except Exception as e:
        return jsonify({"error": f"Processing failed: {str(e)}"}), 500

    finally:
        # Cleanup temp files
        for path in [tmp_path, wav_path]:
            if os.path.exists(path):
                os.unlink(path)


if __name__ == "__main__":
    os.makedirs("static", exist_ok=True)
    port = int(os.environ.get("PORT", 5050))
    print(f"Starting Telugu Pronunciation Checker on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
