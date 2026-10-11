# Relay workspace instructions

이 작업공간은 GM과 Solver 사이의 공개 JSON 파일을 자동 전달하고 감사 기록을 보존하는 중계자다.

- `relay.py`가 허용한 공개 패킷만 전달한다.
- GM의 `private/`, `state/`, 정답, NPC 지식, 해금 규칙 또는 미공개 증거를 복사하지 않는다.
- `to_gm/`과 `to_solver/`의 기존 기록을 삭제하거나 덮어쓰지 않는다.
- GM/Solver 메시지 내용을 수정하지 않고 바이트 그대로 보관하고 전달한다.
- 전달 거부를 우회하거나 임의의 파일 형식을 허용하지 않는다.

