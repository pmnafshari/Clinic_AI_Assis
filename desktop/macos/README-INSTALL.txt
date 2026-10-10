Clinic Workspace - LOCAL TEST BUILD (not notarized; ad-hoc signed on the build Mac)

1. Drag "Clinic Workspace" onto "Applications".
2. Open Applications, Control-click "Clinic Workspace" > Open, then Open again. macOS asks once because this
   build has no Developer ID; if it still refuses, open System Settings > Privacy & Security and press
   "Open Anyway" for Clinic Workspace. Do not turn off Gatekeeper or System Integrity Protection.
3. Choose "Isolated demo" (synthetic data, inside this app only) or "Connected clinic" (the clinic app on
   this Mac, 127.0.0.1:5000 - the same database as the browser version).
4. Sign in with your clinic account. Demo accounts: assistant / assistant-dev-pass, dentist /
   dentist-dev-pass, admin / admin-dev-pass (synthetic, documented in the UAT guide).

The browser version keeps working as before; this app never changes it. Uninstall: quit the app and
move it to the Bin; the clinic's data stays where it is. The demo's own data is in
~/Library/Containers/local.clinicdemo.workspace (Settings and Permissions shows the folder).
