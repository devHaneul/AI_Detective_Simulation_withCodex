#!/usr/bin/env python3
"""Initialize the game and manage its background GM/relay controller."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import ModuleType
from typing import Any


ROOT = Path(__file__).resolve().parent
RELAY_DIR = ROOT / "ai-detective-relay"
CONTROLLER_STATE_PATH = RELAY_DIR / ".controller.json"
CONTROLLER_STOP_PATH = RELAY_DIR / ".controller_stop"
CONTROLLER_LOG_PATH = RELAY_DIR / "controller.log"
DEFAULT_GM_MODEL = "gpt-5.6-terra"


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"모듈을 불러올 수 없습니다: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def controller_state() -> dict[str, Any] | None:
    if not CONTROLLER_STATE_PATH.is_file():
        return None
    try:
        value = json.loads(CONTROLLER_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def process_is_running(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            return ctypes.get_last_error() == 5  # access denied means it exists
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_snapshot() -> dict[int, tuple[int, str]]:
    """Return {pid: (parent_pid, executable_name)} for the current process table."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESSENTRY32W),
        ]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESSENTRY32W),
        ]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        snapshot_handle = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        invalid_handle_value = ctypes.c_void_p(-1).value
        if snapshot_handle == invalid_handle_value:
            return {}
        result: dict[int, tuple[int, str]] = {}
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        try:
            success = kernel32.Process32FirstW(snapshot_handle, ctypes.byref(entry))
            while success:
                result[int(entry.th32ProcessID)] = (
                    int(entry.th32ParentProcessID),
                    entry.szExeFile,
                )
                success = kernel32.Process32NextW(snapshot_handle, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot_handle)
        return result

    proc = Path("/proc")
    result: dict[int, tuple[int, str]] = {}
    if not proc.is_dir():
        return result
    for item in proc.iterdir():
        if not item.name.isdigit():
            continue
        try:
            stat = (item / "stat").read_text(encoding="utf-8")
            closing = stat.rfind(")")
            fields = stat[closing + 2 :].split()
            parent_pid = int(fields[1])
            name = (item / "comm").read_text(encoding="utf-8").strip()
            result[int(item.name)] = (parent_pid, name)
        except (OSError, ValueError, IndexError):
            continue
    return result


def find_solver_codex_pid(
    start_pid: int | None = None,
    snapshot: dict[int, tuple[int, str]] | None = None,
) -> int | None:
    """Find the nearest parent Codex CLI that launched the Solver command."""
    processes = snapshot if snapshot is not None else process_snapshot()
    current_pid = start_pid or os.getpid()
    visited: set[int] = set()
    while current_pid > 0 and current_pid not in visited:
        visited.add(current_pid)
        process = processes.get(current_pid)
        if not process:
            return None
        parent_pid = process[0]
        parent = processes.get(parent_pid)
        if not parent:
            return None
        executable = Path(parent[1]).name.lower()
        if executable in {"codex", "codex.exe"}:
            return parent_pid
        current_pid = parent_pid
    return None


def controller_is_running() -> bool:
    state = controller_state()
    return bool(state and process_is_running(state.get("pid")))


