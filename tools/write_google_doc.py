"""
Reads .tmp/course_takeaways.json and appends takeaways to a persistent Google Doc.
Each run adds the new lesson(s) after a page break. The doc ID is stored in
.tmp/master_doc_id.txt so the same doc is reused across runs.

Usage:
    python tools/write_google_doc.py

First run: creates the doc and stores its ID. Subsequent runs append to it.
"""

import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

TMP_DIR = Path(__file__).parent.parent / ".tmp"
INPUT_FILE = TMP_DIR / "course_takeaways.json"
MASTER_DOC_FILE = TMP_DIR / "master_doc_id.txt"
ROOT_DIR = Path(__file__).parent.parent
CREDENTIALS_FILE = ROOT_DIR / "credentials.json"
TOKEN_FILE = ROOT_DIR / "token.json"

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def get_credentials() -> Credentials:
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                print("ERROR: credentials.json not found. See workflows/extract_skool_course.md.")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds


# ---------------------------------------------------------------------------
# Doc management
# ---------------------------------------------------------------------------

def _community_label(community_url: str) -> str:
    slug = community_url.rstrip("/").split("/")[-1]
    return slug.replace("-", " ").title()


def get_or_create_doc(docs_service, community_label: str) -> tuple[str, bool]:
    """Returns (doc_id, is_new). Reuses stored doc if it still exists."""
    doc_title = f"{community_label} — Course Takeaways"

    if MASTER_DOC_FILE.exists():
        doc_id = MASTER_DOC_FILE.read_text(encoding="utf-8").strip()
        try:
            docs_service.documents().get(documentId=doc_id).execute()
            print(f"Using existing Google Doc ({doc_id})")
            return doc_id, False
        except HttpError:
            print("Stored doc not found or inaccessible — creating a new one.")

    print(f"Creating Google Doc: '{doc_title}'...")
    doc = docs_service.documents().create(body={"title": doc_title}).execute()
    doc_id = doc["documentId"]
    MASTER_DOC_FILE.write_text(doc_id, encoding="utf-8")
    return doc_id, True


def get_doc_end_index(docs_service, doc_id: str) -> int:
    """Returns the index of the last character in the doc body."""
    doc = docs_service.documents().get(documentId=doc_id).execute()
    content = doc.get("body", {}).get("content", [])
    if content:
        return content[-1].get("endIndex", 1)
    return 1


# ---------------------------------------------------------------------------
# Document content
# ---------------------------------------------------------------------------

def build_requests(data: dict, insert_at: int, is_new_doc: bool) -> list[dict]:
    """
    Build insertText + style batchUpdate requests.
    Prepends a page break when appending to an existing doc.
    All text is inserted at insert_at so requests are applied in sequence.
    """
    section_name = data.get("section_name", "Course")
    lessons = data.get("lessons", [])

    lines = []

    # Page break separator (not needed for the very first write)
    if not is_new_doc:
        lines.append(("\f", "pagebreak"))  # \f = form feed = page break in Docs

    phases: dict[str, list] = {}
    for lesson in lessons:
        phase = lesson.get("phase_title") or section_name
        phases.setdefault(phase, []).append(lesson)

    for phase_title, phase_lessons in phases.items():
        lines.append((phase_title, "h1"))
        for lesson in phase_lessons:
            lines.append((lesson.get("lesson_title", "Untitled"), "h2"))
            takeaways = lesson.get("takeaways", [])
            if takeaways:
                for t in takeaways:
                    lines.append((t, "bullet"))
            else:
                lines.append(("[No transcript available]", "normal"))
        lines.append(("", "normal"))

    # Build full text and track offsets from insert_at
    full_text = ""
    style_requests = []
    current_index = insert_at

    for text, style in lines:
        if style == "pagebreak":
            full_text += text
            current_index += len(text)
            continue

        line_text = text + "\n"
        start = current_index
        end = start + len(line_text)

        if style == "h1":
            style_requests.append(_paragraph_style(start, end, "HEADING_1"))
        elif style == "h2":
            style_requests.append(_paragraph_style(start, end, "HEADING_2"))
        elif style == "bullet":
            style_requests.append(_bullet(start, end))

        full_text += line_text
        current_index = end

    return [
        {
            "insertText": {
                "location": {"index": insert_at},
                "text": full_text,
            }
        }
    ] + style_requests


def _paragraph_style(start: int, end: int, named_style: str) -> dict:
    return {
        "updateParagraphStyle": {
            "range": {"startIndex": start, "endIndex": end},
            "paragraphStyle": {"namedStyleType": named_style},
            "fields": "namedStyleType",
        }
    }


def _bullet(start: int, end: int) -> dict:
    return {
        "createParagraphBullets": {
            "range": {"startIndex": start, "endIndex": end},
            "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
        }
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not INPUT_FILE.exists():
        print(f"ERROR: {INPUT_FILE} not found. Run extract_takeaways.py first.")
        sys.exit(1)

    data = json.loads(INPUT_FILE.read_text(encoding="utf-8"))
    community_label = _community_label(data.get("community_url", "Skool"))
    lessons = data.get("lessons", [])

    print("Authenticating with Google...")
    creds = get_credentials()
    docs_service = build("docs", "v1", credentials=creds)

    doc_id, is_new_doc = get_or_create_doc(docs_service, community_label)

    # Find where to insert (end of doc, minus the trailing newline sentinel)
    end_index = get_doc_end_index(docs_service, doc_id)
    insert_at = max(1, end_index - 1)

    print("Building document content...")
    requests = build_requests(data, insert_at, is_new_doc)

    print(f"Writing {len(lessons)} lesson(s) to Google Doc...")
    docs_service.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": requests},
    ).execute()

    doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
    print(f"\nDone!\nGoogle Doc URL: {doc_url}")


if __name__ == "__main__":
    main()
