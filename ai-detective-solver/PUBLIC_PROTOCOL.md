# Public relay protocol

Solver가 만들 수 있는 질문 패킷:

```json
{
  "type": "solver_question",
  "question_id": "question_001",
  "target_npc": "마진우",
  "attached_evidence": ["E01", "E02"],
  "grounding_note": "두 금액 사이에 30만 원 차이가 있다.",
  "relation": "amount_mismatch",
  "fact_links": [
    {
      "evidence_id": "E01",
      "fact_id": "F_E01_ACTUAL_150K",
      "claim": "4월 28일 장비 대여 실결제액은 150,000원이다."
    }
  ],
  "question": "같은 정산 건의 차액은 어떤 비용입니까?"
}
```

GM 공개 패킷은 NPC 공개 답변, 새로 해금된 증거, 공개 시스템 알림, 공개 증거 ID 목록만 포함한다. `solver.py ingest`는 허용 목록 밖의 최상위 필드와 `ground_truth`, `npc_knowledge`, `unlock_rules`, `private`, `internal_state`, `final_answer`라는 키를 어느 깊이에서든 거부한다.

GM 공개 패킷의 `in_reply_to`는 어떤 Solver 질문에 대한 응답인지 나타낸다. `solver.py ask`는 이 값을 사용해 자신의 질문에 대한 응답만 기다린다.
