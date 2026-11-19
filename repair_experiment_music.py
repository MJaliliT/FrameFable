"""Audit or restore the music in a saved FrameFable experiment batch."""

import argparse
import sys

from experiments import ExperimentError, batch_path, read_json, snapshot, timestamp, write_bytes, write_json
from soundtrack import MODEL_ID, MUSIC_OPTIONS, MUSIC_SEED, music_prompt, needs_music_repair, run_musicgen_worker


def repair(batch_id, apply):
    path = batch_path(batch_id)
    batch = snapshot(path, detailed=True)
    if batch["status"] == "running":
        raise ExperimentError("Wait until the batch finishes.")
    if apply and any((path / "votes").glob("*.json")):
        raise ExperimentError("This batch already has votes. Keep its original audio so the ratings remain comparable.")

    candidates = [
        variant for variant in batch["variants"]
        if not variant.get("error") and needs_music_repair(path / str(variant["index"]) / "music.wav")
    ]
    print(f"{len(candidates)} of {len(batch['variants'])} saved variants need music repair.", flush=True)
    failed = False
    for variant in candidates:
        index = variant["index"]
        if not apply:
            print(f"Variant {index}: missing, invalid, or too quiet")
            continue
        directory = path / str(index)
        target = directory / "music.wav"
        backup = directory / "music.original.wav"
        data = {
            "tone": variant["configuration"]["tone"],
            "description": variant["musicDescription"].strip()[:300] or "Gentle storybook music.",
        }
        try:
            audio = run_musicgen_worker(data)
            if any((path / "votes").glob("*.json")):
                raise ExperimentError("A vote arrived during repair. Stop voting before repairing audio.")
            if target.exists() and not backup.exists():
                write_bytes(backup, target.read_bytes())
            current = read_json(directory / "variant.json")
            write_bytes(target, audio)
            if "music.wav" not in current["media"]:
                current["media"].append("music.wav")
            current["errors"].pop("music", None)
            current["calls"].append({
                "action": "music_repair", "model": MODEL_ID, "createdAt": timestamp(), "usage": None,
                "providerInputs": {"prompt": music_prompt(data), **MUSIC_OPTIONS,
                                   "seedAttempts": [MUSIC_SEED, MUSIC_SEED + 1]},
            })
            write_json(directory / "variant.json", current)
            print(f"Variant {index}: repaired", flush=True)
        except (OSError, ValueError) as error:
            print(f"Variant {index}: {error}", file=sys.stderr, flush=True)
            failed = True
    return int(failed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch_id", help="Code after #vote= in the voting link")
    parser.add_argument("--apply", action="store_true", help="Generate and archive replacements using local MusicGen")
    args = parser.parse_args()
    try:
        sys.exit(repair(args.batch_id, args.apply))
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
