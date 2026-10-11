from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"모듈을 불러올 수 없습니다: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gm_module = load_module("gm_module", ROOT / "ai-detective-gm" / "gm.py")
solver_module = load_module(
    "solver_module", ROOT / "ai-detective-solver" / "solver.py"
)
relay_module = load_module("relay_module", ROOT / "ai-detective-relay" / "relay.py")
play_module = load_module("play_module", ROOT / "play.py")
gm_codex_module = load_module(
    "gm_codex_module", ROOT / "ai-detective-gm" / "gm_codex.py"
)


class FakeGMJudge:
    def reset(self) -> None:
        return None

    def decide(self, question_path: Path, candidate_rule_ids: list[str]) -> dict:
        request = json.loads(question_path.read_text(encoding="utf-8"))
        if "U01_AMOUNT_GAP" not in candidate_rule_ids:
            raise AssertionError("U01_AMOUNT_GAP must be an eligible rule")
        return {
            "question_id": request["question_id"],
            "classification": "fact_confirmed",
            "answer": "두 금액 사이에 300,000원의 차이가 있다는 점을 인정합니다.",
            "matched_rule_id": "U01_AMOUNT_GAP",
            "rationale": "두 공개 금액 사실을 질문이 직접 연결했다.",
        }


class InvalidGMJudge(FakeGMJudge):
    def __init__(self) -> None:
        self.calls = 0

    def decide(self, question_path: Path, candidate_rule_ids: list[str]) -> dict:
        self.calls += 1
        decision = super().decide(question_path, candidate_rule_ids)
        decision["matched_rule_id"] = "U04_FILE_COPY"
        return decision


class GameMechanismTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        temp_root = Path(self.temp_dir.name)
        self.case_dir = temp_root / "case_001"
        shutil.copytree(
            ROOT / "ai-detective-gm" / "cases" / "case_001", self.case_dir
        )
        self.engine = gm_module.GMEngine(self.case_dir, temp_root / "outbox")
        self.engine.reset()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def question(
        question_id: str,
        target: str,
        evidence: list[str],
        facts: list[tuple[str, str]],
        text: str,
        relation: str = "general",
    ) -> dict:
        return {
            "type": "solver_question",
            "question_id": question_id,
            "target_npc": target,
            "attached_evidence": evidence,
            "grounding_note": "테스트 근거 메모",
            "relation": relation,
            "fact_links": [
                {"evidence_id": evidence_id, "fact_id": fact_id, "claim": fact_id}
                for evidence_id, fact_id in facts
            ],
            "question": text,
        }

    def test_01_solver_has_no_ground_truth_file(self) -> None:
        solver_root = ROOT / "ai-detective-solver"
        names = {path.name.lower() for path in solver_root.rglob("*") if path.is_file()}
        self.assertNotIn("ground_truth.json", names)

    def test_02_solver_has_no_npc_knowledge_file(self) -> None:
        solver_root = ROOT / "ai-detective-solver"
        names = {path.name.lower() for path in solver_root.rglob("*") if path.is_file()}
        self.assertNotIn("npc_knowledge.json", names)

    def test_03_only_e01_and_e02_are_initially_public(self) -> None:
        packet = self.engine.reset()
        public_ids = [item["id"] for item in packet["newly_unlocked_evidence"]]
        self.assertEqual(["E01", "E02"], public_ids)
        self.assertEqual(["E01", "E02"], packet["public_state"]["unlocked_evidence"])

    def test_04_bad_question_does_not_unlock_evidence(self) -> None:
        request = self.question(
            "q_bad",
            "마진우",
            ["E01", "E02"],
            [
                ("E01", "F_E01_ACTUAL_150K"),
                ("E02", "F_E02_CLAIM_450K"),
            ],
            "두 기록에 같은 사람이 등장하니 출입 시각을 설명해 주세요.",
            "timeline_contradiction",
        )
        packet = self.engine.answer(request)
        self.assertEqual([], packet["newly_unlocked_evidence"])
        self.assertEqual("insufficient_grounding", packet["npc_response"]["classification"])
        self.assertEqual(["E01", "E02"], packet["public_state"]["unlocked_evidence"])

    def test_05_valid_connections_unlock_following_evidence(self) -> None:
        first = self.question(
            "q_amount",
            "마진우",
            ["E01", "E02"],
            [
                ("E01", "F_E01_ACTUAL_150K"),
                ("E02", "F_E02_CLAIM_450K"),
            ],
            "E01의 실결제액은 15만 원인데 E02의 청구액은 45만 원입니다. 이 30만 원 차액은 무엇입니까?",
            "amount_mismatch",
        )
        first_packet = self.engine.answer(first)
        self.assertEqual(
            ["E03"],
            [item["id"] for item in first_packet["newly_unlocked_evidence"]],
        )

        second = self.question(
            "q_reentry",
            "마진우",
            ["E02", "E03"],
            [
                ("E02", "F_E02_LEFT_BEFORE_21"),
                ("E03", "F_E03_CARD_2132"),
            ],
            "E02에서는 21시 전에 퇴근했다고 했지만 E03에는 21:32 카드 사용이 있습니다. 다시 돌아왔습니까?",
            "timeline_contradiction",
        )
        second_packet = self.engine.answer(second)
        self.assertEqual(
            ["E04"],
            [item["id"] for item in second_packet["newly_unlocked_evidence"]],
        )

        third = self.question(
            "q_device",
            "마진우",
            ["E03", "E04"],
            [
                ("E03", "F_E03_CARD_2132"),
                ("E04", "F_E04_USB_2136"),
            ],
            "E03의 21:32 재입실 직후 E04에서 같은 사용자 세션 PC에 SSD가 연결됐습니다. 직접 연결했습니까?",
            "actor_device_sequence",
        )
        third_packet = self.engine.answer(third)
        self.assertEqual(
            ["E05"],
            [item["id"] for item in third_packet["newly_unlocked_evidence"]],
        )

        fourth = self.question(
            "q_copy",
            "마진우",
            ["E04", "E05"],
            [
                ("E04", "F_E04_USB_2136"),
                ("E05", "F_E05_DATASET_COPY"),
            ],
            "E04에 연결된 SSD와 E05의 파일 복사 대상 일련번호가 같습니다. 결선 데이터를 복사했습니까?",
            "device_file_sequence",
        )
        fourth_packet = self.engine.answer(fourth)
        self.assertEqual(
            ["E06"],
            [item["id"] for item in fourth_packet["newly_unlocked_evidence"]],
        )
        self.assertEqual(
            ["E01", "E02", "E03", "E04", "E05", "E06"],
            fourth_packet["public_state"]["unlocked_evidence"],
        )
        self.assertEqual(
            "축하합니다. 모든 증거를 다 풀었습니다. 게임을 자동 종료합니다.",
            fourth_packet["system_notification"],
        )
        self.assertTrue(self.engine.is_complete())

        after_completion = self.question(
            "q_after_completion",
            "김민수",
            [],
            [],
            "추가로 확인된 내용이 있습니까?",
        )
        after_completion_packet = self.engine.answer(after_completion)
        self.assertEqual(
            "새로 공개된 증거가 없습니다.",
            after_completion_packet["system_notification"],
        )

    def test_06_unknown_fact_is_not_invented(self) -> None:
        before = self.engine._state()["established_facts"].copy()
        request = self.question(
            "q_filename",
            "장예원",
            [],
            [],
            "마진우가 작성한 정산표의 정확한 파일명이 무엇입니까?",
        )
        packet = self.engine.answer(request)
        after = self.engine._state()["established_facts"]
        self.assertEqual("memory_limitation", packet["npc_response"]["classification"])
        self.assertIn("기억나지 않습니다", packet["npc_response"]["answer"])
        self.assertEqual([], packet["npc_response"]["established_facts"])
        self.assertEqual(before, after)

    def test_07_repeated_dead_end_leaks_nothing_new(self) -> None:
        request = self.question(
            "q_repeat_1",
            "김민수",
            [],
            [],
            "정산표 차액을 어디에 썼습니까?",
        )
        first = self.engine.answer(request)
        request["question_id"] = "q_repeat_2"
        second = self.engine.answer(request)
        self.assertEqual(first["npc_response"]["answer"], second["npc_response"]["answer"])
        self.assertEqual([], first["newly_unlocked_evidence"])
        self.assertEqual([], second["newly_unlocked_evidence"])
        self.assertEqual(["E01", "E02"], second["public_state"]["unlocked_evidence"])

    def test_08_codex_decision_is_revalidated_before_unlock(self) -> None:
        request = self.question(
            "q_invalid_codex",
            "마진우",
            ["E01"],
            [("E01", "F_E01_ACTUAL_150K")],
            "청구 금액에 관해 답해 주세요.",
            "amount_mismatch",
        )
        decision = {
            "question_id": "q_invalid_codex",
            "classification": "fact_confirmed",
            "answer": "차액을 인정합니다.",
            "matched_rule_id": "U01_AMOUNT_GAP",
            "rationale": "테스트",
        }
        with self.assertRaises(gm_module.GameError):
            self.engine.answer(request, decision=decision)
        self.assertEqual(
            ["E01", "E02"], self.engine._state()["unlocked_evidence"]
        )

    def test_09_valid_codex_decision_unlocks_only_rule_evidence(self) -> None:
        request = self.question(
            "q_valid_codex",
            "마진우",
            ["E01", "E02"],
            [
                ("E01", "F_E01_ACTUAL_150K"),
                ("E02", "F_E02_CLAIM_450K"),
            ],
            "E01의 실결제액과 E02의 청구액 차이를 설명해 주세요.",
            "amount_mismatch",
        )
        decision = {
            "question_id": "q_valid_codex",
            "classification": "fact_confirmed",
            "answer": "두 금액 사이의 차이가 있다는 점을 인정합니다.",
            "matched_rule_id": "U01_AMOUNT_GAP",
            "rationale": "두 공개 사실을 질문이 직접 연결했다.",
        }
        packet = self.engine.answer(request, decision=decision)
        self.assertEqual(
            ["E03"],
            [item["id"] for item in packet["newly_unlocked_evidence"]],
        )
        self.assertEqual("codex", self.engine._state()["unlock_history"][0]["decided_by"])

    def test_10_only_structurally_eligible_rules_are_candidates(self) -> None:
        valid = self.question(
            "q_candidate",
            "마진우",
            ["E01", "E02"],
            [
                ("E01", "F_E01_ACTUAL_150K"),
                ("E02", "F_E02_CLAIM_450K"),
            ],
            "E01의 실결제액과 E02의 청구액 차이를 설명해 주세요.",
            "amount_mismatch",
        )
        self.assertEqual(["U01_AMOUNT_GAP"], self.engine.eligible_rule_ids(valid))

        without_evidence_numbers = self.question(
            "q_without_numbers",
            "마진우",
            ["E01", "E02"],
            [
                ("E01", "F_E01_ACTUAL_150K"),
                ("E02", "F_E02_CLAIM_450K"),
            ],
            "실결제액 15만 원과 청구액 45만 원의 차이를 설명해 주세요.",
            "amount_mismatch",
        )
        self.assertEqual(
            [], self.engine.eligible_rule_ids(without_evidence_numbers)
        )

        with_only_one_evidence_number = self.question(
            "q_one_number",
            "마진우",
            ["E01", "E02"],
            [
                ("E01", "F_E01_ACTUAL_150K"),
                ("E02", "F_E02_CLAIM_450K"),
            ],
            "E01의 실결제액 15만 원과 청구액 45만 원의 차이를 설명해 주세요.",
            "amount_mismatch",
        )
        self.assertEqual(
            [], self.engine.eligible_rule_ids(with_only_one_evidence_number)
        )

        missing_fact = self.question(
            "q_missing",
            "마진우",
            ["E01"],
            [("E01", "F_E01_ACTUAL_150K")],
            "청구 금액을 설명해 주세요.",
            "amount_mismatch",
        )
        self.assertEqual([], self.engine.eligible_rule_ids(missing_fact))

        self.engine.answer(valid)
        self.assertEqual([], self.engine.eligible_rule_ids(valid))


