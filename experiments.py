"""Persistent, unlisted experiment batches and rating files."""

import base64
import io
import itertools
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_hex
from statistics import mean
from threading import Lock, Thread
from time import perf_counter

from narration import MODEL as VOICE_MODEL, narration_events, synthesize
from providers import StudioError, execute, sampling_options
from soundtrack import MODEL_ID as MUSIC_MODEL, MUSIC_OPTIONS, MUSIC_SEED, music_prompt, run_musicgen_worker
from story_pdf import image_bytes, make_story_pdf

TONES = ("Humorous", "Suspenseful", "Mystery")
LEVELS = {
    "Beginning": "Familiar words and short, simple sentences.",
    "Developing": "Familiar vocabulary, clear transitions and varied short sentences.",
    "Expanding": "More varied vocabulary and context for unfamiliar words.",
    "Advanced": "Richer vocabulary, a more nuanced plot and varied sentences.",
}
LENGTHS = (250, 450, 750)
QUESTIONS = {
    "tone": "How well did the story reflect the requested tone?",
    "music": "How appropriate was the background music?",
    "narration": "How appropriate was the narration?",
    "length": "How well did the story match the target length?",
    "level": "How well did the story match the target reading level?",
    "identity": "How consistently did the story preserve subject identities?",
    "subject_accuracy": "How accurately were the subjects identified?",
    "box_accuracy": "How accurately did the boxes fit the subjects?",
    "bridge_needed": "How necessary was the connecting scene?",
    "bridge_helpful": "How helpful was the connecting scene?",
}
ID = re.compile(r"[0-9a-f]{32}")
LOCK = Lock()
ACTIVE = set()
MEDIA = {"story.pdf": "application/pdf", "music.wav": "audio/wav", "narration.wav": "audio/wav"}
BRIDGE_MEDIA = re.compile(r"bridge-[1-3]\.png")
MAX_BATCHES = 24
MAX_STORAGE = 768_000_000


class ExperimentError(ValueError):
    pass


def store():
    return Path(os.environ.get("FRAMEFABLE_EXPERIMENTS_DIR") or
                os.environ.get("STATE_DIRECTORY") or (Path(__file__).resolve().parent / ".experiments"))


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def write_bytes(path, value):
    temporary = path.with_name("." + path.name + "." + token_hex(4))
    with LOCK:
        used = sum(file.stat().st_size for file in store().glob("**/*") if file.is_file())
        if used + len(value) > MAX_STORAGE:
            raise ExperimentError("Experiment storage is full. Ask the server operator to archive older batches.")
        try:
            temporary.write_bytes(value)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def write_json(path, value):
    write_bytes(path, json.dumps(value, ensure_ascii=False, indent=2).encode())


def read_json(path):
    return json.loads(path.read_bytes())


