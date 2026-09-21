from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import uvicorn


@dataclass
class SubmissionRecord:
    id: str
    payload: dict[str, Any]
    files: dict[str, str]


@dataclass
class MockPortalState:
    submissions: list[SubmissionRecord] = field(default_factory=list)
    port: int | None = None
    server: uvicorn.Server | None = None


MOCK_PORTAL_STATE = MockPortalState()

TEMPLATES_DIR = Path(__file__).resolve().parent / "mock_portal_templates"
TEMPLATES_DIR.mkdir(exist_ok=True)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def create_mock_form_template() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{{ role }} - {{ employer }}</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 800px; margin: 2rem auto; padding: 0 1rem; }
    .section { margin-bottom: 2rem; padding: 1rem; border: 1px solid #ddd; border-radius: 8px; }
    .section h2 { margin-top: 0; }
    label { display: block; margin-bottom: 0.5rem; font-weight: 500; }
    input, select, textarea { width: 100%; padding: 0.5rem; margin-bottom: 1rem; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
    .radio-group { display: flex; gap: 1rem; margin-bottom: 1rem; }
    .radio-group label { font-weight: normal; display: flex; align-items: center; gap: 0.25rem; }
    button { padding: 0.75rem 1.5rem; background: #0066cc; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 1rem; }
    button:hover { background: #0052a3; }
    .sensitive-badge { background: #fff3cd; color: #856404; padding: 0.25rem 0.5rem; border-radius: 4px; font-size: 0.875rem; }
  </style>
</head>
<body>
  <header>
    <h1>{{ role }}</h1>
    <p>{{ employer }} - London - 2027 Summer Internship</p>
  </header>

  <main data-employer="{{ employer }}" data-role="{{ role }}" data-requisition="mock-portal-requisition">
    <form id="application-form" class="ats-form" data-ats-application data-automation-id="application-form" data-argus-form-identity="mock-portal-form" method="post" action="/submit" enctype="multipart/form-data">
    <section class="section">
      <h2>Personal Information</h2>
      <div class="grid">
        <div>
          <label for="first_name">Given Name <span class="required">*</span></label>
          <input type="text" id="first_name" name="first_name" required autocomplete="given-name">
        </div>
        <div>
          <label for="last_name">Family Name <span class="required">*</span></label>
          <input type="text" id="last_name" name="last_name" required autocomplete="family-name">
        </div>
      </div>
      <div>
        <label for="email">Email Address <span class="required">*</span></label>
        <input type="email" id="email" name="email" required autocomplete="email">
      </div>
      <div>
        <label for="phone">Phone Number <span class="required">*</span></label>
        <input type="tel" id="phone" name="phone" required autocomplete="tel">
      </div>
    </section>

    <section class="section">
      <h2>Address</h2>
      <div>
        <label for="address">Address Line 1 <span class="required">*</span></label>
        <input type="text" id="address" name="address" required autocomplete="address-line1">
      </div>
      <div class="grid">
        <div>
          <label for="postcode">Postcode <span class="required">*</span></label>
          <input type="text" id="postcode" name="postcode" required autocomplete="postal-code">
        </div>
        <div>
          <label for="city">City <span class="required">*</span></label>
          <input type="text" id="city" name="city" required autocomplete="address-level2">
        </div>
      </div>
    </section>

    <section class="section">
      <h2>Education</h2>
      <div>
        <label for="university">University / Institution <span class="required">*</span></label>
        <input type="text" id="university" name="university" required>
      </div>
      <div>
        <label for="degree">Degree <span class="required">*</span></label>
        <input type="text" id="degree" name="degree" required>
      </div>
      <div>
        <label for="graduation_date">Expected Graduation Date <span class="required">*</span></label>
        <input type="month" id="graduation_date" name="graduation_date" required>
      </div>
    </section>

    <section class="section">
      <h2>Documents</h2>
      <div>
        <label for="cv">CV Upload <span class="required">*</span></label>
        <input type="file" id="cv" name="cv" accept=".pdf,.doc,.docx" required>
      </div>
      <div>
        <label for="cover_letter">Cover Letter Upload <span class="required">*</span></label>
        <input type="file" id="cover_letter" name="cover_letter" accept=".pdf,.doc,.docx" required>
      </div>
    </section>

    <section class="section">
      <h2>Additional Questions</h2>
      <div>
        <label for="source">How did you hear about us? <span class="required">*</span></label>
        <select id="source" name="source" required>
          <option value="">Select...</option>
          <option value="careers_page">Company Careers Page</option>
          <option value="job_board">Job Board (LinkedIn, Indeed, etc.)</option>
          <option value="referral">Employee Referral</option>
          <option value="university">University Career Services</option>
          <option value="other">Other</option>
        </select>
      </div>
    </section>

    <section class="section">
      <h2>Work Authorisation <span class="sensitive-badge">SENSITIVE</span></h2>
      <p>This question is sensitive and must not be auto-answered.</p>
      <fieldset>
        <legend>Do you currently have the legal right to work in the UK?</legend>
        <div class="radio-group">
          <label><input type="radio" name="work_authorisation" value="yes" required> Yes</label>
          <label><input type="radio" name="work_authorisation" value="no" required> No</label>
        </div>
      </fieldset>
    </section>

    <button type="submit">Submit Application</button>
  </form>
  </main>
</body>
</html>"""


def create_confirmation_template() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Application Submitted</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 600px; margin: 3rem auto; padding: 0 1rem; text-align: center; }
    .reference { font-family: monospace; font-size: 1.25rem; color: #0066cc; }
  </style>
</head>
<body>
  <h1>Application Received</h1>
  <p>Thank you for your application to <strong>{{ employer }}</strong> for the <strong>{{ role }}</strong> position.</p>
  <p class="reference">Reference: {{ reference }}</p>
  <p>We will review your application and be in touch soon.</p>
</body>
</html>"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    form_template_path = TEMPLATES_DIR / "form.html"
    confirmation_template_path = TEMPLATES_DIR / "confirmation.html"
    if not form_template_path.exists():
        form_template_path.write_text(create_mock_form_template())
    if not confirmation_template_path.exists():
        confirmation_template_path.write_text(create_confirmation_template())
    yield


app = FastAPI(title="ARGUS Mock Employer Portal", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "form.html",
        {
            "employer": "Mock Employer Ltd",
            "role": "Summer Analyst",
        },
    )


@app.post("/submit", response_class=HTMLResponse)
async def submit(
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(...),
    address: str = Form(...),
    postcode: str = Form(...),
    city: str = Form(...),
    university: str = Form(...),
    degree: str = Form(...),
    graduation_date: str = Form(...),
    cv: UploadFile = Form(...),
    cover_letter: UploadFile = Form(...),
    source: str = Form(...),
    work_authorisation: str = Form(...),
):
    submission_id = uuid.uuid4().hex[:12]
    payload = {
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "phone": phone,
        "address": address,
        "postcode": postcode,
        "city": city,
        "university": university,
        "degree": degree,
        "graduation_date": graduation_date,
        "cover_letter_filename": cover_letter.filename if cover_letter else "unknown",
        "source": source,
        "work_authorisation": "[REDACTED - SENSITIVE]",
    }
    files = {"cv": cv.filename or "unknown"} if cv else {}
    if cover_letter:
        files["cover_letter"] = cover_letter.filename
    record = SubmissionRecord(id=submission_id, payload=payload, files=files)
    MOCK_PORTAL_STATE.submissions.append(record)
    return templates.TemplateResponse(
        request,
        "confirmation.html",
        {
            "employer": "Mock Employer Ltd",
            "role": "Summer Analyst",
            "reference": f"MOCK-{submission_id.upper()}",
        },
    )


@app.get("/api/submissions", response_class=JSONResponse)
async def get_submissions():
    return {
        "submissions": [
            {"id": s.id, "payload": s.payload, "files": s.files}
            for s in MOCK_PORTAL_STATE.submissions
        ]
    }


@app.post("/api/reset", response_class=JSONResponse)
async def reset_submissions():
    MOCK_PORTAL_STATE.submissions.clear()
    return {"status": "ok", "cleared": True}


def start_mock_portal(port: int = 0) -> tuple[str, uvicorn.Server]:
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    MOCK_PORTAL_STATE.server = server
    import threading

    def run_server():
        server.run()

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    while not server.started:
        import time

        time.sleep(0.01)
    actual_port = server.servers[0].sockets[0].getsockname()[1] if server.servers else port
    MOCK_PORTAL_STATE.port = actual_port
    base_url = f"http://127.0.0.1:{actual_port}"
    return base_url, server


def stop_mock_portal() -> None:
    if MOCK_PORTAL_STATE.server:
        MOCK_PORTAL_STATE.server.should_exit = True
        MOCK_PORTAL_STATE.server = None
    MOCK_PORTAL_STATE.port = None


import pytest


@pytest.fixture(scope="session")
def mock_portal():
    base_url, server = start_mock_portal(0)
    yield base_url
    stop_mock_portal()


@pytest.fixture(autouse=True)
def reset_mock_portal():
    MOCK_PORTAL_STATE.submissions.clear()
    yield
    MOCK_PORTAL_STATE.submissions.clear()