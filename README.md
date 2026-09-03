# hhd-z13

Handheld Daemon (hhd) customization for the ASUS ROG Flow Z13 (GZ302EA,
AMD Strix Halo). Two parts, kept in one repo because the second exists to
be superseded by the first:

- **`hhd_z13/`** — a real hhd plugin package (`hhd.plugins` entry point,
  `pip install -e .`). Adds CPU EPP/boost/Perf-Mode/frequency-floor-and-
  ceiling controls (backed by `z13ctl-plus`) and a refresh-rate (60/180 Hz)
  QAM control, working both on the KDE desktop and inside gamescope. See
  `hhd_z13/plugin.py` and `hhd_z13/display.py` for the design rationale in
  each module's own docstring.
- **`hhd-patches/`** — the local patches this plugin route was written to
  get *off* of: an unversioned edit to hhd's own installed Python (the QAM
  toggle button), plus the supporting Wayland overlay wrapper and KWin
  placement script. Kept because the toggle patch is still a deliberately
  retained local edit (see `hhd-patches/plan.md`), not because more
  patching is the plan going forward.

Install the plugin:

```sh
sudo pip install --break-system-packages --force-reinstall -e .
sudo systemctl restart hhd@<user>
```

Patches: see `hhd-patches/README.md` and `hhd-patches/install.sh`.
