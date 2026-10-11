# Clinic Workspace for macOS (P28) - install, use, return to the web version

Clinic Workspace is a macOS app that shows the clinic's own staff pages and Jarvis's page in native windows. It
does not replace the web version: every read, write, role check and audit stays in the clinic app (`run.py`), and
the browser keeps working exactly as before.

**This is a local test build:** ad-hoc signed on the build Mac, not notarized, arm64 only, macOS 14 or newer.
Distribution to other Macs needs a Developer ID and notarization (not available - see "Limits").

## Install and first launch
1. Open `ClinicWorkspace-<version>-<build>-local.dmg` and drag **Clinic Workspace** onto **Applications**.
   Check the image first if you like: `shasum -a 256 ClinicWorkspace-....dmg` must match the `.sha256` file.
2. Open **Applications**, Control-click **Clinic Workspace** > **Open**, then **Open** again (macOS asks once because
   the build has no Developer ID). If macOS still refuses: System Settings > Privacy & Security > **Open Anyway**.
   Never turn Gatekeeper or System Integrity Protection off.
3. Choose an environment:
   - **Isolated demo** - synthetic patients, its own database and files inside this app only (its sandbox
     container). Nothing done there reaches the clinic. Accounts: `assistant` / `assistant-dev-pass`,
     `dentist` / `dentist-dev-pass`, `admin` / `admin-dev-pass` (synthetic).
   - **Connected clinic** - the clinic app running on this Mac at `127.0.0.1:5000`, the same database as the browser
     version. You sign in as yourself. **Writes from the app are off** until you switch them on in **Settings and
     Permissions**; reading works straight away.
4. The coloured bar at the top always says which environment is open (**ISOLATED DEMO** / **CONNECTED CLINIC**).
   Switch with the **Environment** menu (unsaved form changes are asked about first).

The connected clinic needs the clinic app running. Start it as before, or let **Clinic Service** start it at login:
drag **Clinic Service** from the disk image to Applications too, open it, **Choose clinic folder...** (the one with
`run.py` and `.venv`), **Switch on**, and approve it in System Settings > General > Login Items (macOS may also ask
once whether it may open that folder). If the clinic app is already running it does nothing - there is never a second
one. **Switch off** stops the clinic app it started. Its log: `~/Library/Logs/clinic-service.log`.

Why a second app: Clinic Workspace is sandboxed, and macOS refuses a sandboxed app an unsandboxed login item (tested:
`SMAppService` "Operation not permitted"); a sandboxed one could not run the clinic's own code with its own folder and
workers. Clinic Service is small, unsandboxed (hardened runtime, no entitlements) and does only this.

## Everyday use
- Close the window: the app stays in the Dock (the demo backend keeps running); click the Dock icon to reopen.
- **Quit** (Cmd-Q) stops only what the app started (its demo backend). It never stops the clinic app, Jarvis or
  Ollama. Quitting signs you out, as closing a browser does.
- Files: uploads use the macOS file picker - only the file you pick is read. Downloads ask where to save. Clinic
  images (guide pages) open in a preview window under the same sign-in.
- Links to other sites, the patient portal or the public site open in your default browser.
- View > Zoom In / Zoom Out / Actual Size (Cmd + / Cmd - / Cmd 0); the window can be resized freely.
- **Jarvis** (Cmd-Shift-J): Jarvis's own page plus what it is doing - listening, clinic-guide answers (switched off:
  J02 is not accepted), and voice actions (none - not built). **Pause listening** asks Jarvis to close the
  microphone until you resume. Jarvis runs on its own (its login service); the app never records.

## Jarvis as its own app (Jarvis.app)
Drag **Jarvis** from the disk image to Applications too if you want Jarvis with its own icon. Its window has two tabs
(Cmd-1, Cmd-2):
- **Jarvis** - what Jarvis is doing (listening, clinic-guide answers, voice actions), **Pause/Resume listening**, and
  Jarvis's own page. A clinic page it cites opens in the Clinic tab.
