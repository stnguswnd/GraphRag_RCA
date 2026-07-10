# RCA GraphRAG 파이프라인 — 진행 상황 & 남은 문제

> 마지막 업데이트: 2026-07-09
> 웨이퍼맵 불량 원인분석(RCA) 지식그래프 파이프라인. 스펙 원본은 `KG_nodes_relations_io.md`.

---

## 1. 현재 상태 (요약)

`.txt` 문헌 → 청킹 → Neo4j 적재 → LLM KG 추출 → GraphRAG 질의응답까지
**2~6단계 전부 실제 Neo4j + OpenAI로 엔드투엔드 실행 완료.** 기능은 동작함.

하이브리드 RAG(7~9)는 범위에서 제외.

---

## 2. 만든 파일

### 목업 데이터 (`data/rca_mock/`)
- `docs/*.txt` — 실제 문헌 5편 (etch / deposition / cmp / litho / wet_clean)
- `seeds/defect_patterns.json` — 불량 패턴 9종 (시드, VLM 출력 클래스와 동일해야 함)
- `seeds/process_steps.json` — 공정 6종 (join key: `lot_history.step`)
- `seeds/parameter_types.json` — 공정 변수 20종 (join key: `telemetry.param`)
- `seeds/detection_methods.json` — MCP 도구 9종 (verify 5 / pipeline 4)
- `detected_by_rules.json` — DETECTED_BY 규칙표

### 파이프라인 코드 (`rca/`)
- `2_load_txt.py` — .txt 문헌 로드 → `outputs/parsed_docs.jsonl`
- `3_split.py` — 청킹 → `outputs/chunks.jsonl` (chunk_id = `{doc_id}#c{nn}`)
- `4_ingest_chunks_to_neo4j.py` — 시드 앵커 적재 + Chunk 적재 + NEXT_CHUNK
- `5_build_kg_from_chunks.py` — LLM으로 Cause/관계 추출 + DETECTED_BY 규칙 부여
- `6_ask_graphrag.py` — GraphCypherQAChain 질의응답

### 실행 순서
```
python 1_test_connection.py          # (프로젝트 루트, 공용) Neo4j 연결 확인
python rca/2_load_txt.py
python rca/3_split.py
python rca/4_ingest_chunks_to_neo4j.py
python rca/5_build_kg_from_chunks.py
python rca/6_ask_graphrag.py
```

### 마지막 실행 결과 (2026-07-09)
- 문헌 5편 → 청크 21개
- 시드 9/6/20/9 적재 OK
- Cause 24개 추출, 5종 관계 + DETECTED_BY 생성 OK
- 6단계 질의 5문항 중 **4문항 정답**

---

## 3. 남은 문제 (우선순위 순)

### [P1] 6단계 Text2Cypher가 Cause를 DefectPattern으로 오인
- **증상**: "focus_ring_erosion 원인은 어떤 도구로 확인해?" → 빈 결과.
  LLM이 `MATCH (dp:DefectPattern {name:'focus_ring_erosion'})`로 잘못 생성.
  (focus_ring_erosion은 `Cause`인데 `DefectPattern`으로 매칭)
- **원인**: GraphCypherQAChain 특유의 확률적 Cypher 오류. 파이프라인 버그 아님.
- **해결안**:
  - `rca/6_ask_graphrag.py`의 CYPHER_GENERATION_TEMPLATE에
    "원인 이름은 반드시 Cause.name 또는 Cause.uid로 매칭. DefectPattern.name에는
    불량 패턴 9종만 온다" 규칙을 강화.
  - few-shot 예시(원인 질문 → 올바른 Cypher)를 프롬프트에 추가.

### [P2] 추출 노이즈 (Cause가 잘게/중복 추출됨)
- **증상**: `elevated_rf_power`, `rf_power_elevated`처럼 같은 개념이 중복.
  `CMP:polish_pressure`, `CMP:material_type_cause`처럼 변수/메타개념을 Cause로 오분류.
- **원인**: `gpt-5.4-mini`의 추출 품질 한계 + dedup 단계 없음.
- **해결안**:
  - 더 큰 모델로 교체 (`OPENAI_MODEL` 조정).
  - 5단계 뒤에 **uid 기반 dedup / entity resolution** 단계 추가
    (임베딩 유사도로 동의어 Cause 병합).
  - 프롬프트에 "공정 변수 자체(rf_power 등)를 Cause로 만들지 말 것. 변수는
    INVOLVES_PARAMETER의 target으로만 사용" 명시.

### [P3] Neo4j DB에 보험 KG와 RCA KG가 혼재
- **증상**: 같은 `neo4j` DB에 기존 보험 데이터(KBDocument/KGEntity/Article…)와
  RCA 데이터(Cause/DefectPattern…)가 함께 존재. 6단계 스키마가 지저분해짐.
- **영향**: 라벨이 달라 충돌은 없지만, Text2Cypher가 큰 스키마에서 혼란 가능.
- **해결안**:
  - RCA 노드만 지우는 초기화 스크립트(`rca/0_reset.py`) 작성.
    예: `MATCH (n) WHERE n:Cause OR n:DefectPattern OR n:ProcessStep OR
    n:ParameterType OR n:DetectionMethod OR (n:Chunk AND n.doc_id IS NOT NULL)
    OR n:Document DETACH DELETE n`
  - 또는 Neo4j를 별도 인스턴스/DB로 분리.

---

## 4. 아직 구현 안 한 것 (스펙 대비)

- **투영(projection) → `rca` 뷰 / `Hypothesis` 노드** — 스펙 §4의 평탄화된 가설.
  현재는 `kgbuild` 원본 그래프만 만들고, 6번은 그 위에서 바로 Text2Cypher.
  스펙의 "그래프 2개(kgbuild/rca) 분리"와 Text2Cypher 안전성은 미구현.
- **`how_to_verify` 조립** — DETECTED_BY(verify)만 부여했고, args_template
  치환($suspect_eq/$window)해서 가설별 도구 호출 지시서로 굳히는 단계 미구현.
- **MITIGATED_BY / Action 노드** (스펙 2순위 기능) — 미구현.
- **RELATED_TO 안전망** — ①~⑥에 안 맞는 관계 보관소. 미구현.
- **VLM 관측 입력(§3) 처리** — 현재 6번은 자연어 질문 입력. VLM JSON 입력 →
  패턴 정합성 체크 → 임베딩 폴백 경로 미구현.

---

## 5. 다음에 할 일 후보 (사용자 선택 대기)

1. 6번 Cypher 프롬프트 정확도 개선 (P1)
2. 추출 dedup / entity resolution 단계 추가 (P2)
3. RCA 전용 초기화 스크립트 (P3)
4. `Hypothesis` 투영 단계 구현 (스펙 §4 완성)
