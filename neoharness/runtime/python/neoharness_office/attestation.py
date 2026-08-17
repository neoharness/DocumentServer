from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import pwd
import selectors
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .paths import OUTPUT_ROOT, exact_read_file, file_fact
from .quality import (
    PROVENANCE_SCHEMA,
    QualityPolicyError,
    load_policy,
    policy_identity,
    qualify_artifact,
)


SOCKET_PATH = Path("/run/neoharness-office/finalizer.sock")
ATTESTATION_ROOT = Path("/run/neoharness-office/attestations")
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_STREAM_BYTES = 8 * 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 900
ATTESTATION_SCHEMA = "ai.neoharness.office.server-attestation.v1"


class AttestationError(ValueError):
    """An attested finalizer request violated the local contract."""


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _exact_output(raw: object) -> Path:
    if not isinstance(raw, str):
        raise AttestationError("artifact path must be a string")
    return exact_read_file(raw, roots=(OUTPUT_ROOT,))


def _bounded_process(
    argv: Sequence[str],
    *,
    uid: int,
    gid: int,
    timeout_seconds: int,
) -> tuple[int, bytes, bytes, bool, bool]:
    try:
        account = pwd.getpwuid(uid)
        home = account.pw_dir
    except KeyError:
        home = "/workspace/work"
    environment = [
        "/usr/bin/env",
        "-i",
        f"HOME={home}",
        "LANG=C.UTF-8",
        "LC_ALL=C.UTF-8",
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE=1",
        "NHO_INSTALL_ROOT=/opt/neoharness-office",
    ]
    command = [
        "/usr/bin/setpriv",
        f"--reuid={uid}",
        f"--regid={gid}",
        "--clear-groups",
        "--no-new-privs",
        *environment,
        *argv,
    ]
    process = subprocess.Popen(
        command,
        cwd="/workspace/work",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}
    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    while selector.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0 and not timed_out:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        events = selector.select(timeout=min(max(remaining, 0), 0.25))
        if not events and process.poll() is not None:
            events = [(key, selectors.EVENT_READ) for key in selector.get_map().values()]
        for key, _ in events:
            chunk = os.read(key.fileobj.fileno(), 64 * 1024)
            if not chunk:
                selector.unregister(key.fileobj)
                key.fileobj.close()
                continue
            stream = key.data
            available = MAX_STREAM_BYTES - len(captured[stream])
            if available > 0:
                captured[stream].extend(chunk[:available])
            if len(chunk) > max(available, 0):
                truncated[stream] = True
    if process.poll() is None:
        process.wait()
    return (
        124 if timed_out else int(process.returncode or 0),
        bytes(captured["stdout"]),
        bytes(captured["stderr"]),
        truncated["stdout"],
        truncated["stderr"],
    )


def _option_value(argv: Sequence[str], name: str) -> str | None:
    for index, item in enumerate(argv):
        if item == name:
            if index + 1 >= len(argv):
                raise AttestationError(f"{name} requires a value")
            return argv[index + 1]
        prefix = f"{name}="
        if item.startswith(prefix):
            return item[len(prefix) :]
    return None


def _json_file(path: Path) -> object:
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_STREAM_BYTES:
            raise AttestationError("finalizer evidence exceeds the local bound")
        return json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AttestationError(f"finalizer evidence is invalid: {exc}") from exc


def _execution_evidence(tool: str, argv: Sequence[str], stdout: bytes) -> object:
    if tool == "office":
        manifest = _option_value(argv, "--manifest")
        path = Path(manifest or "/workspace/output/nh-office-manifest.json")
        return _json_file(_exact_output(str(path)))
    json_output = _option_value(argv, "--json-output")
    if json_output is not None:
        return _json_file(exact_read_file(json_output))
    if len(stdout) > MAX_STREAM_BYTES:
        raise AttestationError("finalizer stdout exceeds the evidence bound")
    try:
        return json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        # Read-only helpers need no publication evidence. A successful
        # mutating helper without structured provenance simply records none.
        return None


def _provenances(value: object) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []

    def visit(candidate: object) -> None:
        if isinstance(candidate, Mapping):
            if candidate.get("schema") == PROVENANCE_SCHEMA:
                found.append(candidate)
                return
            for nested in candidate.values():
                visit(nested)
        elif isinstance(candidate, list):
            for nested in candidate:
                visit(nested)

    visit(value)
    return found


