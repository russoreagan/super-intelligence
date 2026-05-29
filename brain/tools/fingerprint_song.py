"""
CLI tool to add a song to the auditory cortex fingerprint database.

Usage:
    uv run python -m brain.tools.fingerprint_song <audio_file> <title> <artist>
    uv run python -m brain.tools.fingerprint_song /path/to/song.mp3 "Bohemian Rhapsody" "Queen"

The audio file is fingerprinted and its hashes appended to:
    brain/second_brain/audio_fingerprints.json

Supports any format readable by librosa (mp3, wav, flac, ogg, m4a, ...).
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

_DB_PATH = Path(__file__).parent.parent / "second_brain" / "audio_fingerprints.json"


def fingerprint_file(audio_path: str, title: str, artist: str) -> None:
    try:
        import librosa
    except ImportError:
        print("ERROR: librosa is required. Run: uv sync")
        sys.exit(1)

    from brain.clusters.audio_dsp import compute_spectrogram, extract_peaks, generate_hashes

    print(f"Loading audio: {audio_path}")
    audio, sr = librosa.load(audio_path, sr=16000, mono=True)
    print(f"  Duration: {len(audio) / sr:.1f}s | Sample rate: {sr}Hz")

    print("Computing spectrogram...")
    spec = compute_spectrogram(audio, sr)

    print("Extracting peaks...")
    peaks = extract_peaks(spec)
    print(f"  Found {len(peaks)} constellation points")

    print("Generating hashes...")
    hashes = generate_hashes(peaks)
    print(f"  Generated {len(hashes)} hash pairs")

    if not hashes:
        print("WARNING: no hashes generated — audio may be too short or silent")
        sys.exit(1)

    # Load existing DB
    with open(_DB_PATH) as f:
        db = json.load(f)

    song_id = str(uuid.uuid4())
    db["songs"][song_id] = {"title": title, "artist": artist}

    for hash_val, t_ref in hashes:
        key = str(hash_val)
        db["hashes"].setdefault(key, [])
        db["hashes"][key].append([song_id, t_ref])

    with open(_DB_PATH, "w") as f:
        json.dump(db, f)

    total_songs = len(db["songs"])
    total_hashes = len(db["hashes"])
    print(f"\nAdded '{title}' by {artist} (id={song_id})")
    print(f"DB now has {total_songs} songs and {total_hashes} unique hash buckets.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Add a song to the auditory cortex fingerprint DB")
    parser.add_argument("audio_file", help="Path to audio file (mp3, wav, flac, ...)")
    parser.add_argument("title", help="Song title")
    parser.add_argument("artist", help="Artist name")
    args = parser.parse_args()

    if not Path(args.audio_file).exists():
        print(f"ERROR: File not found: {args.audio_file}")
        sys.exit(1)

    fingerprint_file(args.audio_file, args.title, args.artist)


if __name__ == "__main__":
    main()
