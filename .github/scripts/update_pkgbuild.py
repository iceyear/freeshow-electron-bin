#!/usr/bin/env python3
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.request import urlopen

PKGBUILD_PATH = "PKGBUILD"
SRCINFO_PATH = ".SRCINFO"
REPO = "ChurchApps/FreeShow"


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def write_text(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def extract_var(content: str, var_name: str) -> str:
    match = re.search(rf"^{re.escape(var_name)}=([^\n]+)$", content, re.MULTILINE)
    if not match:
        raise RuntimeError(f"Missing {var_name} in PKGBUILD")
    return match.group(1).strip().strip("\"")


def extract_sha256sums(content: str) -> list[str]:
    match = re.search(r"^sha256sums=\((?P<body>[\s\S]*?)\)\s*$", content, re.MULTILINE)
    if not match:
        raise RuntimeError("Missing sha256sums in PKGBUILD")
    entries = []
    for line in match.group("body").splitlines():
        line = line.strip().strip("\"")
        if not line:
            continue
        entries.append(line)
    return entries


def set_env(key: str, value: str) -> None:
    env_path = os.environ.get("GITHUB_ENV")
    if env_path:
        with open(env_path, "a", encoding="utf-8") as handle:
            handle.write(f"{key}={value}\n")


def download_file(url: str, dest: str) -> None:
    with urlopen(url) as resp, open(dest, "wb") as handle:
        handle.write(resp.read())


def detect_electron_major(asset_path: str) -> str:
    with tempfile.TemporaryDirectory() as workdir:
        subprocess.run(["ar", "x", asset_path], cwd=workdir, check=True)
        data_archives = sorted(
            name for name in os.listdir(workdir) if name.startswith("data.tar.")
        )
        if not data_archives:
            raise RuntimeError("Unable to locate data.tar.* in deb archive")
        extractor = "bsdtar" if shutil.which("bsdtar") else "tar"
        subprocess.run([extractor, "-xf", data_archives[0]], cwd=workdir, check=True)
        binary_candidates = [
            os.path.join(workdir, "opt", "FreeShow", "FreeShow"),
            os.path.join(workdir, "opt", "FreeShow", "freeshow"),
        ]
        binary_path = next((path for path in binary_candidates if os.path.exists(path)), None)
        if not binary_path:
            raise RuntimeError("Unable to locate FreeShow binary in deb")
        output = subprocess.check_output(["strings", binary_path], text=True)
    match = re.search(r"Chrome/[0-9.]* Electron/([0-9]+)", output)
    if not match:
        raise RuntimeError("Unable to detect Electron major version from deb")
    return match.group(1)


def main() -> int:
    pkgbuild = read_text(PKGBUILD_PATH)
    current_tag = extract_var(pkgbuild, "_tag")
    current_pkgver = extract_var(pkgbuild, "pkgver")
    current_assetver = extract_var(pkgbuild, "_assetver")
    current_electron = extract_var(pkgbuild, "_electronversion")
    current_shas = extract_sha256sums(pkgbuild)
    current_sha = current_shas[1] if len(current_shas) > 1 else ""

    with urlopen(f"https://api.github.com/repos/{REPO}/releases/latest") as resp:
        data = json.load(resp)

    latest_tag = data["tag_name"]
    assets = data.get("assets", [])
    asset = next(
        (item for item in assets if item.get("name", "").endswith("-amd64.deb")),
        None,
    )
    if not asset:
        raise RuntimeError("No amd64 deb asset found in latest release")

    asset_name = asset["name"]
    asset_url = asset["browser_download_url"]
    digest = asset.get("digest", "")
    if not digest.startswith("sha256:"):
        raise RuntimeError("Asset digest missing sha256")
    latest_sha = digest.split("sha256:")[-1]

    latest_assetver = asset_name.replace("FreeShow-", "").split("-amd64")[0]
    latest_pkgver = latest_tag.lstrip("v")
    with tempfile.TemporaryDirectory() as workdir:
        asset_path = os.path.join(workdir, asset_name)
        download_file(asset_url, asset_path)
        latest_electron = detect_electron_major(asset_path)

    if (
        latest_tag == current_tag
        and latest_sha == current_sha
        and latest_assetver == current_assetver
        and latest_electron == current_electron
    ):
        set_env("PKG_UPDATED", "0")
        set_env("NEW_PKGVER", current_pkgver)
        print("No update available.")
        return 0

    pkgbuild = re.sub(r"^pkgver=.*$", f"pkgver={latest_pkgver}", pkgbuild, flags=re.MULTILINE)
    pkgbuild = re.sub(r"^_tag=.*$", f"_tag={latest_tag}", pkgbuild, flags=re.MULTILINE)
    pkgbuild = re.sub(r"^_assetver=.*$", f"_assetver={latest_assetver}", pkgbuild, flags=re.MULTILINE)
    pkgbuild = re.sub(r"^_assetname=.*$", f"_assetname={asset_name}", pkgbuild, flags=re.MULTILINE)
    pkgbuild = re.sub(
        r"^_electronversion=.*$",
        f"_electronversion={latest_electron}",
        pkgbuild,
        flags=re.MULTILINE,
    )
    pkgbuild = re.sub(
        r"^sha256sums=\([\s\S]*?\)\s*$",
        "sha256sums=(\n    'e08b8699c47bfa38365f7194d2dce675b3f36ef36235be993579db8647a8b307'\n    '"
        + latest_sha
        + "'\n)",
        pkgbuild,
        flags=re.MULTILINE,
    )

    write_text(PKGBUILD_PATH, pkgbuild)

    srcinfo = read_text(SRCINFO_PATH)
    srcinfo = re.sub(r"^pkgver = .*$", f"pkgver = {latest_pkgver}", srcinfo, flags=re.MULTILINE)
    srcinfo = re.sub(
        r"^provides = freeshow=.*$",
        f"provides = freeshow={latest_pkgver}",
        srcinfo,
        flags=re.MULTILINE,
    )
    srcinfo = re.sub(
        r"^depends = electron.*$",
        f"depends = electron{latest_electron}",
        srcinfo,
        flags=re.MULTILINE,
    )
    source_line = (
        f"source = freeshow-electron-{latest_pkgver}-amd64.deb::"
        f"https://github.com/{REPO}/releases/download/{latest_tag}/{asset_name}"
    )
    srcinfo = re.sub(r"^source = freeshow-electron-.*$", source_line, srcinfo, flags=re.MULTILINE)
    srcinfo = re.sub(
        r"^sha256sums = [0-9a-f]{64}$",
        "sha256sums = e08b8699c47bfa38365f7194d2dce675b3f36ef36235be993579db8647a8b307",
        srcinfo,
        count=1,
        flags=re.MULTILINE,
    )
    srcinfo = re.sub(
        r"^sha256sums = [0-9a-f]{64}$",
        f"sha256sums = {latest_sha}",
        srcinfo,
        count=1,
        flags=re.MULTILINE,
    )
    write_text(SRCINFO_PATH, srcinfo)

    set_env("PKG_UPDATED", "1")
    set_env("NEW_PKGVER", latest_pkgver)
    print(f"Updated to {latest_pkgver} ({latest_tag}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
