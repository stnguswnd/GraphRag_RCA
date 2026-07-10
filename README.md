# Wafer Defect RCA — GraphRAG

반도체 웨이퍼 결함의 **근본원인 분석(RCA)** 을 위한 지식그래프 파이프라인.

공정 문헌에서 "어떤 불량 패턴이 어느 공정을 의심케 하는가",
"그 공정에서 어떤 고장 모드가 어떤 원인으로 생기는가", "그 원인은 어떤 fab 신호로 검증하는가"를
LLM으로 추출해 Neo4j 그래프로 만들고, 관측된 패턴에 대한 **원인 가설 목록**을 생성한다.

그래프는 가설 **생성**까지만 책임진다. 채택/기각은 fab 데이터(SQL)의 몫이며,
그래프는 `Evidence` 노드(`Parameter`/`Maintenance`/`Recipe`)로 그 SQL과 이어진다.

```text
data/raw/ 문헌 (표·산문)
  -> 로드              (2_load_txt.py)
  -> 표 행 단위 청킹    (3_split.py)
  -> 시드 앵커 + Chunk 적재 (4_ingest_chunks_to_neo4j.py)
  -> LLM 추출 + 검증 규칙 (5_build_kg_from_chunks.py)
  -> 결정적 순회 + 가설 출력 (6_ask_graphrag.py)  -> stdout + outputs/hypotheses.json
```

## 그래프 구조 (schema v2.2)

```text
                ┌──ATTRIBUTED_TO───────────────────────────┐   (문헌이 공정을 안 밝힐 때)
                │                                           ▼
DefectPattern ──ARISES_IN──> ProcessStep <──OCCURS_IN── FailureMode
 (Edge-Ring)                   (ETCH)        (join)   (incorrect_etch_rate)
                                                            │ CAUSED_BY
                                                            ▼
                                                          Cause
                                                            │ VERIFIED_BY
                                     ┌──────────────────────┼──────────────────────┐
                                     ▼                      ▼                      ▼
                                Parameter              Maintenance              Recipe
                             → telemetry.param       → maintenance         → lot_history.recipe_id
                                 [자동]                  [반자동]               [반자동]
```

- **질의 진입점:** `DefectPattern` (고정 3종: `Center` / `Scratch` / `Edge-Ring`)
- **join 노드:** `ProcessStep` — 패턴 문서와 troubleshooting 문서가 만나는 지점
- **검증 종착점:** `:Evidence` (Parameter / Maintenance / Recipe, `fab_table` 프로퍼티로 조회 대상 명시)

가설 1건 = 경로 1개. 두 갈래가 있다:
`DefectPattern → ProcessStep → FailureMode → Cause → Evidence` (공정 경유),
`DefectPattern → Cause` (문헌 직결, `ATTRIBUTED_TO`).

**검증 등급** — "fab.db에 있느냐"가 아니라 **"agent가 스스로 판정할 수 있느냐"**로 가른다:

| 등급 | Evidence | 근거 |
|---|---|---|
| `[자동]` | `Parameter` | `telemetry.param`과 결정적 조인 + 정상범위 판정 → agent가 결론까지 |
| `[반자동]` | `Maintenance` / `Recipe` | fab 조회는 되지만 조인 키·기대값이 없어 판정은 사람 몫 |
| `[근거없음]` | 없음 | 문헌 서술로만 존재 (예: RTP 원인 — fab 6스텝 밖) |

전체 명세는 [`schema_v2.md`](schema_v2.md) (정본), fab 데이터 스키마는 [`fab.md`](fab.md) 참조.
`schema.md`는 v1 기록용.

## 준비

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows
pip install -r requirements.txt
cp .env_example .env            # NEO4J_*, OPENAI_API_KEY 채우기
```

## 실행

```bash
python 1_test_connection.py            # Neo4j 연결 확인
python 0_reset.py                      # DB 전체 초기화 (스키마 변경 후 필수, y 확인)
python 2_load_txt.py                   # data/raw/ -> outputs/parsed_docs.jsonl
python 3_split.py                      #           -> outputs/chunks.jsonl (표 행 = 청크)
python 4_ingest_chunks_to_neo4j.py     # 시드 앵커(:Evidence 포함) + Document/Chunk 적재
python 5_build_kg_from_chunks.py       # LLM 추출 -> outputs/extracted_kg.jsonl + Neo4j
python 6_ask_graphrag.py               # 패턴별 가설 전건 -> stdout + outputs/hypotheses.json
```

- `6_ask_graphrag.py`는 기본으로 탐색된 **모든** 가설을 낸다. 상한을 두려면 `TOP_K=3 python 6_ask_graphrag.py`.
- 질문은 `"{패턴} 결함 패턴이 나타나는 근본 원인은 무엇인가요?"` 하나로 고정.
  Cypher는 LLM이 생성하지 않는다(결정적 순회). LLM은 경로를 한국어 문장으로 옮기고 관계를 추출할 때만 쓴다.
- `outputs/hypotheses.json`: 가설마다 경로·검증 등급·`direction`·`fab_table`·점수 성분·
  근거(`chunk_ids`/`quotes`)를 담은 구조화 출력. hypothesis agent의 입력용.

> `0_reset.py`를 건너뛰고 스키마를 바꾸면 중복 노드가 생긴다.
> Neo4j의 UNIQUE 제약은 null을 무시하므로, `id`가 없는 옛 노드를 `MERGE {id: ...}`가 찾지 못한다.

## 데이터

```text
data/
  raw/      실문헌 (파이프라인 입력. .txt / .md / 무확장자, 하위 디렉토리는 제외)
    center_pattern_cause.txt, pattern_cause, scratch_pattern_cause
                                       문서 A: 패턴 -> 공정 산문        (ARISES_IN)
    Semiconductor Devices..._troubleshootingTABLE.md
                                       문서 B: 교과서 트러블슈팅 표 82행 (FailureMode -> Cause -> Evidence)
    ref56_table1_pattern_causes.md     문서 C: 논문 Table 1, 패턴 -> 원인 직결 (ATTRIBUTED_TO)
    _reference/                        교과서 본문 339KB — 로더가 읽지 않음
  seeds/    고정 vocabulary (문헌에서 뽑지 않고 미리 적재하는 앵커)
    defect_patterns.json   3종   VLM 출력 클래스와 일치해야 함
    process_steps.json     6종   join key: lot_history.step
    parameters.json       20종   join key: telemetry.param  (steps 필드 = 별칭 해석 스코프)
```

`FailureMode` / `Cause` / `Maintenance` / `Recipe`만 LLM이 문헌에서 자유롭게 만든다.
앵커 3종(`DefectPattern`/`ProcessStep`/`Parameter`)은 시드에 있는 노드에 **연결만** 한다 —
LLM이 뱉은 표기(`circular ring`, `etching step`, `RF Power`)는 시드 `aliases` 역인덱스로
canonical id에 치환된 뒤 `MATCH`로만 붙으므로, 시드 밖 노드는 생길 수 없다.
`Parameter`는 공정 조건부로 해석된다 (`temperature`가 ETCH에선 `temperature`, DEPO에선 `susceptor_temp`).

시드 id는 추출 코드의 `Literal`에도 하드코딩돼 있어, 시드만 바꾸면 실행 즉시
`assert_enums_match_seeds()`가 예외를 던진다. 둘을 함께 고칠 것.

## 현재 상태

진행 상황·남은 문제·다음 작업은 [`STATUS.md`](STATUS.md) 참조.
최신 실행: 문헌 5편 → 청크 95개 → 가설 **125건** (Center 62 / Edge-Ring 53 / Scratch 10).