class GameRuntime:
    """Coordinates the private GM, relay records, and public Solver workspace."""

    def __init__(
        self,
        root: Path,
        case_id: str,
        gm_model: str | None = None,
        gm_judge: Any | None = None,
        codex_executable: str | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", case_id):
            raise ValueError("사건 ID 형식이 올바르지 않습니다.")
        if gm_model not in (None, DEFAULT_GM_MODEL):
            raise ValueError(f"GM 모델은 {DEFAULT_GM_MODEL}만 사용할 수 있습니다.")
        gm_model = DEFAULT_GM_MODEL
        self.root = root.resolve()
        self.gm_dir = self.root / "ai-detective-gm"
        self.solver_dir = self.root / "ai-detective-solver"
        self.relay_dir = self.root / "ai-detective-relay"
        self.case_dir = self.gm_dir / "cases" / case_id
        if not (self.case_dir / "private" / "ground_truth.json").is_file():
            raise ValueError(f"사건을 찾을 수 없습니다: {case_id}")

        self.gm_module = load_module("auto_game_gm", self.gm_dir / "gm.py")
        self.gm_codex_module = load_module(
            "auto_game_gm_codex", self.gm_dir / "gm_codex.py"
        )
        self.solver_module = load_module(
            "auto_game_solver", self.solver_dir / "solver.py"
        )
        self.relay_module = load_module(
            "auto_game_relay", self.relay_dir / "relay.py"
        )
        self.gm = self.gm_module.GMEngine(self.case_dir, self.gm_dir / "outbox")
        self.gm_judge = gm_judge or self.gm_codex_module.CodexGMJudge(
            self.gm_dir,
            self.case_dir,
            model=gm_model,
            executable=codex_executable,
        )
        self.solver = self.solver_module.SolverWorkspace(self.solver_dir)
        self.relay = self.relay_module.FileRelay(
            self.relay_dir, self.gm_dir, self.solver_dir
        )
        self.stop_event = threading.Event()
        self.completed_event = threading.Event()
        if gm_judge is None:
            self.gm_judge.cancel_event = self.stop_event
        self.thread: threading.Thread | None = None
        self.marker = self.relay_dir / ".managed_by_play"
        self.error_path = self.relay_dir / "controller_error.log"
        self.gm_error_path = self.case_dir / "state" / "gm_codex_error.log"

    def _clear_gm_inbox(self) -> None:
        inbox = self.gm_dir / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        for path in inbox.glob("question_*.json"):
            path.unlink()

    def _ingest_available_packets(self) -> None:
        inbox = self.solver_dir / "inbox"
        paths = list(inbox.glob("gm_response_*.json"))
        initial = inbox / "initial_packet.json"
        if initial.is_file():
            paths.insert(0, initial)
        for path in paths:
            packet = self.solver_module.read_json(path)
            response_id = packet.get("response_id")
            if response_id == "initial":
                if self.solver.index_path.exists():
                    continue
            elif (self.solver.responses_dir / f"{response_id}.json").exists():
                continue
            self.solver.ingest(packet)

    def initialize(self) -> dict[str, Any]:
        self.completed_event.clear()
        self.marker.write_text("play.py manages relay delivery\n", encoding="utf-8")
        if self.error_path.exists():
            self.error_path.unlink()
        if self.gm_error_path.exists():
            self.gm_error_path.unlink()
        self._clear_gm_inbox()
        self.solver.reset_public()
        self.gm_judge.reset()
        initial_packet = self.gm.reset()
        self.relay.sync_once()
        self._ingest_available_packets()
        if self.gm.is_complete():
            self.completed_event.set()
        return initial_packet

    def process_once(self) -> None:
        self.relay.sync_once()
        self._ingest_available_packets()

        if self.gm.is_complete():
            self.completed_event.set()
            return

        state = self.gm._state()
        processed = {
            item.get("question_id") for item in state.get("questions", [])
        }
        for path in sorted((self.gm_dir / "inbox").glob("question_*.json")):
            request = self.gm_module.read_json(path)
            question_id = request.get("question_id")
            if question_id in processed:
                continue
            try:
                candidate_rule_ids = self.gm.eligible_rule_ids(request)
                decision = self.gm_judge.decide(path, candidate_rule_ids)
                self.gm.answer(request, decision=decision)
            except Exception as exc:
                if self.stop_event.is_set():
                    return
                self.gm_error_path.write_text(
                    f"{type(exc).__name__}: {exc}\n", encoding="utf-8"
                )
                safe_error_decision = {
                    "question_id": question_id,
                    "classification": "gm_error",
                    "answer": (
                        "GM Codex의 이번 판단이 안전 검증을 통과하지 못했습니다. "
                        "잠시 후 질문을 다시 제출해 주세요."
                    ),
                    "matched_rule_id": None,
                    "rationale": "background GM Codex invocation failed",
                }
                self.gm.answer(request, decision=safe_error_decision)
            processed.add(question_id)
            if self.gm.is_complete():
                break

        self.relay.sync_once()
        self._ingest_available_packets()
        if self.gm.is_complete():
            self.completed_event.set()

    def _run(self) -> None:
        while not self.stop_event.is_set() and not self.completed_event.is_set():
            try:
                self.process_once()
            except Exception as exc:  # keep the controller alive and expose the failure
                self.error_path.write_text(
                    f"{type(exc).__name__}: {exc}\n", encoding="utf-8"
                )
            self.stop_event.wait(0.25)

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(
            target=self._run, name="ai-detective-controller", daemon=True
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)
        if self.marker.exists():
            self.marker.unlink()


def discover_cases(root: Path) -> list[dict[str, str]]:
    cases: list[dict[str, str]] = []
    cases_dir = root / "ai-detective-gm" / "cases"
    for truth_path in sorted(cases_dir.glob("*/private/ground_truth.json")):
        try:
            truth = json.loads(truth_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        cases.append(
            {
                "id": truth_path.parents[1].name,
                "title": str(truth.get("title", truth_path.parents[1].name)),
                "briefing": str(
                    truth.get(
                        "public_briefing",
                        "초기 증거를 검토하고 관계자들의 진술을 확인해 사건을 수사하세요.",
                    )
                ),
            }
        )
    return cases


def render_game_briefing(
    case_info: dict[str, str],
    packet: dict[str, Any],
    gm_model: str,
) -> str:
    evidence_items = packet.get("newly_unlocked_evidence", [])
    lines = [
        "╭──────────────────────────────────────────────────────────────╮",
        "│                    AI 탐정 게임 · 수사 브리핑                │",
        "╰──────────────────────────────────────────────────────────────╯",
        "",
        f"사건  | {case_info['title']}",
        f"번호  | {case_info['id']}",
        f"GM    | {gm_model}",
        "",
        "━━━━━━━━━━━━━━━━━━━━  사건 상황  ━━━━━━━━━━━━━━━━━━━━",
        case_info["briefing"].strip(),
        "",
        "━━━━━━━━━━━━━━━━━━━━  수사 목표  ━━━━━━━━━━━━━━━━━━━━",
        "공개된 증거와 NPC 답변을 연결해 모든 증거를 해금하세요.",
        "",
        "━━━━━━━━━━━━━━━━━━━━  등장인물  ━━━━━━━━━━━━━━━━━━━━",
    ]
    roster = packet.get("npc_roster", [])
    if roster:
        lines.extend(f"• {npc['name']} — {npc['role']}" for npc in roster)
    else:
        lines.append("• 공개된 등장인물이 없습니다.")

    lines.extend(
        [
            "",
            f"━━━━━━━━━━━━━━  현재 보유 증거 · {len(evidence_items)}건  ━━━━━━━━━━━━━━",
        ]
    )
    for index, evidence in enumerate(evidence_items, start=1):
        if index > 1:
            lines.extend(["", "──────────────────────────────────────────────────────────────"])
        lines.extend(["", f"[{evidence['id']}] {evidence['title']}"])
        content_lines = evidence["content"].rstrip().splitlines()
        if content_lines and content_lines[0].startswith("# "):
            content_lines = content_lines[1:]
            while content_lines and not content_lines[0].strip():
                content_lines.pop(0)
        if content_lines:
            lines.extend(["", *content_lines])
        public_facts = evidence.get("public_facts", [])
        if public_facts:
            lines.extend(["", "공개 사실"])
            lines.extend(
                f"• [{fact['id']}] {fact['statement']}" for fact in public_facts
            )

    unlocked = packet.get("public_state", {}).get("unlocked_evidence", [])
    lines.extend(
        [
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"수사 준비 완료 · 보유 증거: {', '.join(unlocked) or '없음'}",
            "다음 행동을 자연어로 지시해 수사를 시작하세요.",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AI 탐정 게임과 백그라운드 GM 컨트롤러 관리"
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="start",
        choices=("start", "stop", "status", "cases", "controller"),
        help="기본값: start",
    )
    parser.add_argument("--case", help="사건 ID")
    parser.add_argument(
        "--gm-model",
        default=DEFAULT_GM_MODEL,
        choices=(DEFAULT_GM_MODEL,),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--solver-pid",
        type=int,
        help=argparse.SUPPRESS,
    )
    return parser


def list_cases() -> int:
    cases = discover_cases(ROOT)
    if not cases:
        print("실행 가능한 사건이 없습니다.", file=sys.stderr)
        return 2
    print(json.dumps({"cases": cases}, ensure_ascii=False, indent=2))
    return 0


def resolve_case(case_id: str | None) -> str:
    cases = discover_cases(ROOT)
    if not cases:
        raise RuntimeError("실행 가능한 사건이 없습니다.")
    known_case_ids = {case["id"] for case in cases}
    if case_id:
        if case_id not in known_case_ids:
            raise ValueError(f"사건을 찾을 수 없습니다: {case_id}")
        return case_id
    if len(cases) == 1:
        return cases[0]["id"]
    choices = ", ".join(case["id"] for case in cases)
    raise ValueError(f"--case로 사건을 선택하세요. 사용 가능: {choices}")


def run_controller(
    case_id: str,
    gm_model: str | None,
    solver_pid: int | None = None,
) -> int:
    gm_model = gm_model or DEFAULT_GM_MODEL
    runtime = GameRuntime(ROOT, case_id, gm_model=gm_model)
    pid = os.getpid()
    runtime.start()
    try:
        while (
            not CONTROLLER_STOP_PATH.is_file()
            and not runtime.completed_event.is_set()
        ):
            if solver_pid and not process_is_running(solver_pid):
                break
            write_json(
                CONTROLLER_STATE_PATH,
                {
                    "status": "running",
                    "pid": pid,
                    "case_id": case_id,
                    "gm_model": gm_model,
                    "solver_pid": solver_pid,
                    "updated_at": time.time(),
                },
            )
            time.sleep(0.5)
    finally:
        runtime.stop()
        state = controller_state()
        if state and state.get("pid") == pid and CONTROLLER_STATE_PATH.exists():
            CONTROLLER_STATE_PATH.unlink()
        if CONTROLLER_STOP_PATH.exists():
            CONTROLLER_STOP_PATH.unlink()
    return 0


def launch_controller(case_id: str, gm_model: str | None) -> int:
    gm_model = gm_model or DEFAULT_GM_MODEL
    if controller_is_running():
        state = controller_state() or {}
        print(
            f"이미 컨트롤러가 실행 중입니다: {state.get('case_id', 'unknown')} "
            f"(PID {state.get('pid', 'unknown')})"
        )
        print("새 게임을 시작하려면 먼저 `python game.py stop`을 실행하세요.")
        return 2

    for stale_path in (CONTROLLER_STATE_PATH, CONTROLLER_STOP_PATH):
        if stale_path.exists():
            stale_path.unlink()

    codex = shutil.which("codex")
    if not codex:
        print("오류: PATH에서 codex 실행 파일을 찾을 수 없습니다.", file=sys.stderr)
        return 2

    runtime = GameRuntime(
        ROOT,
        case_id,
        gm_model=gm_model,
        codex_executable=codex,
    )
    try:
        packet = runtime.initialize()
        solver_pid = find_solver_codex_pid()
        command = [
            sys.executable,
            str(ROOT / "play.py"),
            "controller",
            "--case",
            case_id,
        ]
        command.extend(["--gm-model", gm_model])
        if solver_pid:
            command.extend(["--solver-pid", str(solver_pid)])

        popen_options: dict[str, Any] = {
            "cwd": ROOT,
            "stdin": subprocess.DEVNULL,
        }
        if os.name == "nt":
            popen_options["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            popen_options["start_new_session"] = True

        CONTROLLER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CONTROLLER_LOG_PATH.open("a", encoding="utf-8") as log:
            process = subprocess.Popen(
                command,
                stdout=log,
                stderr=log,
                **popen_options,
            )

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = controller_state()
            if state and state.get("pid") == process.pid:
                case_info = next(
                    (
                        item
                        for item in discover_cases(ROOT)
                        if item["id"] == case_id
                    ),
                    {
                        "id": case_id,
                        "title": case_id,
                        "briefing": "초기 증거를 검토하고 사건을 수사하세요.",
                    },
                )
                print("\n" + render_game_briefing(case_info, packet, gm_model))
                print("\n실행 정보")
                print(f"백그라운드 컨트롤러 PID: {process.pid}")
                if solver_pid:
                    print(
                        f"Solver Codex PID {solver_pid} 종료 시 컨트롤러도 자동 종료됩니다."
                    )
                else:
                    print(
                        "주의: Solver Codex 프로세스를 찾지 못해 자동 종료 감시가 "
                        "비활성화되었습니다. 종료할 때 `python game.py stop`을 실행하세요."
                    )
                print("이제 현재 Solver Codex에서 공개 증거를 읽고 수사를 진행하세요.")
                return 0
            if process.poll() is not None:
                break
            time.sleep(0.1)
        if process.poll() is None:
            process.terminate()
        runtime.stop()
        print(
            f"오류: 컨트롤러가 시작되지 않았습니다. {CONTROLLER_LOG_PATH}를 확인하세요.",
            file=sys.stderr,
        )
        return 2
    except Exception:
        runtime.stop()
        raise


def stop_controller() -> int:
    state = controller_state()
    if not state or not process_is_running(state.get("pid")):
        for stale_path in (CONTROLLER_STATE_PATH, CONTROLLER_STOP_PATH):
            if stale_path.exists():
                stale_path.unlink()
        print("실행 중인 게임 컨트롤러가 없습니다.")
        return 0

    CONTROLLER_STOP_PATH.write_text("stop\n", encoding="utf-8")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not process_is_running(state["pid"]):
            print("게임 컨트롤러를 종료했습니다.")
            return 0
        time.sleep(0.2)
    print(
        "오류: 컨트롤러가 제한 시간 안에 종료되지 않았습니다.",
        file=sys.stderr,
    )
    return 2


def show_status() -> int:
    state = controller_state()
    running = bool(state and process_is_running(state.get("pid")))
    public_status = {
        "running": running,
        "pid": state.get("pid") if state else None,
        "case_id": state.get("case_id") if state else None,
        "gm_model": state.get("gm_model") if state else None,
        "solver_pid": state.get("solver_pid") if state else None,
    }
    print(json.dumps(public_status, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    configure_stdio()
    args = build_parser().parse_args()
    try:
        if args.command == "cases":
            return list_cases()
        if args.command == "status":
            return show_status()
        if args.command == "stop":
            return stop_controller()

        case_id = resolve_case(args.case)
        gm_model = args.gm_model
        if gm_model and not re.fullmatch(r"[A-Za-z0-9._-]+", gm_model):
            print("오류: GM 모델 ID 형식이 올바르지 않습니다.", file=sys.stderr)
            return 2

        if args.command == "controller":
            return run_controller(case_id, gm_model, args.solver_pid)
        return launch_controller(case_id, gm_model)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
