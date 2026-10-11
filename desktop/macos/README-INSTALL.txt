Clinic Workspace - LOCAL TEST BUILD (not notarized; ad-hoc signed on the build Mac)

1. Drag "Clinic Workspace" onto "Applications".
2. Open Applications, Control-click "Clinic Workspace" > Open, then Open again. macOS asks once because this
   build has no Developer ID; if it still refuses, open System Settings > Privacy & Security and press
   "Open Anyway" for Clinic Workspace. Do not turn off Gatekeeper or System Integrity Protection.
3. Choose "Isolated demo" (synthetic data, inside this app only) or "Connected clinic" (the clinic app on
   this Mac, 127.0.0.1:5000 - the same database as the browser version).
4. Sign in with your clinic account. Demo accounts: assistant / assistant-dev-pass, dentist /
   dentist-dev-pass, admin / admin-dev-pass (synthetic, documented in the UAT guide).

Optional - "Jarvis": drag it to Applications for Jarvis as an app of its own - one window with a Jarvis tab
(its state, Pause/Resume, its page) and a Clinic tab (the clinic app on this Mac: sign in, then every record your
role allows, through the clinic's own checks). It never listens itself; Jarvis's login service does.

Optional - "Clinic Service": drag it to Applications too if the clinic app should start by itself when you
log in (the connected clinic needs it running). Open it once, choose the clinic folder (the one with run.py
and .venv), press Switch on and approve it in System Settings > General > Login Items. macOS may also ask
once whether it may open the clinic folder. It never starts a second clinic app. It is a separate app
because Clinic Workspace is sandboxed and macOS does not let a sandboxed app start the clinic's own code.

The browser version keeps working as before; this app never changes it. Uninstall: switch Clinic
Service off (if used), quit the apps and move them to the Bin; the clinic's data stays where it is. The demo's own data is in
~/Library/Containers/local.clinicdemo.workspace (Settings and Permissions shows the folder).
