# Wafer Defect RCA — GraphRAG

반도체 웨이퍼 결함의 **근본원인 분석(RCA)** 을 위한 지식그래프 파이프라인.

공정 문헌(`.txt` / `.pdf`)에서 "어떤 불량 패턴이 어느 공정을 의심케 하는가",
"그 공정에서 어떤 고장 모드가 어떤 원인으로 생기는가", "그 원인은 어떤 계측 변수와 얽히는가"를
LLM으로 추출해 Neo4j 그래프로 만든다.

그래프는 **원인 가설을 생성**하는 데까지만 책임진다.
가설 검증은 별도 fab 데이터(SQL)가 맡고, 그래프는 `Parameter` 노드로 그 SQL과 이어진다.

```text
문헌 (.txt / .pdf / 향후 어떤 형식이든)
  -> 로드/정제        (2_load_txt.py)           # pdf는 컬럼 인식 추출(2단·표 대응)
  -> 청킹             (3_split.py)
  -> 시드 앵커 + Chunk 적재 (4_ingest_chunks_to_neo4j.py)
  -> 통합 KG 추출     (5_build_kg_from_chunks.py)      # 형식 무관 추출 + 원인 표준화 내장
  -> 질의 + 검증      (6_ask_graphrag.py / 7_verify.py)
```

`5`는 **하나의 추출기**로 txt·pdf를 모두 처리하고, 표현이 다른 같은 원인을
**적재 시점에 한 노드로 표준화(canonicalization)**한다. 그래서 문서 출처가 달라도
같은 Cause 를 공유한다(사후 봉합 없음). 공용 유틸은 `kg_common.py`.

## 그래프 구조

```text
DefectPattern ──ARISES_IN──> ProcessStep <──OCCURS_IN── FailureMode
 (Edge-Ring)                   (ETCH)                (post_etch_residue)
                                 ▲                          │ CAUSED_BY
Equipment ──PART_OF──────────────┘                          ▼
 (ETCH-03)                                                Cause
                                                     (high_etch_rate)
                                                            │ INVOLVES_PARAMETER
                                                            ▼
                                                        Parameter
                                                      (etch_rate → fab SQL)
```

- **질의 진입점:** `DefectPattern` (고정 3종)
- **join 노드:** `ProcessStep` — 패턴 문서와 troubleshooting 문서가 만나는 지점
- **검증 종착점:** `Parameter` (`Parameter.id` ↔ fab `telemetry.param`)

`DefectPattern → ProcessStep → FailureMode → Cause → Parameter` 경로 하나하나가 **가설 1건**이다.

전체 명세는 [`schema.md`](schema.md), fab 데이터 스키마는 [`fab.md`](fab.md) 참조.

## 준비

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows
pip install -r requirements_macos.txt
cp .env_example .env            # NEO4J_*, OPENAI_API_KEY 채우기
```

## 실행

```bash
python 1_test_connection.py            # Neo4j 연결 확인
python 0_reset.py                      # DB 전체 초기화 (스키마 변경 후 필수)
python 2_load_txt.py                   # data/docs/*.{txt,pdf} -> outputs/parsed_docs.jsonl
python 3_split.py                      #                -> outputs/chunks.jsonl
python 4_ingest_chunks_to_neo4j.py     # 시드 앵커 + Document/Chunk 적재
python 5_build_kg_from_chunks.py       # 통합 추출 + 원인 표준화 -> extracted_kg.jsonl + Neo4j
python 6_ask_graphrag.py               # 가설 + 문헌 기반 후보 원인
python data/fab/generate_fab.py        # (검증용) 목업 fab.db 생성
python 7_verify.py                     # KG 가설을 fab 텔레메트리로 검증
```

> `5`는 형식(txt/pdf)을 가리지 않고 **관련 청크만** 추출한 뒤 원인을 표준화한다.
> 추출은 `extracted_kg.jsonl`에 캐시되므로, 표준화만 다시 돌리려면 `python 5_build_kg_from_chunks.py resume`.

> `0_reset.py`를 건너뛰고 스키마를 바꾸면 중복 노드가 생긴다.
> Neo4j의 UNIQUE 제약은 null을 무시하므로, `id`가 없는 옛 노드를 `MERGE {id: ...}`가 찾지 못한다.

## 데이터

```text
data/
  docs/
    doc_A_wafermap_patterns.txt      패턴 -> 공정        (ARISES_IN)
    doc_B~doc_G_*_troubleshooting.txt  공정별 고장 -> 원인 -> 변수
    ref*.pdf                         학술 논문 (패턴->원인 표 등)
  seeds/    고정 vocabulary (문헌에서 뽑지 않고 미리 적재하는 앵커)
    defect_patterns.json   8종   WM-811K, VLM 출력 클래스와 정렬
    process_steps.json     6종   join key: lot_history.step
    parameters.json       20종   join key: telemetry.param
```

- `5`가 **하나의 추출기**로 모든 문서를 처리한다. troubleshooting 문서는 전체 인과 사슬
  (FailureMode/Cause/관계)을, 논문 표는 `DefectPattern -[:ATTRIBUTED_TO]-> Cause`를 준다 — 같은 추출기가 둘 다 뽑는다.
- 추출된 `Cause`는 **적재 전 표준화**된다: 표현이 달라도 같은 근본원인(같은 검증변수)이면 한 노드로 합쳐,
  txt·pdf가 처음부터 같은 Cause 를 공유하고 논문 원인도 검증변수를 승계한다.

`FailureMode` / `Cause` / `Equipment`만 LLM이 문헌에서 자유롭게 만든다.
나머지 세 라벨은 시드에 있는 것에 **연결만** 하고 새로 만들지 않는다.

## 현재 상태

진행 상황과 남은 문제는 [`STATUS.md`](STATUS.md) 참조.
