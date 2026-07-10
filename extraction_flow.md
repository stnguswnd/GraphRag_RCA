# 텍스트가 지식그래프로 적재되기까지 — 추출/적재 흐름

> 문헌(`.txt`/`.pdf`)의 텍스트가 **어떻게 추출되어 Neo4j에 적재되는가**를,
> 방금 돌린 `ref56_shin2025` 논문 1편 테스트를 실제 예시로 정리한 문서.

---

## 0. 한눈에 보기

```text
PDF/TXT ──2단계──> 텍스트 ──3단계──> 청크(≈500자) ──5단계 LLM──> 후보 노드/관계(넓게)
                                                          │
                                            ┌─────────────┘  validate_kg (가지치기)
                                            ▼
                                      고정 vocabulary·근거에 맞는 것만
                                            │
                                            ▼ (MERGE)
                                         Neo4j 그래프
```

**핵심: 추출은 2단 필터다.**
1. **LLM은 넓게 뽑는다** — 청크에서 그럴듯한 FailureMode/Cause/관계를 다 만든다.
2. **`validate_kg`가 좁힌다** — 고정 목록(DefectPattern 3·ProcessStep 6·Parameter 20)에
   붙고, 원문에 근거가 있고, 신뢰도 ≥ 2인 것만 남긴다. 나머지는 **사유를 남기고 버린다.**

> 즉 "해당되는 것만 추출"되는 게 **최종 결과**는 맞지만, 그 필터링은 LLM이 아니라
> **적재 직전의 `validate_kg`가** 한다. LLM 자체는 과다 추출한다.

---

## 1. 2단계 — 텍스트 추출 (`2_load_txt.py`)

파일 하나 = 문헌 하나(`Document`).

- **`.txt`**: 그대로 읽는다.
- **`.pdf`**: `pypdf`로 **페이지별** 텍스트를 뽑아 빈 줄(`\n\n`)로 이어붙인다.
  텍스트가 없는 스캔 이미지 PDF는 추출 결과가 비어 **조용히 건너뛴다.**
- 공통 정제(`clean_text`): 유니코드 NFC, 줄바꿈 통일, 연속 공백/빈 줄 축소.

결과는 `outputs/parsed_docs.jsonl`에 `doc_id`, `title`, `source`, `file_type`(`txt`|`pdf`)와 함께 저장.

> PDF 추출은 레이아웃을 완벽히 복원하지 못한다. 논문의 **표(表)**는 셀이 세로로
> 흩어져 아래 예시(c27~c30)처럼 깨진 채 들어온다. 이게 뒤 단계 노이즈의 큰 원인이다.

---

## 2. 3단계 — 청킹 (`3_split.py`)

`RecursiveCharacterTextSplitter`로 **문단→줄→문장** 순으로 자른다.
- `chunk_size=500`, `chunk_overlap=80`, 20자 미만 청크는 버림
- `chunk_id = {doc_id}#c00`, `#c01` … (문서별 순번)
- 이 단계는 **소스(txt/pdf) 구분 없이** `parsed_docs.jsonl`만 읽으므로 PDF도 그대로 흐른다.

ref56 논문은 여기서 **192개 청크**가 됐다.

---

## 3. 4단계 — 뼈대 적재 (`4_ingest_chunks_to_neo4j.py`)

아직 지식은 없고 **문서 골격 + 고정 앵커**만 넣는다.
- `(:Document)-[:HAS_CHUNK]->(:Chunk)-[:NEXT_CHUNK]->(:Chunk)`
- 시드 앵커 3종(`DefectPattern`/`ProcessStep`/`Parameter`)을 `MERGE` — 문헌에서 만드는 게 아니라 미리 박아둔다.

---

## 4. 5단계 — LLM 추출 → 가지치기 → 적재 (`5_build_kg_from_chunks.py`)

청크 하나하나에 대해:

1. **`build_prompt`**: 청크 원문 + "고정 목록에서만 골라라"는 규칙을 LLM에 준다.
2. **structured output**: `RcaGraph`(FailureMode/Cause/Equipment/Relationship) 스키마로 강제.
3. **`validate_kg` (가지치기)** — 여기서 버려진다:
   - 앵커 표기 정규화(`edge-ring`→`Edge-Ring`), 못 붙이면 버림
   - `ARISES_IN`은 **원문에 그 공정명이 실제로 있을 때만** (환각 방지)
   - 이 청크에서 안 뽑힌 노드를 가리키는 관계 버림
   - 신뢰도 < 2 버림
   - 어떤 FailureMode도 안 가리키는 **고아 Cause** 버림
4. **Neo4j `MERGE`**: 살아남은 것만 적재. 관계에는 근거로 `chunk_ids`·`quotes`를 붙이고,
   `(:Chunk)-[:MENTIONS]->(노드)`로 어느 청크에서 나왔는지 남긴다.

---

## 5. 실제 예시 (ref56 테스트, Neo4j 미저장 · 확인용)

### 예시 A — 초록/서론 청크는 **0건** (`#c05`)

원문(발췌):
```
Moreover, it can continuously maintain the high classification performance ...
1. Introduction
The semiconductor manufacturing industry remains a key driving force ...
```
→ RCA 내용이 없음. **추출 0, 버림 0.** 논문 192청크 중 **189개가 이런 케이스.**

---

### 예시 B — 표에서 뽑혀 **그대로 적재되는** 청크 (`#c29`)

