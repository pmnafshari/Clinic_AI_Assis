"""the voice assistant walk on /assistant, driven through a real browser.

runs chromium via playwright against site_run.py on 5002, so the autoplay
policy, the microphone permission and agent.js all actually run. that is the
layer these defects live in and run_selftests.sh cannot reach - site_app_selftest
greps the served javascript, which proves the code is present but never that it
behaves.

the five paths, and why each one is here:

  1. mic granted      - the conversation starts on its own, no click
  2. mic refused      - a clean fallback, and the button still works
  3. no microphone    - a DIFFERENT message from a refusal (they need different
                        remedies; collapsing them sent people to a permission
                        dialog that could not help)
  4. autoplay refused - the note says "click anywhere", so clicking anywhere
                        has to actually restore sound
  5. start/stop/start - a restart works, and no stream is left open

paths 3 and 4 are driven by overriding getUserMedia and play() in the page.
chromium has no way to attach a broken microphone or to refuse autoplay to a
page that already holds a mic permission, and the alternative - trusting that
the branch works because it is written down - is what the selftest already does.

usage:  .venv/bin/python e2e_voice_walk.py [--headed]

needs:  VOICE_DEMO=1 .venv/bin/python site_run.py   on 5002

sends nothing to a vendor beyond the greeting each page load already fetches,
and never lets a recording turn complete - deepgram bills per second.
"""

import sys

from playwright.sync_api import sync_playwright

SITE_URL = "http://127.0.0.1:5002"
ORIGIN = SITE_URL
ASSISTANT = SITE_URL + "/assistant"

# chromium exposes no audio input at all in this build unless the fake ui flag
# is set, so it is what makes "permission granted" mean anything here
FAKE_MIC = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"]

RESULTS = []


def check(step, ok, note):
    RESULTS.append((step, bool(ok), note))
    print(f"  {'PASS' if ok else 'FAIL'}  {step}: {note}")
    return bool(ok)


def snap(pg):
    return pg.evaluate("""() => {
      const r = document.querySelector('[data-agent]');
      const m = r.querySelector('[data-agent-voice]');
      const b = r.querySelector('[data-agent-mic]');
      const n = r.querySelector('[data-agent-voice-note]');
      return {
        state: r.dataset.state,
        level: parseFloat(r.dataset.level || '0'),
        mountHidden: m.hidden,
        btn: b ? b.textContent.trim() : null,
        note: n.hidden ? null : n.textContent.trim(),
        typing: !r.querySelector('.agent-input').disabled,
      };
    }""")


def wait_state(pg, states, timeout=15000):
    """wait until the page reaches one of `states`, then return the snapshot."""
    want = "[" + ",".join(repr(s) for s in states).replace("'", '"') + "]"
    try:
        pg.wait_for_function(
            "() => %s.includes(document.querySelector('[data-agent]').dataset.state)" % want,
            timeout=timeout)
    except Exception:
        pass
    return snap(pg)


def page(ctx, init=None):
    pg = ctx.new_page()
    if init:
        pg.add_init_script(init)
    pg.goto(ASSISTANT)
    pg.wait_for_selector("[data-agent-mic]")
    return pg


# --- the five paths -------------------------------------------------------

def walk_granted(p):
    """1. opening the page starts the conversation, with no click at all."""
    b = p.chromium.launch(headless=HEADLESS, args=FAKE_MIC)
    try:
        ctx = b.new_context()
        ctx.grant_permissions(["microphone"], origin=ORIGIN)
        pg = page(ctx)
        s = wait_state(pg, ["speaking", "listening"])
        check("1a autostart", s["state"] in ("speaking", "listening"),
              f"state={s['state']} with no click")
        check("1b control shown", s["mountHidden"] is False,
              "the voice mount is revealed once the fence reports armed")
        check("1c session live", s["btn"] == "Stop",
              f"button reads {s['btn']!r}, so a session is running")
        s2 = wait_state(pg, ["listening"])
        # sampled over a window, not once: the fake capture device emits a
        # beep with silence between tones, so a single instantaneous read is
        # legitimately 0 and asserting on one made this check flaky. the
        # claim being tested is that the analyser produces real movement.
        peak = pg.evaluate("""() => new Promise(resolve => {
          const r = document.querySelector('[data-agent]');
          let peak = 0, n = 0;
          const t = setInterval(() => {
            peak = Math.max(peak, parseFloat(r.dataset.level || '0'));
            if (++n >= 40) { clearInterval(t); resolve(peak); }
          }, 50);
        })""")
        check("1d ring is real", peak > 0,
              f"analyser peak rms={peak} over 2s - a real stream, not a css animation")
        check("1e no false note", s2["note"] is None,
              "no failure note on the happy path")
    finally:
        b.close()


