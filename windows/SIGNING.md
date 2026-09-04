# Code signing, and why the app warns on other PCs

## What is happening

`PeekESP.exe` and `peek-agent.exe` are unsigned. When someone downloads and
runs one, Windows shows:

> **Windows protected your PC**
> Microsoft Defender SmartScreen prevented an unrecognised app from starting.

It is not blocked. **More info → Run anyway** starts it. But it looks alarming,
most people stop there, and on a managed machine it may genuinely be blocked by
policy.

Two separate things cause it:

1. **No Authenticode signature**, so Windows cannot say who published it.
2. **No reputation.** SmartScreen also scores how many people have run this
   exact file before. A brand-new binary has no history — and this changes with
   every rebuild, because the hash changes.

There is a third, unrelated problem worth knowing about: **PyInstaller binaries
get false-positived by antivirus engines** reasonably often. A single-file
PyInstaller exe unpacks a Python runtime to a temp directory and runs it, which
is behaviourally similar to a dropper. The build passes `--noupx` for exactly
this reason — UPX-packed binaries are far more likely to be flagged — but it
does not eliminate the risk.

## What does NOT fix it

**A self-signed certificate.** This is the trap, and the Microsoft
"Cryptography Tools" article points straight at it: `MakeCert.exe` and
`Cert2SPC.exe` are listed there, and the article's own remarks column says
*"for testing purposes only"* against both. They are also deprecated —
`New-SelfSignedCertificate` is the modern equivalent, and it is equally
test-only.

A self-signed certificate is trusted by exactly one machine: the one whose
certificate store you put the root into. Everywhere else, the chain terminates
in an untrusted root and Windows treats the file as unsigned — or worse, as
signed by someone it cannot verify.

You can watch this happen. Signing a build here with a freshly generated
self-signed certificate succeeded, was timestamped properly by DigiCert, and
then:

```
SignTool Error: A certificate chain processed, but terminated in a root
certificate which is not trusted by the trust provider.
```

That is on the *same machine that created the certificate*. On a stranger's PC
it does nothing at all.

## What does fix it

You need a certificate from a public CA. Since **June 2023** the CA/Browser
Forum requires code-signing private keys to be held on FIPS 140-2 Level 2
hardware, so the cheap file-based `.pfx` certificates that used to exist are
gone. Every option below involves either a hardware token or a cloud HSM.

| Option | Cost | Needs | SmartScreen |
|---|---|---|---|
| **Azure Trusted Signing** | ~$10/month | Azure account; individuals need a verifiable identity history | Reputation builds over time |
| **Certum Open Source** | ~€25–30/year | Open-source project, ID check, hardware token | Builds over time |
| **OV from a commercial CA** | ~$200–400/year | Registered organisation, hardware token | Builds over time |
| **EV from a commercial CA** | ~$400–700/year | Registered organisation, hardware token | **Immediate** |

Only **EV** grants SmartScreen reputation on day one. OV certificates still
warn until enough people have run the signed binaries — but the warning names
you, which is a different conversation with the person downloading it.

For a personal open-source project, the two that make sense are **Azure Trusted
Signing** (cheapest ongoing, no physical token to lose, and Microsoft's own
service) and **Certum's open-source certificate** (cheapest overall, but a
physical USB token and a slower issuance process).

Check current eligibility before paying for either — Azure's individual-developer
terms and Certum's open-source terms have both changed more than once.

## Signing a build once you have a certificate

```bash
python build.py
python sign.py --thumbprint 8A3F...        # certificate in the Windows store
python sign.py --pfx code.pfx              # password from PEEK_SIGN_PASSWORD
python sign.py --azure-dlib <dll> --azure-metadata meta.json
python sign.py --verify                    # report on what is already signed
```

`sign.py` finds `signtool.exe` in the newest installed Windows SDK, or takes
`SIGNTOOL=` from the environment.

**Every mode timestamps, and that is not optional.** A signature without a
timestamp stops verifying the day the certificate expires — one to three years
out. Binaries that people were happily running last week suddenly warn, with
nothing having changed. A timestamp pins the signature to a moment when the
certificate was valid, so it keeps verifying forever. `sign.py` tries DigiCert
then Sectigo, because timestamp servers have outages and a failed timestamp
fails the whole signing operation.

Never pass a `.pfx` password on the command line. It lands in shell history and
in the process list, where any local user can read it. `sign.py` takes it from
`PEEK_SIGN_PASSWORD` instead.

## Testing that sign.py works, without buying anything

```powershell
$c = New-SelfSignedCertificate -Type CodeSigningCert `
     -Subject "CN=PeekESP Test" -CertStoreLocation Cert:\CurrentUser\My
python sign.py --thumbprint $c.Thumbprint
```

It will sign and then fail verification, which is the correct outcome and
proves the tooling works. Delete it afterwards:

```powershell
Remove-Item "Cert:\CurrentUser\My\$($c.Thumbprint)" -Force
```

Do not ship a build signed this way. It is not better than unsigned — it is
unsigned with an extra step that makes it look like you tried to hide something.

## What to tell people in the meantime

An unsigned build is a perfectly normal thing to publish. What helps:

- **Publish the SHA-256** alongside the download, so anyone can verify the file
  is the one you built. `package.py` writes it next to the zip.
- **Say plainly that it is unsigned** and give the Run-anyway steps, rather than
  letting the warning be a surprise. The zip's `README.txt` does this.
- **Build in CI from a tagged commit**, so the binary is traceable to source
  anyone can read.
- **Report false positives.** If Defender flags a release, submit it at
  <https://www.microsoft.com/en-us/wdsi/filesubmission>. They are usually
  cleared within a day or two, and the correction applies to everyone.

**winget does not require code signing.** An unsigned package is publishable —
see [../packaging/winget/README.md](../packaging/winget/README.md).
