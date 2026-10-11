#!/usr/bin/env python3
"""Public-only Solver workspace CLI using the Python standard library."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any


class SolverError(ValueError):
    pass


FORBIDDEN_KEYS = {
    "ground_truth",
    "npc_knowledge",
    "unlock_rules",
    "private",
    "internal_state",
    "final_answer",
}


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SolverError(f"JSON을 읽을 수 없습니다: {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise SolverError("JSON 최상위 값은 객체여야 합니다.")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temp, path)


def safe_inbox_file(inbox: Path, filename: str) -> Path:
    if Path(filename).name != filename or filename in {"", ".", ".."}:
        raise SolverError("inbox 안의 파일명만 지정할 수 있습니다.")
    candidate = (inbox / filename).resolve()
    if candidate.parent != inbox.resolve() or not candidate.is_file():
        raise SolverError(f"inbox 파일이 없습니다: {filename}")
    return candidate


def reject_private_keys(value: Any, location: str = "packet") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise SolverError(f"비공개 필드가 포함된 패킷을 거부했습니다: {location}.{key}")
            reject_private_keys(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_private_keys(child, f"{location}[{index}]")


class SolverWorkspace:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.evidence_dir = base / "evidence"
        self.responses_dir = base / "npc_responses"
        self.outbox = base / "outbox"
        self.index_path = self.evidence_dir / "evidence_index.json"
        self.investigation_path = base / "investigation.md"

    def _index(self) -> dict[str, Any]:
        if self.index_path.exists():
            return read_json(self.index_path)
        return {"evidence": {}, "npc_roster": []}

    def ingest(self, packet: dict[str, Any]) -> list[str]:
        reject_private_keys(packet)
        allowed_packet_keys = {
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
        if packet.get("type") != "gm_public_packet":
            raise SolverError("GM 공개 패킷 형식이 아닙니다.")
        extra = set(packet) - allowed_packet_keys
        if extra:
            raise SolverError(f"허용되지 않은 최상위 필드: {sorted(extra)}")

        index = self._index()
        roster = packet.get("npc_roster", [])
        if roster:
            index["npc_roster"] = roster
        imported: list[str] = []
        for evidence in packet.get("newly_unlocked_evidence", []):
            if not isinstance(evidence, dict) or set(evidence) != {
                "id",
                "title",
                "content",
                "public_facts",
            }:
                raise SolverError("공개 증거 필드가 올바르지 않습니다.")
            evidence_id = evidence["id"]
            if not isinstance(evidence_id, str) or not re.fullmatch(r"E\d{2}", evidence_id):
                raise SolverError("증거 ID 형식이 올바르지 않습니다.")
            facts = evidence["public_facts"]
            if not isinstance(facts, list) or not all(
                isinstance(fact, dict) and set(fact) == {"id", "statement"}
                for fact in facts
            ):
                raise SolverError("공개 사실 형식이 올바르지 않습니다.")
            content = evidence["content"]
            if not isinstance(content, str):
                raise SolverError("증거 본문은 문자열이어야 합니다.")
            fact_block = "\n".join(
                f"- `{fact['id']}`: {fact['statement']}" for fact in facts
            )
            rendered = f"{content.rstrip()}\n\n## 공개 사실 ID\n\n{fact_block}\n"
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            (self.evidence_dir / f"{evidence_id}.md").write_text(
                rendered, encoding="utf-8"
            )
            index["evidence"][evidence_id] = {
                "title": evidence["title"],
                "public_facts": facts,
            }
            imported.append(evidence_id)
        write_json(self.index_path, index)

        npc_response = packet.get("npc_response")
        if npc_response is not None:
            allowed_response_keys = {
                "npc",
                "answer",
                "classification",
                "established_facts",
            }
            if not isinstance(npc_response, dict) or set(npc_response) != allowed_response_keys:
                raise SolverError("NPC 공개 응답 필드가 올바르지 않습니다.")
            self.responses_dir.mkdir(parents=True, exist_ok=True)
            write_json(
                self.responses_dir / f"{packet['response_id']}.json",
                {
                    "response_id": packet["response_id"],
                    "in_reply_to": packet.get("in_reply_to"),
                    "npc_response": npc_response,
                    "newly_unlocked_evidence": [
                        item["id"]
                        for item in packet.get("newly_unlocked_evidence", [])
                    ],
                    "system_notification": packet.get("system_notification", ""),
                },
            )

        log = (
            f"- `{packet['response_id']}` 수신: "
            f"{packet.get('system_notification', '')}"
        )
        if imported:
            log += f" (증거: {', '.join(imported)})"
        with self.investigation_path.open("a", encoding="utf-8") as handle:
            handle.write(log + "\n")
        return imported

    def make_question(
        self,
        target: str,
        evidence_ids: list[str],
        fact_ids: list[str],
        relation: str,
        note: str,
        question: str,
    ) -> Path:
        index = self._index()
        if not target.strip() or not note.strip() or not question.strip():
            raise SolverError("대상자, 근거 메모, 질문은 비워둘 수 없습니다.")
        known_evidence = index["evidence"]
        if any(evidence_id not in known_evidence for evidence_id in evidence_ids):
            raise SolverError("공개되지 않은 증거가 첨부되었습니다.")
        fact_lookup: dict[str, tuple[str, str]] = {}
        for evidence_id in evidence_ids:
            for fact in known_evidence[evidence_id]["public_facts"]:
                fact_lookup[fact["id"]] = (evidence_id, fact["statement"])
        missing = [fact_id for fact_id in fact_ids if fact_id not in fact_lookup]
        if missing:
            raise SolverError(f"첨부 증거에서 찾을 수 없는 사실 ID: {missing}")

        self.outbox.mkdir(parents=True, exist_ok=True)
        existing_numbers = []
        for path in self.outbox.glob("question_*.json"):
            match = re.fullmatch(r"question_(\d+)\.json", path.name)
            if match:
                existing_numbers.append(int(match.group(1)))
        number = max(existing_numbers, default=0) + 1
        question_id = f"question_{number:03d}"
        packet = {
            "type": "solver_question",
            "question_id": question_id,
            "target_npc": target,
            "attached_evidence": evidence_ids,
            "grounding_note": note,
            "relation": relation,
            "fact_links": [
                {
                    "evidence_id": fact_lookup[fact_id][0],
                    "fact_id": fact_id,
                    "claim": fact_lookup[fact_id][1],
                }
                for fact_id in fact_ids
            ],
            "question": question,
        }
        output = self.outbox / f"{question_id}.json"
        write_json(output, packet)
        return output

    def ask(
        self,
        target: str,
        evidence_ids: list[str],
        fact_ids: list[str],
        relation: str,
        note: str,
        question: str,
        timeout: float,
    ) -> dict[str, Any]:
        output = self.make_question(
            target, evidence_ids, fact_ids, relation, note, question
        )
        question_id = output.stem
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for response_path in sorted(self.responses_dir.glob("gm_response_*.json")):
                response = read_json(response_path)
                if response.get("in_reply_to") == question_id:
                    return response
            time.sleep(0.2)
        raise SolverError(
            f"GM 응답 대기 시간이 초과되었습니다 ({timeout:g}초). "
            "play.py로 게임을 시작했는지 확인하세요."
        )

    def reset_public(self) -> None:
        for directory, pattern in (
            (self.evidence_dir, "*.md"),
            (self.responses_dir, "*.json"),
            (self.base / "inbox", "*.json"),
            (self.outbox, "question_*.json"),
        ):
            directory.mkdir(parents=True, exist_ok=True)
            for path in directory.glob(pattern):
                path.unlink()
        if self.index_path.exists():
            self.index_path.unlink()
        self.investigation_path.write_text(
            "# Investigation\n\n"
            "## Evidence Matrix\n\n"
            "| Evidence | Person | Time | Amount | File | Action | Source | Metadata |\n"
            "|---|---|---|---:|---|---|---|---|\n\n"
            "## Confirmed Facts\n\n"
            "## Inferences\n\n"
            "## Hypotheses\n\n"
            "## Contradictions\n\n"
            "## Unknowns\n\n"
            "## NPC Knowledge Map\n\n"
            "## Dead Ends\n\n"
            "## Next Highest-Value Question\n\n"
            "## Import Log\n\n",
            encoding="utf-8",
        )

    def status(self) -> dict[str, Any]:
        index = self._index()
        response_count = len(list(self.responses_dir.glob("gm_response_*.json")))
        return {
            "evidence": sorted(index["evidence"]),
            "npc_roster": index.get("npc_roster", []),
            "npc_response_count": response_count,
        }


def render_gm_response(
    workspace: SolverWorkspace,
    response: dict[str, Any],
) -> str:
    npc_response = response.get("npc_response", {})
    lines = [
        "=== GM / NPC 답변 ===",
        f"NPC: {npc_response.get('npc', '알 수 없음')}",
        f"답변: {npc_response.get('answer', '')}",
        f"판정: {npc_response.get('classification', 'unknown')}",
    ]
    notification = response.get("system_notification")
    if notification:
        lines.append(f"알림: {notification}")

    evidence_ids = response.get("newly_unlocked_evidence", [])
    if not evidence_ids:
        lines.extend(["", "=== 새 증거 ===", "없음"])
        return "\n".join(lines)

    index = workspace._index().get("evidence", {})
    lines.extend(["", "=== 새 증거 공개 ==="])
    for evidence_id in evidence_ids:
        evidence_path = workspace.evidence_dir / f"{evidence_id}.md"
        title = index.get(evidence_id, {}).get("title", evidence_id)
        lines.append(f"\n[{evidence_id}] {title}")
        if evidence_path.is_file():
            lines.append(evidence_path.read_text(encoding="utf-8").rstrip())
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI 탐정 Solver CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    ingest = subparsers.add_parser("ingest", help="inbox의 GM 공개 패킷 수신")
    ingest.add_argument("filename", help="inbox 안의 JSON 파일명")
    question = subparsers.add_parser("question", help="근거 연결형 질문 생성")
    question.add_argument("--target", required=True)
    question.add_argument("--evidence", nargs="*", default=[])
    question.add_argument("--facts", nargs="*", default=[])
    question.add_argument(
        "--relation",
        required=True,
        choices=[
            "amount_mismatch",
            "timeline_contradiction",
            "actor_device_sequence",
            "device_file_sequence",
            "general",
        ],
    )
    question.add_argument("--note", required=True)
    question.add_argument("--question", required=True)
    ask = subparsers.add_parser(
        "ask", help="질문 생성 후 자동 GM 응답을 기다림(play.py 실행 필요)"
    )
    ask.add_argument("--target", required=True)
    ask.add_argument("--evidence", nargs="*", default=[])
    ask.add_argument("--facts", nargs="*", default=[])
    ask.add_argument(
        "--relation",
        required=True,
        choices=[
            "amount_mismatch",
            "timeline_contradiction",
            "actor_device_sequence",
            "device_file_sequence",
            "general",
        ],
    )
    ask.add_argument("--note", required=True)
    ask.add_argument("--question", required=True)
    ask.add_argument("--timeout", type=float, default=300.0)
    subparsers.add_parser("status", help="현재 공개 자료 표시")
    return parser


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args()
    base = Path(__file__).resolve().parent
    workspace = SolverWorkspace(base)
    try:
        if args.command == "ingest":
            path = safe_inbox_file(base / "inbox", args.filename)
            imported = workspace.ingest(read_json(path))
            print("수신 완료", f"(새 증거: {', '.join(imported)})" if imported else "")
        elif args.command == "question":
            output = workspace.make_question(
                args.target,
                args.evidence,
                args.facts,
                args.relation,
                args.note,
                args.question,
            )
            print("생성:", output.relative_to(base))
        elif args.command == "ask":
            print("=== Solver 질문 ===")
            print(f"대상: {args.target}")
            print(f"질문: {args.question}")
            print(f"근거: {args.note}")
            print("GM 답변을 기다리는 중...", flush=True)
            response = workspace.ask(
                args.target,
                args.evidence,
                args.facts,
                args.relation,
                args.note,
                args.question,
                args.timeout,
            )
            print()
            print(render_gm_response(workspace, response))
        else:
            print(json.dumps(workspace.status(), ensure_ascii=False, indent=2))
    except SolverError as exc:
        print(f"오류: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
