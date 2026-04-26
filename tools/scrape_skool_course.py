"""
Logs into Skool, presents a numbered section menu, and scrapes all video
transcripts from the chosen section. Saves results to .tmp/course_data.json.

Usage:
    python tools/scrape_skool_course.py                        # interactive menu
    python tools/scrape_skool_course.py --section "Build Your Portfolio"  # skip menu
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 stdout/stderr on Windows to handle emoji in lesson/phase titles
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

load_dotenv()

TMP_DIR = Path(__file__).parent.parent / ".tmp"
TMP_DIR.mkdir(exist_ok=True)
SESSION_FILE = TMP_DIR / "skool_session.json"
OUTPUT_FILE = TMP_DIR / "course_data.json"


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def login(page, email: str, password: str) -> None:
    print("Logging into Skool...")
    page.goto("https://www.skool.com/login", wait_until="networkidle")
    page.fill('input[type="email"]', email)
    page.fill('input[type="password"]', password)
    page.click('button[type="submit"]')
    try:
        page.wait_for_url(re.compile(r"skool\.com/(?!login)"), timeout=20000)
    except PWTimeout:
        print("ERROR: Login timed out. Check SKOOL_EMAIL / SKOOL_PASSWORD in .env.")
        sys.exit(1)
    print("Logged in.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _meta_title(node: dict) -> str:
    """Extract display title from a Skool course/module/lesson node.

    Skool stores human-readable titles in metadata.title, not at the top level.
    Top-level 'name' is a short hex hash (e.g. 'd99017d2').
    """
    meta = node.get("metadata") or {}
    return (
        meta.get("title")
        or node.get("title")
        or node.get("displayName")
        or meta.get("desc")
        or node.get("name")
        or ""
    )


def _find_key(node, key: str):
    """Recursively find the first non-empty value for a key in a nested structure."""
    if isinstance(node, dict):
        if key in node and node[key]:
            return node[key]
        for v in node.values():
            result = _find_key(v, key)
            if result is not None:
                return result
    elif isinstance(node, list):
        for item in node:
            result = _find_key(item, key)
            if result is not None:
                return result
    return None


# ---------------------------------------------------------------------------
# Section discovery + menu
# ---------------------------------------------------------------------------

def get_sections(page, community_url: str) -> list[dict]:
    """Returns list of {'title': str, 'url': str, 'id': str, 'hash': str}."""
    # Must navigate to community home first — direct /classroom hits redirect to /about
    print(f"Navigating to community home: {community_url}")
    page.goto(community_url, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)

    classroom_url = community_url.rstrip("/") + "/classroom"
    print(f"Navigating to classroom: {classroom_url}")
    page.goto(classroom_url, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)

    return _sections_from_next_data(page, community_url)


def _sections_from_next_data(page, community_url: str) -> list[dict]:
    """
    Parse allCourses from pageProps. Each course object has:
      id: UUID (e.g. '4d72d7dae6224d698ba6a2ad9dff8aec')
      name: short hex hash (e.g. 'd99017d2') — used in URLs
      metadata.title: human-readable name (e.g. 'Build Your Portfolio')
    """
    try:
        raw = page.evaluate(
            "() => document.getElementById('__NEXT_DATA__')?.textContent"
        )
        if not raw:
            return []
        data = json.loads(raw)

        all_courses = _find_key(data, "allCourses")
        if not all_courses:
            print("WARNING: allCourses not found in __NEXT_DATA__")
            return []

        sections = []
        for course in all_courses:
            course_id = course.get("id", "")
            course_hash = course.get("name", "")  # hash used in URLs
            if not course_id:
                continue
            title = _meta_title(course)
            # Course page URLs use the hash name, not the UUID
            url = f"{community_url.rstrip('/')}/classroom/{course_hash}"
            sections.append({
                "title": title,
                "url": url,
                "id": course_id,
                "hash": course_hash,
            })

        return sections
    except Exception as e:
        print(f"Section discovery failed: {e}")
        return []


def pick_section(sections: list[dict], section_arg: str | None) -> dict:
    """Returns the chosen section dict."""
    if section_arg:
        needle = section_arg.lower()
        for s in sections:
            if needle in s["title"].lower():
                print(f"Section matched: {s['title']}")
                return s
        print(f"ERROR: No section matching '{section_arg}' found.")
        print("Available sections:")
        for i, s in enumerate(sections, 1):
            print(f"  [{i}] {s['title']}")
        sys.exit(1)

    print("\nAvailable sections:")
    for i, s in enumerate(sections, 1):
        print(f"  [{i}] {s['title']}")
    while True:
        raw = input("\nWhich section? Enter a number: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(sections):
            chosen = sections[int(raw) - 1]
            print(f"Selected: {chosen['title']}")
            return chosen
        print(f"Please enter a number between 1 and {len(sections)}.")


# ---------------------------------------------------------------------------
# Lesson discovery
# ---------------------------------------------------------------------------

def discover_lessons(page, section: dict, community_url: str) -> list[dict]:
    """Navigate to the course page and return all lessons with phase metadata."""
    url = section.get("url") or (community_url.rstrip("/") + "/classroom")
    print(f"Loading course: {url}")
    # Skool redirects to the first lesson URL — that's fine, the full course
    # tree is still in __NEXT_DATA__ on the redirected page.
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    print(f"  Landed at: {page.url}")

    raw = page.evaluate(
        "() => document.getElementById('__NEXT_DATA__')?.textContent"
    )
    if not raw:
        print("WARNING: __NEXT_DATA__ not found on course page.")
        return _lessons_from_dom(page, section, community_url)

    data = json.loads(raw)
    pp = data.get("props", {}).get("pageProps", {})
    course_wrap = pp.get("course")
    if not course_wrap:
        print("WARNING: 'course' key not found in pageProps.")
        return _lessons_from_dom(page, section, community_url)

    lessons = _lessons_from_course_tree(course_wrap, section, community_url)
    if lessons:
        return lessons

    print("WARNING: No lessons from __NEXT_DATA__ tree. Trying DOM fallback.")
    return _lessons_from_dom(page, section, community_url)


def _lessons_from_course_tree(course_wrap: dict, section: dict, community_url: str) -> list[dict]:
    """
    Parse the Skool course tree structure:
      course_wrap = {
        "course": { id, name (hash), metadata: { title } },
        "children": [  # top-level modules
          {
            "course": { id, name, metadata: { title } },
            "children": [  # lessons
              { "course": { id, name, metadata: { title, videoLink? } }, "children": [] },
              ...
            ]
          },
          ...
        ]
      }

    Modules with children → their children are lessons.
    Modules with no children but videoLink in metadata → standalone lessons.
    Modules with no children and no videoLink → section dividers, skip them.
    """
    course_obj = course_wrap.get("course", {})
    course_hash = course_obj.get("name", "")  # e.g. "d99017d2" — used in lesson URLs
    top_children = course_wrap.get("children", [])

    lessons = []
    phase_counter = 0
    current_section = section["title"]

    for mod_wrap in top_children:
        if not isinstance(mod_wrap, dict):
            continue
        mod = mod_wrap.get("course", {})
        mod_meta = mod.get("metadata") or {}
        mod_title = _meta_title(mod)
        mod_children = mod_wrap.get("children", [])

        if mod_children:
            # Module with lessons
            for les_idx, les_wrap in enumerate(mod_children):
                if not isinstance(les_wrap, dict):
                    continue
                les = les_wrap.get("course", {})
                les_meta = les.get("metadata") or {}
                les_title = _meta_title(les)
                les_id = les.get("id", "")
                les_url = (
                    f"{community_url.rstrip('/')}/classroom/{course_hash}?md={les_id}"
                    if les_id else ""
                )
                lessons.append({
                    "phase_title": mod_title or current_section,
                    "phase_index": phase_counter,
                    "lesson_title": les_title,
                    "lesson_index": les_idx,
                    "lesson_id": les_id,
                    "lesson_url": les_url,
                    "video_link": les_meta.get("videoLink", ""),
                    "video_type": None,
                    "video_src": None,
                    "transcript": "",
                    "transcript_method": "none",
                })
            phase_counter += 1
        else:
            # No children — standalone item
            has_video = bool(mod_meta.get("videoLink"))
            if has_video:
                mod_id = mod.get("id", "")
                mod_url = (
                    f"{community_url.rstrip('/')}/classroom/{course_hash}?md={mod_id}"
                    if mod_id else ""
                )
                lessons.append({
                    "phase_title": current_section,
                    "phase_index": phase_counter,
                    "lesson_title": mod_title,
                    "lesson_index": 0,
                    "lesson_id": mod_id,
                    "lesson_url": mod_url,
                    "video_link": mod_meta.get("videoLink", ""),
                    "video_type": None,
                    "video_src": None,
                    "transcript": "",
                    "transcript_method": "none",
                })
                phase_counter += 1
            # else: section divider (trophy/pin with no video) — skip

    return lessons


def _lessons_from_dom(page, section: dict, community_url: str) -> list[dict]:
    """
    Last-resort: find all ?md= lesson links in the DOM and infer phase from
    the nearest preceding heading element.
    """
    try:
        page.wait_for_selector("a[href*='?md=']", timeout=10000)
    except PWTimeout:
        pass

    try:
        items = page.evaluate("""
            () => {
                const out = [];
                const seen = new Set();
                for (const a of document.querySelectorAll('a[href*="?md="]')) {
                    const href = a.getAttribute('href') || '';
                    if (seen.has(href)) continue;
                    seen.add(href);

                    let phase = '';
                    let el = a.parentElement;
                    for (let depth = 0; el && depth < 12; depth++) {
                        let prev = el.previousElementSibling;
                        while (prev && !phase) {
                            const tag = (prev.tagName || '').toLowerCase();
                            if (/^h[1-6]$/.test(tag)) {
                                phase = prev.innerText.trim();
                                break;
                            }
                            const h = prev.querySelector('h1,h2,h3,h4,h5,h6');
                            if (h) { phase = h.innerText.trim(); break; }
                            prev = prev.previousElementSibling;
                        }
                        if (phase) break;
                        el = el.parentElement;
                    }

                    out.push({
                        href,
                        title: a.innerText.trim().split('\\n')[0].trim(),
                        phase
                    });
                }
                return out;
            }
        """)

        lessons = []
        for i, item in enumerate(items or []):
            href = item.get("href", "")
            if not href:
                continue
            md_match = re.search(r"[?&]md=([^&]+)", href)
            lesson_id = md_match.group(1) if md_match else f"lesson_{i}"
            full_url = f"https://www.skool.com{href}" if href.startswith("/") else href
            lessons.append({
                "phase_title": item.get("phase") or section["title"],
                "phase_index": 0,
                "lesson_title": item.get("title") or f"Lesson {i + 1}",
                "lesson_index": i,
                "lesson_id": lesson_id,
                "lesson_url": full_url,
                "video_link": "",
                "video_type": None,
                "video_src": None,
                "transcript": "",
                "transcript_method": "none",
            })
        return lessons
    except Exception as e:
        print(f"DOM lesson discovery failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Transcript extraction
# ---------------------------------------------------------------------------

def _loom_video_id(url: str) -> str | None:
    """Extract Loom video ID from a share URL."""
    match = re.search(r"loom\.com/share/([a-f0-9]{32})", url or "")
    return match.group(1) if match else None


def _youtube_video_id(url: str) -> str | None:
    """Extract YouTube video ID from a watch/share/embed URL."""
    match = re.search(
        r"(?:youtube\.com/(?:watch\?.*v=|embed/)|youtu\.be/)([a-zA-Z0-9_-]{11})",
        url or "",
    )
    return match.group(1) if match else None


def extract_transcript(page, lesson: dict) -> dict:
    """
    Extracts transcript using video_link stored during lesson discovery:
    1. Loom video   → navigate to loom.com/share/{id}, intercept captions VTT
    2. YouTube link → youtube_transcript_api
    3. Whisper      → yt-dlp download + local Whisper (Loom URL works with yt-dlp)

    Does NOT navigate to the Skool lesson page — video_link from __NEXT_DATA__
    already tells us where the video lives.
    """
    video_link = lesson.get("video_link", "")
    print(f"  Processing: {lesson['lesson_title']}")

    # --- Method 1: Loom ---
    loom_id = _loom_video_id(video_link)
    if loom_id:
        transcript = _transcript_loom(page, loom_id)
        if transcript:
            lesson["video_type"] = "loom"
            lesson["video_src"] = loom_id
            lesson["transcript"] = transcript
            lesson["transcript_method"] = "loom_vtt"
            print(f"    OK Loom VTT ({len(transcript)} chars)")
            return lesson
        print(f"    Loom VTT empty — trying Whisper fallback...")
        transcript = _transcript_whisper_loom(loom_id)
        if transcript:
            lesson["video_type"] = "loom"
            lesson["video_src"] = loom_id
            lesson["transcript"] = transcript
            lesson["transcript_method"] = "whisper"
            print(f"    OK Whisper ({len(transcript)} chars)")
            return lesson

    # --- Method 2: YouTube ---
    yt_id = _youtube_video_id(video_link)
    if yt_id:
        transcript = _transcript_youtube(yt_id)
        if transcript:
            lesson["video_type"] = "youtube"
            lesson["video_src"] = yt_id
            lesson["transcript"] = transcript
            lesson["transcript_method"] = "youtube_api"
            print(f"    OK YouTube ({len(transcript)} chars)")
            return lesson

    if not video_link:
        print(f"    SKIP No video link")
    else:
        print(f"    SKIP No transcript available (video_link: {video_link[:60]})")
    return lesson


def _transcript_loom(page, loom_id: str) -> str:
    """Navigate to Loom share page and capture the auto-generated captions VTT."""
    loom_url = f"https://www.loom.com/share/{loom_id}"
    captured = {"text": None}

    def handle_response(response):
        url = response.url
        if "cdn.loom.com" in url and "captions" in url and ".vtt" in url:
            if captured["text"] is None:
                try:
                    captured["text"] = response.text()
                except Exception:
                    pass

    page.on("response", handle_response)
    try:
        page.goto(loom_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(4000)
    except PWTimeout:
        pass
    finally:
        page.remove_listener("response", handle_response)

    if captured["text"]:
        return _parse_vtt(captured["text"])
    return ""


def _transcript_youtube(video_id: str) -> str:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
        return " ".join(entry["text"] for entry in transcript_list)
    except Exception as e:
        print(f"    YouTube transcript API error: {e}")
        return ""


def _parse_vtt(vtt_text: str) -> str:
    """Strip VTT timing/cue metadata and return plain text."""
    lines = vtt_text.splitlines()
    text_lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("WEBVTT") or line.startswith("NOTE"):
            continue
        if re.match(r"^\d{2}:\d{2}", line) or "-->" in line:
            continue
        if re.match(r"^\d+$", line):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        if line:
            text_lines.append(line)
    return " ".join(text_lines)


def _transcript_whisper_loom(loom_id: str) -> str:
    """Download Loom audio with yt-dlp and transcribe with local Whisper."""
    import shutil
    if not shutil.which("ffmpeg"):
        print("    ERROR: ffmpeg not found on PATH. Install with: winget install Gyan.FFmpeg")
        return ""

    try:
        import yt_dlp
        import whisper
    except ImportError as e:
        print(f"    ERROR: Missing package for Whisper fallback: {e}")
        return ""

    audio_path = TMP_DIR / f"audio_{loom_id}.mp3"
    loom_url = f"https://www.loom.com/share/{loom_id}"

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(audio_path.with_suffix("")),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
        "quiet": True,
        "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([loom_url])
    except Exception as e:
        print(f"    yt-dlp download failed: {e}")
        return ""

    if not audio_path.exists():
        print("    Audio file not created by yt-dlp.")
        return ""

    try:
        model_name = os.getenv("WHISPER_MODEL", "base")
        model = whisper.load_model(model_name)
        result = model.transcribe(str(audio_path))
        return result.get("text", "")
    except Exception as e:
        print(f"    Whisper transcription failed: {e}")
        return ""
    finally:
        audio_path.unlink(missing_ok=True)



# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Scrape Skool course section transcripts")
    parser.add_argument("--section", help="Section name to scrape (skips interactive menu)")
    parser.add_argument("--lesson", help="Scrape only lessons whose title contains this string (case-insensitive)")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--no-headless", dest="headless", action="store_false",
                        help="Show browser window (useful for MFA)")
    args = parser.parse_args()

    email = os.getenv("SKOOL_EMAIL")
    password = os.getenv("SKOOL_PASSWORD")
    community_url = os.getenv("SKOOL_COMMUNITY_URL")

    if not email or not password:
        print("ERROR: SKOOL_EMAIL and SKOOL_PASSWORD must be set in .env")
        sys.exit(1)
    if not community_url:
        print("ERROR: SKOOL_COMMUNITY_URL must be set in .env")
        sys.exit(1)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)

        if SESSION_FILE.exists() and (time.time() - SESSION_FILE.stat().st_mtime) < 82800:
            print("Reusing saved Skool session.")
            context = browser.new_context(storage_state=str(SESSION_FILE))
        else:
            context = browser.new_context()

        page = context.new_page()

        # Login check
        page.goto("https://www.skool.com", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        if "login" in page.url or page.query_selector('a[href="/login"]'):
            login(page, email, password)
        context.storage_state(path=str(SESSION_FILE))

        # Discover and pick section
        sections = get_sections(page, community_url)
        if not sections:
            print("ERROR: Could not find any sections. Try --no-headless to debug.")
            browser.close()
            sys.exit(1)

        chosen = pick_section(sections, args.section)

        # Discover lessons
        print(f"\nDiscovering lessons in '{chosen['title']}'...")
        lessons = discover_lessons(page, chosen, community_url)
        if not lessons:
            print("ERROR: No lessons found. Try --no-headless to debug.")
            browser.close()
            sys.exit(1)

        print(f"Found {len(lessons)} lessons.")

        if args.lesson:
            query = args.lesson.lower()
            filtered = [l for l in lessons if query in l["lesson_title"].lower()]
            if not filtered:
                print(f"ERROR: No lessons found matching '{args.lesson}'")
                print("Available lessons:")
                for l in lessons:
                    print(f"  - {l['lesson_title']}")
                browser.close()
                sys.exit(1)
            print(f"Filtering to {len(filtered)} lesson(s) matching '{args.lesson}'")
            lessons = filtered

        print("Extracting transcripts...\n")

        for i, lesson in enumerate(lessons):
            lessons[i] = extract_transcript(page, lesson)
            _save(lessons, chosen, community_url)

        browser.close()

    print(f"\nDone. Saved to {OUTPUT_FILE}")
    success = sum(1 for l in lessons if l["transcript_method"] != "none")
    print(f"Transcripts extracted: {success}/{len(lessons)}")


def _save(lessons: list, section: dict, community_url: str) -> None:
    data = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "community_url": community_url,
        "section_name": section["title"],
        "lessons": lessons,
    }
    OUTPUT_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
