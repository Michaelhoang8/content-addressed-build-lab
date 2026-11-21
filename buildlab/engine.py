"""Bounded local DAG execution, verified action cache and atomic release pointer."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import tempfile
import threading
import time
import uuid

from . import operations
from .operations import canonical


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def relative_name(name):
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 128
        or "\\" in name
        or ":" in name
    ):
        raise ValueError("Invalid relative artifact name")
    path = PurePosixPath(name)
    if (
        not path.parts
        or path.is_absolute()
        or path.as_posix() != name
        or any(
            part in (".", "..") or not re.fullmatch(r"[A-Za-z0-9_.-]+", part)
            for part in path.parts
        )
    ):
        raise ValueError("Artifact name must be a normalized relative path")
    return name


def _json_value(value, depth=0):
    if depth > 20:
        raise ValueError("JSON configuration nesting exceeds limit")
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        for item in value.values():
            _json_value(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _json_value(item, depth + 1)
    elif type(value) not in (str, int, float, bool, type(None)) or (
        type(value) is float and not math.isfinite(value)
    ):
        raise ValueError("Only finite JSON configuration values are supported")


def validate_graph(spec):
    if (
        not isinstance(spec, dict)
        or set(spec) != {"format", "actions"}
        or type(spec["format"]) is not int
        or spec["format"] != 1
    ):
        raise ValueError("Expected build format 1")
    actions = spec["actions"]
    if not isinstance(actions, list) or not 1 <= len(actions) <= 32:
        raise ValueError("Graph must have 1..32 actions")
    by_id = {}
    for action in actions:
        if not isinstance(action, dict) or set(action) != {
            "id",
            "operation",
            "deps",
            "inputs",
            "outputs",
            "argv",
            "config",
        }:
            raise ValueError("Invalid action fields")
        identity = action["id"]
        if (
            not isinstance(identity, str)
            or not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", identity)
            or identity in by_id
        ):
            raise ValueError("Invalid or duplicate action ID")
        if (
            not isinstance(action["operation"], str)
            or action["operation"] not in operations.OPERATIONS
        ):
            raise ValueError("Unknown owned operation")
        for field in ["deps", "inputs", "outputs"]:
            values = action[field]
            if (
                not isinstance(values, list)
                or len(values) > 8
                or any(not isinstance(v, str) for v in values)
                or len(set(values)) != len(values)
            ):
                raise ValueError("Invalid bounded action list")
        if not action["outputs"]:
            raise ValueError("Action must declare outputs")
        for name in action["inputs"] + action["outputs"]:
            relative_name(name)
        argv = action["argv"]
        if (
            not isinstance(argv, list)
            or len(argv) > 32
            or any(not isinstance(v, str) or len(v) > 256 for v in argv)
        ):
            raise ValueError("Invalid owned argv")
        if not isinstance(action["config"], dict):
            raise ValueError("Config must be a JSON object")
        _json_value(action)
        if len(canonical(action)) > 16384:
            raise ValueError("Action definition exceeds byte limit")
        by_id[identity] = action
    for action in actions:
        if any(dependency not in by_id for dependency in action["deps"]):
            raise ValueError("Unknown dependency")
    visited, active, ordered = set(), set(), []

    def visit(identity):
        if identity in active:
            raise ValueError("Dependency cycle")
        if identity in visited:
            return
        active.add(identity)
        for dependency in sorted(by_id[identity]["deps"]):
            visit(dependency)
        active.remove(identity)
        visited.add(identity)
        ordered.append(identity)

    for identity in sorted(by_id):
        visit(identity)
    return by_id, ordered


def tool_identity():
    package = Path(__file__).parent
    return {
        "engine_api": 1,
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "sources": {
            name: sha256((package / name).read_bytes().replace(b"\r\n", b"\n"))
            for name in ["engine.py", "operations.py"]
        },
    }


def action_key(action, inputs, dependency_metadata, tool):
    material = {
        "key_format": 1,
        "id": action["id"],
        "operation": action["operation"],
        "argv": action["argv"],
        "config": action["config"],
        "outputs": sorted(action["outputs"]),
        "sources": {
            name: {"sha256": sha256(data), "bytes": len(data)}
            for name, data in sorted(inputs.items())
        },
        "dependency_outputs": dependency_metadata,
        "tool": tool,
    }
    return sha256(canonical(material))


class Trace:
    def __init__(self):
        self.origin = time.perf_counter_ns()
        self.events = []
        self.lock = threading.Lock()

    def emit(self, event, **fields):
        with self.lock:
            row = {
                "event_id": len(self.events),
                "time_ns": time.perf_counter_ns() - self.origin,
                "event": event,
                **fields,
            }
            self.events.append(row)


def atomic_replace(source, target, trace, context):
    """Retry bounded Windows sharing failures; never delete a destination to force it."""
    for attempt in range(6):
        try:
            os.replace(source, target)
            return
        except PermissionError as error:
            if getattr(error, "winerror", None) not in (5, 32, 33) or attempt == 5:
                raise
            trace.emit(
                "RENAME_RETRY",
                context=context,
                attempt=attempt + 1,
                winerror=error.winerror,
                delay_ms=10 * (attempt + 1),
            )
            time.sleep(0.01 * (attempt + 1))


@dataclass(frozen=True)
class ActionResult:
    identity: str
    key: str
    files: dict
    status: str
    duration_ns: int


@contextmanager
def exclusive(cache):
    cache.mkdir(parents=True, exist_ok=True)
    lock = cache / "build.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise RuntimeError(
            "Local cache is locked; concurrent builds are not supported"
        ) from error
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink()


class Engine:
    def __init__(self, workspace, cache, output, *, workers=3):
        if type(workers) is not int or not 1 <= workers <= 4:
            raise ValueError("Worker count must be 1..4")
        self.workspace = Path(workspace).resolve()
        self.cache = Path(cache).resolve()
        self.output = Path(output).resolve()
        self.workers = workers

    def _snapshot_sources(self, actions):
        snapshot = {}
        for name in sorted(
            {name for action in actions.values() for name in action["inputs"]}
        ):
            path = (self.workspace / name).resolve()
            if (
                not path.is_relative_to(self.workspace)
                or not path.is_file()
                or path.stat().st_size > 1_000_000
            ):
                raise ValueError("Source must be a bounded file inside the workspace")
            data = path.read_bytes()
            if len(data) > 1_000_000:
                raise ValueError("Source changed beyond file limit")
            snapshot[name] = data
        if sum(map(len, snapshot.values())) > 4_000_000:
            raise ValueError("Source snapshot exceeds four megabytes")
        return snapshot

    def _read_object(self, key, outputs):
        entry = self.cache / "objects" / key
        if entry.is_symlink():
            raise ValueError("Cache object is a symlink")
        manifest_path = entry / "manifest.json"
        if manifest_path.is_symlink() or manifest_path.stat().st_size > 16384:
            raise ValueError("Invalid cache manifest")
        manifest = json.loads(manifest_path.read_bytes())
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"format", "key", "files"}
            or manifest["format"] != 1
            or manifest["key"] != key
        ):
            raise ValueError("Cache identity mismatch")
        metadata = manifest["files"]
        if not isinstance(metadata, dict) or set(metadata) != set(outputs):
            raise ValueError("Cache output declarations differ")
        files_root = entry / "files"
        if files_root.is_symlink():
            raise ValueError("Cache files directory is a symlink")
        actual = {
            p.relative_to(files_root).as_posix()
            for p in files_root.rglob("*")
            if p.is_file()
        }
        if actual != set(outputs):
            raise ValueError("Cache has missing or extra files")
        contents = {}
        for name in outputs:
            relative_name(name)
            path = files_root / name
            record = metadata[name]
            if (
                not isinstance(record, dict)
                or set(record) != {"sha256", "bytes"}
                or type(record["bytes"]) is not int
                or not 0 <= record["bytes"] <= 1_000_000
            ):
                raise ValueError("Invalid cache file record")
            if (
                path.is_symlink()
                or not path.resolve().is_relative_to(files_root.resolve())
                or path.stat().st_size != record["bytes"]
            ):
                raise ValueError("Cache file type or length mismatch")
            data = path.read_bytes()
            if len(data) != record["bytes"] or sha256(data) != record["sha256"]:
                raise ValueError("Cached output hash mismatch")
            contents[name] = data
        return metadata, contents

    def _execute(self, action, sources, dependencies, tool, trace):
        started = time.perf_counter_ns()
        identity = action["id"]
        trace.emit(
            "ACTION_STARTED", action=identity, thread=threading.current_thread().name
        )
        inputs = {name: sources[name] for name in action["inputs"]}
        metadata = {name: dependencies[name].files for name in sorted(dependencies)}
        key = action_key(action, inputs, metadata, tool)
        entry = self.cache / "objects" / key
        if entry.exists() or entry.is_symlink():
            try:
                files, _ = self._read_object(key, action["outputs"])
                elapsed = time.perf_counter_ns() - started
                trace.emit(
                    "CACHE_HIT",
                    action=identity,
                    key=key,
                    duration_ns=elapsed,
                    output_bytes=sum(f["bytes"] for f in files.values()),
                )
                return ActionResult(identity, key, files, "HIT", elapsed)
            except (ValueError, OSError, TypeError, KeyError) as error:
                quarantine = self.cache / "quarantine" / f"{key}-{uuid.uuid4().hex}"
                atomic_replace(entry, quarantine, trace, "cache_quarantine")
                trace.emit(
                    "CACHE_REJECTED", action=identity, key=key, reason=str(error)
                )
        trace.emit("ACTION_EXECUTING", action=identity, key=key)
        staging = Path(
            tempfile.mkdtemp(prefix=key[:12] + "-", dir=self.cache / "staging")
        )
        files_root = staging / "files"
        files_root.mkdir()
        try:
            dependency_bytes = {
                name: self._read_object(result.key, result.files)[1]
                for name, result in dependencies.items()
            }
            operations.transform(action, inputs, dependency_bytes, files_root)
            actual = {
                p.relative_to(files_root).as_posix()
                for p in files_root.rglob("*")
                if p.is_file()
            }
            if actual != set(action["outputs"]):
                raise ValueError(
                    "Action did not produce exactly its declared output files"
                )
            files = {}
            for name in sorted(actual):
                path = files_root / name
                if path.is_symlink() or path.stat().st_size > 1_000_000:
                    raise ValueError("Action output exceeds bounds or is a symlink")
                data = path.read_bytes()
                files[name] = {"sha256": sha256(data), "bytes": len(data)}
            (staging / "manifest.json").write_bytes(
                canonical({"format": 1, "key": key, "files": files})
            )
            atomic_replace(staging, entry, trace, "cache_object_publish")
            elapsed = time.perf_counter_ns() - started
            trace.emit(
                "ACTION_BUILT",
                action=identity,
                key=key,
                duration_ns=elapsed,
                output_bytes=sum(f["bytes"] for f in files.values()),
            )
            return ActionResult(identity, key, files, "BUILT", elapsed)
        except Exception as error:
            partial = (
                [
                    p.relative_to(files_root).as_posix()
                    for p in files_root.rglob("*")
                    if p.is_file()
                ]
                if files_root.exists()
                else []
            )
            trace.emit(
                "ACTION_FAILED",
                action=identity,
                key=key,
                error=str(error),
                staged_files=partial,
                duration_ns=time.perf_counter_ns() - started,
            )
            raise
        finally:
            if staging.exists():
                # This path is an exact mkdtemp result within our owned staging root.
                if not staging.resolve().is_relative_to(
                    (self.cache / "staging").resolve()
                ):
                    raise RuntimeError(
                        "Refusing to remove an out-of-root staging directory"
                    )
                shutil.rmtree(staging)

    def _publish_release(self, results, trace):
        self.output.mkdir(parents=True, exist_ok=True)
        releases = self.output / "releases"
        releases.mkdir(exist_ok=True)
        staged = Path(tempfile.mkdtemp(prefix=".release-stage-", dir=self.output))
        manifest = {
            "format": 1,
            "actions": {
                name: {"key": result.key, "files": result.files}
                for name, result in sorted(results.items())
            },
        }
        artifact_digest = sha256(
            canonical({name: result.files for name, result in sorted(results.items())})
        )
        release = sha256(canonical(manifest))[:16] + "-" + uuid.uuid4().hex[:12]
        try:
            for name, result in sorted(results.items()):
                _, contents = self._read_object(result.key, result.files)
                for filename, data in contents.items():
                    target = staged / "actions" / name / filename
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
            (staged / "release.json").write_bytes(canonical(manifest))
            atomic_replace(
                staged, releases / release, trace, "release_directory_publish"
            )
            pointer = {
                "format": 1,
                "release": release,
                "manifest_sha256": sha256(canonical(manifest)),
                "artifact_digest": artifact_digest,
            }
            descriptor, temp_name = tempfile.mkstemp(
                prefix=".current-", dir=self.output
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(canonical(pointer))
                    stream.flush()
                    os.fsync(stream.fileno())
                atomic_replace(
                    temp_name,
                    self.output / "current.json",
                    trace,
                    "current_pointer_select",
                )
            finally:
                if Path(temp_name).exists():
                    Path(temp_name).unlink()
            trace.emit(
                "RELEASE_PUBLISHED", release=release, artifact_digest=artifact_digest
            )
            return pointer
        finally:
            if staged.exists():
                if not staged.resolve().is_relative_to(self.output):
                    raise RuntimeError("Unexpected release staging path")
                shutil.rmtree(staged)

    def build(self, spec):
        actions, order = validate_graph(spec)
        # Copy through canonical JSON so callers cannot mutate a running graph.
        actions = {
            name: json.loads(canonical(action)) for name, action in actions.items()
        }
        sources = self._snapshot_sources(actions)
        tool = tool_identity()
        trace = Trace()
        results = {}
        failure = None
        pointer = None
        with exclusive(self.cache):
            for name in ["objects", "staging", "quarantine"]:
                (self.cache / name).mkdir(exist_ok=True)
            trace.emit(
                "BUILD_STARTED",
                workers=self.workers,
                actions=len(actions),
                source_bytes=sum(map(len, sources.values())),
            )
            pending = set(order)
            running = {}
            with ThreadPoolExecutor(
                max_workers=self.workers, thread_name_prefix="build-action"
            ) as pool:
                while pending or running:
                    if failure is None:
                        ready = sorted(
                            name
                            for name in pending
                            if all(
                                dependency in results
                                for dependency in actions[name]["deps"]
                            )
                        )
                        for name in ready[: self.workers - len(running)]:
                            pending.remove(name)
                            deps = {dep: results[dep] for dep in actions[name]["deps"]}
                            future = pool.submit(
                                self._execute, actions[name], sources, deps, tool, trace
                            )
                            running[future] = name
                    if not running:
                        break
                    completed, _ = wait(running, return_when=FIRST_COMPLETED)
                    for future in completed:
                        name = running.pop(future)
                        try:
                            results[name] = future.result()
                        except Exception as error:
                            failure = {"action": name, "error": str(error)}
                    # Already-running actions finish and may publish valid cache objects;
                    # a failed build never publishes a new whole-release pointer.
            if failure is None:
                try:
                    pointer = self._publish_release(results, trace)
                except Exception as error:
                    failure = {"action": None, "error": str(error)}
            trace.emit("BUILD_FINISHED", status="FAILED" if failure else "SUCCESS")
        return {
            "status": "FAILED" if failure else "SUCCESS",
            "failure": failure,
            "pointer": pointer,
            "actions": {
                name: {
                    "key": result.key,
                    "files": result.files,
                    "status": result.status,
                    "duration_ns": result.duration_ns,
                }
                for name, result in sorted(results.items())
            },
            "not_completed": sorted(set(actions) - set(results)),
            "tool": tool,
            "duration_ns": trace.events[-1]["time_ns"],
            "events": trace.events,
        }
