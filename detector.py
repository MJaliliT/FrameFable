"""Local, text-guided bounding boxes for the storytelling panel."""

import base64
import binascii
import io
import math
import os
from functools import lru_cache
from pathlib import Path
from threading import Lock
from time import monotonic

MODEL_ID = "IDEA-Research/grounding-dino-tiny"
MODEL_DIR = Path(__file__).resolve().parent / ".models" / "grounding-dino-tiny"
MIN_SCORE = 0.40
INFERENCE_LOCK = Lock()
DETECTION_SECONDS = 30
EDGES = ("left", "top", "right", "bottom")


class DetectorUnavailable(RuntimeError):
    """The optional local model could not be loaded or run."""


@lru_cache(maxsize=1)
def _load_detector():
    if not MODEL_DIR.is_dir():
        raise DetectorUnavailable("Grounding DINO weights are missing.")
    try:
        import torch
        from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

        torch.set_num_threads(min(4, os.cpu_count() or 1))
        processor = AutoProcessor.from_pretrained(MODEL_DIR, local_files_only=True)
        model = AutoModelForZeroShotObjectDetection.from_pretrained(
            MODEL_DIR, local_files_only=True, use_safetensors=True
        ).eval()
    except (ImportError, OSError, RuntimeError) as error:
        raise DetectorUnavailable("Grounding DINO could not load locally.") from error
    return torch, processor, model


def prepare_model():
    """Download the public detector checkpoint without sending user images."""
    from huggingface_hub import snapshot_download

    snapshot_download(
        MODEL_ID, local_dir=MODEL_DIR,
        allow_patterns=["*.json", "*.txt", "model.safetensors"],
    )
    _load_detector()
    print(f"Ready to run {MODEL_ID} locally.")


def _area(box):
    return (box[2] - box[0]) * (box[3] - box[1])


def _iou(a, b):
    width = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    height = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    intersection = width * height
    return intersection / (_area(a) + _area(b) - intersection)


def _hint(candidate, width, height):
    box = candidate.get("hint")
    if not isinstance(box, dict):
        return None
    values = [box.get(edge) for edge in EDGES]
    if not all(type(value) is int and 0 <= value <= 1000 for value in values):
        return None
    if values[0] >= values[2] or values[1] >= values[3]:
        return None
    return tuple(value * extent / 1000 for value, extent in zip(values, (width, height, width, height)))


def choose_detection(detections, hint):
    """Select one confident instance. A rough hint only breaks instance ambiguity."""
    distinct = []
    for box, score in sorted(detections, key=lambda item: item[1], reverse=True):
        if not math.isfinite(score) or score < MIN_SCORE or any(_iou(box, old) >= 0.65 for old, _ in distinct):
            continue
        distinct.append((box, score))
    if len(distinct) <= 1:
        return distinct[0] if distinct else None
    if hint is not None:
        anchored = sorted(distinct, key=lambda item: _iou(item[0], hint), reverse=True)
        overlap = _iou(anchored[0][0], hint)
        if overlap >= 0.20 and overlap - _iou(anchored[1][0], hint) >= 0.12:
            return anchored[0]
    return distinct[0] if distinct[0][1] - distinct[1][1] >= 0.18 else None


def _detections(image, label, model_parts):
    torch, processor, model = model_parts
    inputs = processor(images=image, text=label.casefold().strip().rstrip(".") + ".", return_tensors="pt")
    with torch.inference_mode():
        outputs = model(**inputs)
    result = processor.post_process_grounded_object_detection(
        outputs, inputs.input_ids, threshold=MIN_SCORE, text_threshold=0.30,
        target_sizes=[(image.height, image.width)]
    )[0]
    for coords, score in zip(result["boxes"].tolist(), result["scores"].tolist()):
        if not all(math.isfinite(value) for value in coords):
            continue
        box = tuple(max(0, min(extent, value)) for value, extent in zip(
            coords, (image.width, image.height, image.width, image.height)
        ))
        if box[0] < box[2] and box[1] < box[3]:
            yield box, score


def locate_subjects(image_url, candidates):
    if not INFERENCE_LOCK.acquire(blocking=False):
        raise DetectorUnavailable("Grounding DINO is busy processing another image.")
    deadline = monotonic() + DETECTION_SECONDS
    try:
        model_parts = _load_detector()
        if monotonic() >= deadline:
            raise DetectorUnavailable("Grounding DINO took too long on this server.")
        from PIL import Image

        try:
            raw = base64.b64decode(image_url.split(",", 1)[1], validate=True)
            with Image.open(io.BytesIO(raw)) as source:
                if source.width * source.height > 4_000_000:
                    raise ValueError("The detector image is too large.")
                image = source.convert("RGB")
        except (IndexError, OSError, binascii.Error, Image.DecompressionBombError) as error:
            raise ValueError("The detector needs a valid image.") from error

        detected = {}
        selections = []
        try:
            for candidate in candidates:
                if monotonic() >= deadline:
                    raise DetectorUnavailable("Grounding DINO took too long on this server.")
                label = candidate["label"].strip().casefold()
                if label not in detected:
                    detected[label] = list(_detections(image, label, model_parts))
                selections.append(choose_detection(detected[label], _hint(candidate, image.width, image.height)))
            if monotonic() >= deadline:
                raise DetectorUnavailable("Grounding DINO took too long on this server.")
        except DetectorUnavailable:
            raise
        except (RuntimeError, OSError) as error:
            raise DetectorUnavailable("Grounding DINO could not process this image.") from error
    finally:
        INFERENCE_LOCK.release()
    results = []
    for index, selection in enumerate(selections):
        if selection is None:
            results.append(None)
            continue
        box, score = selection
        # Avoid assigning one physical detection to multiple occurrences.
        if any(other is not None and _iou(box, other[0]) >= 0.90
               for other_index, other in enumerate(selections) if other_index != index):
            results.append(None)
            continue
        normalized = [round(value / extent * 1000) for value, extent in zip(
            box, (image.width, image.height, image.width, image.height)
        )]
        if normalized[0] >= normalized[2] or normalized[1] >= normalized[3]:
            results.append(None)
        else:
            results.append({"box": dict(zip(EDGES, normalized)), "score": round(score, 3)})
    return results


if __name__ == "__main__":
    prepare_model()