원문(PDF 표가 깨진 상태로 들어옴):
```
Wafer surface photoresist (PR) rupture due to electron overcharge or
abnormalities in the plasma ion beam implant process
Scratch  A pattern of defects ... Scratches on the wafer surface by
transfer robots during the wafer handling sequence,
```
LLM 추출 → `validate_kg` 통과 → **적재 대상**:
| 종류 | 내용 |
|---|---|
| FailureMode | `wafer_surface_photoresist_rupture`, `scratches_on_wafer_surface` |
| Cause | `electron_overcharge`, `abnormalities_in_the_plasma_ion_beam_implant_process`, `transfer_robots_during_wafer_handling_sequence` |
| 관계 | `wafer_surface_photoresist_rupture -OCCURS_IN-> EDS`<br>`… -CAUSED_BY-> electron_overcharge` / `… -CAUSED_BY-> abnormalities_…`<br>`scratches_on_wafer_surface -OCCURS_IN-> EDS`<br>`… -CAUSED_BY-> transfer_robots_…` |

> ⚠️ 살아남았지만 **품질은 의심스럽다.** `electron_overcharge`, `transfer_robots_…`,
> (다른 청크의) `humans` 같은 Cause는 troubleshooting 문서의 통제된 원인 스타일과 이질적이다.
> "적재됨 ≠ 좋은 데이터"임을 보여주는 예.

---

### 예시 C — LLM은 5건 뽑았지만 **1건만 생존** (`#c27`) ★가지치기 핵심

원문(defect 원인 표가 깨진 상태):
```
Center  Defects concentrated in the center of the wafer
   Irregular radio frequency (RF) operation ... or unusual liquid flow
Donut   ... Accumulation of residues resistant to removal during photoresist cleaning
Edge-Loc  ... Irregular temperature annealing ...
```

**LLM이 만든 후보 → `validate_kg` 판정:**
| LLM 후보 | 판정 |
|---|---|
| `ARISES_IN: Edge-Ring -> CLEAN` | ✅ **생존** (원문에 근거) |
| `ARISES_IN: Center -> ETCH` | ❌ 원문에 'ETCH' 언급 없음 (**환각**) |
| `CAUSED_BY: Center -> irregular_rf_operation` | ❌ Center는 DefectPattern이지 FailureMode가 아님 → 청크 미추출 노드 참조 |
| `CAUSED_BY: Center -> unusual_liquid_flow` | ❌ 동일 |
| `CAUSED_BY: Edge-Ring -> accumulation_of_residues_…` | ❌ 동일 |
| `CAUSED_BY: Edge-Ring -> irregular_temperature_annealing` | ❌ 동일 |
| Cause `irregular_rf_operation`, `unusual_liquid_flow`, `accumulation_…`, `irregular_temperature_annealing` | ❌ **고아 Cause**(어떤 FailureMode도 안 가리킴) |

→ **9건 버림, 1건 적재.** LLM이 "Center/Edge-Ring을 FailureMode처럼" 잘못 쓴 걸
`validate_kg`가 걸러낸다. (다만 살아남은 `Edge-Ring->CLEAN`도 시드 정본 `Edge-Ring->ETCH`와
어긋나 완벽하진 않다.)

---

## 6. 테스트 요약 — ref56 (192청크)

| 지표 | 값 |
|---|---|
| 유효 산출 청크 | **3 / 192** (초록·수식·참고문헌이 대부분) |
| 적재된 FailureMode / Cause / Equipment | 5 / 7 / 0 |
| 적재된 관계 | 13 (`ARISES_IN` 1, `OCCURS_IN` 5, `CAUSED_BY` 7) |
| 버려진 관계·노드 | 50 |
| 토큰 | 입력 507,923 · 출력 10,141 · **합계 518,064** (청크당 ~2,700) |

버림 사유 분포: `ARISES_IN` 공정 환각 22, 고아 Cause 11, 신뢰도<2 10, 미추출 노드 참조 8.

---

## 7. 시사점

- **추출/적재 메커니즘 자체는 정상 작동한다.** LLM 과다추출을 `validate_kg`가 잘 걸러낸다.
- 그러나 **학술 논문은 이 파이프라인과 핏이 나쁘다.** 토큰의 99%를 쓰고 유효 청크는 3개뿐,
  그마저 PDF 표 깨짐 + 통제 안 된 자유텍스트 Cause로 노이즈가 섞인다.
- KG 소스로는 **troubleshooting `.txt` 문서가 적합**하고, 논문은 (a) KG 추출에서 제외하거나
  (b) 원인 서술 섹션만 발췌해 넣는 편이 낫다.

> **후속(2026-07-10 2차):** 위 예시 C에서 버려졌던 `Center → 원인` 같은 지식은
> 스키마 미스매치(`DefectPattern→Cause` 관계 부재)가 원인이었다. 이후:
> - `2_load_txt.py`가 pdf를 **컬럼 인식**으로 추출해 표 행이 살아났고,
> - 새 관계 `ATTRIBUTED_TO`(DefectPattern→Cause)와 `5b_extract_pattern_causes.py`로
>   논문 표의 패턴→원인을 백본에 흡수한다.
> 즉 이 문서의 "예시 C 버림"은 이제 `5b`가 담당해 살린다. 자세한 건 `schema.md`·`STATUS.md`.

> 이 문서의 예시는 `outputs/chunks.jsonl` + 확인용 스크립트(`check_pdf_extraction.py`, Neo4j 미저장)로 재현.
