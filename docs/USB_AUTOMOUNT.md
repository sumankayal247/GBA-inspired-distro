# Headless USB automount

`install.sh --apply` (also `--apply --repair`) installs `udiskie`, `udisks2`,
`dbus` and `util-linux` using apt without recommended desktop packages. ISO
builds include the same stack. Re-run the installer to update existing systems;
review its dry-run first. Package installation is not rolled back by the file
backup mechanism. Package indexes must already be current (`sudo apt-get update`
if necessary).

`/usr/local/bin/gba-session`, installed from `src/gba_session.sh`, starts udiskie
without notifications, a tray or a file manager. Both the X11 and KMS fallback
paths use it. A per-UID flock prevents concurrent session workers; the worker
restarts udiskie on failure, and session exit stops and reaps it. No GNOME,
session D-Bus or polkit agent is started. The system D-Bus activates udisks2;
on a running systemd host installation also starts/checks both services.

The existing TTY wrapper runs as root. Its automounter uses that same identity,
so no additional polkit grant is necessary. A manually launched non-root session
requires the distribution's normal active local logind session authorization.
Do not add broad passwordless mount rules to bypass authorization failures.
On non-systemd hosts, init must start the system D-Bus. The standalone experimental
`gba_init.sh` PID-1 path does not start a system bus and is not covered by this
installer-session integration; merely installing udiskie is insufficient there.

Udisks normally mounts at `/media/<user>/<label>` or `/run/media/<user>/<label>`;
both are already supported by `gba_usb.py`. Detection/import code is unchanged.
Only media with **new supported ROM content** should trigger the import prompt;
an empty stick or one whose ROMs are already imported should not.

## Appliance verification (requires real hardware)

1. Apply the installer, reboot into the console appliance and check that the
   launcher and game launch still work with no desktop running.
2. Run `pgrep -af udiskie` and `systemctl status dbus udisks2` from another
   console/SSH session. Expect one udiskie for the appliance user.
3. Insert a stick containing a new `.gba`, `.gbc` or `.gb` file. Check
   `lsblk -o NAME,TRAN,FSTYPE,MOUNTPOINTS` and `cat /proc/mounts`: its filesystem
   should be mounted beneath one of the roots above. Check the existing USB
   Import prompt and complete/cancel an import normally.
4. Unplug the stick while idle: the appliance must stay running and its USB
   source should disappear. Replug with new content and verify detection again.
   Also test a stick already connected at boot.
5. Restart the appliance session, then repeat `pgrep -af udiskie`. There must
   still be only one instance, with hotplug still working.
6. If mounting fails, inspect the boot log (`/data/logs/latest.log`) and
   `journalctl -u udisks2 -u dbus`. Check session identity/authorization before
   changing policy. Unsupported, corrupt, or encrypted filesystems may require
   additional filesystem support or interactive unlocking.

Automated tests use stub processes to check flags, lock exclusion, cleanup and
supervisor exit-code preservation. USB watcher tests cover the unchanged import
logic. These do not substitute for boot, physical hotplug and UI verification.
