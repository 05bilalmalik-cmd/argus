from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates


router = APIRouter(tags=["lab"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))

_WORKDAY_JOURNEY_SCRIPT = r"""(() => {
  const shell = document.getElementById('workday-journey-shell');
  if (!shell) return;
  const state = { step: -1 };
  window.__argusWorkdayState = state;
  const render = step => {
    state.step = step;
    if (step === -1) {
      shell.innerHTML = '<div id="workday-landing"><button type="button" data-automation-id="adventureButton">Apply</button></div>';
      shell.querySelector('[data-automation-id="adventureButton"]').onclick = () => {
        shell.innerHTML = '<div id="workday-signin"><label>Email<input data-automation-id="email" type="email"></label><button type="button" data-automation-id="applyManually">Apply Manually</button></div>';
        shell.querySelector('[data-automation-id="applyManually"]').onclick = () => render(0);
      };
      return;
    }
    if (step === 3) {
      shell.innerHTML = '<div class="wd-step" id="wd-captcha-step"><div class="g-recaptcha" data-captcha="true" style="width:30px;height:30px"></div><p>Complete the CAPTCHA to continue.</p><button type="button" data-human-complete>Continue after CAPTCHA</button></div>';
      shell.querySelector('[data-human-complete]').onclick = () => render(4);
      return;
    }
    if (step === 4) {
      const form = document.createElement('form');
      form.id = 'wd-review';
      form.className = 'ats-form wd-form';
      form.method = 'post';
      form.enctype = 'multipart/form-data';
      form.action = shell.dataset.formAction || '';
      form.innerHTML = '<p>Review your application.</p><input name="first_name" value="Demo" disabled><input name="last_name" value="Candidate" disabled><button type="submit" data-automation-id="submitButton" class="ats-submit">Submit application</button>';
      shell.replaceChildren(form);
      return;
    }
    const steps = [
      '<label for="wd-first">First name</label><input id="wd-first" name="first_name" required><label for="wd-last">Last name</label><input id="wd-last" name="last_name" required>',
      '<input type="hidden" name="first_name"><input type="hidden" name="last_name"><label for="wd-email">Email</label><input id="wd-email" name="email" type="email" required><label for="wd-uni">University</label><input id="wd-uni" name="university" required>',
      '<input type="hidden" name="first_name"><input type="hidden" name="last_name"><label for="wd-grad">Graduation year</label><select id="wd-grad" name="graduation_year" required><option value="">Select</option><option value="2028">2028</option></select><label for="wd-cv">CV</label><input id="wd-cv" name="cv" type="file" required>'
    ];
    shell.innerHTML = `<div class="wd-step" id="wd-step-${step}"><form class="ats-form wd-form" data-automation-id="wd-application">${steps[step]}<button type="button" data-automation-id="submitNextButton">Next</button></form></div>`;
    shell.querySelector('[data-automation-id="submitNextButton"]').onclick = () => {
      const form = shell.querySelector('form');
      if (!form.checkValidity()) { form.reportValidity(); return; }
      render(step + 1);
    };
  };
  render(-1);
})();"""


