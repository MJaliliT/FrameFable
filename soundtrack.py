"""Generate a short, loopable instrumental soundtrack on this computer."""

import io
import json
import logging
import os
import subprocess
import sys
import wave
from functools import lru_cache
from pathlib import Path
from secrets import token_urlsafe
from threading import Lock, Thread
from time import monotonic

MODEL_ID = "facebook/musicgen-small"
MODEL_DIR = Path(__file__).resolve().parent / ".models" / "musicgen-small"
LOOP_SECONDS = 20
CROSSFADE_SECONDS = 0.6
GENERATION_LOCK = Lock()
TONE_GUIDANCE = {
    "Humorous": "Playful, light and gentle, with a bouncy acoustic rhythm.",
    "Suspenseful": "A slow pulse, soft tension, and a sense of child-friendly adventure.",
    "Mystery": "Curious and atmospheric, with a gentle mysterious motif.",
}
# Some audio decoding operators still need CPU execution on Apple Silicon.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")


class SoundtrackError(ValueError):
    """A soundtrack error suitable for display."""


@lru_cache(maxsize=1)
def load_musicgen():
    if not (MODEL_DIR / "model.safetensors").is_file():
        raise SoundtrackError("MusicGen weights are missing. Run the setup command in the README.")
    try:
        import torch
        from transformers import AutoProcessor, MusicgenForConditionalGeneration

        torch.set_num_threads(min(4, os.cpu_count() or 1))
        processor = AutoProcessor.from_pretrained(MODEL_DIR, local_files_only=True)
        model = MusicgenForConditionalGeneration.from_pretrained(
            MODEL_DIR, local_files_only=True, use_safetensors=True
        ).eval()
        device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
        try:
            model.to(device)
        except (RuntimeError, NotImplementedError):
            model.to("cpu")
        return torch, processor, model
    except (ImportError, OSError, ValueError, RuntimeError) as error:
        raise SoundtrackError("MusicGen could not load. Check the local setup in the README.") from error


