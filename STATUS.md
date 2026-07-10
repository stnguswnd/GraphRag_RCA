# RCA GraphRAG 파이프라인 — 진행 상황 & 남은 문제

> 마지막 업데이트: 2026-07-10
> 웨이퍼맵 불량 원인분석(RCA) 지식그래프 파이프라인. **스키마 원본은 `schema.md`.**

---

## 1. 현재 상태 (요약)

`.txt` 문헌 → 청킹 → Neo4j 적재 → LLM KG 추출 → GraphRAG 질의응답.

**2026-07-10: 전체 코드를 `schema.md` 백본으로 재작성함.** 아래 5종 관계가 정본이다.

```
DefectPattern ──ARISES_IN──> ProcessStep <──OCCURS_IN── FailureMode
                              (join)                        │ CAUSED_BY
Equipment ──PART_OF────────> ProcessStep                    ▼
                                                          Cause ──INVOLVES_PARAMETER──> Parameter
                                                                                    (→ fab SQL)
```

이전 스키마에서 바뀐 점:
- `FailureMode`, `Equipment`, `Parameter` 노드 신설 / `ParameterType` → `Parameter` 대체
- `ARISES_IN`, `PART_OF` 관계 신설
- `OCCURS_IN` 소스가 `Cause` → `FailureMode`, `CAUSED_BY`가 `Cause→Cause` → `FailureMode→Cause`
- `MANIFESTS_AS`, `DetectionMethod`, `DETECTED_BY`, `cause_type`(5M) **제거**
- 모든 노드의 유일 키를 `name`/`uid` → **`id`** 로 통일
- `DefectPattern` 9종 → **3종** (Center / Scratch / Edge-Ring)
- `Parameter` 20종을 **`fab.md`의 장비군별 파라미터**로 교체 (join key: `telemetry.param`)

**2026-07-10: 보험(PDF 약관) 파이프라인을 전부 제거하고 RCA 전용 저장소로 정리함.**
`rca/` 하위에 있던 코드를 루트로 승격했고, `data/rca_mock/` → `data/`,
목업 문서는 스키마에 정확히 대응하는 `doc_A`~`doc_D` 4편만 남겼다.

**0~4단계 실행 검증 완료** (문헌 4편 → 청크 11개, 시드 3/6/20 적재, 잔재 라벨 0).
5~6단계는 OpenAI 호출이라 미실행.

---

## 2. 파일 구조

### 데이터 (`data/`)
- `docs/` — 문헌 4편
  - `doc_A_wafermap_patterns.txt` — 패턴 → 공정 (`ARISES_IN`)
  - `doc_B/C/D_*_troubleshooting.txt` — 고장 모드 → 원인 → 변수 (ETCH/DEPO/CMP)
- `seeds/defect_patterns.json` — 불량 패턴 **3종** (VLM 출력 클래스와 동일해야 함)
- `seeds/process_steps.json` — 공정 6종 (join key: `lot_history.step`)
- `seeds/parameters.json` — 공정 변수 20종 (join key: `telemetry.param`, `fab.md` 기준)

### 파이프라인 코드 (루트)
- `0_reset.py` — Neo4j DB 전체 초기화 (노드/관계/제약/인덱스)
- `1_test_connection.py` — Neo4j 연결 확인
- `2_load_txt.py` — 문헌 로드 → `outputs/parsed_docs.jsonl`
- `3_split.py` — 청킹 → `outputs/chunks.jsonl` (chunk_id = `{doc_id}#c{nn}`)
- `4_ingest_chunks_to_neo4j.py` — 시드 앵커 3종 적재 + Chunk 적재 + NEXT_CHUNK
- `5_build_kg_from_chunks.py` — LLM으로 FailureMode/Cause/Equipment + 4종 관계 추출, PART_OF는 규칙
- `6_ask_graphrag.py` — GraphCypherQAChain 질의응답

실행 순서는 `README.md` 참조.

> `0_reset.py`를 건너뛰고 스키마를 바꾸면 `id` 없는 옛 앵커 노드가 남아 **중복 노드**가 생긴다.
> Neo4j의 UNIQUE 제약은 null을 무시하므로 `MERGE {id: ...}`가 옛 노드를 못 찾는다.

---

## 3. 남은 문제 (우선순위 순)

### [P1] 5~6단계 재작성 후 미실행 — 검증 필요
- 0~4단계는 실제로 돌렸다. 5~6단계는 가지치기/프롬프트 렌더링 로직만 단위 확인.
- **할 일**: `5` → `6`을 돌려 관계 개수와 질의 정답률 확인.

### [P2] 추출 품질 — ARISES_IN 누락, Equipment 오추출
문서 정리 **이전**(문헌 11편) 실행 결과에서 관찰된 것:
- **ARISES_IN이 2건만 추출됨** (`Edge-Ring→ETCH`, `Center→DEPO`).
  당시 `doc_A`에 명시된 `Edge-Ring→DEPO`, `Donut→CMP`가 누락.
- **Equipment 오추출**: `depo_01`, `deposition_tool`, `etch_chamber`, `exposure_tool` 등
  장비 인스턴스가 아닌 것들이 노드가 됨. `ETCH-03`, `CMP-01`만 유효.
- **Cause 56개 / FailureMode 41개** — 과다 추출.
- 노이즈의 상당 부분은 내용이 겹치던 옛 목업 문서 7편에서 나왔고, 그 문서들은 삭제했다.
  **문서 정리 후 재실행해서 얼마나 개선됐는지 먼저 확인할 것.**
- 남은 해결안: Equipment id 형식 검증(`^[A-Z]+-\d+$`), 임베딩 기반 dedup / entity resolution.

### [P3] FailureMode id가 공정 스코프를 갖지 않음
- `schema.md`의 예시를 따라 `FailureMode.id`를 맨 이름(`post_etch_residue`)으로 뒀다.
- 그래서 ETCH의 "particle contamination"과 CLEAN의 "particle contamination"이 **한 노드로 병합**되고,
  `OCCURS_IN`이 두 공정을 가리키게 된다.
- 의도한 동작이 아니면 `{STEP}:{name}` 형태로 바꿔야 한다. **미결정.**

---

## 4. 아직 구현 안 한 것

- **`Hypothesis` 투영 노드** — `DefectPattern → ProcessStep → FailureMode → Cause → Parameter`
  경로 하나하나가 가설 1건. 이를 평탄화한 노드/뷰는 미구현.
- **fab SQL 검증 연결** — `Parameter.id` ↔ `telemetry.param` join은 스키마상 준비됐으나
  실제 조회 코드는 없음.
- **anneal(RTA) 스코프** — 문서 A가 RTA를 언급하나 fab 6스텝 밖. 포함 여부 미결정.
- **VLM 관측 입력 처리** — 현재 6번은 자연어 질문 입력.

---

## 5. 다음에 할 일 후보 (사용자 선택 대기)

1. `5` → `6` 실행 + 정리된 문서에서 ARISES_IN 4건이 다 나오는지 확인 (P1, P2)
2. Equipment id 검증 / 추출 dedup 단계 추가 (P2)
3. `FailureMode.id` 공정 스코프 결정 (P3)
4. `Hypothesis` 투영 단계 구현
5. fab SQL 검증 경로 연결