- **Clinic** - the clinic app on this Mac (`127.0.0.1:5000`, the same database as the browser). Sign in as yourself;
  you then see and change every record your role allows - a dentist all clinical records - through the clinic's own
  role checks, CSRF protection and audit, exactly as in the browser. Writes are on by default here (Settings and
  Permissions switches them off). There is no direct database access: the app talks only to the clinic app.
Jarvis.app never listens itself (no microphone access) - Jarvis's login service does, and keeps running when you close
or quit Jarvis.app. Voice still reads and writes no patient record (J03 not started; answers off while J02 is not
accepted). Its sign-in is its own: signing in here does not sign you in to Clinic Workspace or the browser.

## Settings and Permissions
Shows, for the open environment: who is signed in and what the clinic lets that role do; writes from this app (on/
off); the microphone (this app never records - Jarvis's own service holds that permission); files (one pick at a
time); notifications (asked only when you press the button); open at login; whether the clinic app is running
and whether Clinic Service is installed (it opens Clinic Service; it cannot switch it); the demo
backend's state and data folder. A state macOS controls is never changed silently - the app asks macOS or opens
System Settings.

## Return to the web version
Nothing to do: the web version was never changed or stopped. Open `http://127.0.0.1:5000` in your browser as before
(start the clinic app as before if it is not running). Writes made in the connected clinic from the app are in the
clinic's own database and show in the browser. To go back to the code exactly as it was before this app, check out
the tag `baseline-pre-p28-2026-10-10` - that is an application rollback; it never restores a database.

## Update and rollback
Install a newer `.dmg` the same way (replace the app in Applications). The demo's data and your settings stay (they
are in the app's container, not in the app). To roll back, install the older `.dmg` the same way; no data changes
format between these builds.

## Uninstall
1. In Settings and Permissions switch off "Open at login"; in Clinic Service press **Switch off** (if used).
2. Quit the apps and move **Clinic Workspace** (and **Clinic Service**, **Jarvis**) from Applications to the Bin.
The clinic's data, the web version and Jarvis are untouched. The demo's own synthetic data stays in
`~/Library/Containers/local.clinicdemo.workspace` until you delete that folder yourself (it holds only the demo).

## Backups
The clinic's backups are unchanged (`backup.py`, see the operations guide). The demo needs none - it is synthetic;
deleting its folder starts it again from the bundled snapshot.

## Troubleshooting
- "The clinic app is not running": start it as before, or switch on Clinic Service; its log is
  `~/Library/Logs/clinic-service.log`.
- "The isolated demo could not start": Settings and Permissions shows the demo backend's state and its log folder;
  press Restart. "Another copy of Clinic Workspace is running the demo": quit the other copy.
- Sign-in page appears again: your session ended (idle, sign-out, or the app was quit) - sign in again.
- A write is refused with "writes from this app are switched off": switch them on in Settings and Permissions.

## Limits (honest)
- Not notarized, ad-hoc signed: macOS asks before the first open; distribution to other Macs needs a Developer ID.
- The isolated demo cannot read text out of uploaded files or ingest new guides: the clinic's file workers use
  `sandbox-exec` and Homebrew's `tesseract`, which cannot run inside the app's sandbox. Files are stored and shown;
  the connected clinic does the reading as before.
- The demo's disk-encryption guard is switched off for the demo only: inside the app's sandbox macOS reports
  FileVault as off even when it is on. The demo holds synthetic data; the clinic app keeps the guard.
- Patient portal and public site are not hosted in the app (separate audiences and sessions) - use the browser.
- arm64 only; tested on one Mac (macOS 27).

## Build from source
`desktop/macos/build.sh` on a clean checkout (needs Xcode command line tools, `uv`, PyPI access for the pinned
packages in `desktop/macos/requirements-bundle.lock`, and the project's `.venv` for the synthetic snapshot).
Output: `../macos-builds/ClinicWorkspace-<version>-<build>-local.dmg` with `.sha256` and `.build.json`.
Bundled licences: `Contents/Resources/BUNDLED_PACKAGES.md` and `THIRD_PARTY_NOTICES.md` in the app.
