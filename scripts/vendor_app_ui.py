"""Embed the standard web interface from nodestro-app-ui, or check the embedded copy (NAL-C2).

The interface is developed in https://github.com/peterkarlhuber/nodestro-app-ui. This repository serves a copy of one
of its commits from ``src/nodestro_api/webui/``; ``VENDORED.md`` there names the commit, the version and the SHA-256
of its ``manifest.json``, which lists every file with its own SHA-256.

What the checks prove: the copy is complete and unchanged against the manifest that came with it (no missing, extra
or edited file). They do not authenticate the source: the manifest comes from the same commit as the files. The trust
rests on the pinned full commit SHA, TLS to GitHub, and the review of the ``webui/`` diff in this repository's pull
requests (anyone who may push here could also regenerate manifest and record together).

    python scripts/vendor_app_ui.py --commit <sha>              # download that commit, check it, replace webui/
    python scripts/vendor_app_ui.py --commit <sha> --source DIR # the same from a local checkout at that commit
    python scripts/vendor_app_ui.py --check                     # offline: the embedded files match the manifest

There are no releases yet (no tags); once there are, the release archive can replace the commit download. Rolling back
means embedding the previous commit. ``tests/test_webui_vendored.py`` runs ``--check`` in CI.
"""

from __future__ import annotations

import argparse
import io
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from nodestro_api import ui_package  # noqa: E402 - after the path of this checkout

WEBUI = ROOT / "src" / "nodestro_api" / "webui"
REPOSITORY = "peterkarlhuber/nodestro-app-ui"
MANIFEST = "manifest.json"
RECORD = "VENDORED.md"
#: The contract versions this backend serves (nodestro_api.app_contract.CONTRACT_VERSIONS, which a test compares; not
#: imported, so the script needs no runtime); the interface's range must include one.
SERVED_CONTRACTS = {1}
_SHA = re.compile(r"^[0-9a-f]{40}$")
_LIMIT = ui_package.LIMIT


class VendorError(Exception):
    pass


def _sha256(data: bytes) -> str:
    return ui_package.sha256(data)


def _safe(path: str) -> PurePosixPath:
    try:
        return ui_package.safe_path(path)
    except ui_package.UiPackageError:
        raise VendorError(f"unsafe path in the manifest: {path!r}") from None


def check_tree(folder: Path) -> dict[str, object]:
    """``folder`` holds exactly the files its ``manifest.json`` lists (plus ``VENDORED.md``), each with its hash."""
    try:
        return ui_package.check_tree(folder, SERVED_CONTRACTS, extra=(RECORD,))
    except ui_package.UiPackageError as exc:
        message = str(exc)
        if message.startswith("changed:"):
            message += " (edit nodestro-app-ui, not the embedded copy)"
        raise VendorError(message) from None


def record_text(commit: str, manifest: dict[str, object], manifest_sha: str) -> str:
    contract = manifest["contract"]
    assert isinstance(contract, dict)
    return (
        "# Embedded web interface\n\n"
        "These files are a copy of the standard web interface from "
        f"[nodestro-app-ui](https://github.com/{REPOSITORY}). Do not edit them here: change nodestro-app-ui, then run\n"
        "`python scripts/vendor_app_ui.py --commit <sha>` (CI checks the copy with `--check`).\n\n"
        f"- Commit: `{commit}`\n"
        f"- Version: `{manifest['version']}`\n"
        f"- Contract: `{contract['min']}` to `{contract['max']}`\n"
        f"- Manifest SHA-256: `{manifest_sha}`\n"
    )


def read_record(folder: Path) -> dict[str, str]:
    try:
        text = (folder / RECORD).read_text("utf-8")
    except OSError:
        raise VendorError(f"no {RECORD} in {folder}") from None
    found = dict(re.findall(r"^- ([A-Za-z0-9 -]+): `([^`]+)`", text, re.MULTILINE))
    for key in ("Commit", "Version", "Manifest SHA-256"):
        if key not in found:
            raise VendorError(f"{RECORD} lacks {key}")
    return found


