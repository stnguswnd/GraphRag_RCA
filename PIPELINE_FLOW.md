# 파이프라인 플로우 — 단계별 상세 설명

> 문서 한 편이 **지식그래프**가 되고, 관측된 불량 패턴이 **검증된 원인 가설**이 되기까지
> 데이터가 각 단계에서 어떻게 변형되는지 순서대로 따라간다.

---

## 0. 전체 그림

```
                    [입력]                          [저장소]
 .txt / .pdf 문헌 ───┐
                    │  0_reset      DB 초기화
                    │  1_test       연결 확인
                    ▼
 2_load_txt  ─────────────────────────────────▶ outputs/parsed_docs.jsonl   (문서 텍스트)
                    │                                     │
                    ▼                                     ▼
 3_split     ─────────────────────────────────▶ outputs/chunks.jsonl        (≈500자 청크)
                    │                                     │
        ┌───────────┴───────────┐                        ▼
        ▼                       ▼               ┌──────────────────┐
 4_ingest  (뼈대 적재)   ───────────────────────▶│                  │
        │  Chunk + 앵커                          │      Neo4j       │
        ▼                                        │   지식그래프     │
 5_build_kg   (txt 백본)  ──────────────────────▶│                  │
 5b_pattern_causes (pdf) ───────────────────────▶│                  │
        │                                        └────────┬─────────┘
        ▼                                                 │
 6_ask_graphrag   (질의: 패턴 → 가설)  ◀────────────────────┤
        │                                                 │
        ▼                                        ┌──────────────────┐
 7_verify   (가설 → fab 대조)  ◀──────────────────│  data/fab/fab.db │
                                                 └──────────────────┘
                                                   (telemetry 실측값)
```

**두 갈래를 기억하면 전체가 이해된다:**
- **적재 갈래** (0→5): 문서 → 청크 → 그래프
- **질의 갈래** (6→7): 관측 패턴 → 그래프 순회로 가설 → fab 데이터로 검증

---

## 1. 준비 단계

### `0_reset.py` — DB 초기화
- **왜:** 스키마를 바꾸면(예: 패턴 3→8) `id` 없는 옛 노드가 남아 중복이 생긴다. Neo4j UNIQUE 제약은 null을 무시하므로 `MERGE {id:...}`가 옛 노드를 못 찾는다.
- **동작:** 모든 노드/관계/제약/인덱스를 배치로 삭제. `y` 확인을 묻는다.

### `1_test_connection.py` — 연결 확인
- Neo4j에 붙는지만 확인. 그래프 변경 없음.

---

## 2. 문서 로드 — `2_load_txt.py`

**입력:** `data/docs/`의 `.txt`, `.pdf`
**출력:** `outputs/parsed_docs.jsonl` (문서 1편 = 1줄)

### 하는 일
1. 파일 하나 = 문헌 하나(`Document`).
2. **`.txt`**: 그대로 읽고 정제(유니코드 NFC, 줄바꿈/공백 정리).
3. **`.pdf`**: `pdfplumber`로 **컬럼 인식 추출**.
   - 페이지 가운데 선(`mid = width/2`)을 긋고, 그 선을 걸치는 단어 수(`straddle`)를 센다.
   - `straddle`이 전체의 5% 미만이면 **2단 편집**으로 판단 → 좌 컬럼 전체를 위→아래로 읽고, 그다음 우 컬럼.
   - `x_tolerance=1.0`로 붙은 단어에 공백 복원 (`Irregularradiofrequency` → `Irregular radio frequency`).
   - 텍스트 없는 스캔 PDF는 조용히 건너뜀.

### 왜 컬럼 인식이 필요한가
학술 논문은 2단 + 테두리 없는 표가 흔하다. 단순 추출은 좌/우 칸을 한 줄로 섞어 **"패턴 | 원인" 표가 깨진다.** 컬럼별로 읽으면 행 구조가 살아난다.

```json
// parsed_docs.jsonl 한 줄 (예시)
{"doc_id": "doc_A_wafermap_patterns", "page_content": "...", 
 "metadata": {"title": "...", "source": "...", "file_type": "txt"}}
```

---

## 3. 청킹 — `3_split.py`

**입력:** `parsed_docs.jsonl`
**출력:** `outputs/chunks.jsonl` (청크 1개 = 1줄)

