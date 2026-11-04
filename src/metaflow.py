#!/usr/bin/env python3
"""One foreground owner for the three version-one Nexus pairs."""
from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import re
import selectors
import signal
import socket
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable


class CompositionError(RuntimeError):
    pass


def proc_fields(pid: int) -> tuple[int, int, int, str]:
    """Return ppid, starttime, uid and immutable executable for a live pid."""
    raw = Path(f"/proc/{pid}/stat").read_text()
    tail = raw[raw.rfind(")") + 2 :].split()
    ppid = int(tail[1])
    starttime = int(tail[19])
    uid = Path(f"/proc/{pid}").stat().st_uid
    exe = os.path.realpath(f"/proc/{pid}/exe")
    return ppid, starttime, uid, exe


@dataclass
class ProcessIdentity:
    role: str
    pid: int
    pidfd: int
    ppid: int
    starttime: int
    uid: int
    exe: str

    @classmethod
    def capture(cls, role: str, pid: int, expected_ppid: int, expected_uid: int,
                expected_exe: str, pidfd: int | None = None) -> "ProcessIdentity":
        owned_fd = pidfd if pidfd is not None else os.pidfd_open(pid)
        try:
            ppid, starttime, uid, exe = proc_fields(pid)
            expected_real = os.path.realpath(expected_exe)
            if (ppid, uid, exe) != (expected_ppid, expected_uid, expected_real):
                raise CompositionError(
                    f"{role} identity mismatch: pid={pid} ppid={ppid} uid={uid} exe={exe}")
            return cls(role, pid, owned_fd, ppid, starttime, uid, exe)
        except BaseException:
            if pidfd is None:
                os.close(owned_fd)
            raise

    def verify(self) -> None:
        try:
            signal.pidfd_send_signal(self.pidfd, 0)
            current = proc_fields(self.pid)
        except (ProcessLookupError, FileNotFoundError) as exc:
            raise CompositionError(f"{self.role} is dead: pid={self.pid}") from exc
        if current != (self.ppid, self.starttime, self.uid, self.exe):
            raise CompositionError(f"{self.role} lifetime identity changed: pid={self.pid}")

    def alive(self) -> bool:
        try:
            self.verify()
            return True
        except CompositionError:
            return False

    def send(self, sig: int) -> None:
        self.verify()
        signal.pidfd_send_signal(self.pidfd, sig)


@dataclass(frozen=True)
class OwnedInode:
    path: Path
    device: int
    inode: int
    kind: str
    handle: int

    @classmethod
    def capture(cls, path: Path, kind: str) -> "OwnedInode":
        value = path.lstat()
        handle = os.open(path, os.O_PATH | os.O_NOFOLLOW)
        return cls(path, value.st_dev, value.st_ino, kind, handle)

    def still_same(self) -> bool:
        try:
            value = self.path.lstat()
        except FileNotFoundError:
            return False
        right_kind = stat.S_ISSOCK(value.st_mode) if self.kind == "socket" else stat.S_ISREG(value.st_mode)
        held = os.fstat(self.handle)
        return (right_kind and (value.st_dev, value.st_ino) == (self.device, self.inode)
                and (held.st_dev, held.st_ino) == (self.device, self.inode))

    def close(self) -> None:
        try: os.close(self.handle)
        except OSError: pass


@dataclass
class Pair:
    name: str
    process: subprocess.Popen[bytes]
    parent: ProcessIdentity
    daemon: ProcessIdentity
    translator: ProcessIdentity
    log: OwnedInode
    sockets: list[OwnedInode] = field(default_factory=list)

    def identities(self) -> list[ProcessIdentity]:
        return [self.parent, self.daemon, self.translator]

    def verify(self) -> None:
        for identity in self.identities():
            identity.verify()


