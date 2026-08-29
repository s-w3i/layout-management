"""Optional local C++ accelerator for deterministic DRAM conflict checks."""

from __future__ import annotations

import ctypes
import fcntl
import hashlib
import os
import platform
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "native" / "coordination.cpp"
CACHE = ROOT / ".cache" / "native"


@dataclass(frozen=True, slots=True)
class NativeConflict:
    safe_count: int
    kind: str | None
    overlap_node: int | None
    winner: int | None
    participants: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class BackendInfo:
    requested: str
    selected: str
    build_hash: str = ""
    compile_seconds: float = 0.0
    fallback_reason: str = ""


def _compiler_identity() -> str:
    completed = subprocess.run(
        ("g++", "--version"), capture_output=True, text=True, check=True
    )
    return completed.stdout.splitlines()[0]


def _build() -> tuple[Path, str, float]:
    identity = _compiler_identity()
    digest = hashlib.sha256()
    digest.update(SOURCE.read_bytes())
    digest.update(identity.encode())
    digest.update(platform.platform().encode())
    build_hash = digest.hexdigest()[:16]
    CACHE.mkdir(parents=True, exist_ok=True)
    target = CACHE / f"coordination-{build_hash}.so"
    if target.exists():
        return target, build_hash, 0.0
    lock_path = CACHE / "build.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.exists():
            return target, build_hash, 0.0
        started = time.monotonic()
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="coordination-", suffix=".so", dir=CACHE
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            subprocess.run(
                (
                    "g++", "-std=c++17", "-O3", "-DNDEBUG", "-shared", "-fPIC",
                    str(SOURCE), "-o", str(temporary),
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target, build_hash, time.monotonic() - started


class NativeKernel:
    def __init__(self, library: Path, agent_count: int):
        self.library = ctypes.CDLL(str(library))
        value = self.library
        value.amr_coord_create.argtypes = (ctypes.c_int,)
        value.amr_coord_create.restype = ctypes.c_void_p
        value.amr_coord_destroy.argtypes = (ctypes.c_void_p,)
        value.amr_coord_set_path.argtypes = (
            ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_int,
        )
        value.amr_coord_set_path.restype = ctypes.c_int
        value.amr_coord_remove.argtypes = (ctypes.c_void_p, ctypes.c_int)
        value.amr_coord_following.argtypes = (
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        )
        value.amr_coord_following.restype = ctypes.c_int
        value.amr_coord_reversed_passage.argtypes = (
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_int), ctypes.c_int,
        )
        value.amr_coord_reversed_passage.restype = ctypes.c_int
        value.amr_coord_check_prefix.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint64),
        )
        value.amr_coord_check_prefix.restype = ctypes.c_int
        self.agent_count = agent_count
        self.handle = value.amr_coord_create(agent_count)
        if not self.handle:
            raise RuntimeError("native coordination supports 1 to 64 AMRs")

    def close(self) -> None:
        if self.handle:
            self.library.amr_coord_destroy(self.handle)
            self.handle = None

    def __del__(self):
        self.close()

    def set_path(self, agent: int, nodes: tuple[int, ...]) -> None:
        values = (ctypes.c_int * len(nodes))(*nodes)
        if not self.library.amr_coord_set_path(
            self.handle, agent, values, len(nodes)
        ):
            raise RuntimeError("native coordination rejected an AMR path")

    def remove(self, agent: int) -> None:
        self.library.amr_coord_remove(self.handle, agent)

    def following(self, follower: int, leader: int, blocked: int) -> bool:
        return bool(self.library.amr_coord_following(
            self.handle, follower, leader, blocked
        ))

    def reversed_passage(self, first: int, second: int) -> tuple[int, ...]:
        capacity = 4096
        output = (ctypes.c_int * capacity)()
        count = self.library.amr_coord_reversed_passage(
            self.handle, first, second, output, capacity
        )
        if count < 0:
            raise RuntimeError("native reversed passage exceeded its output capacity")
        return tuple(output[index] for index in range(count))

    def check_prefix(
        self, agent: int, candidates: tuple[int, ...], priority_ranks: tuple[int, ...]
    ) -> NativeConflict:
        candidate_values = (ctypes.c_int * len(candidates))(*candidates)
        rank_values = (ctypes.c_int * len(priority_ranks))(*priority_ranks)
        kind, overlap, winner = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        participants = ctypes.c_uint64()
        safe = self.library.amr_coord_check_prefix(
            self.handle,
            agent,
            candidate_values,
            len(candidates),
            rank_values,
            ctypes.byref(kind),
            ctypes.byref(overlap),
            ctypes.byref(winner),
            ctypes.byref(participants),
        )
        if safe < 0:
            raise RuntimeError("native coordination state is inconsistent")
        names = {1: "head_to_head", 2: "cycle"}
        return NativeConflict(
            safe,
            names.get(kind.value),
            overlap.value if kind.value else None,
            winner.value if winner.value >= 0 else None,
            tuple(
                index
                for index in range(self.agent_count)
                if participants.value & (1 << index)
            ),
        )


def load_native(agent_count: int, requested: str = "auto") -> tuple[NativeKernel | None, BackendInfo]:
    if requested == "python":
        return None, BackendInfo(requested, "python")
    try:
        library, build_hash, compile_seconds = _build()
        return (
            NativeKernel(library, agent_count),
            BackendInfo(requested, "native", build_hash, compile_seconds),
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        if requested == "native":
            raise RuntimeError(f"native coordination unavailable: {exc}") from exc
        return None, BackendInfo(requested, "python", fallback_reason=str(exc))
