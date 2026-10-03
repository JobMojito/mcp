"""Condense one answer's ``answer_assessment_raw_data`` for ``view="full"``.

WHY THIS EXISTS
``get_interview_result_details`` with ``view="full"`` used to return the API
response verbatim, and it failed on every real interview it was asked for:
30 Sep 2026 (result 2f3755af…, 805,685 characters of data, twice) and 2 Oct
(476,977), against a 150,000-character result limit. The per-answer raw
assessment blob was 776,484 of the 805,685. Of that, three arrays were 753,000:

* ``voice``        — Azure pronunciation output, one entry per recognised
                     segment, each carrying EVERY word with its offset, duration
                     and accuracy, plus four renderings of the same text.
* ``reading_detection_results`` — the proctor's per-frame gaze record (~120
                     frames per answer).
* ``assemblyai``   — the STT turn again, word by word with timings.

``elevenlabs`` (per-character timings) and ``candidate_images`` (storage paths)
are the same kind of thing when present. None of it is readable by an agent,
and every one of them already has a summary next to it in the same blob —
``PronunciationAssessment`` per segment, ``reading_stats``,
``stt_reliability``. Those summaries are what someone asking for the "full"
data actually wants; the frame and word arrays are what made the call fail.

WHAT IS KEPT
Every small key passes through untouched — scores, flags, ``reading_stats``,
``stt_reliability``, ``proctor_status``, ``model``, ``language`` … — including
keys added after this was written. Per Azure segment we keep the recognised text
(``Display``; Azure's own transcription, which can differ from the AssemblyAI
answer and is the evidence when the pronunciation score looks wrong), its
confidence, ``PronunciationAssessment`` and ``SentimentAnalysis``, plus the
lowest-scoring words. Diarisation and voice identification become seconds per
speaker.

WHAT IS DROPPED is named in ``_omitted`` on the condensed blob, so a reader can
tell "not recorded" from "removed here". An unknown key is dropped only when it
is large (``_LARGE_UNKNOWN_CHARS``), so a new bulky array cannot quietly bring
the overflow back, and a new small summary still gets through.
"""

from __future__ import annotations

import json

#: Bulky arrays dropped outright; each has a summary elsewhere in the blob.
_DROP = frozenset(
    {
        "assemblyai",  # word timings; the text is the answer, quality is stt_reliability
        "elevenlabs",  # per-character timings of the same transcript
        "reading_detection_results",  # per-frame gaze; summarised in reading_stats
        "audio_mp3_local_path",  # storage path; useless without signing
    }
)

#: An unrecognised key whose JSON is longer than this is dropped and listed in
#: `_omitted`. The largest known summary (`reading_stats`, `stt_reliability`) is
#: under 600 characters; the arrays above start in the thousands.
_LARGE_UNKNOWN_CHARS = 4_000

#: Azure's own mispronunciation threshold is an AccuracyScore below 60.
_LOW_ACCURACY = 60
#: Lowest-scoring words kept per segment. Unscripted Azure grades against what it
#: heard, so this is a sample of the weakest words, not a full error list.
_MAX_LOW_WORDS = 15

_HANDLED = frozenset({"voice", "candidate_images", "pyannote"})


def condense_answer_assessment(raw):
    """Return a condensed copy of one ``answer_assessment_raw_data`` value.

    Accepts whatever the column holds: an object (current rows), a list of
    objects (older rows wrap it as ``[null, {...}]``), or null. Anything else is
    returned unchanged.
    """
    if isinstance(raw, list):
        return [condense_answer_assessment(item) for item in raw]
    if not isinstance(raw, dict):
        return raw

    out: dict = {}
    omitted: list[str] = []
    for key, value in raw.items():
        if key in _DROP:
            omitted.append(key)
        elif key == "voice":
            out[key] = _condense_voice(value)
        elif key == "candidate_images":
            # Storage paths of proctor snapshots; the count is the readable part.
            out["candidate_images_count"] = len(value) if isinstance(value, list) else 0
            omitted.append(key)
        elif key == "pyannote":
            out[key] = _condense_pyannote(value)
        elif key not in _HANDLED and _json_len(value) > _LARGE_UNKNOWN_CHARS:
            omitted.append(key)
        else:
            out[key] = value
    if omitted:
        out["_omitted"] = omitted
    return out


def _condense_voice(segments):
    if not isinstance(segments, list):
        return segments
    condensed = []
    for segment in segments:
        if not isinstance(segment, dict):
            condensed.append(segment)
            continue
        item = {
            key: segment[key]
            for key in ("Display", "Confidence", "PronunciationAssessment", "SentimentAnalysis")
            if key in segment
        }
        low = _low_accuracy_words(segment.get("Words"))
        if low:
            item["LowAccuracyWords"] = low
        condensed.append(item)
    return condensed


def _low_accuracy_words(words) -> list[dict]:
    if not isinstance(words, list):
        return []
    flagged = []
    for index, word in enumerate(words):
        if not isinstance(word, dict):
            continue
        assessment = word.get("PronunciationAssessment") or {}
        score = assessment.get("AccuracyScore")
        error = assessment.get("ErrorType")
        is_error = error not in (None, "None")
        if is_error or (isinstance(score, (int, float)) and score < _LOW_ACCURACY):
            flagged.append((score if isinstance(score, (int, float)) else -1, index, word, error))
    # Keep the weakest, then restore spoken order so the list reads naturally.
    flagged.sort(key=lambda entry: entry[0])
    kept = sorted(flagged[:_MAX_LOW_WORDS], key=lambda entry: entry[1])
    result = []
    for score, _, word, error in kept:
        entry = {"Word": word.get("Word"), "AccuracyScore": None if score == -1 else score}
        if error not in (None, "None"):
            entry["ErrorType"] = error
        result.append(entry)
    return result


def _condense_pyannote(value):
    """Turn pyannote's per-turn and per-frame lists into seconds per speaker.

    ``diarization`` and ``identification`` hold one entry per speaking turn;
    what a reader needs is how long each voice spoke (a second voice is the
    ``audio_multiple_voices`` risk). ``confidence.score`` is one number per 20 ms
    frame and is dropped. ``voiceprints`` is one entry per enrolled speaker and is
    kept, as is anything else small.
    """
    if not isinstance(value, dict):
        return value
    out: dict = {}
    omitted: list[str] = []
    for key, item in value.items():
        if key == "diarization" and isinstance(item, list):
            out["speaker_seconds"] = _seconds_by(item, "speaker")
            out["turns"] = len(item)
        elif key == "identification" and isinstance(item, list):
            out["identified_seconds"] = _seconds_by(item, "match")
        elif key == "confidence" or _json_len(item) > _LARGE_UNKNOWN_CHARS:
            omitted.append(key)
        else:
            out[key] = item
    if omitted:
        out["_omitted"] = omitted
    return out


def _seconds_by(turns: list, field: str) -> dict[str, float]:
    """Sum ``end - start`` per value of ``field``; a null match reads "unmatched"."""
    seconds: dict[str, float] = {}
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        start, end = turn.get("start"), turn.get("end")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        label = turn.get(field)
        label = "unmatched" if label is None else str(label)
        seconds[label] = seconds.get(label, 0.0) + max(0.0, end - start)
    return {label: round(total, 1) for label, total in seconds.items()}


def _json_len(value) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        return 0
