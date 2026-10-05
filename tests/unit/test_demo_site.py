"""The replay demo on GitHub Pages: it builds, and it publishes nothing private."""

import json
import re
from pathlib import Path

import scripts.build_demo_site as build_demo_site

ROOT = Path(__file__).resolve().parents[2]


def test_the_demo_site_builds_with_the_replay_loaded_first(tmp_path, monkeypatch):
    monkeypatch.setattr(build_demo_site, "SITE", tmp_path / "site")
    build_demo_site.build()
    page = (tmp_path / "site" / "index.html").read_text("utf-8")
    assert page.index("replay.js") < page.index("app.js")  # the stand-in server comes first
    assert "Replay demo" in page and "No AI runs on this page" in page
    for name in ("app.js", "styles.css", "replay.js", "demo.css", "recording.json", ".nojekyll"):
        assert (tmp_path / "site" / name).exists(), name


def test_the_recording_contains_nothing_private():
    text = (ROOT / "demo" / "recording.json").read_text("utf-8")
    recording = json.loads(text)
    assert recording["turns"] and recording["status"]["components"]
    for private in (r"vishnu", r"@gmail", r"\.ts\.net", r"tailscale", r"api[_-]?key", r"password"):
        assert not re.search(private, text, re.IGNORECASE), private
    # Only the fictional sample files may appear.
    files = set(re.findall(r"[\w-]+\.(?:pdf|docx|md|txt|csv)", text))
    assert files <= {"Sample_Resume_Alex_Tan_2026.pdf", "Sample_Resume_Alex_Tan_2025.pdf",
                     "ai_engineering_report.md"}, files  # fmt: skip


def test_every_recorded_turn_ends_properly():
    recording = json.loads((ROOT / "demo" / "recording.json").read_text("utf-8"))
    for turn in recording["turns"]:
        assert turn["events"][-1]["event"]["type"] == "done", turn["question"]