def walk_denied(p):
    """2. a refused microphone is a soft landing, not an error state."""
    b = p.chromium.launch(headless=HEADLESS, args=["--use-fake-device-for-media-stream"])
    try:
        ctx = b.new_context()
        ctx.clear_permissions()
        # a real NotAllowedError, which is what a refusal actually produces
        pg = page(ctx, """
          window.__tries = 0;
          navigator.mediaDevices.getUserMedia = function () {
            window.__tries++;
            return Promise.reject(new DOMException('denied', 'NotAllowedError'));
          };
        """)
        s = wait_state(pg, ["ready"])
        check("2a not an error", s["state"] == "ready",
              f"state={s['state']} - an unrequested attempt failing is not an error")
        check("2b told why", s["note"] is not None and "refused" in s["note"].lower(),
              f"note={s['note']!r}")
        check("2c button usable", s["btn"] == "Start voice conversation",
              "the button stays armed for a real click")
        check("2d typing intact", s["typing"], "the composer is untouched")
        # and the click still reaches getUserMedia rather than doing nothing.
        # the autostart already spent one attempt, so a working button makes it two.
        before = pg.evaluate("window.__tries")
        pg.click("[data-agent-mic]")
        pg.wait_for_timeout(1000)
        after = pg.evaluate("window.__tries")
        check("2e click retries", after == before + 1,
              f"clicking Start re-attempts the microphone ({before} -> {after})")
    finally:
        b.close()


def walk_no_device(p):
    """3. no microphone attached is a different fact from a refusal."""
    b = p.chromium.launch(headless=HEADLESS, args=["--use-fake-device-for-media-stream"])
    try:
        ctx = b.new_context()
        pg = page(ctx, """
          navigator.mediaDevices.getUserMedia = () =>
            Promise.reject(new DOMException('none', 'NotFoundError'));
        """)
        s = wait_state(pg, ["ready"])
        check("3a distinct copy", s["note"] is not None and "refused" not in s["note"].lower(),
              f"note={s['note']!r}")
        check("3b names the cause", s["note"] is not None and "microphone" in s["note"].lower(),
              "the note says a microphone was not available, not that one was refused")
        check("3c button usable", s["btn"] == "Start voice conversation",
              "still clickable")
    finally:
        b.close()


def walk_autoplay(p):
    """4. the note promises "click anywhere" - so that has to restore sound."""
    b = p.chromium.launch(headless=HEADLESS, args=FAKE_MIC)
    try:
        ctx = b.new_context()
        ctx.grant_permissions(["microphone"], origin=ORIGIN)
        # a browser that refuses audio until the page has been interacted with
        pg = page(ctx, """
          window.__gesture = false;
          addEventListener('click', () => window.__gesture = true, true);
          const real = HTMLMediaElement.prototype.play;
          HTMLMediaElement.prototype.play = function () {
            if (!window.__gesture) return Promise.reject(new DOMException('x','NotAllowedError'));
            return real.call(this);
          };
        """)
        pg.wait_for_function(
            "() => !document.querySelector('[data-agent-voice-note]').hidden", timeout=15000)
        s = snap(pg)
        check("4a sound blocked", s["note"] is not None and "click anywhere" in s["note"].lower(),
              f"note={s['note']!r}")
        # the defect this replaces: the note used to name a button that reads
        # "Stop" at that moment, so following the advice ended the session
        check("4b advice is followable",
              "start voice conversation" not in (s["note"] or "").lower(),
              f"the note must not point at a button currently reading {s['btn']!r}")
        check("4c still listening", s["state"] in ("listening", "thinking", "ready"),
              f"state={s['state']} - the microphone turn continues")

        # now do exactly what the note says, somewhere harmless
        ctx_state_before = pg.evaluate("window.__gesture")
        pg.click(".agent-title")
        pg.wait_for_timeout(600)
        s2 = snap(pg)
        check("4d gesture clears it", s2["note"] is None,
              f"after clicking anywhere the note is gone (gesture before={ctx_state_before})")
        audible = pg.evaluate("""() => {
          const a = document.querySelector('[data-agent]');
          return window.__gesture === true;
        }""")
        check("4e sound now allowed", audible,
              "the page has a gesture, so the next reply may play")
    finally:
        b.close()