def make_loop(samples, rate):
    """Overlap the tail with the head and keep an exact 20-second loop."""
    import numpy as np

    count = rate * LOOP_SECONDS
    overlap = round(rate * CROSSFADE_SECONDS)
    if samples.ndim != 2 or samples.shape[1] < count + overlap:
        raise SoundtrackError("MusicGen did not return enough audio to make the loop.")
    samples = samples[:, :count + overlap]
    if not np.isfinite(samples).all():
        raise SoundtrackError("MusicGen returned unusable audio. Try again.")
    blend = np.linspace(0, 1, overlap, dtype=np.float32)
    seam = samples[:, count:count + overlap] * (1 - blend) + samples[:, :overlap] * blend
    loop = np.concatenate((samples[:, overlap:count], seam), axis=1)
    peak = float(np.max(np.abs(loop)))
    if peak < 0.001:
        raise SoundtrackError("MusicGen returned silent audio. Try again.")
    loop *= min(0.78 / peak, 3)
    pcm = (loop.T * 32767).clip(-32768, 32767).astype("<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as recording:
        recording.setparams((samples.shape[0], 2, rate, 0, "NONE", "not compressed"))
        recording.writeframes(pcm.tobytes())
    return output.getvalue()


def music_prompt(data):
    description = data.get("description") if isinstance(data, dict) else None
    if not isinstance(description, str) or not description.strip() or len(description) > 300:
        raise SoundtrackError("Provide a short musical description for this story.")
    tone = data.get("tone")
    tone_hint = TONE_GUIDANCE.get(tone, "") if isinstance(tone, str) else ""
    return (
        "Instrumental background score for a children's storybook, without vocals or spoken words. "
        "Soft acoustic arrangement, steady tempo, repeating motif, suitable for looping beneath narration. "
        f"{tone_hint} {description.strip()}"
    )


def synthesize_music(data):
    prompt = music_prompt(data)
    if not GENERATION_LOCK.acquire(blocking=False):
        raise SoundtrackError("MusicGen is already making a soundtrack. Try again when it finishes.")
    try:
        torch, processor, model = load_musicgen()
        inputs = processor(text=[prompt], padding=True, return_tensors="pt")

        def generate():
            with torch.inference_mode():
                return model.generate(
                    **inputs.to(model.device), do_sample=True, guidance_scale=3, max_new_tokens=1050,
                )[0].float().cpu().numpy()

        try:
            audio = generate()
        except (RuntimeError, NotImplementedError):
            if model.device.type == "cpu":
                raise
            model.to("cpu")
            audio = generate()
        return make_loop(audio, model.config.audio_encoder.sampling_rate)
    except (RuntimeError, NotImplementedError) as error:
        raise SoundtrackError("MusicGen could not generate a soundtrack on this computer.") from error
    finally:
        GENERATION_LOCK.release()


JOB_LOCK = Lock()
JOBS = {}
JOB_TTL_SECONDS = 900
WORKER_TIMEOUT_SECONDS = 300
MAX_SAVED_JOBS = 8


def start_soundtrack(data):
    music_prompt(data)
    with JOB_LOCK:
        if any(result is None for result in JOBS.values()):
            raise SoundtrackError("MusicGen is already composing a soundtrack. Try again when it finishes.")
        for job, (finished, _) in list(JOBS.items()):
            if monotonic() - finished >= JOB_TTL_SECONDS or len(JOBS) >= MAX_SAVED_JOBS:
                del JOBS[job]
        job = token_urlsafe(24)
        JOBS[job] = None
        try:
            Thread(target=_run_soundtrack, args=(job, data), daemon=True).start()
        except RuntimeError as error:
            del JOBS[job]
            raise SoundtrackError("The server could not start MusicGen. Try again.") from error
    return job


def run_musicgen_worker(data):
    try:
        worker = subprocess.run(
            [sys.executable, "-B", str(Path(__file__).resolve()), "--render"],
            input=json.dumps(data).encode(), capture_output=True, timeout=WORKER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise SoundtrackError("MusicGen exceeded the five-minute server limit.") from error
    if worker.returncode != 0:
        logging.error("MusicGen worker exited with status %s: %s", worker.returncode,
                      worker.stderr[-2500:].decode(errors="replace").strip() or "no diagnostic output")
        try:
            error = json.loads(worker.stdout).get("error")
        except (ValueError, AttributeError):
            error = None
        raise SoundtrackError(error or "MusicGen stopped unexpectedly. Check the VPS service logs.")
    if not worker.stdout.startswith(b"RIFF"):
        raise SoundtrackError("MusicGen did not return a WAV soundtrack.")
    return worker.stdout


def _run_soundtrack(job, data):
    try:
        result = run_musicgen_worker(data)
    except SoundtrackError as error:
        result = str(error)
        if error.__cause__:
            logging.exception("MusicGen job failed")
    except Exception:
        logging.exception("Unexpected MusicGen error")
        result = "MusicGen encountered an unexpected server error. Check the FrameFable service logs."
    with JOB_LOCK:
        JOBS[job] = (monotonic(), result)


def soundtrack_result(data):
    job = data.get("job") if isinstance(data, dict) else None
    with JOB_LOCK:
        if not isinstance(job, str) or job not in JOBS:
            raise SoundtrackError("This music job is no longer available. The server may have restarted.")
        completed = JOBS[job]
        if completed is None:
            return None
        finished, result = completed
        if monotonic() - finished >= JOB_TTL_SECONDS:
            del JOBS[job]
            raise SoundtrackError("This music job has expired.")
    if isinstance(result, str):
        raise SoundtrackError(result)
    return result


def prepare_model():
    from huggingface_hub import snapshot_download

    snapshot_download(
        MODEL_ID, local_dir=MODEL_DIR,
        allow_patterns=["*.json", "spiece.model", "model.safetensors"],
    )
    _, _, model = load_musicgen()
    print(f"Ready to run {MODEL_ID} on {model.device}.")


if __name__ == "__main__":
    if sys.argv[1:] == ["--render"]:
        try:
            audio = synthesize_music(json.load(sys.stdin))
        except SoundtrackError as error:
            if error.__cause__:
                logging.exception("MusicGen failed to generate audio")
            print(json.dumps({"error": str(error)}))
            raise SystemExit(1) from None
        sys.stdout.buffer.write(audio)
    else:
        prepare_model()
