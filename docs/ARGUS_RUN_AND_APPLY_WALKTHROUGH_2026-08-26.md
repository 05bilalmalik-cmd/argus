# ARGUS: Run and Apply Walkthrough

Date: 2026-08-26  
Installed executable: `%USERPROFILE%\Downloads\argus-0.1.0\ARGUS-0.1.0\dist\ARGUS.exe`  
Normal desktop shortcut: `%USERPROFILE%\Desktop\ARGUS.lnk`

## Important current state

The normal desktop shortcut and packaged `ARGUS.exe` deliberately start ARGUS in `OFF` mode. Use them to inspect records and complete your profile and documents. The packaged launcher overwrites inherited automation settings back to `OFF`; it cannot start `REVIEW_ONLY` or submit a real application. Operator review/live modes must use the supported source launcher under `scripts/start.ps1`. Live mode is intentionally not a dashboard switch.

ARGUS uses the existing private data under `%LOCALAPPDATA%\ARGUS` in every sequence below.

## 1. Prepare one application in safe mode

1. Double-click `ARGUS.lnk` on the desktop. If the dashboard does not open automatically, visit `http://127.0.0.1:8787`.
2. Confirm the status at the top says `OFF`.
3. Open **Profile** and verify all factual identity, contact, education, work-authorisation, and location information.
4. Open **Documents** and confirm the exact approved CV and any cover letter you want to use.
5. Open **Answer Bank** and review the approved reusable answers. Do not pre-answer legal, demographic, disability, conflict, consent, sponsorship, or other employer-specific declarations unless they are verified facts and the question is unambiguous.
6. Open **Applications** and choose one older application for the first test.
7. If the row says **Resolve target**, do not click it while ARGUS is still `OFF`: the external-browser egress guard requires the stored source hostname to be allowlisted first. Note the hostname shown by the stored source URL. For example, from `https://jobs.example.com/role/123`, record `jobs.example.com`.

It is normal for the repaired database to have no items marked `READY` until their exact targets are re-verified. This prevents the old wrong-page behaviour.

## 2. Run a visible review without submission

Close the running ARGUS process; closing only the browser tab does not stop it. Open Windows PowerShell and paste the following, replacing `jobs.example.com` with the one exact hostname from the verified application:

```powershell
$env:ARGUS_DATA_DIR = "$env:LOCALAPPDATA\ARGUS"
$env:ARGUS_AUTOMATION_MODE = "REVIEW_ONLY"
$env:ARGUS_ENABLE_LIVE_SUBMIT = "false"
$env:ARGUS_ENABLE_TRACKR_LIVE = "false"
$env:ARGUS_SWEEP_INTERVAL_HOURS = "0"
$env:ARGUS_BROWSER_HEADLESS = "false"
$env:ARGUS_LIVE_DOMAIN_ALLOWLIST = "jobs.example.com"
Set-Location "$env:USERPROFILE\Downloads\argus-0.1.0\ARGUS-0.1.0"
.\scripts\start.ps1
```

Leave that PowerShell window open. In ARGUS:

1. Confirm the header says `REVIEW ONLY`.
2. Open **Settings**. Confirm live submission is `DISABLED`, the browser is `HEADED`, and the only allowed domain is the exact hostname you entered.
3. Return to **Applications** and open the same record. If it says **Resolve target**, click that now. ARGUS can inspect the stored external source because its exact hostname is allowlisted. If the role is expired or the page is not the exact application, cancel it and choose another. Never treat a company home page, search page, or different requisition as the target.
4. After ARGUS verifies the exact application destination, click **Apply in Navigator** when that action becomes available.
5. Let the visible browser navigate and fill only approved values.
6. If ARGUS reports **Human action required**, complete the CAPTCHA, MFA, login, legal declaration, or sensitive question yourself in that same visible browser. Return to ARGUS and click **Continue**. Continue only rescans the page; it never submits.
7. Check every page and every filled answer. A review run must stop before a real submission.

If ARGUS displays **Source-resolution application/session binding was inconsistent; retry was refused** while its header says `OFF`, no browser session was created and nothing was submitted. Close ARGUS and repeat this section in `REVIEW_ONLY` with the stored source hostname allowlisted exactly.

If the browser opens a wrong role, wrong employer, generic careers page, or unexpected domain, cancel the Navigator session. Do not broaden the allowlist to make it proceed.

## 3. Arm one exact employer target

After the review is correct, close ARGUS and run the same PowerShell sequence again with these two values changed:

```powershell
$env:ARGUS_AUTOMATION_MODE = "ARMED"
$env:ARGUS_ENABLE_LIVE_SUBMIT = "true"
```

Keep the exact reviewed-host allowlist, headed browser, Trackr disabled, and sweep interval `0`. Start through the source launcher again if it is no longer running:

```powershell
Set-Location "$env:USERPROFILE\Downloads\argus-0.1.0\ARGUS-0.1.0"
.\scripts\start.ps1
```

Confirm the ARGUS header says `ARMED` and **Settings** shows `LIVE ENABLED`, `HEADED`, and only the expected hostname.

## 4. Apply and submit

1. Open the reviewed application and click **Apply in Navigator**.
2. Complete any human-only step in the same visible employer browser, then click **Continue** in ARGUS.
3. When available, click **Review exact manifest**.
4. Compare the displayed employer, role, requisition, ATS provider, destination hostname, application ID, documents, and evidence with the employer page.
5. If every item is exact, click **Confirm this exact manifest**. This confirms identity only; it does not submit.
6. Review the identity once more, then click **Submit this exact application once**.
7. Wait for ARGUS to correlate the employer receipt/reference. Success is shown as **Submission confirmed by the exact receipt**.

If ARGUS reports `UNKNOWN` or `SUBMISSION UNKNOWN`, do not retry. Check the employer portal and confirmation email first; a second click could create a duplicate.

## 5. Return to safe mode

After the one application, close the ARMED ARGUS process and launch `ARGUS.lnk` from the desktop again. Confirm the header says `OFF`. The desktop shortcut clears the live allowlist and keeps Trackr and scheduled sweeps disabled.

## Troubleshooting boundaries

- **Wrong or expired page:** cancel, use **Resolve target**, and select another application if the requisition is closed.
- **Redirect blocked:** verify the redirect is genuinely part of the same ATS journey before adding its exact hostname. Never add `*.com`, `*`, a URL, a path, or a broad company wildcard.
- **CAPTCHA/MFA/legal or demographic question:** complete it yourself in the visible browser, then use **Continue**.
- **Unsupported or changed portal:** leave it blocked/manual. Do not lower the risk score or broaden the allowlist.
- **No receipt after submit:** treat the outcome as unknown, check the employer account and email, and do not retry automatically.