### 하는 일
- `RecursiveCharacterTextSplitter`로 **문단→줄→문장** 순으로 자른다 (`chunk_size=500`, `overlap=80`).
- 20자 미만 청크는 버림.
- `chunk_id = {doc_id}#c00, #c01 …` (문서별 순번).
- **소스(txt/pdf) 구분 없이** 흐른다 — pdf도 여기서부턴 그냥 텍스트.

```json
// chunks.jsonl 한 줄
{"chunk_id": "ref56_..#c12", "chunk_index": 12, "page_content": "Center  Defects concentrated ... Irregular radio frequency (RF) operation ...",
 "metadata": {"doc_id": "ref56_..", "file_type": "pdf", ...}}
```

---

## 4. 뼈대 적재 — `4_ingest_chunks_to_neo4j.py`

**입력:** `chunks.jsonl` + `data/seeds/*.json`
**출력(Neo4j):** Document/Chunk 골격 + 고정 앵커

### 하는 일
1. **제약 생성** — 라벨별 `id` UNIQUE.
2. **시드 앵커 적재** (문헌에서 뽑는 게 아니라 미리 박는 고정 vocabulary):
   - `DefectPattern` 8종 (WM-811K) — 질의 진입점
   - `ProcessStep` 6종 (LITHO/ETCH/DEPO/CMP/CLEAN/EDS) — join 노드. `id` ↔ fab `lot_history.step`
   - `Parameter` 20종 — 검증 종착점. `id` ↔ fab `telemetry.param`
3. **Document/Chunk 골격:**
   - `(:Document)-[:HAS_CHUNK]->(:Chunk)`
   - `(:Chunk)-[:NEXT_CHUNK]->(:Chunk)` (같은 문서 순서)

> 이 시점엔 아직 "지식"은 없다. 빈 그래프 + 앵커 + 청크 텍스트뿐.

---

## 5. 통합 지식 추출 — `5_build_kg_from_chunks.py` (형식 무관 + 원인 표준화 내장)

**입력:** `chunks.jsonl` 전체 중 **패턴/공정을 언급하는 청크**(형식 무관 관련성 게이트, 509→~91).
txt든 pdf든 **한 추출기**가 처리한다. 3-pass 구조:

### pass 1 — 추출 (형식 무관)
청크마다 하나의 프롬프트로, 그 청크가 담을 수 있는 걸 다 뽑는다:
- FailureMode / Cause / Equipment
- 관계 5종: ARISES_IN, OCCURS_IN, CAUSED_BY, INVOLVES_PARAMETER, **ATTRIBUTED_TO**(DefectPattern→Cause)

troubleshooting 문서는 전체 사슬을, 논문 표는 `ATTRIBUTED_TO`(패턴→원인)를 준다 — **같은 추출기**가 둘 다.
이어서 `validate_kg`가 가지치기(앵커 매핑, 환각/노이즈/고아 제거). 결과는 `extracted_kg.jsonl`에 캐시.

### pass 2 — 원인 표준화 (canonicalization) ★핵심
추출된 모든 `Cause`를 **적재 전에** 하나로 정리한다:
1. 임베딩 유사도로 **후보쌍** 생성(재현율).
2. LLM이 "같은 근본원인인가?" **판정**(정밀도).
3. **검증변수 제약**: 한 클러스터에 서로 다른 Parameter(gas_flow vs chamber_pressure 등)가 공존하면 병합 거부.
→ 표현이 달라도 같은 원인이면 한 canonical id 로 합친다.
   (예: 논문 'irregular RF operation' ↔ 백본 'rf_power_drift' → 한 노드)

### pass 3 — 적재
canonical id 로 Neo4j MERGE. 각 Cause 는 여러 소스의 근거(`chunk_ids`, 별칭)를 누적한다.

만들어지는 그래프(백본 + 문헌이 **같은 Cause 에서 만남**):
```
DefectPattern ─ARISES_IN─> ProcessStep <─OCCURS_IN─ FailureMode ─CAUSED_BY─┐
      │                                                                     ▼
      └────────ATTRIBUTED_TO(문헌)───────────────────────────────────────► Cause ─INVOLVES_PARAMETER─> Parameter
```
> **왜 통합했나:** 문서마다 따로 뽑아 사후에 봉합하면 두 층이 겉돈다(원인 표현이 달라 노드가 갈림).
> 추출은 형식 무관으로 하고, **표준화를 적재의 일부**로 넣어 처음부터 하나의 그래프가 되게 한다.
> 추출 캐시가 있으면 `python 5_build_kg_from_chunks.py resume` 로 표준화만 다시 돌린다.