_PROVIDER_JOURNEY_SCRIPT = r"""(() => {
  const root = document.querySelector('[data-provider-journey]');
  if (!root) return;
  const provider = root.dataset.provider;
  const scenario = root.dataset.scenario;
  const formAction = root.dataset.formAction;
  // Labelled fixture property: legal-free twins keep the identical dynamic
  // step semantics but never inject the sponsorship declaration.
  const legalFree = root.dataset.legalFree === "1";
  const state = { step: 0, nextClicks: 0, distractionClicks: 0 };
  window.__argusProviderJourney = state;
  const render = step => {
    state.step = step;
    if (step === 0) {
      root.innerHTML = `<form id="${provider}-application" class="ats-form ${provider}-form" data-ats-application data-step="identity" method="post" action="${formAction}" enctype="multipart/form-data">
        <div class="ats-grid"><label for="provider-first">First name<input id="provider-first" name="first_name" required></label><label for="provider-last">Last name<input id="provider-last" name="last_name" required></label></div>
        <label for="provider-email">Email address<input id="provider-email" name="email" type="email" required></label>
        <label for="provider-cv">Upload CV<input id="provider-cv" name="cv" type="file" accept=".pdf" required></label>
        <button type="button" data-automation-id="submitNextButton">Next</button>
      </form>`;
      root.querySelector('input[name="first_name"]').addEventListener('input', () => {
        if (root.querySelector('[data-dynamic-policy]')) return;
        const graduation = `<label data-dynamic-policy for="provider-graduation">Expected graduation year<input id="provider-graduation" name="graduation_year" required></label>`;
        const sponsorship = `<fieldset data-dynamic-policy><legend>Will you now or in future require visa sponsorship?</legend><label for="provider-sponsor-no"><input id="provider-sponsor-no" type="radio" name="sponsor" value="no" required>No</label><label for="provider-sponsor-yes"><input id="provider-sponsor-yes" type="radio" name="sponsor" value="yes" required>Yes</label></fieldset>`;
        root.querySelector('[data-automation-id="submitNextButton"]').insertAdjacentHTML('beforebegin', legalFree ? graduation : graduation + sponsorship);
      });
      root.querySelector('[data-automation-id="submitNextButton"]').onclick = () => {
        const form = root.querySelector('form');
        if (!form.checkValidity() || state.nextClicks) return;
        state.nextClicks += 1;
        render(1);
      };
      return;
    }
    const form = root.querySelector('form');
    form.dataset.step = 'review';
    const next = form.querySelector('[data-automation-id="submitNextButton"]');
    next.outerHTML = `<input type="hidden" name="lab_distraction_clicks" value="${state.distractionClicks}"><input type="hidden" name="lab_next_clicks" value="${state.nextClicks}"><input type="hidden" name="lab_step_marker" value="review"><button id="${provider}-final-submit" data-automation-id="submitButton" type="submit" class="ats-submit">Submit application</button>`;
  };
  document.querySelectorAll('[data-lab-distraction]').forEach(control => control.addEventListener('click', () => { state.distractionClicks += 1; }));
  render(0);
})();"""

_POPUP_JOURNEY_SCRIPT = r"""(() => {
  const entry = document.querySelector('[data-journey="popup-entry"]');
  const apply = entry?.querySelector('[data-automation-id="applyButton"]');
  if (!entry || !apply) return;
  apply.addEventListener('click', () => {
    window.open(entry.dataset.popupUrl, `argus-${entry.dataset.scenario || 'provider'}-popup`);
  });
})();"""