def check(folder: Path = WEBUI) -> dict[str, str]:
    """The embedded copy matches its manifest, and the manifest is the one ``VENDORED.md`` names."""
    record = read_record(folder)
    manifest = check_tree(folder)
    if _sha256((folder / MANIFEST).read_bytes()) != record["Manifest SHA-256"]:
        raise VendorError(f"{MANIFEST} is not the one {RECORD} names")
    if manifest.get("version") != record["Version"]:
        raise VendorError(f"{RECORD} names version {record['Version']}, the manifest {manifest.get('version')}")
    if not _SHA.match(record["Commit"]):
        raise VendorError(f"{RECORD}: the commit is not a full SHA")
    return record


def _download(commit: str, into: Path) -> Path:
    url = f"https://codeload.github.com/{REPOSITORY}/tar.gz/{commit}"
    with urllib.request.urlopen(url, timeout=60) as answer:
        data = answer.read(_LIMIT + 1)
    if len(data) > _LIMIT:
        raise VendorError("the download is larger than 20 MB")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = [member for member in archive.getmembers() if member.isfile() and "/src/" in f"/{member.name}"]
        # The download is limited above; the unpacked files too (a small archive may expand a lot).
        if sum(member.size for member in members) > _LIMIT:
            raise VendorError("the interface is larger than 20 MB unpacked")
        for member in members:
            parts = PurePosixPath(member.name).parts
            if "src" not in parts[1:2]:
                continue
            relative = _safe("/".join(parts[2:]))
            target = into.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            assert source is not None
            target.write_bytes(source.read())
    return into


def _from_checkout(source: Path, commit: str, into: Path) -> Path:
    head = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    if head != commit:
        raise VendorError(f"{source} is at {head}, not {commit}")
    dirty = subprocess.run(
        ["git", "-C", str(source), "status", "--porcelain", "--", "src"], check=True, capture_output=True, text=True
    ).stdout.strip()
    if dirty:
        raise VendorError(f"{source}/src has uncommitted changes")
    # symlinks=True copies a link as a link, which check_tree then refuses (never the file it points to).
    shutil.copytree(source / "src", into, symlinks=True, dirs_exist_ok=True)
    return into


def vendor(commit: str, source: Path | None = None, folder: Path = WEBUI) -> dict[str, str]:
    if not _SHA.match(commit):
        raise VendorError("--commit takes a full 40-character SHA")
    with tempfile.TemporaryDirectory() as temporary:
        staged = Path(temporary) / "webui"
        staged.mkdir()
        if source is not None:
            _from_checkout(source, commit, staged)
        else:
            _download(commit, staged)
        manifest = check_tree(staged)
        manifest_sha = _sha256((staged / MANIFEST).read_bytes())
        (staged / RECORD).write_text(record_text(commit, manifest, manifest_sha), "utf-8", newline="\n")
        if folder.exists():
            shutil.rmtree(folder)
        shutil.copytree(staged, folder)
    return check(folder)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--commit", help="the nodestro-app-ui commit to embed (full SHA)")
    parser.add_argument("--source", type=Path, help="a local nodestro-app-ui checkout at that commit")
    parser.add_argument("--check", action="store_true", help="check the embedded copy (offline)")
    arguments = parser.parse_args(argv)
    try:
        if arguments.check:
            record = check()
            print(f"webui: nodestro-app-ui {record['Version']} at {record['Commit'][:12]}, every file matches")
            return 0
        if not arguments.commit:
            parser.error("--commit or --check")
        record = vendor(arguments.commit, arguments.source)
        print(f"embedded nodestro-app-ui {record['Version']} at {record['Commit']}")
        return 0
    except VendorError as exc:
        print(f"vendor_app_ui: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