---

## 6. 질의 — `6_ask_graphrag.py`

**입력:** 관측된 DefectPattern (8종 각각)
**출력:** 원인 가설 + 문헌 후보 원인

패턴마다 **쿼리 2개**를 각각 돌린다:

### ① fab 검증 가설 (`HYPOTHESIS_QUERY`, 엄격)
```cypher
MATCH (p:DefectPattern {id:$pattern})-[:ARISES_IN]->(s:ProcessStep)
MATCH (fm:FailureMode)-[:OCCURS_IN]->(s)
MATCH (fm)-[:CAUSED_BY]->(c:Cause)
MATCH (c)-[ip:INVOLVES_PARAMETER]->(param:Parameter)
WHERE s.id IN param.steps          -- 공정 정합성 가드(변수가 그 공정 소속일 때만)
RETURN ...
```
- 완전경로가 다 있어야 나온다(하나라도 끊기면 0건).
- LLM이 이 사실들을 **가설 문장으로 다듬어** 출력.
- 각 가설은 `telemetry.param = 'X' (방향 high/low)` 라는 **검증 지시문**으로 끝난다.

### ② 문헌 기반 후보 원인 (`LITERATURE_QUERY`, 느슨)
```cypher
MATCH (p:DefectPattern {id:$pattern})-[a:ATTRIBUTED_TO]->(c:Cause)
OPTIONAL MATCH (c)-[:INVOLVES_PARAMETER]->(param:Parameter)
RETURN ...
```
- 백본 경로가 없어도 나온다 → **8패턴 전부** 후보를 냄.
- Parameter가 없으면 "정성적 단서"로 표시(검증수단 없음).

> 두 결과를 **별 섹션으로 분리** 출력 → 검증 가능/불가를 섞지 않음.

---

## 7. 검증 — `7_verify.py`

**입력:** 6번이 낸 가설(패턴/공정/파라미터/방향) + `data/fab/fab.db`
**출력:** 각 가설의 `정상/이상 · 가설 지지/기각` 판정

### 사전: 목업 fab 생성 (`data/fab/generate_fab.py`)
- `fab.db`(SQLite): lot_history / telemetry / alarm / maintenance.
- telemetry.param은 20개 Parameter.id와 **정확히 일치**(join key).
- 정상범위는 `fab_model.yaml`에.

### telemetry 데이터 모델 — 왜 이렇게 조인하나 (여기가 헷갈리는 지점)

`telemetry`는 "lot별 표"가 아니라 **"장비가 찍는 센서 시계열"**이다. 구조는 4칸: `(장비, 시각, 파라미터, 값)`.

```
장비       시각            파라미터            값
ETCH-03   06-03 11:00    etch_rate          538.57
ETCH-03   06-03 11:00    rf_power           988.69     ← 한 시각에 여러 파라미터 = 여러 행 (long format)
ETCH-03   06-03 13:00    etch_rate          538.55     ← 2시간 뒤 또 찍음 (시계열)
...
```

**결정적 특징: `telemetry`에는 `lot_id`가 없다.** 장비 센서는 "지금 처리 중인 웨이퍼가 뭔지" 모르고, "이 장비가 이 시각에 이 값을 찍었다"만 안다(실제 팹의 SECS/GEM S6F11 구조). 그래서 telemetry만으로는 "LOT-0003의 etch_rate"를 못 고른다.

**다리는 `lot_history`다.** "어느 lot이 어느 장비를 언제~언제 점유했는지"를 안다:
```
lot        공정    장비        ts_in            ts_out
LOT-0003   ETCH   ETCH-03    06-03 11:00     06-03 20:00
```
이 **시간창**으로 telemetry를 자르면, 그때 ETCH-03이 찍은 값 = 그때 처리 중이던 LOT-0003의 값.

