# Packaging

What it actually takes to put the agent in a package manager, ordered by how
much of it is work you control.

Only the **Linux agent** is packaged here. The firmware is flashed, and the
Windows app goes through [winget](winget/README.md).

---

## AUR — genuinely easy, do this one

The Arch User Repository is not a repository of reviewed packages; it is a
collection of build recipes, and publishing to it is a `git push`. No sponsor,
no review queue, no waiting.

```bash
# One-time: an AUR account with your SSH public key added
#   https://aur.archlinux.org/register

cd packaging/aur
updpkgsums                 # fills in the real sha256 for the release tarball
makepkg --printsrcinfo > .SRCINFO
makepkg -si                # build and install it locally first

git clone ssh://aur@aur.archlinux.org/peekesp.git aur-peekesp
cp PKGBUILD peekesp.install .SRCINFO aur-peekesp/
cd aur-peekesp
git add -A && git commit -m "peekesp 1.1.0" && git push
```

Users then get it with `yay -S peekesp` or any other AUR helper.

**Bump `pkgver`, re-run `updpkgsums` and regenerate `.SRCINFO` for every
release.** A PKGBUILD whose checksum does not match the tarball fails for
everyone, and `.SRCINFO` is what the AUR web interface reads — a stale one
shows the wrong version indefinitely.

This is *not* `extra` or `community`. Those need a Trusted User to adopt the
package, which is a different and much longer conversation.

---

## Debian and Ubuntu — build the .deb, host it yourself

```bash
sh packaging/build-deb.sh
sudo apt install ./dist/peekesp_1.1.0_all.deb
```

Needs only `dpkg-deb`. No debhelper, no build container, no sponsor.

Attach the `.deb` to the GitHub release and people can install it directly.
That covers most of the value with none of the process.

### If you want `apt install peekesp` to work

You need an apt repository, which is a directory of packages plus a signed
index. GitHub Pages can host one, since it is only static files:

```bash
# once
gpg --full-generate-key                        # a signing key for the repo
gpg --armor --export YOUR_KEY_ID > docs/apt/peekesp.gpg

# per release
mkdir -p docs/apt/pool
cp dist/peekesp_*_all.deb docs/apt/pool/
cd docs/apt
dpkg-scanpackages pool /dev/null > Packages
gzip -kf Packages
apt-ftparchive release . > Release
gpg --clearsign -o InRelease Release
gpg -abs -o Release.gpg Release
```

Users add it with:

```bash
curl -fsSL https://shouravx.github.io/PeekESP/apt/peekesp.gpg \
  | sudo tee /etc/apt/keyrings/peekesp.gpg > /dev/null
echo "deb [signed-by=/etc/apt/keyrings/peekesp.gpg] https://shouravx.github.io/PeekESP/apt ./" \
  | sudo tee /etc/apt/sources.list.d/peekesp.list
sudo apt update && sudo apt install peekesp
```

**The signing key is the whole security model of this.** Anyone holding it can
publish a package that installs as root on every machine that trusts the
repository. Keep it off the build machine, out of CI, and out of this
repository.

### Getting into Debian proper

A different order of effort, and worth being clear about before starting:

- Find a Debian Developer willing to **sponsor** the upload. Nothing enters
  without one.
- Full Debian Policy compliance: a real `debian/` directory, `debian/copyright`
  in machine-readable format, a `watch` file, a changelog in Debian's format,
  and a `lintian` run with no errors and few warnings.
- An **ITP** bug filed against `wnpp`, then review, then upload, then the
  NEW queue.
- After that, Ubuntu picks it up from Debian automatically.

Realistically months, mostly waiting. A **Launchpad PPA** is the shortcut for
Ubuntu specifically — same source package, no sponsor, `ppa:shouravx/peekesp` —
but it is Ubuntu-only and still wants a proper `debian/` directory.

---

## Which to do

| | Effort | Reach |
|---|---|---|
| `.deb` on the release page | minutes | anyone who reads the releases |
| **AUR** | an afternoon | every Arch user, via their normal helper |
| Self-hosted apt repo | a day, plus key hygiene forever | anyone you tell |
| Launchpad PPA | a week | Ubuntu |
| Debian proper | months | everyone, eventually |

If it were my afternoon: **AUR, and the `.deb` on the release page.** Between
them that is most of the reachable audience, and neither commits you to
maintaining a signing key or a relationship with a sponsor.

The one-line installer already covers everyone else, and unlike a distro
package it works on DietPi, Raspberry Pi OS and anything else with systemd
without a separate build per distribution.
