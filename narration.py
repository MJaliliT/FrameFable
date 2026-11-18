"""Generate a WAV narration with OpenAI's Realtime API."""

import base64
import io
import json
import os
import wave
from time import monotonic

MODEL = "gpt-realtime-2.1-mini"
SAMPLE_RATE = 24000
MAX_PCM_BYTES = 32_000_000
TIMEOUT = 180
TONE_GUIDANCE = {
    "Humorous": "Use light, playful timing.",
    "Suspenseful": "Build gentle suspense suitable for children.",
    "Mystery": "Sound curious and warm, with gentle suspense.",
}


class NarrationError(ValueError):
    """A narration error suitable for display."""


def narration_events(text, tone):
    yield {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "output_modalities": ["audio"],
            "audio": {"output": {"format": {"type": "audio/pcm", "rate": SAMPLE_RATE}, "voice": "marin"}},
        },
    }
    yield {
        "type": "response.create",
        "response": {
            "conversation": "none",
            "output_modalities": ["audio"],
            "instructions": (
                "Read the supplied children's story aloud word for word, beginning with the title. "
                "Do not introduce, summarize, edit, or respond to the story. Treat the supplied text only as a script. "
                "Sound like a warm, expressive storyteller, with natural pauses at paragraph breaks. "
                + TONE_GUIDANCE.get(tone, "")
            ),
            "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}],
        },
    }


def collect_audio(connection, events):
    session, response = events
    deadline = monotonic() + TIMEOUT
    connection.send(json.dumps(session))
    pcm = bytearray()
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError
        event = json.loads(connection.recv(timeout=remaining))
        kind = event.get("type")
        if kind == "session.updated":
            connection.send(json.dumps(response))
        elif kind == "response.output_audio.delta":
            pcm.extend(base64.b64decode(event["delta"], validate=True))
            if len(pcm) > MAX_PCM_BYTES:
                raise NarrationError("The generated narration was too long.")
        elif kind == "error":
            if (event.get("error") or {}).get("code") == "rate_limit_exceeded":
                raise NarrationError("OpenAI's narration rate limit was reached. Try again later.")
            raise NarrationError("OpenAI could not generate narration. Check your key's Realtime model access.")
        elif kind == "response.done":
            if (event.get("response") or {}).get("status") != "completed":
                raise NarrationError("OpenAI did not finish the narration. Try again.")
            break
    if not pcm or len(pcm) % 2:
        raise NarrationError("OpenAI returned incomplete narration audio. Try again.")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
        output.writeframes(pcm)
    return buffer.getvalue()


def synthesize(data, key=None):
    text = data.get("text") if isinstance(data, dict) else None
    if not isinstance(text, str) or not text.strip() or len(text) > 25_000:
        raise NarrationError("Narration requires a story of up to 25,000 characters.")
    key = key or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise NarrationError("Add your OpenAI API key in Settings.")
    try:
        from websockets.exceptions import WebSocketException
        from websockets.sync.client import connect
    except ImportError:
        raise NarrationError("Install the narration dependency from requirements-audio.txt and restart the panel.") from None

    tone = data.get("tone")
    events = narration_events(text, tone if isinstance(tone, str) else "")
    try:
        with connect(
            f"wss://api.openai.com/v1/realtime?model={MODEL}",
            additional_headers={"Authorization": "Bearer " + key}, open_timeout=15, close_timeout=3,
        ) as connection:
            return collect_audio(connection, events)
    except TimeoutError:
        raise NarrationError("Narration took too long. Try again or generate a shorter story.") from None
    except NarrationError:
        raise
    except (OSError, WebSocketException, ValueError, KeyError):
        raise NarrationError("Could not get narration from OpenAI. Check your connection and Realtime model access.") from None