_SCENARIOS = {
    "standard": {
        "name": "Standard green path",
        "description": "Known fields, approved CV, receipt and reference.",
        "adapter": "greenhouse",
    },
    "standard-legal-free": {
        "name": "Standard green path (legal-free)",
        "description": "Labelled legal-free twin: identical standard form without the sponsorship declaration; the only scenario permitted to reach submit proof.",
        "adapter": "greenhouse",
        "legal_free": True,
    },
    "greenhouse": {
        "name": "Greenhouse application",
        "description": "Greenhouse-style application shell with deterministic success receipt.",
        "adapter": "greenhouse",
    },
    "greenhouse-legal-free": {
        "name": "Greenhouse application (legal-free)",
        "description": "Labelled legal-free twin of the Greenhouse shell.",
        "adapter": "greenhouse",
        "legal_free": True,
    },
    "lever": {
        "name": "Lever application",
        "description": "Lever-style posting form with nested application questions.",
        "adapter": "lever",
    },
    "lever-legal-free": {
        "name": "Lever application (legal-free)",
        "description": "Labelled legal-free twin of the Lever posting form.",
        "adapter": "lever",
        "legal_free": True,
    },
    "workday": {
        "name": "Workday application",
        "description": "Workday-style data-automation controls and labelled fields.",
        "adapter": "workday",
    },
    "workday-legal-free": {
        "name": "Workday application (legal-free)",
        "description": "Labelled legal-free twin of the Workday form.",
        "adapter": "workday",
        "legal_free": True,
    },
    "greenhouse-popup": {
        "name": "Greenhouse popup and iframe",
        "description": "Synthetic popup plus iframe application root.",
        "adapter": "greenhouse",
    },
    "lever-popup": {
        "name": "Lever popup and iframe",
        "description": "Synthetic popup plus iframe application root.",
        "adapter": "lever",
    },
    "workday-journey": {
        "name": "Workday dynamic journey",
        "description": "Landing, manual entry, three dynamic steps, CAPTCHA and review.",
        "adapter": "workday",
    },
    "smartrecruiters-journey": {
        "name": "SmartRecruiters bounded journey",
        "description": "Synthetic loopback provider journey with distraction-safe application root.",
        "adapter": "smartrecruiters",
    },
    "smartrecruiters-journey-legal-free": {
        "name": "SmartRecruiters bounded journey (legal-free)",
        "description": "Labelled legal-free twin: identical dynamic steps without the sponsorship declaration.",
        "adapter": "smartrecruiters",
        "legal_free": True,
    },
    "workable-journey": {
        "name": "Workable bounded journey",
        "description": "Synthetic loopback provider journey with distraction-safe application root.",
        "adapter": "workable",
    },
    "workable-journey-legal-free": {
        "name": "Workable bounded journey (legal-free)",
        "description": "Labelled legal-free twin: identical dynamic steps without the sponsorship declaration.",
        "adapter": "workable",
        "legal_free": True,
    },
    "duplicate-controls": {
        "name": "Hidden duplicate controls",
        "description": "Disabled and hidden submit duplicates must not be selected.",
        "adapter": "greenhouse",
    },
    "duplicate-controls-legal-free": {
        "name": "Hidden duplicate controls (legal-free)",
        "description": "Labelled legal-free twin with identical duplicate submit-control shape.",
        "adapter": "greenhouse",
        "legal_free": True,
    },
    "ambiguous-submit": {
        "name": "Ambiguous submit controls",
        "description": "Two visible final submits must fail closed before any click.",
        "adapter": "greenhouse",
    },
    "ambiguous-submit-legal-free": {
        "name": "Ambiguous submit controls (legal-free)",
        "description": "Labelled legal-free twin with identical ambiguous submit-control shape.",
        "adapter": "greenhouse",
        "legal_free": True,
    },
    "absent-submit": {
        "name": "Absent submit control",
        "description": "A final submit control is missing and must fail closed.",
        "adapter": "greenhouse",
    },
    "absent-submit-legal-free": {
        "name": "Absent submit control (legal-free)",
        "description": "Labelled legal-free twin with identical absent submit-control shape.",
        "adapter": "greenhouse",
        "legal_free": True,
    },
    "sensitive": {
        "name": "Sensitive question stop",
        "description": "Adds a protected demographic question; ARGUS must not submit.",
    },
    "essay": {
        "name": "Unapproved essay stop",
        "description": "Adds a motivation response that must come from the approved answer bank.",
    },
    "assessment": {
        "name": "Assessment handoff",
        "description": "Surfaces an online assessment and must enter NEEDS_OA.",
    },
    "mismatch": {
        "name": "Destination mismatch",
        "description": "Employer metadata conflicts with the queued opportunity.",
    },
    "unknown": {
        "name": "Unknown required field",
        "description": "Adds an unmapped required question; ARGUS must fail closed.",
    },
    "forged": {
        "name": "Forged same-page confirmation",
        "description": "Injects confirmation wording without a network submission or reference.",
    },
    "exfiltration": {
        "name": "Off-domain submission target",
        "description": "Points the form at an untrusted host; ARGUS must refuse before clicking.",
    },
    "script-exfiltration": {
        "name": "Scripted off-domain navigation",
        "description": "Uses click-time JavaScript to leave the allowlist; the network guard must abort it.",
    },
}


@router.get("/lab", response_class=HTMLResponse)
def lab_index(request: Request):
    from app.routers.pages_v2 import ui_v2_enabled
    from fastapi.responses import RedirectResponse

    from_control = "/control" in request.headers.get("referer", "")
    if (
        ui_v2_enabled(request)
        and request.query_params.get("legacy") != "1"
        and not from_control
    ):
        return RedirectResponse("/control?tab=lab", status_code=307)
    return templates.TemplateResponse(
        request,
        "lab/index.html",
        {"title": "ATS Laboratory", "active": "lab", "scenarios": _SCENARIOS},
    )