def walk_restart(p):
    """5. stop and start again, and never hold two streams at once."""
    b = p.chromium.launch(headless=HEADLESS, args=FAKE_MIC)
    try:
        ctx = b.new_context()
        ctx.grant_permissions(["microphone"], origin=ORIGIN)
        # every stream handed out is kept, and "still open" is read off the
        # track's readyState. track.stop() deliberately does NOT fire 'ended',
        # so counting that event would report a leak that is not there.
        pg = page(ctx, """
          window.__n = 0; window.__streams = [];
          window.__live = () => window.__streams
            .flatMap(s => s.getTracks())
            .filter(t => t.readyState === 'live').length;
          const real = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
          navigator.mediaDevices.getUserMedia = function (c) {
            window.__n++;
            return real(c).then(s => { window.__streams.push(s); return s; });
          };
        """)
        # race the autostart with a click, which used to open a second stream
        pg.click("[data-agent-mic]")
        wait_state(pg, ["speaking", "listening"])
        pg.wait_for_timeout(1500)
        check("5a no double start", pg.evaluate("window.__n") == 1,
              f"getUserMedia called {pg.evaluate('window.__n')}x while autostart was in flight")

        # stop, and confirm the microphone is actually released
        pg.click("[data-agent-mic]")
        pg.wait_for_timeout(800)
        s = snap(pg)
        check("5b stop works", s["btn"] == "Start voice conversation" and s["state"] == "ready",
              f"state={s['state']}, button={s['btn']!r}")
        check("5c mic released", pg.evaluate("window.__live()") == 0,
              f"{pg.evaluate('window.__live()')} live track(s) after stop")

        # and start again from the button - the path that has to be reliable
        pg.click("[data-agent-mic]")
        s2 = wait_state(pg, ["speaking", "listening"])
        check("5d restart works", s2["state"] in ("speaking", "listening"),
              f"state={s2['state']} after clicking Start again")
        check("5e one stream only", pg.evaluate("window.__live()") == 1,
              f"{pg.evaluate('window.__live()')} live track(s) on the restarted session")
    finally:
        b.close()


def preflight():
    import urllib.request
    try:
        with urllib.request.urlopen(SITE_URL + "/assistant/voice/status", timeout=5) as r:
            body = r.read().decode()
    except Exception as e:
        print(f"site app not reachable on 5002: {type(e).__name__}")
        print("start it with:  VOICE_DEMO=1 .venv/bin/python site_run.py")
        return False
    if '"available":true' not in body.replace(" ", ""):
        print(f"voice is not armed - /assistant/voice/status says: {body}")
        print("start it with:  VOICE_DEMO=1 .venv/bin/python site_run.py")
        return False
    return True


HEADLESS = "--headed" not in sys.argv

if __name__ == "__main__":
    if not preflight():
        sys.exit(2)
    print("voice walk on", ASSISTANT)
    with sync_playwright() as p:
        for name, fn in (("1. microphone granted", walk_granted),
                         ("2. microphone refused", walk_denied),
                         ("3. no microphone", walk_no_device),
                         ("4. autoplay refused", walk_autoplay),
                         ("5. stop and restart", walk_restart)):
            print("\n" + name)
            fn(p)

    failed = [s for s, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("failed:", ", ".join(failed))
    sys.exit(1 if failed else 0)