def batch_path(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ExperimentError("Enter a valid experiment batch code.")
    path = store() / value
    if not (path / "batch.json").is_file():
        raise ExperimentError("This experiment batch was not found.")
    return path


def variants_for(data):
    axes = []
    for key, allowed in (("tones", TONES), ("levels", LEVELS), ("lengths", LENGTHS)):
        values = data.get(key)
        if not isinstance(values, list) or not values or any(value not in allowed for value in values):
            raise ExperimentError("Select at least one supported value for each experiment setting.")
        axes.append([value for value in allowed if value in values])
    return [{"tone": tone, "readingLevel": level, "readingGuidance": LEVELS[level], "approximateWords": length}
            for tone, level, length in itertools.product(*axes)]


def word_count(text):
    return len(re.findall(r"\b\w+(?:[’'-]\w+)*\b", text, re.UNICODE))


def questions_for(variant):
    return {key: question for key, question in QUESTIONS.items()
            if (key != "music" or "music.wav" in variant["media"])
            and (key != "narration" or "narration.wav" in variant["media"])
            and (not key.startswith("bridge_") or variant["bridges"])}


def create(data, key):
    if not isinstance(data, dict):
        raise ExperimentError("Provide experiment inputs.")
    configs = variants_for(data)
    settings = data.get("settings")
    if not isinstance(settings, dict):
        raise ExperimentError("Choose a provider in Settings.")
    provider = settings.get("provider")
    model = settings.get("model")
    if provider not in ("openai", "ollama") or not isinstance(model, str) or not model or len(model) > 150:
        raise ExperimentError("Choose a provider and vision model in Settings.")
    if provider == "openai" and not sampling_options(model):
        raise ExperimentError("Select an OpenAI model that supports temperature zero, such as GPT-6.1 Terra or GPT-4.1 mini.")
    if provider == "openai" and not (key or os.environ.get("OPENAI_API_KEY")):
        raise ExperimentError("Add an OpenAI API key in Settings.")
    images, observations, subjects = (data.get(field) for field in ("images", "observations", "subjects"))
    if not isinstance(images, list) or not 1 <= len(images) <= 20 or not isinstance(observations, list) or len(observations) != len(images) or not isinstance(subjects, list):
        raise ExperimentError("Analyze the ordered images before running an experiment.")
    for image in images:
        if not isinstance(image, dict) or not isinstance(image.get("name"), str):
            raise ExperimentError("An input image is invalid.")
        image_bytes(image.get("src"))
    settings = {field: settings[field] for field in ("provider", "model", "endpoint", "bridgeImages") if field in settings}
    if not isinstance(data.get("analysisCalls", []), list) or len(subjects) > 160:
        raise ExperimentError("The analyzed inputs are invalid.")
    ident = token_hex(16)
    with LOCK:
        root = store()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if ACTIVE:
            raise ExperimentError("Another experiment batch is already running on this server.")
        if sum(path.is_dir() for path in root.iterdir()) >= MAX_BATCHES:
            raise ExperimentError("The experiment archive is full. Ask the server operator to archive older batches.")
        path = root / ident
        path.mkdir(mode=0o700)
        ACTIVE.add(ident)
    try:
        batch = {"id": ident, "createdAt": timestamp(), "settings": settings, "images": images,
                 "observations": observations, "subjects": subjects, "analysisCalls": data.get("analysisCalls", []),
                 "configurations": configs, "status": "running", "completed": 0}
        write_json(path / "batch.json", batch)
        Thread(target=run_batch, args=(path, batch, key), daemon=True).start()
    except Exception:
        with LOCK:
            ACTIVE.discard(ident)
        if not any(path.iterdir()):
            path.rmdir()
        raise
    return {"id": ident, "total": len(configs)}


def run_batch(path, batch, key):
    try:
        assign_names(batch, key)
        write_json(path / "batch.json", batch)
        for index, config in enumerate(batch["configurations"], 1):
            calls = []
            destination = path / str(index)
            destination.mkdir()
            try:
                variant = generate_variant(destination, batch, config, key, calls)
            except (ValueError, OSError, TypeError, KeyError) as error:
                variant = {"configuration": config, "createdAt": timestamp(), "error": str(error), "calls": calls}
            write_json(destination / "variant.json", variant)
            batch["completed"] = index
            write_json(path / "batch.json", batch)
        batch["status"] = "completed"
    except Exception as error:
        batch.update(status="failed", error=str(error))
    finally:
        try:
            write_json(path / "batch.json", batch)
        finally:
            with LOCK:
                ACTIVE.discard(batch["id"])


def model_call(settings, key, calls, action, data):
    audit = []
    record = {"action": action, "requestedModel": settings["model"], "providerInputs": audit, "createdAt": timestamp()}
    calls.append(record)
    started = perf_counter()
    try:
        answer = execute({"action": action, "settings": settings, "data": data}, key=key, audit=audit, experiment=True)
        record.update(answer["metric"])
        return answer["result"]
    except StudioError as error:
        record["error"] = str(error)
        raise
    finally:
        record.setdefault("ms", round((perf_counter() - started) * 1000))


def assign_names(batch, key):
    unique = {}
    for subject in batch["subjects"]:
        unique.setdefault(subject["group"], {"subjectId": subject["group"], "label": subject["label"],
                                              "name": subject.get("name", "").strip()})
    missing = [item for item in unique.values() if not item["name"]]
    suggestions = {}
    if missing:
        try:
            result = model_call(batch["settings"], key, batch["analysisCalls"], "names", missing)
            suggestions = {item["subjectId"]: item["name"].strip() for item in result["names"]}
        except StudioError:
            pass  # Use the recorded subject label when automatic naming fails.
    for subject in batch["subjects"]:
        group = unique[subject["group"]]
        subject["name"] = group["name"] or suggestions.get(subject["group"]) or group["label"]
        subject["nameSource"] = "entered" if group["name"] else "automatic"


def generate_variant(path, batch, config, key, calls):
    settings, images = batch["settings"], batch["images"]
    errors, media = {}, []
    by_id = {subject["id"]: subject for subject in batch["subjects"]}
    scenes = [{"scene": item["scene"], "setting": item["setting"], "action": item["action"],
               "subjects": [{"label": by_id[subject["id"]]["label"], "detail": by_id[subject["id"]]["detail"],
                             "subjectId": by_id[subject["id"]]["group"], "name": by_id[subject["id"]]["name"]}
                            for subject in item["subjects"]]} for item in batch["observations"]]
    proposed = model_call(settings, key, calls, "plan", {"scenes": scenes, **config})["bridges"] if len(scenes) > 1 else []
    bridges = {}
    for item in proposed:
        if 1 <= item["after"] < len(scenes) and item["description"].strip():
            bridges.setdefault(item["after"], item["description"])
    bridges = dict(sorted(bridges.items())[:3])
    sequence = []
    bridge_numbers = {after: number for number, after in enumerate(bridges, 1)}
    for index, scene in enumerate(scenes):
        sequence.append((scene, images[index]["src"], False))
        if index + 1 in bridges:
            event = bridges[index + 1]
            image = None
            if settings["provider"] == "openai" and settings.get("bridgeImages"):
                try:
                    image = model_call(settings, key, calls, "image",
                                       {"event": event, "adjacentScenes": scenes[index:index + 2]})
                except StudioError as error:
                    errors[f"bridge_{index + 1}"] = str(error)
                if image:
                    filename = f"bridge-{bridge_numbers[index + 1]}.png"
                    try:
                        write_bytes(path / filename, image_bytes(image))
                        media.append(filename)
                    except (OSError, ValueError) as error:
                        errors[filename] = str(error)
            sequence.append(({"connectingEvent": event}, image, True))
    inputs = {**config, "scenes": [{"sequenceNumber": index, **item[0]} for index, item in enumerate(sequence, 1)]}
    result = model_call(settings, key, calls, "story", inputs)
    ordered = sorted(result["scenes"], key=lambda scene: scene["scene"])
    if ([scene["scene"] for scene in ordered] != list(range(1, len(sequence) + 1))
            or not result["title"].strip() or any(not item["text"].strip() for item in ordered)):
        raise ExperimentError("The story did not contain usable text for every scene.")
    created = timestamp()
    pdf_data = {"title": result["title"], "createdAtLabel": datetime.now(timezone.utc).strftime("%b %d, %Y, %I:%M %p UTC"),
                "preferences": {**config, "sourceImages": len(images)},
                "scenes": [{"text": item["text"], "image": sequence[index][1], "bridge": sequence[index][2]}
                           for index, item in enumerate(ordered)]}
    write_bytes(path / "story.pdf", make_story_pdf(pdf_data))
    media.append("story.pdf")
    text = "\n\n".join([result["title"], *(item["text"] for item in ordered)])
    music_data = {"tone": config["tone"], "description": result["musicDescription"].strip()[:300] or "Gentle storybook music."}
    for name, model, payload, generate in (
        ("music", MUSIC_MODEL, {"prompt": music_prompt(music_data), **MUSIC_OPTIONS, "seedAttempts": [MUSIC_SEED, MUSIC_SEED + 1]}, lambda: run_musicgen_worker(music_data)),
        ("narration", VOICE_MODEL, list(narration_events(text, config["tone"])), lambda: synthesize({"text": text, "tone": config["tone"]}, key)),
    ):
        if name == "narration" and settings["provider"] != "openai":
            errors[name] = "Narration requires OpenAI."
            continue
        started = perf_counter()
        entry = {"action": name, "model": model, "providerInputs": payload, "usage": None, "createdAt": timestamp()}
        calls.append(entry)
        try:
            write_bytes(path / (name + ".wav"), generate())
            media.append(name + ".wav")
        except (ValueError, OSError) as error:
            errors[name] = str(error)
        finally:
            entry["ms"] = round((perf_counter() - started) * 1000)
    return {"title": result["title"], "createdAt": created, "configuration": config, "media": media,
            "bridges": list(bridges.values()), "errors": errors, "calls": calls,
            "story": ordered, "musicDescription": result["musicDescription"],
            "wordCount": word_count(" ".join(item["text"] for item in ordered))}


def snapshot(path, detailed=False):
    batch = read_json(path / "batch.json")
    if batch["status"] == "running" and batch["id"] not in ACTIVE:
        batch.update(status="interrupted", error="The server restarted. Previously saved variants are still available.")
    if not detailed:
        return {field: batch.get(field) for field in ("id", "createdAt", "status", "completed", "error")} | {
            "total": len(batch["configurations"])}
    batch["variants"] = [{"index": index, **read_json(path / str(index) / "variant.json")}
                         for index in range(1, batch["completed"] + 1)]
    return batch


def rating_report_pdf(report):
    try:
        from reportlab.lib.colors import HexColor
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.utils import simpleSplit
        from reportlab.pdfgen.canvas import Canvas
    except ImportError:
        raise ExperimentError("Install the PDF dependency from requirements-pdf.txt and try again.") from None

    buffer = io.BytesIO()
    pdf = Canvas(buffer, pagesize=letter)
    pdf.setTitle("FrameFable Voting Results")
    ink, muted, accent, track = map(HexColor, ("#182730", "#4f6170", "#244cc2", "#e1e9fa"))

    def text(value, x, y, size=10, color=ink, bold=False):
        pdf.setFillColor(color)
        pdf.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        pdf.drawString(x, y, str(value))

    text("FrameFable Voting Results", 42, 740, 23, bold=True)
    y = 714
    for line in simpleSplit("Voter: " + report["voter"], "Helvetica", 11, 528):
        text(line, 42, y, 11)
        y -= 15
    date = datetime.fromisoformat(report["createdAt"]).strftime("%b %d, %Y, %I:%M %p UTC")
    text(date + "  |  Batch " + report["batch"][:8], 42, y, color=muted)
    y -= 29
    text(f"Overall mean  {report['meanScore']:.2f} / 5", 42, y, 15, bold=True)
    text(f"{report['variantsRated']} variants rated  |  {report['ratingCount']} ratings", 290, y, 11, muted)
    y -= 38
    text("Average rating by question", 42, y, 13, bold=True)
    for item in report["criteria"].values():
        y -= 24
        text(item["question"], 42, y)
        pdf.setFillColor(track)
        pdf.roundRect(350, y - 2, 151, 10, 3, fill=1, stroke=0)
        pdf.setFillColor(accent)
        pdf.roundRect(350, y - 2, 151 * item["mean"] / 5, 10, 3, fill=1, stroke=0)
        text(f"{item['mean']:.2f} / 5", 519, y)
    y -= 38
    text("Star rating distribution", 42, y, 13, bold=True)
    y -= 22
    text("Question", 42, y, bold=True)
    for offset, label in enumerate(("1", "2", "3", "4", "5", "Total")):
        text(label, 349 + offset * 36, y, bold=True)
    for item in report["criteria"].values():
        y -= 20
        text(item["question"], 42, y)
        for offset, count in enumerate([*item["distribution"], item["count"]]):
            text(count, 349 + 36 * offset, y)
    pdf.save()
    return buffer.getvalue()


def save_votes(path, data):
    batch = snapshot(path, True)
    voter = data.get("voter")
    ratings = data.get("ratings")
    variants = [variant for variant in batch["variants"] if not variant.get("error")]
    if batch["status"] == "running" or not variants or not isinstance(voter, str) or not voter.strip() or len(voter) > 100 or not isinstance(ratings, dict):
        raise ExperimentError("Enter your name and complete the ratings for a finished batch.")
    if set(ratings) != {str(variant["index"]) for variant in variants}:
        raise ExperimentError("Rate every available variant before finishing.")
    answers = defaultdict(list)
    for variant in variants:
        scores = ratings[str(variant["index"])]
        if not isinstance(scores, dict) or set(scores) != set(questions_for(variant)) or any(type(score) is not int or not 1 <= score <= 5 for score in scores.values()):
            raise ExperimentError("Give every applicable question a score from one to five.")
        for key, score in scores.items():
            answers[key].append(score)
    report = {"batch": batch["id"], "voter": voter.strip(), "createdAt": timestamp(), "ratings": ratings,
              "variantsRated": len(variants), "ratingCount": sum(map(len, answers.values())),
              "meanScore": round(mean(score for scores in answers.values() for score in scores), 2),
              "criteria": {key: {"question": QUESTIONS[key], "mean": round(mean(scores), 2), "count": len(scores),
                                 "distribution": [scores.count(score) for score in range(1, 6)]}
                           for key, scores in answers.items()}}
    pdf = rating_report_pdf(report)
    directory = path / "votes"
    directory.mkdir(exist_ok=True, mode=0o700)
    ballot_id = token_hex(16)
    pdf_path = directory / (ballot_id + ".pdf")
    write_bytes(pdf_path, pdf)
    try:
        write_json(directory / (ballot_id + ".json"), report)
    except (OSError, ExperimentError):
        pdf_path.unlink(missing_ok=True)
        raise
    return report | {"reportPdf": base64.b64encode(pdf).decode("ascii")}


def artifact(url):
    parts = url.split("/")
    if (len(parts) != 6 or not parts[4].isascii() or not parts[4].isdigit()
            or (parts[5] not in MEDIA and not BRIDGE_MEDIA.fullmatch(parts[5]))):
        raise ExperimentError("This artifact was not found.")
    path = batch_path(parts[3]) / str(int(parts[4])) / parts[5]
    return path.read_bytes(), MEDIA.get(parts[5], "image/png")
