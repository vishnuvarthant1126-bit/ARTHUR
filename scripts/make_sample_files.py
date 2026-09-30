"""Create clearly fictional sample files for trying ARTHUR's file tools.

    python scripts/make_sample_files.py   -> Documents/ARTHUR/samples/

Two resumes (an older and a newer one, to test "find my LATEST resume") and a
project report. Delete them any time or replace them with your own files.
Requires the dev dependencies (fpdf2, python-docx).
"""

import os
import sys
import time
from pathlib import Path

from docx import Document
from fpdf import FPDF

TARGET = Path.home() / "Documents" / "ARTHUR" / "samples"

RESUME_2025 = """SAMPLE RESUME (fictional) - Alex Tan - 2025
Junior Software Developer
Skills: Python, Flask, SQL, Git.
Experience: Intern at a logistics start-up (2024-2025), built internal dashboards."""

RESUME_2026 = """SAMPLE RESUME (fictional) - Alex Tan - 2026
AI Engineer
Technical skills: Python, FastAPI, PyTorch, LLM agents, RAG with ChromaDB, Whisper speech
recognition, Docker, PostgreSQL, Git, CI/CD with GitHub Actions.
Experience: Software Developer at a logistics start-up (2025-2026) - built an internal
assistant that answers questions from company documents (RAG), cutting support tickets by 30%.
Projects: ARTHUR - a local voice assistant with tool calling, memory and web search.
Education: B.Sc. Computer Science, 2025."""

REPORT = [
    "ARTHUR Project Report (sample)",
    "Goal: a local-first personal AI assistant with voice, memory, documents and tools.",
    "Progress: phases 0 to 13 complete - chat, memory, tools, planning, RAG, web search, voice.",
    "Risks: small local models sometimes skip tools; wake word tested with synthetic voices.",
    "Next steps: file tools, browser agent, computer control, security hardening, Docker.",
]


def pdf(text: str, path: Path) -> None:
    doc = FPDF()
    doc.add_page()
    doc.set_font("Helvetica", size=11)
    doc.multi_cell(0, 6, text)
    path.write_bytes(bytes(doc.output()))


def receipt(path: Path) -> None:
    """A fictional cafe receipt picture, for trying describe_image (Phase 17)."""
    from PIL import Image, ImageDraw

    lines = [
        "BLUE LANTERN CAFE",
        "Receipt #0427   2026-09-30",
        "",
        "Flat white           4.80",
        "Blueberry muffin     3.90",
        "Sparkling water      2.50",
        "",
        "TOTAL (SGD)         11.20",
        "Thank you!",
    ]
    image = Image.new("RGB", (520, 460), "white")
    draw = ImageDraw.Draw(image)
    for i, line in enumerate(lines):
        draw.text((40, 30 + i * 44), line, fill="black", font_size=28)
    image.save(path)


def main() -> None:
    TARGET.mkdir(parents=True, exist_ok=True)
    old = TARGET / "Sample_Resume_Alex_Tan_2025.pdf"
    new = TARGET / "Sample_Resume_Alex_Tan_2026.pdf"
    pdf(RESUME_2025, old)
    pdf(RESUME_2026, new)
    a_year_ago = time.time() - 365 * 86400
    os.utime(old, (a_year_ago, a_year_ago))  # so the 2026 one is clearly the latest

    report = Document()
    report.add_heading(REPORT[0], level=1)
    for line in REPORT[1:]:
        report.add_paragraph(line)
    report.save(TARGET / "ARTHUR Project Report.docx")
    receipt(TARGET / "Sample_Receipt_Cafe.png")
    for f in sorted(TARGET.iterdir()):
        print(f"  {f.name}", file=sys.stderr)


if __name__ == "__main__":
    main()
