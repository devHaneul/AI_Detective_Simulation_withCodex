#!/usr/bin/env python3
"""File-based Game Master engine using only the Python standard library."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


class GameError(ValueError):
    """Raised when a public request is malformed or violates the game boundary."""


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GameError(f"JSON을 읽을 수 없습니다: {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise GameError(f"JSON 최상위 값은 객체여야 합니다: {path.name}")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temp, path)


def safe_inbox_file(inbox: Path, filename: str) -> Path:
    if Path(filename).name != filename or filename in {"", ".", ".."}:
        raise GameError("inbox 안의 파일명만 지정할 수 있습니다.")
    candidate = (inbox / filename).resolve()
    if candidate.parent != inbox.resolve():
        raise GameError("inbox 밖의 파일은 읽을 수 없습니다.")
    if not candidate.is_file():
        raise GameError(f"inbox 파일이 없습니다: {filename}")
    return candidate


class GMEngine:
    def __init__(self, case_dir: Path, outbox: Path) -> None:
        self.case_dir = case_dir
        self.private_dir = case_dir / "private"
        self.state_path = case_dir / "state" / "game_state.json"
        self.outbox = outbox
        self.truth = read_json(self.private_dir / "ground_truth.json")
        self.knowledge = read_json(self.private_dir / "npc_knowledge.json")
        self.rules = read_json(self.private_dir / "unlock_rules.json")
        self.catalog = {
            item["id"]: item for item in self.truth.get("evidence_catalog", [])
        }

    def _state(self) -> dict[str, Any]:
        return read_json(self.state_path)

    def _save_state(self, state: dict[str, Any]) -> None:
        write_json(self.state_path, state)

    def _public_evidence(self, evidence_id: str) -> dict[str, Any]:
        item = self.catalog.get(evidence_id)
        if not item:
            raise GameError(f"증거 카탈로그에 없는 ID입니다: {evidence_id}")
        evidence_path = self.private_dir / "evidence" / item["file"]
        return {
            "id": evidence_id,
            "title": item["title"],
            "content": evidence_path.read_text(encoding="utf-8"),
            "public_facts": item["public_facts"],
        }

    def reset(self, export: bool = True) -> dict[str, Any]:
        initial_ids = list(self.truth["initial_evidence"])
        initial_facts: list[str] = []
        for evidence_id in initial_ids:
            initial_facts.extend(
                fact["id"] for fact in self.catalog[evidence_id]["public_facts"]
            )
        state = {
            "case_id": self.truth["case_id"],
            "ground_truth_locked": True,
            "unlocked_evidence": initial_ids,
            "established_facts": initial_facts,
            "npc_interactions": [],
            "questions": [],
            "dead_ends": [],
            "unlock_history": [],
            "next_response_number": 1,
        }
        self._save_state(state)
        self.outbox.mkdir(parents=True, exist_ok=True)
        for generated in self.outbox.glob("*.json"):
            generated.unlink()
        packet = {
            "type": "gm_public_packet",
            "case_id": self.truth["case_id"],
            "response_id": "initial",
            "in_reply_to": None,
            "npc_roster": self.truth["public_npc_roster"],
            "npc_response": None,
            "newly_unlocked_evidence": [
                self._public_evidence(evidence_id) for evidence_id in initial_ids
            ],
            "system_notification": (
                "축하합니다. 모든 증거를 다 풀었습니다. 게임을 자동 종료합니다."
                if set(initial_ids) == set(self.catalog)
                else "수사가 시작되었습니다. 초기 증거가 공개되었습니다."
            ),
            "public_state": {"unlocked_evidence": initial_ids},
        }
        if export:
            write_json(self.outbox / "initial_packet.json", packet)
        return packet

    @staticmethod
    def _question_signature(request: dict[str, Any]) -> str:
        normalized = {
            "target_npc": request.get("target_npc", ""),
            "fact_links": sorted(
                (
                    link.get("evidence_id", ""),
                    link.get("fact_id", ""),
                )
                for link in request.get("fact_links", [])
                if isinstance(link, dict)
            ),
            "relation": request.get("relation", "general"),
            "question": re.sub(r"\s+", "", request.get("question", "")).lower(),
        }
        raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _validate_request(
        self, request: dict[str, Any], state: dict[str, Any]
    ) -> tuple[list[str], list[dict[str, str]], str]:
        if request.get("type") != "solver_question":
            raise GameError("type은 solver_question이어야 합니다.")
        question_id = request.get("question_id")
        if not isinstance(question_id, str) or not question_id.strip():
            raise GameError("question_id가 비어 있습니다.")
        target = request.get("target_npc")
        if target not in self.knowledge.get("npcs", {}):
            raise GameError("등록되지 않은 NPC입니다.")
        question = request.get("question")
        if not isinstance(question, str) or not question.strip():
            raise GameError("질문이 비어 있습니다.")
        grounding_note = request.get("grounding_note")
        if not isinstance(grounding_note, str) or not grounding_note.strip():
            raise GameError("근거 메모가 비어 있습니다.")
        relation = request.get("relation", "general")
        allowed_relations = {
            "amount_mismatch",
            "timeline_contradiction",
            "actor_device_sequence",
            "device_file_sequence",
            "general",
        }
        if relation not in allowed_relations:
            raise GameError("지원하지 않는 증거 관계 유형입니다.")
        evidence_ids = request.get("attached_evidence", [])
        fact_links = request.get("fact_links", [])
        if not isinstance(evidence_ids, list) or not all(
            isinstance(value, str) for value in evidence_ids
        ):
            raise GameError("attached_evidence는 문자열 배열이어야 합니다.")
        if not isinstance(fact_links, list):
            raise GameError("fact_links는 배열이어야 합니다.")
        unlocked = set(state["unlocked_evidence"])
        if any(evidence_id not in unlocked for evidence_id in evidence_ids):
            raise GameError("아직 공개되지 않은 증거가 첨부되었습니다.")

        validated_links: list[dict[str, str]] = []
        for link in fact_links:
            if not isinstance(link, dict):
                raise GameError("fact_links의 각 항목은 객체여야 합니다.")
            evidence_id = link.get("evidence_id")
            fact_id = link.get("fact_id")
            claim = link.get("claim", "")
            if evidence_id not in evidence_ids:
                raise GameError("사실 연결의 증거는 첨부 목록에도 있어야 합니다.")
            public_facts = {
                fact["id"] for fact in self.catalog[evidence_id]["public_facts"]
            }
            if fact_id not in public_facts:
                raise GameError("첨부 증거에서 확인할 수 없는 사실 ID입니다.")
            if not isinstance(claim, str) or not claim.strip():
                raise GameError("각 사실 연결에는 사람이 읽을 수 있는 claim이 필요합니다.")
            validated_links.append(
                {"evidence_id": evidence_id, "fact_id": fact_id, "claim": claim}
            )
        return evidence_ids, validated_links, relation

    def _ordinary_response(
        self, target: str, question: str, has_unresolved_evidence: bool
    ) -> tuple[str, str]:
        npc = self.knowledge["npcs"][target]
        lowered = question.lower()
        for response in npc.get("public_responses", []):
            if any(keyword.lower() in lowered for keyword in response["keywords"]):
                return response["classification"], response["answer"]
        if has_unresolved_evidence:
            return (
                "insufficient_grounding",
                "어떤 기록의 어떤 사실과 제 행동을 연결해서 묻는지 구체적으로 설명해 주세요.",
            )
        return "unknown", npc["unknown_response"]

    def _required_evidence_ids(self, rule: dict[str, Any]) -> list[str]:
        required_fact_ids = set(rule["required_facts"])
        return [
            evidence_id
            for evidence_id, evidence in self.catalog.items()
            if any(
                fact["id"] in required_fact_ids
                for fact in evidence["public_facts"]
            )
        ]

    @staticmethod
    def _question_mentions_evidence_ids(
        question: str,
        evidence_ids: list[str],
    ) -> bool:
        return all(
            re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(evidence_id)}(?![A-Za-z0-9_])",
                question,
                flags=re.IGNORECASE,
            )
            for evidence_id in evidence_ids
        )

    def eligible_rule_ids(self, request: dict[str, Any]) -> list[str]:
        """Return structurally valid unlock rules for the current question."""
        state = self._state()
        _evidence_ids, links, relation = self._validate_request(request, state)
        linked_fact_ids = {link["fact_id"] for link in links}
        target = request["target_npc"]
        return [
            rule["id"]
            for rule in self.rules["rules"]
            if rule["target_npc"] == target
            and rule["relation"] == relation
            and set(rule["required_facts"]).issubset(linked_fact_ids)
            and self._question_mentions_evidence_ids(
                request["question"], self._required_evidence_ids(rule)
            )
            and any(
                evidence_id not in state["unlocked_evidence"]
                for evidence_id in rule["unlocks"]
            )
        ]

    def _rule_selected_by_codex(
        self,
        decision: dict[str, Any],
        request: dict[str, Any],
        state: dict[str, Any],
        linked_fact_ids: set[str],
        relation: str,
    ) -> tuple[dict[str, Any] | None, str, str]:
        required_keys = {
            "question_id",
            "classification",
            "answer",
            "matched_rule_id",
            "rationale",
        }
        if set(decision) != required_keys:
            raise GameError("GM Codex 결정 필드가 올바르지 않습니다.")
        if decision["question_id"] != request.get("question_id"):
            raise GameError("GM Codex 결정의 question_id가 질문과 다릅니다.")

        allowed_classifications = {
            "fact_confirmed",
            "known_general",
            "memory_limitation",
            "unknown",
            "insufficient_grounding",
            "gm_error",
        }
        classification = decision["classification"]
        if classification not in allowed_classifications:
            raise GameError("GM Codex 응답 분류가 올바르지 않습니다.")
        answer = decision["answer"]
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 1200:
            raise GameError("GM Codex 답변은 1~1200자의 문자열이어야 합니다.")
        if not isinstance(decision["rationale"], str):
            raise GameError("GM Codex 판단 근거는 문자열이어야 합니다.")

        rule_id = decision["matched_rule_id"]
        if rule_id is None:
            if classification == "fact_confirmed":
                raise GameError("규칙 없는 응답은 fact_confirmed로 분류할 수 없습니다.")
            return None, classification, answer.strip()
        if not isinstance(rule_id, str):
            raise GameError("matched_rule_id는 문자열 또는 null이어야 합니다.")

        matched_rule = next(
            (rule for rule in self.rules["rules"] if rule["id"] == rule_id), None
        )
        if matched_rule is None:
            raise GameError("GM Codex가 존재하지 않는 해금 규칙을 선택했습니다.")
        if matched_rule["target_npc"] != request["target_npc"]:
            raise GameError("선택한 해금 규칙의 NPC가 질문 대상과 다릅니다.")
        if matched_rule["relation"] != relation:
            raise GameError("선택한 해금 규칙의 관계 유형이 질문과 다릅니다.")
        if not set(matched_rule["required_facts"]).issubset(linked_fact_ids):
            raise GameError("선택한 해금 규칙에 필요한 공개 사실이 부족합니다.")
        required_evidence_ids = self._required_evidence_ids(matched_rule)
        if not self._question_mentions_evidence_ids(
            request["question"], required_evidence_ids
        ):
            raise GameError("질문에 해금 규칙에 필요한 증거 번호가 모두 없습니다.")
        if not any(
            evidence_id not in state["unlocked_evidence"]
            for evidence_id in matched_rule["unlocks"]
        ):
            raise GameError("선택한 해금 규칙은 이미 모두 처리되었습니다.")
        return matched_rule, "fact_confirmed", answer.strip()

    def answer(
        self,
        request: dict[str, Any],
        decision: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self._state()
        evidence_ids, links, relation = self._validate_request(request, state)
        target = request["target_npc"]
        signature = self._question_signature(request)

        previous = next(
            (entry for entry in state["dead_ends"] if entry["signature"] == signature),
            None,
        )
        newly_unlocked: list[str] = []
        established_now: list[str] = []

        linked_fact_ids = {link["fact_id"] for link in links}
        matched_rule = None

        if decision is not None:
            matched_rule, classification, answer = self._rule_selected_by_codex(
                decision, request, state, linked_fact_ids, relation
            )
        elif previous:
            classification = previous["classification"]
            answer = previous["answer"]
        else:
            eligible_rule_ids = set(self.eligible_rule_ids(request))
            for rule in self.rules["rules"]:
                if any(
                    evidence_id not in state["unlocked_evidence"]
                    for evidence_id in rule["unlocks"]
                ) and rule["id"] in eligible_rule_ids:
                    matched_rule = rule
                    break

            if matched_rule is None:
                classification, answer = self._ordinary_response(
                    target, request["question"], bool(evidence_ids)
                )

        if matched_rule:
            if decision is None:
                classification = "fact_confirmed"
                answer = matched_rule["npc_response"]
            for fact_id in matched_rule["establishes_facts"]:
                if fact_id not in state["established_facts"]:
                    state["established_facts"].append(fact_id)
                    established_now.append(fact_id)
            for evidence_id in matched_rule["unlocks"]:
                if evidence_id not in state["unlocked_evidence"]:
                    state["unlocked_evidence"].append(evidence_id)
                    newly_unlocked.append(evidence_id)
                    for fact in self.catalog[evidence_id]["public_facts"]:
                        if fact["id"] not in state["established_facts"]:
                            state["established_facts"].append(fact["id"])
            state["unlock_history"].append(
                {
                    "rule_id": matched_rule["id"],
                    "question_id": request.get("question_id"),
                    "unlocked": newly_unlocked,
                    "decided_by": "codex" if decision is not None else "rules",
                }
            )
        elif decision is not None or previous is None:
            state["dead_ends"].append(
                {
                    "signature": signature,
                    "classification": classification,
                    "answer": answer,
                }
            )

        response_number = state["next_response_number"]
        response_id = f"gm_response_{response_number:03d}"
        state["next_response_number"] += 1
        public_npc_response = {
            "npc": target,
            "answer": answer,
            "classification": classification,
            "established_facts": established_now,
        }
        state["questions"].append(
            {
                "question_id": request.get("question_id"),
                "target_npc": target,
                "attached_evidence": evidence_ids,
                "fact_links": links,
                "relation": relation,
                "question": request["question"],
                "classification": classification,
                "decided_by": "codex" if decision is not None else "rules",
                "matched_rule_id": matched_rule["id"] if matched_rule else None,
            }
        )
        state["npc_interactions"].append(
            {"npc": target, "question_id": request.get("question_id"), "response_id": response_id}
        )
        self._save_state(state)

        all_evidence_unlocked = set(state["unlocked_evidence"]) == set(self.catalog)
        packet = {
            "type": "gm_public_packet",
            "case_id": state["case_id"],
            "response_id": response_id,
            "in_reply_to": request.get("question_id"),
            "npc_roster": [],
            "npc_response": public_npc_response,
            "newly_unlocked_evidence": [
                self._public_evidence(evidence_id) for evidence_id in newly_unlocked
            ],
            "system_notification": (
                "축하합니다. 모든 증거를 다 풀었습니다. 게임을 자동 종료합니다."
                if all_evidence_unlocked and newly_unlocked
                else f"새 증거 {', '.join(newly_unlocked)}가 공개되었습니다."
                if newly_unlocked
                else "새로 공개된 증거가 없습니다."
            ),
            "public_state": {"unlocked_evidence": state["unlocked_evidence"]},
        }
        write_json(self.outbox / f"{response_id}.json", packet)
        return packet

    def is_complete(self) -> bool:
        state = self._state()
        return set(state["unlocked_evidence"]) == set(self.catalog)

    def public_status(self) -> dict[str, Any]:
        state = self._state()
        return {
            "case_id": state["case_id"],
            "unlocked_evidence": state["unlocked_evidence"],
            "question_count": len(state["questions"]),
            "unlock_count": len(state["unlock_history"]),
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI 탐정 Game Master CLI")
    parser.add_argument("--case", default="case_001", help="cases 아래 사건 디렉터리명")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("reset", help="상태 초기화 및 초기 공개 패킷 생성")
    answer_parser = subparsers.add_parser("answer", help="inbox의 Solver 질문 처리")
    answer_parser.add_argument("filename", help="inbox 안의 질문 JSON 파일명")
    subparsers.add_parser("status", help="민감정보 없는 진행 상태 표시")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    base = Path(__file__).resolve().parent
    engine = GMEngine(base / "cases" / args.case, base / "outbox")
    try:
        if args.command == "reset":
            packet = engine.reset()
            print(
                "초기화 완료:",
                ", ".join(packet["public_state"]["unlocked_evidence"]),
            )
            print("생성: outbox/initial_packet.json")
        elif args.command == "answer":
            request_path = safe_inbox_file(base / "inbox", args.filename)
            packet = engine.answer(read_json(request_path))
            print(packet["npc_response"]["answer"])
            print("생성:", f"outbox/{packet['response_id']}.json")
        else:
            print(json.dumps(engine.public_status(), ensure_ascii=False, indent=2))
    except GameError as exc:
        print(f"오류: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
