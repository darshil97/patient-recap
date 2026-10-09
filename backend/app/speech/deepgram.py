"""
Deepgram pre-recorded transcription with speaker diarization.

Plain REST over httpx rather than the Deepgram SDK: this is one endpoint and one
request, and the SDK has changed its call shape across major versions. Owning
the request also makes two things explicit that matter here:

  * The audio is **streamed** from the upload's temporary file with an explicit
    Content-Length, so a long recording is never held in memory.
  * Failures are sorted by who can fix them. A file Deepgram cannot decode is the
    upload's fault (TranscriptError, 422); a rejected key is the server's
    configuration (ModelNotConfiguredError, 500); throttling, timeouts and outages
    are worth retrying (SpeechToTextUnavailableError, 503); anything else is
    Deepgram failing (SpeechToTextError, 502).

`diarize_model` is sent rather than `diarize=true`, which Deepgram has deprecated
and pins to its older diarizer. The two must not both be set.

Nothing here logs transcript text. The request id is logged so a bad result can
be looked up in the Deepgram console.
"""

from __future__ import annotations

import logging
from typing import Any, BinaryIO, Iterator, List, Optional, Union

import httpx

from app.config import DeepgramConfig
from app.core.errors import (
    ModelNotConfiguredError,
    SpeechToTextError,
    SpeechToTextUnavailableError,
    TranscriptError,
    TranscriptTooLargeError,
)
from app.domain.diarization import DiarizedAudio, Utterance

logger = logging.getLogger(__name__)

KEY_VARIABLE = "DEEPGRAM_API_KEY"
LISTEN_PATH = "/v1/listen"
CHUNK_BYTES = 256 * 1024
CONNECT_TIMEOUT_SECONDS = 15.0

# A 400 whose message names the media is the file's fault; any other 400 is more
# likely our own parameters (an unknown model or language in config.yaml).
_BAD_MEDIA_MARKERS = ("audio", "media", "corrupt", "unsupported data", "decode")


def _chunks(stream: BinaryIO) -> Iterator[bytes]:
    while True:
        chunk = stream.read(CHUNK_BYTES)
        if not chunk:
            return
        yield chunk


def _error_message(response: httpx.Response) -> str:
    """Deepgram's own explanation, from its {err_code, err_msg} error body."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict):
        return str(body.get("err_msg") or body.get("message") or body)[:300]
    return str(body)[:300]


def _raise_for_status(response: httpx.Response) -> None:
    status = response.status_code
    if status < 400:
        return

    detail = _error_message(response)
    details = {"provider": "deepgram", "status": status,
               "request_id": response.headers.get("dg-request-id")}

    if status in (401, 403):
        raise ModelNotConfiguredError(
            f"Deepgram rejected the configured key ({status}). Check {KEY_VARIABLE} "
            f"in backend/.env.", details=details)
    if status == 402:
        raise SpeechToTextError(
            "The Deepgram account has run out of credit, so the recording could not "
            "be transcribed.", details=details)
    if status == 413:
        raise TranscriptTooLargeError(
            "The recording is larger than the speech-to-text service accepts.",
            details=details)
    if status in (400, 415, 422) and any(m in detail.lower() for m in _BAD_MEDIA_MARKERS):
        raise TranscriptError(
            f"The recording could not be read as audio ({detail}). Re-export it as "
            f"MP3, M4A or WAV and try again.", details=details)
    if status == 429 or status >= 500:
        raise SpeechToTextUnavailableError(
            "The speech-to-text service is busy or unavailable. Try again in a "
            "moment.", details={**details, "detail": detail})
    raise SpeechToTextError(
        f"The speech-to-text service could not transcribe the recording: {detail}",
        details=details)


def _model_name(metadata: dict) -> Optional[str]:
    info = metadata.get("model_info")
    if isinstance(info, dict):
        for model in info.values():
            if isinstance(model, dict) and model.get("name"):
                return str(model["name"])
    return None


def _speaker_of(value: Any) -> Optional[int]:
    # bool is an int subclass; a True speaker id is a malformed payload.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _from_utterances(results: dict) -> Optional[List[Utterance]]:
    raw = results.get("utterances")
    if not isinstance(raw, list):
        return None
    utterances: List[Utterance] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        speaker = _speaker_of(item.get("speaker"))
        if speaker is None:
            # Utterances without speakers means the diarizer did not run, and
            # mixing labelled and unlabelled lines would misattribute speech.
            return None
        utterances.append(Utterance(
            speaker=speaker,
            text=str(item.get("transcript") or ""),
            start=float(item.get("start") or 0.0),
            end=float(item.get("end") or 0.0),
        ))
    return utterances


def _from_words(results: dict) -> Optional[List[Utterance]]:
    """Fallback when no utterances came back: group words by speaker change."""
    try:
        words = results["channels"][0]["alternatives"][0]["words"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(words, list):
        return None

    utterances: List[Utterance] = []
    for word in words:
        if not isinstance(word, dict):
            continue
        speaker = _speaker_of(word.get("speaker"))
        if speaker is None:
            return None
        text = str(word.get("punctuated_word") or word.get("word") or "")
        start = float(word.get("start") or 0.0)
        end = float(word.get("end") or 0.0)
        if utterances and utterances[-1].speaker == speaker:
            last = utterances[-1]
            utterances[-1] = Utterance(speaker, f"{last.text} {text}", last.start, end)
        else:
            utterances.append(Utterance(speaker, text, start, end))
    return utterances


def parse_response(payload: Any) -> DiarizedAudio:
    """Reduce Deepgram's response to diarized utterances.

    Raises SpeechToTextError when no speaker labels came back: without them the
    voices cannot be told apart, and labelling the whole recording as one person
    would be worse than refusing.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), dict):
        raise SpeechToTextError("The speech-to-text service returned an unexpected "
                                "response.", details={"provider": "deepgram"})

    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    results = payload["results"]

    utterances = _from_utterances(results)
    if utterances is None:
        utterances = _from_words(results)
    if utterances is None:
        raise SpeechToTextError(
            "The speech-to-text service returned no speaker labels, so the doctor "
            "and patient cannot be told apart.",
            details={"provider": "deepgram", "request_id": metadata.get("request_id")})

    duration = metadata.get("duration")
    return DiarizedAudio(
        utterances=tuple(utterances),
        duration_seconds=float(duration) if isinstance(duration, (int, float)) else None,
        model=_model_name(metadata),
        request_id=metadata.get("request_id"),
    )