class AttestationStore:
    def __init__(self, root: Path = ATTESTATION_ROOT) -> None:
        self.root = root

    def prepare(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def record(
        self,
        *,
        uid: int,
        tool: str,
        argv: Sequence[str],
        evidence: object,
    ) -> list[str]:
        policy, identity = load_policy()
        finalizers = policy.get("finalizers")
        if not isinstance(finalizers, Mapping):
            raise AttestationError("quality policy finalizers are invalid")
        recorded: list[str] = []
        for provenance in _provenances(evidence):
            if provenance.get("policy") != identity:
                raise AttestationError("finalizer provenance policy is mismatched")
            finalizer = provenance.get("finalizer")
            artifact = provenance.get("artifact")
            if not isinstance(finalizer, Mapping) or not isinstance(artifact, Mapping):
                raise AttestationError("finalizer provenance is incomplete")
            finalizer_class = finalizer.get("class")
            if not isinstance(finalizer_class, str) or finalizer_class not in finalizers:
                raise AttestationError("finalizer provenance class is invalid")
            path = _exact_output(artifact.get("path"))
            actual = file_fact(path)
            if any(artifact.get(key) != actual[key] for key in ("size", "sha256")):
                raise AttestationError("finalizer provenance does not match output bytes")
            record_id = str(uuid4())
            record = {
                "schema": ATTESTATION_SCHEMA,
                "record_id": record_id,
                "observed_uid": uid,
                "tool": tool,
                "argv": list(argv),
                "recorded_at_unix_ms": time.time_ns() // 1_000_000,
                "provenance": dict(provenance),
                "artifact": actual,
            }
            self._write(record_id, record)
            recorded.append(record_id)
        return recorded

    def _write(self, record_id: str, value: object) -> None:
        self.prepare()
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        descriptor, temporary_name = tempfile.mkstemp(prefix=".record-", dir=self.root)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as target:
                target.write(encoded)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.root / f"{record_id}.json")
        finally:
            if temporary.exists():
                temporary.unlink()

    def qualify(self, *, artifact: object, intent: object) -> dict[str, object]:
        if intent not in {"create", "revise", "derived"}:
            raise AttestationError("publication intent is invalid")
        path = _exact_output(artifact)
        actual = file_fact(path)
        if not self.root.is_dir():
            raise AttestationError("no server-observed finalizer provenance exists")
        candidates: list[tuple[int, Mapping[str, Any]]] = []
        for record_path in self.root.glob("*.json"):
            try:
                record = _json_file(record_path)
            except AttestationError:
                continue
            if not isinstance(record, Mapping) or record.get("schema") != ATTESTATION_SCHEMA:
                continue
            if record.get("artifact") == actual:
                candidates.append((int(record.get("recorded_at_unix_ms", 0)), record))
        for _, record in sorted(candidates, reverse=True):
            provenance = record.get("provenance")
            if not isinstance(provenance, Mapping):
                continue
            try:
                qualification = qualify_artifact(
                    path,
                    intent=str(intent),
                    provenance=provenance,
                )
            except QualityPolicyError:
                continue
            return {
                "ok": True,
                "attestation_schema": ATTESTATION_SCHEMA,
                "record_id": record["record_id"],
                "qualification": qualification,
            }
        raise AttestationError(
            "no server-observed approved finalizer matches the exact artifact hash"
        )


class AttestationService:
    def __init__(
        self,
        *,
        store: AttestationStore | None = None,
        office_command: str = "/opt/neoharness-office/bin/nh-office",
        document_command: str = "/opt/neoharness-office/bin/nh-document",
    ) -> None:
        self.store = store or AttestationStore()
        self.commands = {"office": office_command, "document": document_command}

    def handle(self, request: object, *, uid: int, gid: int) -> dict[str, object]:
        if not isinstance(request, Mapping):
            raise AttestationError("request must be an object")
        operation = request.get("operation")
        if operation == "qualify":
            return self.store.qualify(
                artifact=request.get("artifact"),
                intent=request.get("intent"),
            )
        if operation != "execute":
            raise AttestationError("operation is invalid")
        tool = request.get("tool")
        argv = request.get("argv")
        if tool not in self.commands or not isinstance(argv, list):
            raise AttestationError("finalizer command is invalid")
        if (
            len(argv) > 256
            or not all(isinstance(item, str) for item in argv)
            or sum(len(item.encode()) for item in argv) > MAX_REQUEST_BYTES // 2
        ):
            raise AttestationError("finalizer arguments exceed their bound")
        exit_code, stdout, stderr, stdout_truncated, stderr_truncated = _bounded_process(
            [self.commands[str(tool)], *argv],
            uid=uid,
            gid=gid,
            timeout_seconds=COMMAND_TIMEOUT_SECONDS,
        )
        record_ids: list[str] = []
        if exit_code == 0:
            evidence = _execution_evidence(str(tool), argv, stdout)
            if evidence is not None:
                record_ids = self.store.record(
                    uid=uid,
                    tool=str(tool),
                    argv=argv,
                    evidence=evidence,
                )
        return {
            "ok": exit_code == 0,
            "exit_code": exit_code,
            "stdout": base64.b64encode(stdout).decode("ascii"),
            "stderr": base64.b64encode(stderr).decode("ascii"),
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "attestation_record_ids": record_ids,
        }


