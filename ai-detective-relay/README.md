# Relay

이 폴더는 공개 메시지를 자동 전달하면서 기록을 남기는 중계 작업공간입니다.

- `to_gm/`: GM으로 전달한 Solver 질문의 변경 불가능한 이력입니다.
- `to_solver/`: Solver로 전달한 GM 공개 패킷의 이력입니다.
- `delivery_log.jsonl`: 전달 시각, 방향, 파일명, SHA-256 기록입니다.
- GM의 `private/`, `state/`, 정답, 미해금 증거를 이곳에 복사하지 마세요.
- GM과 Solver CLI는 relay나 상대 작업공간을 직접 읽지 않습니다. 루트의 `play.py`가 `relay.py` 기능을 백그라운드에서 사용해 양쪽 `outbox/`와 `inbox/`를 중계합니다.

일반 게임에서는 별도 relay 명령을 실행하지 말고 저장소 루트에서 `python play.py`만 실행합니다. 아래 명령은 수동 진단용입니다.

```powershell
python relay.py watch
```

한 번만 동기화하려면:

```powershell
python relay.py sync
```
