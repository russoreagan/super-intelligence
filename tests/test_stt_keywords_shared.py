"""
Drift guard: both live-STT paths must resolve the same keyword-boost set.

History: brain/streaming_mic.py shipped a populated BRAIN_STT_KEYWORDS default
while brain/api/stt_live.py defaulted to "" — so engine-API live STT silently
lost the vocabulary boosts the local mic path got. The default now lives in
brain/stt_config.py and both paths must go through it.
"""

from pathlib import Path

from brain.stt_config import DEFAULT_STT_KEYWORDS, stt_keyterms, stt_keywords

REPO = Path(__file__).resolve().parents[1]
STT_PATHS = [
    REPO / "brain" / "streaming_mic.py",
    REPO / "brain" / "api" / "stt_live.py",
]


def test_default_is_populated():
    assert DEFAULT_STT_KEYWORDS, "shared default must carry the boost vocabulary"
    assert "claude:5" in DEFAULT_STT_KEYWORDS


def test_keywords_resolve_from_shared_default(monkeypatch):
    monkeypatch.delenv("BRAIN_STT_KEYWORDS", raising=False)
    keywords = stt_keywords()
    assert keywords == [k.strip() for k in DEFAULT_STT_KEYWORDS.split(",")]
    # nova-3 keyterms are the same set with :boost suffixes stripped
    assert stt_keyterms() == [k.split(":")[0] for k in keywords]
    assert "claude" in stt_keyterms()


def test_env_override_applies(monkeypatch):
    monkeypatch.setenv("BRAIN_STT_KEYWORDS", " foo:2 , bar ,")
    assert stt_keywords() == ["foo:2", "bar"]
    assert stt_keyterms() == ["foo", "bar"]


def test_env_empty_disables_boosts(monkeypatch):
    monkeypatch.setenv("BRAIN_STT_KEYWORDS", "")
    assert stt_keywords() == []
    assert stt_keyterms() == []


def test_both_stt_paths_use_shared_resolver():
    """Neither STT path may read BRAIN_STT_KEYWORDS itself (that's how the
    two defaults diverged); both must import the shared resolver."""
    for path in STT_PATHS:
        src = path.read_text()
        code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
        assert 'environ.get("BRAIN_STT_KEYWORDS"' not in code, (
            f"{path.name} reads BRAIN_STT_KEYWORDS directly — use "
            f"brain.stt_config so both STT paths share one default"
        )
        assert "from brain.stt_config import" in code, (
            f"{path.name} no longer imports the shared STT keyword resolver"
        )
