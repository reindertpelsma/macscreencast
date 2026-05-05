# Using mac-vnc-stream in CI (GitHub Actions / similar)

For ephemeral CI workloads — running tests against a live mac-vnc-stream server inside a workflow — **don't use `setup.sh` or `install.sh`**. Those target persistent installs (LaunchAgent + .app bundle + TCC grant ceremony). For CI you want short-lived, no-bundle, runs-once-per-job.

The right pattern: `pip install` Python deps, run `python3 server.py` directly as a child of the runner shell. On most hosted macOS CI runners (GitHub Actions's `macos-latest` definitely; others vary), the runner image pre-grants Screen Recording + Accessibility to `/bin/bash` or the runner agent. A `python3 server.py` child of the shell inherits those grants via TCC's responsible-app chain, so SCK + CGEvent work without any user grant ceremony or LaunchAgent dance.

Working example: [`.github/workflows/tmate-test.yml`](../.github/workflows/tmate-test.yml). Mirror its shape for your own CI:

```yaml
- name: Install Python dependencies
  run: python3 -m pip install --user --break-system-packages \
    'websockets>=13.0' 'numpy>=1.24' 'Pillow>=10.0' \
    'cryptography>=41.0' 'av>=12.0' \
    pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-Quartz \
    pyobjc-framework-AVFoundation pyobjc-framework-ScreenCaptureKit

- name: Enable screensharingd (gives SCK a display backend)
  run: sudo launchctl load -w /System/Library/LaunchDaemons/com.apple.screensharing.plist

- name: Start mac-vnc-stream server
  run: |
    nohup python3 server.py --listen 127.0.0.1 --port 6081 \
      --password citest --no-manage-screensharingd \
      > /tmp/macvncstream.log 2>&1 &
    until nc -z 127.0.0.1 6081; do sleep 1; done

- name: Run your tests against http://127.0.0.1:6081/?token=citest
  run: ...
```

This works because of macOS CI runner image quirks (Microsoft's `actions/runner-images` pre-grants bash for their orchestration). It is **not** how end-users on Scaleway / AWS Mac / personal Macs install — those need the bundle path that `setup.sh` provides, because their bash is unprivileged.
