#!/usr/bin/env python3
"""Audited file relay between isolated GM and Solver workspaces."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RelayError(ValueError):
    pass


PRIVATE_KEYS = {
    "ground_truth",
    "npc_knowledge",
    "unlock_rules",
    "private",
    "internal_state",
    "final_answer",
}

QUESTION_KEYS = {
    "type",
    "question_id",
    "target_npc",
    "attached_evidence",
    "grounding_note",
    "relation",
    "fact_links",
    "question",
}

PUBLIC_PACKET_KEYS = {
    "type",
    "case_id",
    "response_id",
    "in_reply_to",
    "npc_roster",
    "npc_response",
    "newly_unlocked_evidence",
    "system_notification",
    "public_state",
}


def read_object(data: bytes, filename: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RelayError(f"유효한 UTF-8 JSON이 아닙니다: {filename}: {exc}") from exc
    if not isinstance(value, dict):
        raise RelayError(f"JSON 최상위 값은 객체여야 합니다: {filename}")
    return value


def reject_private_keys(value: Any, location: str = "packet") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in PRIVATE_KEYS:
                raise RelayError(f"비공개 필드가 포함되어 있습니다: {location}.{key}")
            reject_private_keys(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_private_keys(child, f"{location}[{index}]")


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(data)
    os.replace(temp, path)


class FileRelay:
    def __init__(self, relay_dir: Path, gm_dir: Path, solver_dir: Path) -> None:
        self.relay_dir = relay_dir.resolve()
        self.gm_dir = gm_dir.resolve()
        self.solver_dir = solver_dir.resolve()
        self.state_path = self.relay_dir / ".relay_state.json"
        self.log_path = self.relay_dir / "delivery_log.jsonl"

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"version": 1, "delivered": {}, "rejected": {}}
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RelayError(f"relay 상태를 읽을 수 없습니다: {exc}") from exc
        if not isinstance(value, dict):
            raise RelayError("relay 상태 파일 형식이 잘못되었습니다.")
        value.setdefault("delivered", {})
        value.setdefault("rejected", {})
        return value

    def _save_state(self, state: dict[str, Any]) -> None:
        encoded = (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )
        atomic_write(self.state_path, encoded)

    def _archive_path(self, archive_dir: Path, filename: str) -> Path:
        archive_dir.mkdir(parents=True, exist_ok=True)
        first = archive_dir / filename
        if not first.exists():
            return first
        source_name = Path(filename)
        number = 2
        while True:
            candidate = archive_dir / f"{source_name.stem}__{number:03d}{source_name.suffix}"
            if not candidate.exists():
                return candidate
            number += 1

    @staticmethod
    def _validate_question(packet: dict[str, Any], filename: str) -> None:
        if packet.get("type") != "solver_question":
            raise RelayError(f"Solver 질문 패킷이 아닙니다: {filename}")
        if set(packet) != QUESTION_KEYS:
            raise RelayError(f"Solver 질문 필드가 허용 목록과 다릅니다: {filename}")
        reject_private_keys(packet)

    @staticmethod
    def _validate_public_packet(packet: dict[str, Any], filename: str) -> None:
        if packet.get("type") != "gm_public_packet":
            raise RelayError(f"GM 공개 패킷이 아닙니다: {filename}")
        if set(packet) != PUBLIC_PACKET_KEYS:
            raise RelayError(f"GM 공개 패킷 필드가 허용 목록과 다릅니다: {filename}")
        reject_private_keys(packet)

    def _append_log(
        self,
        direction: str,
        source: Path,
        archive: Path,
        destination: Path,
        digest: str,
    ) -> None:
        event = {
            "delivered_at": datetime.now(timezone.utc).isoformat(),
            "direction": direction,
            "source_file": source.name,
            "relay_record": archive.relative_to(self.relay_dir).as_posix(),
            "destination_file": destination.name,
            "sha256": digest,
        }
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _process_file(
        self,
        *,
        direction: str,
        source: Path,
        archive_dir: Path,
        destination_dir: Path,
        validator,
        state: dict[str, Any],
        delivered: list[dict[str, str]],
        rejected: list[dict[str, str]],
    ) -> bool:
        data = source.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        source_stat = source.stat()
        fingerprint = {
            "sha256": digest,
            "mtime_ns": source_stat.st_mtime_ns,
            "file_id": source_stat.st_ino,
        }
        state_key = f"{direction}/{source.name}"
        if state["delivered"].get(state_key) == fingerprint:
            return False
        if state["rejected"].get(state_key) == fingerprint:
            return False
        try:
            packet = read_object(data, source.name)
            validator(packet, source.name)
        except RelayError as exc:
            state["rejected"][state_key] = fingerprint
            rejected.append({"file": source.name, "reason": str(exc)})
            return True

        archive = self._archive_path(archive_dir, source.name)
        destination = destination_dir / source.name
        atomic_write(archive, data)
        atomic_write(destination, data)
        self._append_log(direction, source, archive, destination, digest)
        state["delivered"][state_key] = fingerprint
        state["rejected"].pop(state_key, None)
        delivered.append(
            {
                "direction": direction,
                "file": source.name,
                "relay_record": archive.name,
                "destination": str(destination),
            }
        )
        return True

    def sync_once(self) -> dict[str, list[dict[str, str]]]:
        state = self._load_state()
        delivered: list[dict[str, str]] = []
        rejected: list[dict[str, str]] = []
        changed = False

        solver_outbox = self.solver_dir / "outbox"
        for source in sorted(solver_outbox.glob("question_*.json")):
            changed |= self._process_file(
                direction="solver_to_gm",
                source=source,
                archive_dir=self.relay_dir / "to_gm",
                destination_dir=self.gm_dir / "inbox",
                validator=self._validate_question,
                state=state,
                delivered=delivered,
                rejected=rejected,
            )

        gm_outbox = self.gm_dir / "outbox"
        gm_files = list(gm_outbox.glob("gm_response_*.json"))
        initial = gm_outbox / "initial_packet.json"
        if initial.is_file():
            gm_files.append(initial)
        for source in sorted(
            gm_files,
            key=lambda path: (path.name != "initial_packet.json", path.name),
        ):
            changed |= self._process_file(
                direction="gm_to_solver",
                source=source,
                archive_dir=self.relay_dir / "to_solver",
                destination_dir=self.solver_dir / "inbox",
                validator=self._validate_public_packet,
                state=state,
                delivered=delivered,
                rejected=rejected,
            )

        if changed:
            self._save_state(state)
        return {"delivered": delivered, "rejected": rejected}


def print_result(result: dict[str, list[dict[str, str]]]) -> None:
    for item in result["delivered"]:
        print(
            f"전달 완료: {item['direction']} / {item['file']} "
            f"(relay 기록: {item['relay_record']})"
        )
    for item in result["rejected"]:
        print(f"전달 거부: {item['file']} / {item['reason']}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GM/Solver 공개 파일 자동 relay")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("sync", help="현재 outbox 파일을 한 번 전달")
    watch = subparsers.add_parser("watch", help="outbox를 계속 감시하여 자동 전달")
    watch.add_argument("--interval", type=float, default=1.0, help="감시 간격(초)")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    relay_dir = Path(__file__).resolve().parent
    root = relay_dir.parent
    relay = FileRelay(
        relay_dir,
        root / "ai-detective-gm",
        root / "ai-detective-solver",
    )
    try:
        if args.command == "sync":
            result = relay.sync_once()
            print_result(result)
            if not result["delivered"] and not result["rejected"]:
                print("새로 전달할 파일이 없습니다.")
            return 2 if result["rejected"] else 0
        if args.interval <= 0:
            raise RelayError("감시 간격은 0보다 커야 합니다.")
        managed_marker = relay_dir / ".managed_by_play"
        if managed_marker.exists():
            raise RelayError("play.py가 relay를 관리 중입니다. 별도 watch가 필요하지 않습니다.")
        print("자동 relay를 시작했습니다. 중지하려면 Ctrl+C를 누르세요.")
        while True:
            if managed_marker.exists():
                print("play.py가 relay 관리를 시작하여 별도 watch를 종료합니다.")
                return 0
            print_result(relay.sync_once())
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n자동 relay를 중지했습니다.")
        return 0
    except (OSError, RelayError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
