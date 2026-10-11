# GM private data schemas

아래 구조는 Python 표준 라이브러리로 검증하는 이 프로젝트의 JSON 계약이다. 사건 데이터는 게임 시작 전에 작성하고, 시작 뒤에는 `state/game_state.json`만 변경한다.

## `ground_truth.json`

```text
schema_version: integer
case_id, title, public_briefing, truth: string
timeline: [{time, event}]
person_actions: {NPC name: [action]}
evidence_connections: [{facts: [fact id], meaning}]
final_answer: {culprit, acts, required_reasoning}
initial_evidence: [evidence id]
public_npc_roster: [{name, role}]
evidence_catalog: [{
  id, title, file, provenance,
  public_facts: [{id, statement}]
}]
```

`public_briefing`은 게임 시작 화면에 공개한다. `truth`, `person_actions`, `provenance`, `evidence_connections`, `final_answer`는 외부 패킷으로 내보내지 않는다.

## `npc_knowledge.json`

```text
schema_version: integer
npcs: {
  NPC name: {
    role: string,
    knows: [string],
    does_not_know: [string],
    directly_observed: [string],
    heard_from_others: [string],
    memory_limitations: [string],
    reveal_conditions: [string],
    public_responses: [{keywords, classification, answer}],
    unknown_response: string
  }
}
```

## `unlock_rules.json`

```text
schema_version: integer
immutable_after_start: boolean
rules: [{
  id: string,
  target_npc: string,
  relation: amount_mismatch | timeline_contradiction |
            actor_device_sequence | device_file_sequence | general,
  required_facts: [public fact id],
  establishes_facts: [confirmed fact id],
  npc_response: public string,
  unlocks: [evidence id]
}]
```

Python GM 엔진은 대상 NPC, 공개 사실 ID 집합, 관계 유형, 현재 해금 상태뿐 아니라 규칙에 필요한 증거 번호가 실제 질문 문장에 모두 포함됐는지 검사해 후보 규칙을 만든다. GM Codex는 질문 형식과 관계없이 각 증거의 구체적인 공개 사실과 그 사이의 모순·차이·인과 또는 시간적 연결이 설명됐는지 판단해 후보 규칙 ID 하나 또는 `null`을 선택한다. Python GM 엔진은 선택 결과를 다시 검증한다.

## `gm_decision.schema.json`

```text
question_id: string
classification: fact_confirmed | known_general | memory_limitation |
                unknown | insufficient_grounding
answer: string
matched_rule_id: unlock rule id | null
rationale: private string
```

결정 원본과 `rationale`은 사건의 `state/gm_codex_decisions/`에만 저장하며 공개 패킷이나 relay로 보내지 않는다. `matched_rule_id`가 유효하더라도 Python 검증을 통과하지 못하면 어떤 증거도 공개하지 않는다.

## `game_state.json`

```text
case_id: string
ground_truth_locked: boolean
unlocked_evidence: [evidence id]
established_facts: [fact id]
npc_interactions: [{npc, question_id, response_id}]
questions: [{public question fields and classification}]
dead_ends: [{signature, classification, answer}]
unlock_history: [{rule_id, question_id, unlocked}]
next_response_number: integer
```