def _peer_credentials(connection: socket.socket) -> tuple[int, int]:
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    pid = int.from_bytes(raw[0:4], sys.byteorder)
    uid = int.from_bytes(raw[4:8], sys.byteorder)
    gid = int.from_bytes(raw[8:12], sys.byteorder)
    if pid <= 0 or uid < 0 or gid < 0:
        raise AttestationError("peer credentials are invalid")
    return uid, gid


class _RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if not raw or len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
                raise AttestationError("request exceeds the protocol bound")
            request = json.loads(raw)
            uid, gid = _peer_credentials(self.connection)
            service = self.server.service  # type: ignore[attr-defined]
            response = service.handle(request, uid=uid, gid=gid)
        except Exception as exc:
            response = {
                "ok": False,
                "error_code": "finalizer_attestation_rejected",
                "error": str(exc),
            }
        encoded = json.dumps(response, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > MAX_RESPONSE_BYTES:
            encoded = json.dumps(
                {
                    "ok": False,
                    "error_code": "finalizer_attestation_rejected",
                    "error": "response exceeds the protocol bound",
                },
                separators=(",", ":"),
            ).encode()
        self.wfile.write(encoded + b"\n")


class AttestationServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, path: Path, service: AttestationService) -> None:
        self.path = path
        self.service = service
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() or path.is_symlink():
            path.unlink()
        super().__init__(str(path), _RequestHandler)
        os.chmod(path, 0o666)

    def server_close(self) -> None:
        super().server_close()
        if self.path.exists() or self.path.is_symlink():
            self.path.unlink()


def serve(path: Path = SOCKET_PATH) -> None:
    service = AttestationService()
    service.store.prepare()
    with AttestationServer(path, service) as server:
        server.serve_forever()


def _request(value: object, path: Path = SOCKET_PATH) -> Mapping[str, Any]:
    encoded = json.dumps(value, separators=(",", ":")).encode() + b"\n"
    if len(encoded) > MAX_REQUEST_BYTES:
        raise AttestationError("request exceeds the protocol bound")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(str(path))
        connection.sendall(encoded)
        connection.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = connection.recv(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise AttestationError("response exceeds the protocol bound")
            chunks.append(chunk)
    try:
        result = json.loads(b"".join(chunks))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AttestationError("attestation response is invalid") from exc
    if not isinstance(result, Mapping):
        raise AttestationError("attestation response is invalid")
    return result


def client_main() -> int:
    invoked = Path(sys.argv[0]).name
    if invoked in {"nh-office", "nh-office-attested"}:
        tool = "office"
    elif invoked in {"nh-document", "nh-document-attested"}:
        tool = "document"
    elif invoked == "nh-artifact-qualify":
        parser = argparse.ArgumentParser(prog=invoked)
        parser.add_argument("--artifact", required=True)
        parser.add_argument("--intent", choices=("create", "revise", "derived"), required=True)
        arguments = parser.parse_args()
        result = _request(
            {
                "operation": "qualify",
                "artifact": arguments.artifact,
                "intent": arguments.intent,
            }
        )
        if result.get("ok") is not True:
            print(result.get("error", "artifact is not publication-qualified"), file=sys.stderr)
            return 65
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    else:
        raise SystemExit(f"unsupported attested entry point: {invoked}")

    result = _request({"operation": "execute", "tool": tool, "argv": sys.argv[1:]})
    if result.get("error_code") is not None:
        print(result.get("error", "attested finalizer rejected"), file=sys.stderr)
        return 65
    try:
        stdout = base64.b64decode(result["stdout"], validate=True)
        stderr = base64.b64decode(result["stderr"], validate=True)
        exit_code = int(result["exit_code"])
    except (KeyError, TypeError, ValueError):
        print("attestation response is invalid", file=sys.stderr)
        return 70
    sys.stdout.buffer.write(stdout)
    sys.stderr.buffer.write(stderr)
    if result.get("stdout_truncated") or result.get("stderr_truncated"):
        print("attested finalizer diagnostics were truncated", file=sys.stderr)
    return exit_code


__all__ = [
    "ATTESTATION_ROOT",
    "SOCKET_PATH",
    "AttestationError",
    "AttestationServer",
    "AttestationService",
    "AttestationStore",
    "client_main",
    "serve",
]
