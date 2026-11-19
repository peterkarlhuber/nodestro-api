"""``nodestro-local boot``: Nodestro Local Docker's key file on the host, read once as root, then never again (LD-020).

The launcher keeps the key that seals provider keys, MCP tokens and connection secrets in a file on the host, outside
the data volume, the images, the repository and the backups, readable by the host user only (``0600``, or an ACL with
the current user only on Windows). Docker mounts it read-only at :data:`DEFAULT_KEY_FILE`. The studio's user (10001)
cannot read it, and the host's permissions are not loosened for it. So the container starts this command as root, with
only the capabilities it needs to read the file and change its user (``DAC_READ_SEARCH``, ``SETUID``, ``SETGID``),
and it:

1. reads the key file (a regular file, not a link, at most :data:`MAX_KEY_BYTES`, a valid key);
2. drops to the studio's user for good (:func:`drop_privileges`: no supplementary groups, real, effective and saved
   ids 10001, and it checks that no capability is left and that going back to root fails);
3. for ``serve``: writes the key into a pipe as the handoff of :mod:`nodestro_api.handoff` and replaces itself with
   ``tini -- nodestro-local serve --secrets-fd <pipe>``: the key never appears in an environment variable or on a
   command line, and the studio reads and closes the pipe before anything else runs;
   for ``rekey`` and ``discard-unreadable`` (the launcher's key rotation and key loss, the studio stopped): runs
   :func:`nodestro_runtime.local_vault.rekey` or :func:`~nodestro_runtime.local_vault.discard_unreadable` itself.

``nodestro-local serve`` in the ``local-container`` profile refuses to start without a handed-over key and as root
(:func:`nodestro_api.cli.serve`), so the studio never stores a secret unencrypted and never runs with the extra
capabilities.
"""

from __future__ import annotations

from collections.abc import Sequence
import json
import os
from pathlib import Path
import shutil
import stat

from nodestro_runtime.problems import NodestroError

#: Where the compose file mounts the key file (read-only), and the second one for a rotation.
KEY_FILE_ENV = "NODESTRO_VAULT_KEY_FILE"
DEFAULT_KEY_FILE = "/run/secrets/nodestro-vault-key"
NEW_KEY_FILE_ENV = "NODESTRO_VAULT_KEY_NEW_FILE"
DEFAULT_NEW_KEY_FILE = "/run/secrets/nodestro-vault-key-new"
#: The studio's user and group in the image (nodestro-server image/Dockerfile, target ``local``).
RUN_AS = (10001, 10001)
#: A key file holds 44 characters (32 bytes, base64) and a line end.
MAX_KEY_BYTES = 1024
ACTIONS = ("serve", "rekey", "discard-unreadable")


def _refused(code: str, message: str) -> NodestroError:
    return NodestroError(code, message)


