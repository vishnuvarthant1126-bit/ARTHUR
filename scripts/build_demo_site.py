"""Build the replay demo for GitHub Pages into site/.

    python scripts/build_demo_site.py            # build site/
    python -m http.server 8090 -d site           # look at it: http://127.0.0.1:8090

The site is ARTHUR's real web page (frontend/, unchanged) plus demo/replay.js, which plays
back demo/recording.json (made with scripts/record_demo.py) instead of talking to a server.
"""

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
REPO = "https://github.com/vishnuvarthant1126-bit/ARTHUR"

BANNER = f"""
    <div class="replay-banner" role="note">
      <strong>Replay demo:</strong> a real session recorded on the developer's laptop, played
      back with shortened pauses (the times shown are the real ones). No AI runs on this page -
      ARTHUR runs locally on your own PC. <a href="{REPO}">Code and setup on GitHub</a>.
    </div>"""
NEXT = """
    <button id="replay-next" class="replay-next" type="button" hidden>
      <b>Try</b><span></span>
    </button>"""


def build() -> None:
    SITE.mkdir(exist_ok=True)
    # Empty it rather than delete it: a preview server may be serving the folder (Windows
    # refuses to delete a folder that is in use).
    for old in SITE.iterdir():
        shutil.rmtree(old) if old.is_dir() else old.unlink()
    for name in ("app.js", "styles.css"):
        shutil.copy(ROOT / "frontend" / name, SITE / name)
    for name in ("replay.js", "demo.css", "recording.json"):
        shutil.copy(ROOT / "demo" / name, SITE / name)
    (SITE / ".nojekyll").write_text("", encoding="utf-8")  # serve files as they are

    page = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    replacements = [
        ("<title>ARTHUR</title>", "<title>ARTHUR – replay demo</title>"),
        (
            '<link rel="stylesheet" href="styles.css">',
            '<link rel="stylesheet" href="styles.css">\n  <link rel="stylesheet" href="demo.css">',
        ),
        ("    </header>\n", "    </header>\n" + BANNER + "\n"),
        ('    <ul id="attachments"', NEXT + '\n    <ul id="attachments"'),
        (
            '<script src="app.js"></script>',
            '<script src="replay.js"></script>\n  <script src="app.js"></script>',
        ),
    ]
    for old, new in replacements:
        assert page.count(old) == 1, f"index.html changed - update build_demo_site.py ({old!r})"
        page = page.replace(old, new)
    (SITE / "index.html").write_text(page, encoding="utf-8")
    print(f"built {SITE} ({sum(f.stat().st_size for f in SITE.iterdir()) // 1024} KB)")


if __name__ == "__main__":
    build()
