"""
CLI tool to enroll a speaker into the auditory cortex voiceprint database.

Usage:
    # Record from microphone (5 seconds)
    uv run python -m brain.tools.enroll_speaker --name "Russ" --record

    # Enroll from an existing audio file
    uv run python -m brain.tools.enroll_speaker --name "Russ" --file voice_sample.wav

    # List enrolled speakers
    uv run python -m brain.tools.enroll_speaker --list

    # Delete a speaker profile
    uv run python -m brain.tools.enroll_speaker --delete <speaker_id>

Profiles are stored in: brain/second_brain/speaker_profiles/
"""
from __future__ import annotations

import argparse
import sys


def enroll_from_file(name: str, audio_path: str) -> None:
    try:
        import librosa
    except ImportError:
        print("ERROR: librosa required. Run: uv sync")
        sys.exit(1)

    from brain.clusters.audio_dsp import extract_speaker_embedding
    from brain.second_brain.speaker_store import SpeakerStore

    print(f"Loading audio: {audio_path}")
    audio, sr = librosa.load(audio_path, sr=16000, mono=True)
    duration = len(audio) / sr
    print(f"  Duration: {duration:.1f}s")

    if duration < 1.0:
        print("WARNING: audio is very short (<1s). Longer samples produce better voiceprints.")

    print("Extracting speaker embedding (may download model on first run)...")
    embedding = extract_speaker_embedding(audio, sr)

    if embedding is None:
        print("ERROR: SpeechBrain model unavailable. Install: uv sync")
        sys.exit(1)

    store = SpeakerStore()
    speaker_id = store.enroll(name, embedding)
    print(f"\nEnrolled '{name}' as speaker_id={speaker_id}")
    print(f"Profile saved to: brain/second_brain/speaker_profiles/{speaker_id}.json")


def enroll_from_mic(name: str, duration: int = 5) -> None:
    try:
        import sounddevice as sd
    except ImportError:
        print("ERROR: sounddevice required. Run: uv sync")
        sys.exit(1)

    from brain.clusters.audio_dsp import decode_audio, extract_speaker_embedding
    from brain.second_brain.speaker_store import SpeakerStore

    print(f"Recording {duration}s voice sample for '{name}'...")
    print("Speak naturally after the prompt.\n")
    input("Press Enter to start recording... ")

    audio_raw = sd.rec(duration * 16000, samplerate=16000, channels=1, dtype="int16")
    sd.wait()
    print("Recording complete.")

    audio = decode_audio(audio_raw.tobytes(), dtype="int16")

    print("Extracting speaker embedding (may download model on first run)...")
    embedding = extract_speaker_embedding(audio, 16000)

    if embedding is None:
        print("ERROR: SpeechBrain model unavailable. Install: uv sync")
        sys.exit(1)

    store = SpeakerStore()
    speaker_id = store.enroll(name, embedding)
    print(f"\nEnrolled '{name}' as speaker_id={speaker_id}")
    print(f"Profile saved to: brain/second_brain/speaker_profiles/{speaker_id}.json")


def list_speakers() -> None:
    import time

    from brain.second_brain.speaker_store import SpeakerStore

    store = SpeakerStore()
    speakers = store.list_speakers()

    if not speakers:
        print("No speakers enrolled yet.")
        return

    print(f"{'Name':<20} {'ID':<38} {'Samples':>7}  {'Last updated'}")
    print("-" * 80)
    for s in sorted(speakers, key=lambda x: x["name"]):
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["updated_ts"]))
        print(f"{s['name']:<20} {s['speaker_id']:<38} {s['sample_count']:>7}  {ts}")


def delete_speaker(speaker_id: str) -> None:
    from brain.second_brain.speaker_store import SpeakerStore

    store = SpeakerStore()
    profile_path = store._dir / f"{speaker_id}.json"
    if not profile_path.exists():
        print(f"ERROR: speaker_id={speaker_id} not found")
        sys.exit(1)
    name = store._profiles.get(speaker_id, {}).get("name", "unknown")
    confirm = input(f"Delete profile for '{name}' ({speaker_id})? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        return
    profile_path.unlink()
    print(f"Deleted profile for '{name}'.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage auditory cortex speaker profiles")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--record", action="store_true", help="Record from microphone")
    group.add_argument("--file", metavar="AUDIO_FILE", help="Enroll from audio file")
    group.add_argument("--list", action="store_true", help="List enrolled speakers")
    group.add_argument("--delete", metavar="SPEAKER_ID", help="Delete a speaker profile")
    parser.add_argument("--name", help="Speaker name (required for --record and --file)")
    parser.add_argument("--duration", type=int, default=5,
                        help="Recording duration in seconds (default: 5)")
    args = parser.parse_args()

    if args.list:
        list_speakers()
    elif args.delete:
        delete_speaker(args.delete)
    else:
        if not args.name:
            parser.error("--name is required when enrolling a speaker")
        if args.record:
            enroll_from_mic(args.name, duration=args.duration)
        else:
            from pathlib import Path
            if not Path(args.file).exists():
                print(f"ERROR: File not found: {args.file}")
                sys.exit(1)
            enroll_from_file(args.name, args.file)


if __name__ == "__main__":
    main()
