# JS8Mail builds

JS8Mail is distributed as a portable executable bundle for desktop users.
The application still talks to JS8Call on the local TCP API; the bundle does
not contain JS8Call, a modem, audio drivers, or a radio interface.

## Windows

The Windows build is a 64-bit PyInstaller bundle named `JS8Mail.exe`. It
contains Python and the JS8Mail runtime, so a Windows operator does not need to
install Python or create a virtual environment. Double-clicking it starts with
the normal defaults (`127.0.0.1:2442` for JS8Call and
`127.0.0.1:8765` for the web UI) and opens the UI in the default browser. The
SQLite database is kept beside the executable.

The bundle is portable rather than an installer. Copy the executable to a
dedicated writable directory and keep a backup of `js8mail.sqlite3` before
upgrading. Windows Defender may display the normal warning for an unsigned
locally-built executable; signed releases can be added later.

The build must run on Windows because PyInstaller is not a cross-compiler. From
a Windows checkout, PowerShell can build it with:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\builds\windows\build.ps1
```

The GitHub Actions workflow performs the same build on `windows-latest` and
publishes `JS8Mail-windows-x64` as a downloadable workflow artifact. It runs
nightly at 02:17 UTC and can also be started manually from the Actions tab;
normal pushes do not start a Windows build.

This repository intentionally does not commit generated `.exe` files. They
are platform-specific, large, and reproducible from the script and workflow.