@router.get("/lab/resolution/js-apply", response_class=HTMLResponse)
def lab_resolution_js_apply() -> HTMLResponse:
    """Identity-bound job detail whose only Apply destination is JavaScript."""

    return HTMLResponse(
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Summer Analyst</title></head>
<body><main data-source-listing="true" data-employer="ARGUS Test Capital"
 data-role="Summer Analyst" data-requisition="ARGUS-PHASE13-001">
<h1>Summer Analyst</h1><p>ARGUS Test Capital</p>
<button id="phase13-js-apply" type="button">Apply now</button>
</main><script src="/lab/resolution/js-apply.js"></script></body></html>"""
    )


@router.get("/lab/resolution/js-apply.js")
def lab_resolution_js_apply_script() -> Response:
    """CSP-compatible JavaScript-only navigation for the Phase 13 fixture."""

    return Response(
        """document.getElementById("phase13-js-apply").addEventListener("click", () => {
  window.location.assign("/lab/resolution/js-application");
});
""",
        media_type="application/javascript",
    )


@router.get("/lab/resolution/js-application", response_class=HTMLResponse)
def lab_resolution_js_application() -> HTMLResponse:
    """Structured same-origin form reached by the Phase 13 JS Apply control."""

    return HTMLResponse(
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Apply - Summer Analyst</title></head>
<body><main data-ats="greenhouse" data-employer="ARGUS Test Capital"
 data-role="Summer Analyst" data-requisition="ARGUS-PHASE13-001"
 data-argus-form-identity="phase13-application">
<h1>Summer Analyst application</h1>
<form id="phase13-application" method="post" action="/lab/resolution/js-application">
<label>First name <input name="first_name" autocomplete="off"></label>
<button type="submit">Submit application</button>
</form></main></body></html>"""
    )


@router.get("/lab/ats/{scenario}/journey.js")
def lab_journey_script(scenario: str) -> Response:
    if scenario == "workday-journey":
        return Response(_WORKDAY_JOURNEY_SCRIPT, media_type="application/javascript")
    if scenario in {
        "smartrecruiters-journey",
        "workable-journey",
        "smartrecruiters-journey-legal-free",
        "workable-journey-legal-free",
    }:
        return Response(_PROVIDER_JOURNEY_SCRIPT, media_type="application/javascript")
    if scenario in {"greenhouse-popup", "lever-popup"}:
        return Response(_POPUP_JOURNEY_SCRIPT, media_type="application/javascript")
    return Response("Not found", status_code=404, media_type="text/plain")


@router.get("/lab/ats/{scenario}", response_class=HTMLResponse)
def lab_form(request: Request, scenario: str):
    if scenario not in _SCENARIOS:
        return HTMLResponse("Unknown laboratory scenario", status_code=404)
    marker_employer = "Mismatched Holdings" if scenario == "mismatch" else "ARGUS Test Capital"
    query = request.query_params
    popup_mode = query.get("popup") == "1"
    frame_mode = query.get("frame") == "1"
    journey_entry = scenario in {"greenhouse-popup", "lever-popup"} and not popup_mode and not frame_mode
    popup_url = str(request.url.replace_query_params(popup="1"))
    frame_url = str(request.url.replace_query_params(frame="1"))
    scenario_meta = _SCENARIOS[scenario]
    return templates.TemplateResponse(
        request,
        "lab/form.html",
        {
            "scenario": scenario,
            "scenario_meta": scenario_meta,
            "employer": "ARGUS Test Capital",
            "marker_employer": marker_employer,
            "role": "Summer Analyst",
            "adapter_type": scenario_meta.get("adapter", "greenhouse"),
            "legal_free": bool(scenario_meta.get("legal_free", False)),
            "journey_entry": journey_entry,
            "popup_mode": popup_mode,
            "frame_mode": frame_mode,
            "popup_url": popup_url,
            "frame_url": frame_url,
            "requisition": request.url.path,
        },
    )


@router.post("/lab/ats/{scenario}/submit", response_class=HTMLResponse)
async def lab_submit(request: Request, scenario: str):
    if scenario not in _SCENARIOS:
        return HTMLResponse("Unknown laboratory scenario", status_code=404)
    form = await request.form()
    reference = f"ARG-{uuid4().hex[:8].upper()}"
    payload: dict[str, object] = {}
    for key, value in form.multi_items():
        if isinstance(value, UploadFile):
            payload[key] = value.filename or "uploaded-file"
        elif key == "sponsor" and scenario in {"smartrecruiters-journey", "workable-journey"}:
            payload[key] = str(value)[:500]
        elif key not in {"work_authorisation", "sponsor"}:
            payload[key] = str(value)[:500]
        else:
            payload[key] = "[REDACTED]"
    request.app.state.lab_submissions.append(
        {
            "id": uuid4().hex,
            "scenario": scenario,
            "reference": reference,
            "payload": payload,
        }
    )
    return templates.TemplateResponse(
        request,
        "lab/confirmation.html",
        {
            "scenario": scenario,
            "reference": reference,
            "employer": "ARGUS Test Capital",
            "role": "Summer Analyst",
        },
    )