class Composition:
    def __init__(self, config: dict, name: str, environ: dict[str, str] | None = None,
                 runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run):
        self.config = config
        self.name = name
        self.environ = dict(os.environ if environ is None else environ)
        self.runner = runner
        self.uid = os.getuid()
        self.pairs: list[Pair] = []
        self.provisionals: list[tuple[subprocess.Popen[bytes], int]] = []
        self.owned_files: list[OwnedInode] = []
        self.created_runtime = False
        self.stop_requested = False
        self._wake_write: int | None = None
        self._install_paths()

    def _install_paths(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", self.name):
            raise CompositionError("metaflow name must contain only letters, digits, underscore and hyphen")
        if self.config.get("uidRule") != "current-login":
            raise CompositionError("unsupported deployment UID rule")
        runtime_variable = self.config["runtimeRootEnvironment"]
        state_variable = self.config["stateRootEnvironment"]
        runtime_base = self.environ.get(runtime_variable)
        if not runtime_base:
            raise CompositionError(f"{runtime_variable} is required")
        fallback = Path.home() / self.config["stateRootFallback"]
        state_base = self.environ.get(state_variable, str(fallback))
        self.runtime = Path(runtime_base) / self.config["runtimeNamespace"] / self.name
        self.state = Path(state_base) / self.config["stateNamespace"] / self.name
        if "»" in str(self.runtime) or "»" in str(self.state):
            raise CompositionError("Place paths cannot contain the datom string terminator")
        self.place = f"{{ {self.name} «{self.runtime}» «{self.state}» }}"

    @property
    def startup_deadline(self) -> float:
        return time.monotonic() + float(self.config["startupTimeoutSeconds"])

    def prepare_paths(self) -> None:
        if self.runtime.exists():
            if not self.runtime.is_dir():
                raise CompositionError(f"runtime path is not a directory: {self.runtime}")
            if any(self.runtime.iterdir()):
                raise CompositionError(f"runtime path conflict: {self.runtime}")
        else:
            self.runtime.mkdir(parents=True)
            self.created_runtime = True
        self.state.mkdir(parents=True, exist_ok=True)
        marker = self.runtime / ".metaflow-owner"
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(f"{os.getpid()}\n")
            output.flush()
            os.fsync(output.fileno())
        self.owned_files.append(OwnedInode.capture(marker, "file"))

    def _wait_parent_exec(self, role: str, process: subprocess.Popen[bytes], expected_exe: str,
                          deadline: float, pidfd: int) -> ProcessIdentity:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise CompositionError(f"{role} exited during startup: {process.returncode}")
            try:
                ppid, _, uid, exe = proc_fields(process.pid)
            except FileNotFoundError:
                time.sleep(self._poll_seconds())
                continue
            if exe == os.path.realpath(expected_exe):
                return ProcessIdentity.capture(role, process.pid, os.getpid(), self.uid,
                                               expected_exe, pidfd)
            if ppid != os.getpid() or uid != self.uid:
                raise CompositionError(f"{role} changed owner during exec")
            time.sleep(self._poll_seconds())
        raise CompositionError(f"{role} did not exec the pinned pair executable")

    def _children(self, parent: ProcessIdentity) -> list[int]:
        result = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                ppid, _, _, _ = proc_fields(int(entry.name))
            except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
                continue
            if ppid == parent.pid:
                result.append(int(entry.name))
        return result

    def _select_child(self, parent: ProcessIdentity, role: str, expected_exe: str) -> ProcessIdentity | None:
        expected_real = os.path.realpath(expected_exe)
        candidates: list[int] = []
        for pid in self._children(parent):
            try:
                _, _, uid, exe = proc_fields(pid)
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
            if uid == self.uid and exe == expected_real:
                candidates.append(pid)
        if len(candidates) > 1:
            raise CompositionError(f"ambiguous {role} children beneath {parent.pid}: {candidates}")
        if not candidates:
            return None
        return ProcessIdentity.capture(role, candidates[0], parent.pid, self.uid, expected_exe)

    def _wait_children(self, name: str, parent: ProcessIdentity, item: dict,
                       deadline: float) -> tuple[ProcessIdentity, ProcessIdentity]:
        daemon = translator = None
        while time.monotonic() < deadline:
            parent.verify()
            daemon = daemon or self._select_child(parent, f"{name} daemon", item["daemonExecutable"])
            translator = translator or self._select_child(parent, f"{name} translator", item["translatorExecutable"])
            if daemon and translator:
                daemon.verify(); translator.verify(); parent.verify()
                return daemon, translator
            time.sleep(self._poll_seconds())
        missing = [role for role, value in (("daemon", daemon), ("translator", translator)) if value is None]
        raise CompositionError(f"missing {name} child: {', '.join(missing)}")

    def _wait_sockets(self, pair: Pair, names: Iterable[str], deadline: float) -> None:
        pending = {self.runtime / name for name in names}
        while pending and time.monotonic() < deadline:
            pair.verify()
            for path in list(pending):
                try:
                    mode = path.lstat().st_mode
                except FileNotFoundError:
                    continue
                if not stat.S_ISSOCK(mode):
                    raise CompositionError(f"socket path is not a socket: {path}")
                pair.sockets.append(OwnedInode.capture(path, "socket"))
                pending.remove(path)
            if pending:
                time.sleep(self._poll_seconds())
        if pending:
            raise CompositionError("startup sockets absent: " + ", ".join(map(str, sorted(pending))))

    def start_pair(self, name: str) -> Pair:
        item = self.config[name]
        for key in ("pair", "pairExecutable", "daemonExecutable", "translatorExecutable"):
            if not os.access(item[key], os.X_OK):
                raise CompositionError(f"pinned executable is unavailable: {item[key]}")
        log_path = self.runtime / f"{name}.log"
        log_fd = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        log_file = os.fdopen(log_fd, "wb", buffering=0)
        log_owned = OwnedInode.capture(log_path, "file")
        self.owned_files.append(log_owned)
        try:
            process = subprocess.Popen([item["pair"], self.place], stdout=log_file,
                                       stderr=subprocess.STDOUT, close_fds=True)
        finally:
            log_file.close()
        pidfd = os.pidfd_open(process.pid)
        provisional = (process, pidfd)
        self.provisionals.append(provisional)
        deadline = self.startup_deadline
        parent = self._wait_parent_exec(f"{name} pair", process, item["pairExecutable"], deadline, pidfd)
        daemon, translator = self._wait_children(name, parent, item, deadline)
        pair = Pair(name, process, parent, daemon, translator, log_owned)
        self.pairs.append(pair)
        self.provisionals.remove(provisional)
        self._wait_sockets(pair, item["sockets"], deadline)
        return pair

    def _poll_seconds(self) -> float:
        return float(self.config["bootstrapPollMilliseconds"]) / 1000.0

    def verify_all(self) -> None:
        for pair in self.pairs:
            pair.verify()

    def _command(self, argv: list[str]) -> str:
        self.verify_all()
        try:
            result = self.runner(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=float(self.config["commandTimeoutSeconds"]), check=False,
                                 env=self.environ)
        except subprocess.TimeoutExpired as exc:
            raise CompositionError(f"semantic command timed out: {argv[0]}") from exc
        self.verify_all()
        if result.returncode != 0:
            raise CompositionError(f"semantic command failed ({result.returncode}): {result.stderr.strip()}")
        return result.stdout.strip()

    def ready_herdr(self) -> None:
        item = self.config["herdr"]
        output = ""
        for template in item["readyDatoms"]:
            datom = template.replace("%HERDR%", item["herdr"])
            output = self._command([item["metaCli"], self.name, datom])
            if not output.startswith("Configured."):
                raise CompositionError(f"Herdr configuration was not accepted: {output}")
        if "Ready" not in output:
            raise CompositionError(f"Herdr did not become ready: {output}")

    def ready_belief(self) -> None:
        item = self.config["belief"]
        deadline = self.startup_deadline
        resolved = ""
        while time.monotonic() < deadline:
            try:
                resolved = self._command([item["cli"], self.name, item["resolveDatom"]])
            except CompositionError:
                resolved = ""
            if resolved.startswith("Resolved."):
                break
            time.sleep(self._poll_seconds())
        if not resolved.startswith("Resolved."):
            raise CompositionError("Belief did not answer Resolve")
        ready = self._command([item["metaCli"], self.name, item["readyDatom"]])
        if not (ready.startswith("Configured.") and "Ready" in ready):
            raise CompositionError(f"Belief did not become ready: {ready}")

    def _flow_translator(self) -> ProcessIdentity:
        matches = [p.translator for p in self.pairs if p.name == "flow"]
        if len(matches) != 1:
            raise CompositionError("exactly one owned Flow translator is required")
        return matches[0]

    def admit_flow_peer(self) -> None:
        flow = self._flow_translator()
        # The pidfd and full /proc lifetime tuple are checked on both sides of each admission.
        for nexus in ("herdr", "belief"):
            self.verify_all(); flow.verify()
            item = self.config[nexus]
            template = item["peerDatom"]
            datom = template.replace("%UID%", str(flow.uid)).replace("%PID%", str(flow.pid))
            output = self._command([item["metaCli"], self.name, datom])
            flow.verify(); self.verify_all()
            if not (output.startswith("Configured.") and "Ready" in output):
                raise CompositionError(f"{nexus} peer admission was not ready: {output}")
            late = item.get("lateSockets", [])
            if late:
                pair = next((candidate for candidate in self.pairs if candidate.name == nexus), None)
                if pair is None:
                    raise CompositionError(f"owned {nexus} pair is absent")
                self._wait_sockets(pair, late, self.startup_deadline)
                flow.verify(); self.verify_all()

    def ready_flow(self) -> None:
        item = self.config["flow"]
        output = self._command([item["cli"], self.name, item["readyDatom"]])
        if not output.startswith("Listed."):
            raise CompositionError(f"Flow did not answer Definitions: {output}")

    def start(self) -> None:
        self.prepare_paths()
        self.start_pair("herdr")
        self.ready_herdr()
        self.start_pair("belief")
        self.ready_belief()
        self.start_pair("flow")
        self.admit_flow_peer()
        self.ready_flow()
        print(f"Ready.{{ {self.name} «{self.runtime}» «{self.state}» }}", flush=True)

    def wait(self) -> None:
        watched = [identity for pair in self.pairs for identity in pair.identities()]
        wake_read, wake_write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self._wake_write = wake_write
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(wake_read, selectors.EVENT_READ, None)
                for identity in watched:
                    selector.register(identity.pidfd, selectors.EVENT_READ, identity)
                while not self.stop_requested:
                    events = selector.select()
                    failed = [key.data.role for key, _ in events
                              if key.data is not None and not key.data.alive()]
                    if failed:
                        raise CompositionError("owned process ended: " + ", ".join(failed))
        finally:
            self._wake_write = None
            os.close(wake_read); os.close(wake_write)

    def request_stop(self, *_: object) -> None:
        self.stop_requested = True
        if self._wake_write is not None:
            try: os.write(self._wake_write, b"x")
            except OSError: pass

    def cleanup(self) -> None:
        grace = float(self.config.get("stopGraceSeconds", 3))
        identities = [identity for pair in reversed(self.pairs) for identity in pair.identities()]
        # Pair parents receive the first stop. Every signal uses the retained pidfd.
        for pair in reversed(self.pairs):
            if pair.parent.alive():
                try: pair.parent.send(signal.SIGTERM)
                except CompositionError: pass
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and any(identity.alive() for identity in identities):
            time.sleep(0.02)
        for identity in identities:
            if identity.alive():
                try: identity.send(signal.SIGKILL)
                except CompositionError: pass
        for pair in self.pairs:
            try: pair.process.wait(timeout=grace)
            except subprocess.TimeoutExpired: pass
        # A pair that failed before its complete identity was established is still
        # the exact subprocess we spawned, retained by pidfd. It is never admitted.
        for process, pidfd in self.provisionals:
            if process.poll() is None:
                try: signal.pidfd_send_signal(pidfd, signal.SIGTERM)
                except ProcessLookupError: pass
        provisional_deadline = time.monotonic() + grace
        while time.monotonic() < provisional_deadline and any(p.poll() is None for p, _ in self.provisionals):
            time.sleep(0.02)
        for process, pidfd in self.provisionals:
            if process.poll() is None:
                try: signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                except ProcessLookupError: pass
            try: process.wait(timeout=grace)
            except subprocess.TimeoutExpired: pass
            os.close(pidfd)
        self.provisionals.clear()
        # Only captured socket inodes from these owned pairs may be removed.
        for owned in [socket for pair in self.pairs for socket in pair.sockets]:
            if owned.still_same():
                owned.path.unlink()
            owned.close()
        for owned in reversed(self.owned_files):
            if owned.still_same():
                owned.path.unlink()
            owned.close()
        for pair in self.pairs:
            for identity in pair.identities():
                try: os.close(identity.pidfd)
                except OSError: pass
        try:
            self.runtime.rmdir()
        except OSError:
            pass


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as source:
        return json.load(source)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="metaflow")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("name")
    args = parser.parse_args(argv)
    config_path = os.environ.get("METAFLOW_DEPLOYMENT_CONFIG")
    if not config_path:
        parser.error("METAFLOW_DEPLOYMENT_CONFIG is required; use the Nix package")
    composition = Composition(load_config(config_path), args.name)
    signal.signal(signal.SIGINT, composition.request_stop)
    signal.signal(signal.SIGTERM, composition.request_stop)
    try:
        composition.start()
        composition.wait()
        return 0
    except CompositionError as exc:
        print(f"metaflow: {exc}", file=sys.stderr)
        return 1
    finally:
        composition.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
