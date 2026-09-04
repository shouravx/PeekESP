# Publishing PeekESP to winget

Once this is in, anyone installs it with:

```powershell
winget install shouravx.PeekESP
```

**winget does not require code signing.** Plenty of packages in the repository
are unsigned. It does run automated checks, and one of them is a malware scan —
which is where an unsigned PyInstaller binary can occasionally trip a
heuristic. See [../../windows/SIGNING.md](../../windows/SIGNING.md).

## What ships

winget allows one installer per package, and PeekESP has two executables — the
tray app and the headless agent. So the installer is a **zip of both**, declared
as nested portables. Both land on `PATH`:

| In the zip | Command afterwards |
|---|---|
| `PeekESP.exe` | `PeekESP` — the tray app |
| `peek-agent.exe` | `peek-agent` — headless, for a service or scheduled task |

## Steps

### 1. Build the artefacts

```bash
cd windows
python package.py
```

Quit the tray app first, or PyInstaller cannot replace `PeekESP.exe` — the
build stops and says so rather than shipping yesterday's binary.

This writes `windows/dist/PeekESP-<version>-win-x64.zip`, its `.sha256`, and
the three manifests into `packaging/winget/<version>/`.

The version comes from the `VERSION` file at the repository root. Bump it
before packaging a release.

### 2. Publish the GitHub release

The manifest's `InstallerUrl` points at a release asset that does not exist
yet. Push the tag:

```bash
git tag v1.1.0 && git push origin v1.1.0
```

The [release workflow](../../.github/workflows/release.yml) builds on a clean
Windows runner, attaches the zip and its checksum, and leaves the release as a
**draft**. Check it, then publish it from the Releases page.

Draft on purpose: a published release is a public artefact with a URL people
and tools will hold on to, and un-publishing one after the fact is not
something that fully works.

Then confirm the URL actually serves the file, because a manifest pointing at a
404 is the single most common reason a submission bounces:

```powershell
curl -sIL https://github.com/shouravx/PeekESP/releases/download/v1.1.0/PeekESP-1.1.0-win-x64.zip | findstr /i "HTTP content-length"
```

### 3. Test the manifests locally

```powershell
winget validate --manifest packaging\winget\1.1.0
winget install --manifest packaging\winget\1.1.0
```

`validate` only checks the schema. **`install` is the one that matters** — it
actually installs from the manifest, and it is the only way to find out that a
`RelativeFilePath` inside the zip is wrong or that the hash does not match. A
schema-valid manifest that installs nothing is a normal outcome of skipping it.

Then check the commands exist, and remove it again:

```powershell
PeekESP
peek-agent --once
winget uninstall shouravx.PeekESP
```

### 4. Submit

Fork <https://github.com/microsoft/winget-pkgs>, then:

```
manifests/s/shouravx/PeekESP/1.1.0/shouravx.PeekESP.yaml
manifests/s/shouravx/PeekESP/1.1.0/shouravx.PeekESP.installer.yaml
manifests/s/shouravx/PeekESP/1.1.0/shouravx.PeekESP.locale.en-US.yaml
```

The path is `manifests/<first letter, lowercase>/<Publisher>/<Package>/<Version>/`
and it is case-sensitive.

Open a pull request. Automated validation runs first — installing the package
on a clean VM, checking the URL, the hash, the schema and a malware scan — then
a human reviews it. Expect a few days.

`wingetcreate` can do the fork-and-PR part for you:

```powershell
winget install Microsoft.WingetCreate
wingetcreate submit --token <github-pat> packaging\winget\1.1.0
```

### 5. Later versions

Bump `VERSION`, run `package.py`, tag, publish, and submit the new folder. Or
let `wingetcreate` build the update from the previous manifest:

```powershell
wingetcreate update shouravx.PeekESP --version 1.2.0 --urls <new zip url> --submit --token <pat>
```

## If it bounces

| What the bot says | What it usually means |
|---|---|
| Installer URL not reachable | The release is still a draft, or the tag was never pushed |
| Hash mismatch | The zip was rebuilt after the manifest was written. Re-run `package.py`, which does both together |
| Validation-Domain / SmartScreen | The URL has no reputation yet. Usually clears; a maintainer can override |
| Malware / defender flag | A PyInstaller false positive. Report it at <https://www.microsoft.com/en-us/wdsi/filesubmission> and link the case in the PR |
| Manifest schema error | `winget validate` before pushing catches all of these |