class BoundaryValidationTests(unittest.TestCase):
    def test_start_briefing_shows_situation_roster_and_full_evidence(self) -> None:
        rendered = play_module.render_game_briefing(
            {
                "id": "case_test",
                "title": "테스트 사건",
                "briefing": "공개 사건 상황입니다.",
            },
            {
                "npc_roster": [{"name": "홍길동", "role": "목격자"}],
                "newly_unlocked_evidence": [
                    {
                        "id": "E01",
                        "title": "첫 번째 기록",
                        "content": "# E01 — 첫 번째 기록\n\n증거 본문입니다.\n",
                        "public_facts": [
                            {"id": "F_E01_TEST", "statement": "공개 사실입니다."}
                        ],
                    }
                ],
                "public_state": {"unlocked_evidence": ["E01"]},
            },
            "gpt-5.6-terra",
        )
        self.assertIn("사건 상황", rendered)
        self.assertIn("공개 사건 상황입니다.", rendered)
        self.assertIn("홍길동 — 목격자", rendered)
        self.assertIn("현재 보유 증거 · 1건", rendered)
        self.assertIn("[E01] 첫 번째 기록", rendered)
        self.assertIn("증거 본문입니다.", rendered)
        self.assertIn("[F_E01_TEST] 공개 사실입니다.", rendered)

    def test_solver_rejects_private_fields_in_packet(self) -> None:
        with self.assertRaises(solver_module.SolverError):
            solver_module.reject_private_keys({"ground_truth": {"culprit": "x"}})

    def test_controller_process_check_recognizes_current_process(self) -> None:
        self.assertTrue(play_module.process_is_running(os.getpid()))

    def test_solver_codex_owner_is_found_in_parent_chain(self) -> None:
        snapshot = {
            40: (30, "python.exe"),
            30: (20, "pwsh.exe"),
            20: (10, "codex.exe"),
            10: (1, "explorer.exe"),
        }
        self.assertEqual(
            20,
            play_module.find_solver_codex_pid(start_pid=40, snapshot=snapshot),
        )

    def test_gm_codex_uses_no_daemon_as_global_option(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            judge = gm_codex_module.CodexGMJudge(
                root, root / "case", executable="codex"
            )
            command = judge._command("prompt", root / "output.json", None)
            self.assertEqual(["codex", "--no-daemon", "exec"], command[:3])
            model_index = command.index("--model")
            self.assertEqual("gpt-5.6-terra", command[model_index + 1])

    def test_candidate_schema_only_allows_prefiltered_rules_or_null(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            case_dir = root / "case"
            judge = gm_codex_module.CodexGMJudge(
                ROOT / "ai-detective-gm",
                case_dir,
                executable="codex",
            )
            judge.decisions_dir = root / "decisions"
            judge.decisions_dir.mkdir()
            schema_path = judge._write_candidate_schema(
                "question_001", ["U01_AMOUNT_GAP"]
            )
            schema = gm_codex_module.read_json(schema_path)
            self.assertEqual(
                ["U01_AMOUNT_GAP", None],
                schema["properties"]["matched_rule_id"]["enum"],
            )

    def test_gm_prompt_requires_evidence_numbers_and_cross_evidence_relation(self) -> None:
        judge = gm_codex_module.CodexGMJudge(
            ROOT / "ai-detective-gm",
            ROOT / "ai-detective-gm" / "cases" / "case_001",
            executable="codex",
        )
        prompt = judge._prompt(
            ROOT / "ai-detective-gm" / "inbox" / "question_001.json",
            ["U01_AMOUNT_GAP"],
        )
        self.assertIn("각 증거 번호가 직접 적혀", prompt)
        self.assertIn("구체적인 공개 사실", prompt)
        self.assertIn("특정 문장 템플릿이나 항목 순서는 요구하지 않는다", prompt)

    def test_game_defaults_to_fixed_gm_model(self) -> None:
        args = play_module.build_parser().parse_args(["start"])
        self.assertEqual("gpt-5.6-terra", args.gm_model)

    def test_gm_rejects_model_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(
                gm_codex_module.GMJudgeError,
                "gpt-5.6-terra",
            ):
                gm_codex_module.CodexGMJudge(
                    root,
                    root / "case",
                    model="gpt-6-astra",
                    executable="codex",
                )

    def test_gm_codex_process_is_cancelled_with_solver(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cancel_event = threading.Event()
            cancel_event.set()
            judge = gm_codex_module.CodexGMJudge(
                root,
                root / "case",
                executable=sys.executable,
                timeout=30,
                cancel_event=cancel_event,
            )
            judge._command = lambda *_args: [
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
            ]
            with self.assertRaisesRegex(
                gm_codex_module.GMJudgeError, "Solver 종료"
            ):
                judge._invoke("prompt", root / "output.json", None)

    def test_solver_renders_gm_answer_and_new_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = solver_module.SolverWorkspace(root)
            workspace.evidence_dir.mkdir(parents=True)
            solver_module.write_json(
                workspace.index_path,
                {
                    "evidence": {
                        "E03": {"title": "서버실 출입카드 기록", "public_facts": []}
                    },
                    "npc_roster": [],
                },
            )
            (workspace.evidence_dir / "E03.md").write_text(
                "# E03\n\n21:32 출입 기록\n", encoding="utf-8"
            )
            rendered = solver_module.render_gm_response(
                workspace,
                {
                    "npc_response": {
                        "npc": "마진우",
                        "answer": "다시 회사에 돌아왔습니다.",
                        "classification": "fact_confirmed",
                    },
                    "newly_unlocked_evidence": ["E03"],
                    "system_notification": "새 증거 E03가 공개되었습니다.",
                },
            )
            self.assertIn("다시 회사에 돌아왔습니다.", rendered)
            self.assertIn("[E03] 서버실 출입카드 기록", rendered)
            self.assertIn("21:32 출입 기록", rendered)

    def test_controller_stops_when_solver_owner_exits(self) -> None:
        class FakeRuntime:
            def __init__(self) -> None:
                self.started = False
                self.stopped = False
                self.completed_event = threading.Event()

            def start(self) -> None:
                self.started = True

            def stop(self) -> None:
                self.stopped = True

        with tempfile.TemporaryDirectory() as temp_dir:
            relay_dir = Path(temp_dir)
            fake_runtime = FakeRuntime()
            owner = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(0.5)"]
            )
            started = time.monotonic()
            with (
                mock.patch.object(
                    play_module, "GameRuntime", return_value=fake_runtime
                ),
                mock.patch.object(
                    play_module,
                    "CONTROLLER_STATE_PATH",
                    relay_dir / ".controller.json",
                ),
                mock.patch.object(
                    play_module,
                    "CONTROLLER_STOP_PATH",
                    relay_dir / ".controller_stop",
                ),
            ):
                result = play_module.run_controller(
                    "case_001", None, solver_pid=owner.pid
                )
            owner.wait(timeout=3)
            self.assertEqual(0, result)
            self.assertTrue(fake_runtime.started)
            self.assertTrue(fake_runtime.stopped)
            self.assertLess(time.monotonic() - started, 3)
            self.assertFalse((relay_dir / ".controller.json").exists())

    def test_controller_stops_when_game_is_completed(self) -> None:
        class CompletedRuntime:
            def __init__(self) -> None:
                self.completed_event = threading.Event()
                self.completed_event.set()
                self.stopped = False

            def start(self) -> None:
                return None

            def stop(self) -> None:
                self.stopped = True

        with tempfile.TemporaryDirectory() as temp_dir:
            relay_dir = Path(temp_dir)
            runtime = CompletedRuntime()
            with (
                mock.patch.object(play_module, "GameRuntime", return_value=runtime),
                mock.patch.object(
                    play_module,
                    "CONTROLLER_STATE_PATH",
                    relay_dir / ".controller.json",
                ),
                mock.patch.object(
                    play_module,
                    "CONTROLLER_STOP_PATH",
                    relay_dir / ".controller_stop",
                ),
            ):
                result = play_module.run_controller("case_001", None)
            self.assertEqual(0, result)
            self.assertTrue(runtime.stopped)
            self.assertFalse((relay_dir / ".controller.json").exists())


class RelayAutomationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.relay_dir = self.root / "ai-detective-relay"
        self.gm_dir = self.root / "ai-detective-gm"
        self.solver_dir = self.root / "ai-detective-solver"
        for path in (
            self.relay_dir,
            self.gm_dir / "inbox",
            self.gm_dir / "outbox",
            self.solver_dir / "inbox",
            self.solver_dir / "outbox",
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.relay = relay_module.FileRelay(
            self.relay_dir, self.gm_dir, self.solver_dir
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def write_packet(path: Path, packet: dict) -> None:
        path.write_text(
            json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    @staticmethod
    def question_packet(text: str = "차액은 무엇입니까?") -> dict:
        return {
            "type": "solver_question",
            "question_id": "question_001",
            "target_npc": "마진우",
            "attached_evidence": ["E01", "E02"],
            "grounding_note": "두 금액에 차이가 있다.",
            "relation": "amount_mismatch",
            "fact_links": [],
            "question": text,
        }

    @staticmethod
    def gm_packet() -> dict:
        return {
            "type": "gm_public_packet",
            "case_id": "case_001",
            "response_id": "gm_response_001",
            "in_reply_to": "question_001",
            "npc_roster": [],
            "npc_response": {
                "npc": "마진우",
                "answer": "공개 답변",
                "classification": "known_general",
                "established_facts": [],
            },
            "newly_unlocked_evidence": [],
            "system_notification": "새 증거 없음",
            "public_state": {"unlocked_evidence": ["E01", "E02"]},
        }

    def test_automatic_bidirectional_delivery_and_history(self) -> None:
        question_path = self.solver_dir / "outbox" / "question_001.json"
        response_path = self.gm_dir / "outbox" / "gm_response_001.json"
        self.write_packet(question_path, self.question_packet())
        self.write_packet(response_path, self.gm_packet())

        first = self.relay.sync_once()
        self.assertEqual(2, len(first["delivered"]))
        self.assertTrue((self.relay_dir / "to_gm" / "question_001.json").is_file())
        self.assertTrue((self.gm_dir / "inbox" / "question_001.json").is_file())
        self.assertTrue(
            (self.relay_dir / "to_solver" / "gm_response_001.json").is_file()
        )
        self.assertTrue(
            (self.solver_dir / "inbox" / "gm_response_001.json").is_file()
        )
        self.assertEqual(2, len(self.relay.log_path.read_text(encoding="utf-8").splitlines()))

        self.assertEqual([], self.relay.sync_once()["delivered"])

        self.write_packet(question_path, self.question_packet("수정된 질문입니다."))
        second = self.relay.sync_once()
        self.assertEqual(1, len(second["delivered"]))
        self.assertTrue(
            (self.relay_dir / "to_gm" / "question_001__002.json").is_file()
        )

    def test_private_gm_packet_is_rejected(self) -> None:
        packet = self.gm_packet()
        packet["npc_response"]["private"] = {"culprit": "마진우"}
        source = self.gm_dir / "outbox" / "gm_response_001.json"
        self.write_packet(source, packet)

        result = self.relay.sync_once()
        self.assertEqual([], result["delivered"])
        self.assertEqual(1, len(result["rejected"]))
        self.assertFalse(
            (self.solver_dir / "inbox" / "gm_response_001.json").exists()
        )


class SingleSolverGameTests(unittest.TestCase):
    def test_solver_ask_completes_automatic_gm_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            for name in (
                "ai-detective-gm",
                "ai-detective-solver",
                "ai-detective-relay",
            ):
                shutil.copytree(ROOT / name, temp_root / name)

            runtime = play_module.GameRuntime(
                temp_root, "case_001", gm_judge=FakeGMJudge()
            )
            try:
                initial = runtime.initialize()
                self.assertEqual(
                    ["E01", "E02"], initial["public_state"]["unlocked_evidence"]
                )
                runtime.start()
                response = runtime.solver.ask(
                    "마진우",
                    ["E01", "E02"],
                    ["F_E01_ACTUAL_150K", "F_E02_CLAIM_450K"],
                    "amount_mismatch",
                    "실결제액과 청구액 사이에 30만 원 차이가 있다.",
                    "E01의 실결제액은 15만 원이고 E02의 청구액은 45만 원입니다. 같은 정산 건의 30만 원 차액은 어떤 비용입니까?",
                    10,
                )
                self.assertEqual("question_001", response["in_reply_to"])
                self.assertEqual(
                    "fact_confirmed",
                    response["npc_response"]["classification"],
                )
                self.assertEqual(["E03"], response["newly_unlocked_evidence"])
                self.assertTrue(
                    (temp_root / "ai-detective-solver" / "evidence" / "E03.md").is_file()
                )
            finally:
                runtime.stop()

    def test_invalid_gm_judgment_returns_one_safe_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            for name in (
                "ai-detective-gm",
                "ai-detective-solver",
                "ai-detective-relay",
            ):
                shutil.copytree(ROOT / name, temp_root / name)

            judge = InvalidGMJudge()
            runtime = play_module.GameRuntime(
                temp_root, "case_001", gm_judge=judge
            )
            try:
                runtime.initialize()
                runtime.start()
                response = runtime.solver.ask(
                    "마진우",
                    ["E01", "E02"],
                    ["F_E01_ACTUAL_150K", "F_E02_CLAIM_450K"],
                    "amount_mismatch",
                    "실결제액과 청구액 사이에 30만 원 차이가 있다.",
                    "E01의 실결제액은 15만 원이고 E02의 청구액은 45만 원입니다. 같은 정산 건의 30만 원 차액은 어떤 비용입니까?",
                    10,
                )
                self.assertEqual("gm_error", response["npc_response"]["classification"])
                self.assertEqual([], response["newly_unlocked_evidence"])
                self.assertEqual(1, judge.calls)
            finally:
                runtime.stop()


if __name__ == "__main__":
    unittest.main()