def read_key_file(path: str | Path) -> str:
    """The key in ``path``: refused (``secrets.vault_key_file``) when it is missing, not a regular file, a symbolic
    link, too large or not a key. The reason never quotes the file's content."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise _refused(
            "secrets.vault_key_file",
            f"The key file {path} cannot be read ({error.strerror}); start Nodestro with its launcher "
            "(./nodestro start, .\\nodestro.ps1 start), which creates and mounts it",
        ) from None
    try:
        found = os.fstat(fd)
        if not stat.S_ISREG(found.st_mode):
            raise _refused("secrets.vault_key_file", f"The key file {path} is not a regular file")
        if found.st_size > MAX_KEY_BYTES:
            raise _refused("secrets.vault_key_file", f"The key file {path} is too large to be a key")
        data = os.read(fd, MAX_KEY_BYTES + 1)
    finally:
        os.close(fd)
    try:
        text = data.decode("ascii").strip()
    except UnicodeDecodeError:
        text = ""
    from nodestro_runtime.mcp.vault import VAULT_KEY_ENV, vault_box_from_env

    try:
        box = vault_box_from_env({VAULT_KEY_ENV: text}) if text else None
    except NodestroError:
        box = None
    if box is None:
        raise _refused("secrets.vault_key_file", f"The key file {path} does not hold a key (32 random bytes, base64)")
    return text


def _capabilities() -> dict[str, int]:
    found: dict[str, int] = {}
    try:
        lines = Path("/proc/self/status").read_text().splitlines()
    except OSError:
        return found
    for line in lines:
        name, _, value = line.partition(":")
        if name in ("CapInh", "CapPrm", "CapEff", "CapAmb"):
            found[name] = int(value.strip(), 16)
    return found


def drop_privileges(uid: int = RUN_AS[0], gid: int = RUN_AS[1]) -> None:
    """Become ``uid:gid`` for good, and check it: no supplementary groups, real, effective and saved ids changed, no
    capability left (``/proc/self/status``), and going back to root fails."""
    if os.geteuid() != 0:
        raise _refused(
            "secrets.boot_not_root",
            "nodestro-local boot reads the key file as root and must be the container's first process "
            "(compose.vault-key.yaml); the launcher starts it so",
        )
    os.setgroups([])
    os.setresgid(gid, gid, gid)
    os.setresuid(uid, uid, uid)
    problems: list[str] = []
    if os.getresuid() != (uid, uid, uid) or os.getresgid() != (gid, gid, gid):
        problems.append("user or group ids")
    if os.getgroups():
        problems.append("supplementary groups")
    left = {name: value for name, value in _capabilities().items() if value}
    if left:
        problems.append(f"capabilities {', '.join(sorted(left))}")
    try:
        os.setuid(0)
    except PermissionError:
        pass
    else:
        problems.append("root could be regained")
    if problems:
        raise _refused("secrets.boot_privileges", f"Dropping root's privileges failed: {'; '.join(problems)}")
    import pwd

    try:
        home = pwd.getpwuid(uid).pw_dir
    except KeyError:
        home = "/"  # what Docker sets for a user without a passwd entry
    os.environ["HOME"] = home


def _data_dir() -> Path:
    from nodestro_runtime.profile import RuntimeProfile

    return RuntimeProfile.from_env(os.environ).data_dir


def _serve(key: str) -> None:
    """Hand ``key`` to ``nodestro-local serve`` through a pipe and become it (under tini, the container's init)."""
    read_end, write_end = os.pipe()
    payload = json.dumps({"vault_key": key}).encode()
    try:
        os.write(write_end, payload)  # far below the pipe's capacity: never blocks
    finally:
        os.close(write_end)
    os.set_inheritable(read_end, True)
    command = shutil.which("nodestro-local") or "nodestro-local"
    tini = shutil.which("tini")
    argv = [command, "serve", "--secrets-fd", str(read_end)]
    if tini is not None:
        argv = [tini, "--", *argv]
    os.execv(argv[0], argv)


def main(argv: Sequence[str]) -> int:
    """``nodestro-local boot ACTION`` (see the module documentation)."""
    from nodestro_runtime import local_vault
    from nodestro_runtime.mcp.vault import VAULT_KEY_ENV

    if len(argv) != 1 or argv[0] not in ACTIONS:
        raise SystemExit(f"usage: nodestro-local boot {{{','.join(ACTIONS)}}}")
    action = argv[0]
    # The handoff replaces the operator's variable; it is never passed on to the studio.
    os.environ.pop(VAULT_KEY_ENV, None)
    try:
        key = read_key_file(os.environ.get(KEY_FILE_ENV) or DEFAULT_KEY_FILE)
        new = read_key_file(os.environ.get(NEW_KEY_FILE_ENV) or DEFAULT_NEW_KEY_FILE) if action == "rekey" else None
        drop_privileges()
        if action == "serve":
            _serve(key)
            return 1  # pragma: no cover - execv does not return
        data_dir = _data_dir()
        if action == "rekey":
            assert new is not None
            counts = local_vault.rekey(data_dir, key, new)
            print(f"Sealed again with the new key: {_counted(counts)}.", flush=True)
            return 0
        counts = local_vault.discard_unreadable(data_dir, key)
        print(f"Removed the secrets the key cannot open: {_counted(counts)}.", flush=True)
        return 0
    except NodestroError as error:
        raise SystemExit(f"Nodestro: {error}") from None


def _counted(counts: dict[str, int]) -> str:
    return (
        f"{counts['provider']} provider key(s), {counts['mcp']} MCP token(s) and sign-in(s), "
        f"{counts['connection']} connection secret(s)"
    )


__all__ = [
    "DEFAULT_KEY_FILE",
    "DEFAULT_NEW_KEY_FILE",
    "KEY_FILE_ENV",
    "NEW_KEY_FILE_ENV",
    "RUN_AS",
    "drop_privileges",
    "main",
    "read_key_file",
]