```
lot_history (lot 관점)                 telemetry (장비 관점, lot_id 없음)
┌──────────────────────────┐          ┌────────────────────────────────┐
│ LOT-0003 / ETCH / ETCH-03│          │ ETCH-03 11:00 etch_rate 538.57 │◄─┐
│   11:00 ──────── 20:00    │  장비+   │ ETCH-03 13:00 etch_rate 538.55 │  │ 이 시간창
└───────────┬──────────────┘  시간창  │ ETCH-03 15:00 etch_rate 532.43 │  │ 안의 값이
            │              ─────────▶ │ ETCH-03 17:00 etch_rate 529.89 │  │ LOT-0003 것
            │                         │ ETCH-03 19:00 etch_rate 538.00 │◄─┘
            └───────────────────────▶ │ ETCH-03 21:00 etch_rate 501.20 │← 다른 lot(창 밖)
                                       └────────────────────────────────┘
```

### 검증 동작
1. 용의 lot이 그 공정에서 쓴 **장비 + 처리 시간창**을 `lot_history`에서 찾는다.
   ```sql
   SELECT equipment_id, ts_in, ts_out FROM lot_history
   WHERE lot_id='LOT-0003' AND step='ETCH';        -- → ETCH-03, 11:00, 20:00
   ```
2. 그 **장비 + 시간창** 안의 `telemetry`에서 해당 `param` 값을 격리.
   ```sql
   SELECT value FROM telemetry
   WHERE equipment_id='ETCH-03' AND param='etch_rate'
     AND ts BETWEEN '11:00' AND '20:00';           -- → [538.57, 538.55, 532.43, 529.89, 538.00]
   ```
3. `fab_model.yaml`의 정상범위와 대조. 방향별 **대표값**으로 판정: `high`→최댓값, `low`→최솟값.
   범위를 **예측한 방향으로** 벗어나면 **가설 지지**.

```
Edge-Ring / LOT-0003
  ◎ etch_rate  이상 (대표값 538.57 > 정상 480~520)  → 가설 지지
```

> **판정이 둘인 점 주의:** `정상/이상`(범위 안인가)과 `지지/기각`(예측한 방향으로 벗어났나)은 다르다.
> 가설이 "높다"인데 값이 오히려 **낮으면** → "이상 / 기각"(이상은 맞지만 이 가설은 아님).

> **핵심:** 6번은 "etch_rate를 확인하라"는 **지시문**까지, 7번은 그 값을 실제 조회해 **판정**까지. 이 둘이 이어져 KG 가설 → fab 검증 루프가 닫힌다.

---

## 부록 A — 하나의 예시가 전 과정을 통과하는 경로

관측: 웨이퍼에 **Edge-Ring** 패턴.

| 단계 | 이 예시에서 벌어지는 일 |
|---|---|
| 2·3 | `doc_B_etch_troubleshooting.txt` → 청크들. ref56 표 청크엔 "Edge-Ring ← RTP 온도 이상". |
| 4 | `DefectPattern{Edge-Ring}`, `ProcessStep{ETCH}` 등 앵커 적재. |
| 5 | txt에서 `Edge-Ring -ARISES_IN-> ETCH ... -INVOLVES_PARAMETER-> etch_rate` 추출, 논문 표에서 `Edge-Ring -ATTRIBUTED_TO-> anomalous_temperature_...` 추출 → 표준화로 같은 원인은 한 노드로 합침. |
| 6 | ① 가설: "ETCH 공정 post-etch residue, etch_rate가 높은지 확인". ② 문헌: "RTP 온도 이상"(융합됐으면 검증변수까지). |
| 7 | LOT-0003의 ETCH 텔레메트리에서 `etch_rate=538.6` 조회 → 정상 480~520 초과 → **가설 지지**. |

---

## 부록 B — 실행 순서

```bash
python 0_reset.py                      # 스키마 변경 후 필수
python 2_load_txt.py                   # 문서 로드 (txt + pdf 컬럼 인식)
python 3_split.py                      # 청킹
python 4_ingest_chunks_to_neo4j.py     # 앵커 + Chunk 골격
python 5_build_kg_from_chunks.py       # 통합 추출 + 원인 표준화
python 6_ask_graphrag.py               # 가설 + 문헌 후보 원인
python data/fab/generate_fab.py        # 목업 fab.db 생성
python 7_verify.py                     # fab 텔레메트리로 검증
```

> 관련 문서: 스키마 명세 `schema.md` · 작업 이력 `WORKLOG_2026-07-10.md` · 추출 원리 `extraction_flow.md`