def transcribe(
    audio: Union[bytes, BinaryIO],
    *,
    content_type: str,
    config: DeepgramConfig,
    size: Optional[int] = None,
    client: Optional[httpx.Client] = None,
) -> DiarizedAudio:
    """Send one recording to Deepgram and return its diarized utterances.

    `audio` is either bytes or a binary file positioned at the start; a file is
    streamed in chunks, and `size` is then required so the request carries a
    Content-Length rather than chunked encoding. `client` is for tests.
    """
    api_key = (config.api_key or "").strip()
    if not api_key:
        raise ModelNotConfiguredError(
            f"Audio transcription is not configured. Set {KEY_VARIABLE} in "
            f"backend/.env, or upload a text transcript instead.")

    headers = {"Authorization": f"Token {api_key}", "Content-Type": content_type}
    if isinstance(audio, (bytes, bytearray)):
        content: Union[bytes, Iterator[bytes]] = bytes(audio)
    else:
        if size is None:
            raise ValueError("size is required when streaming audio from a file")
        headers["Content-Length"] = str(size)
        content = _chunks(audio)

    params = {
        "model": config.model,
        "language": config.language,
        "diarize_model": config.diarize_model,
        "utterances": "true",
        "punctuate": "true",
        "smart_format": "true" if config.smart_format else "false",
    }
    timeout = httpx.Timeout(config.timeout_seconds,
                            connect=min(CONNECT_TIMEOUT_SECONDS, config.timeout_seconds))

    owned = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        response = http.post(f"{config.base_url}{LISTEN_PATH}", params=params,
                             headers=headers, content=content, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise SpeechToTextUnavailableError(
            "The speech-to-text service took too long to respond. Try again in a "
            "moment.", details={"provider": "deepgram"}) from exc
    except httpx.HTTPError as exc:
        raise SpeechToTextUnavailableError(
            "Could not reach the speech-to-text service. Try again in a moment.",
            details={"provider": "deepgram", "error": type(exc).__name__}) from exc
    finally:
        if owned:
            http.close()

    _raise_for_status(response)
    try:
        payload = response.json()
    except ValueError as exc:
        raise SpeechToTextError("The speech-to-text service returned a response "
                                "that is not JSON.",
                                details={"provider": "deepgram"}) from exc

    result = parse_response(payload)
    logger.info("Deepgram transcribed %.0fs of audio into %d utterances from %d "
                "voices (model=%s, request_id=%s)",
                result.duration_seconds or 0, len(result.utterances),
                len(result.speakers), result.model, result.request_id)
    return result
