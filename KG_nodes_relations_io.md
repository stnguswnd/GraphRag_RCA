# KG 노드 · 관계 · KG 노드 입출력 형식 (입문용)

> `KG_schema_v0.5.md`에서 발췌해 처음 보는 사람 기준으로 풀어 쓴 문서.
> 설계 결정의 이유·대안 검토는 원본, 런타임 계약의 정본은 `KG_runtime_v0.2.md`.

---

## 0. 3분 요약 — 이 시스템에서 KG가 하는 일

우리 시스템은 **웨이퍼맵 불량의 원인을 추적(RCA)하는 멀티에이전트**다. 그 안에서 지식그래프(KG)의 역할은 딱 하나:

> VLM이 "이 웨이퍼는 Edge-Ring 패턴이다"라고 판독하면,
> **문헌 근거가 있는 원인 가설 목록**과 **각 가설을 검증할 MCP 도구 지시서**를 돌려준다.

```
VLM 판독 ──> [KG 노드] ──> 가설 목록 ──> 가설 에이전트 ──> Critic ──> 보고서
 (관측)      (문헌 조회)   (원인 후보)     (증거 수집)      (판정)
```

증거 수집과 참/거짓 판정은 KG의 일이 아니다. KG는 **"문헌에 따르면 이런 원인들이 가능하고, 이렇게 확인해봐라"까지만** 책임진다.

### 그래프가 왜 2개인가

Neo4j 인스턴스를 두 개 띄운다. 이유는 **LLM이 Cypher 질의를 직접 쓰기(Text2Cypher) 때문**이다.

| | 빌드 그래프 `kgbuild` (:7687) | 질의 뷰 `rca` (:7688) |
|---|---|---|
| 뭐가 들었나 | 문헌에서 추출한 원인·인과관계의 원본 그래프 | 그걸 미리 계산해 평탄화한 `Hypothesis` 노드 |
| 누가 쓰나 | 사람(감사), 투영 스크립트 | **Text2Cypher(LLM)**, 런타임 |
| 비유 | 원장(原帳) | 조회 전용 요약 테이블 |

원본 그래프를 LLM에게 직접 열어주면 깊이 제한, 신뢰도 집계 같은 규칙을 LLM이 매번 지켜야 하는데, LLM은 확률적이라 언젠가 어긴다. 그래서 **규칙이 필요한 계산은 전부 빌드 타임에 코드로 끝내고**(=투영), LLM에게는 계산이 끝난 결과만 보여준다. `rca`에는 원본 인과 그래프 자체가 없어서, LLM이 아무리 창의적인 질의를 써도 규칙을 어길 방법이 물리적으로 없다.

### 용어 미니 사전

| 용어 | 뜻 |
|---|---|
| **시드(seed)** | 문헌 추출 전에 코드로 미리 만들어두는 노드. 이름이 외부 시스템과 약속돼 있어서 LLM이 마음대로 지으면 안 되는 것들 |
| **앵커(anchor)** | 시드된 4종 노드(`DefectPattern`, `ProcessStep`, `ParameterType`, `DetectionMethod`). 문헌이 언급하면 새로 만들지 않고 여기에 연결한다 |
| **투영(projection)** | `kgbuild`의 경로들을 계산해 `rca`의 `Hypothesis` 노드로 굳히는 빌드 타임 작업 |
| **표면 노드** | 결함 패턴으로 직접 발현되는 원인 (`MANIFESTS_AS` 엣지를 가진 `Cause`) |
| **join key** | KG의 값이 실제 팹 데이터(`fab.db`)의 어느 컬럼과 문자열이 일치해야 하는지. 어긋나면 KG와 데이터를 연결 못 한다 |

---

## 1. 노드 — "그래프에 어떤 점들이 있나"

### 1.1 빌드 그래프 (`kgbuild`)의 노드 6종

핵심 구도: **`Cause`(원인) 하나만 LLM이 문헌에서 자유롭게 만들고, 나머지는 전부 미리 시드**한다. 이름이 제멋대로면("edge ring defect" vs "Edge-Ring") VLM 출력이나 팹 데이터와 연결이 끊기기 때문이다.

| 노드 | 뭔가 | 이름은 누가 정하나 |
|---|---|---|
| `DefectPattern` | 웨이퍼맵 불량 패턴 (Edge-Ring 등 9종) | 시드 — VLM 출력 클래스와 동일해야 함 |
| `Cause` ⭐ | 불량의 원인 (예: 포커스링 마모) | **LLM 문헌 추출** — 유일한 개방 엔티티 |
| `ProcessStep` | 공정 단계 (ETCH 등 6종) | 시드 — `fab.db`의 `lot_history.step`과 동일해야 함 |
| `ParameterType` | 공정 변수 (rf_power 등 20종) | 시드 — `telemetry.param`과 동일해야 함 |
| `DetectionMethod` | 원인 확인용 MCP 도구 (9종) | 시드 — 서버 등록 도구명과 동일해야 함 |
| `Chunk` | 문헌 조각 (근거 원문) | 파이프라인 (청킹+임베딩) |

#### `(:DefectPattern)` — 9종 고정

| Property | Type | 설명 |
|---|---|---|
| `name` | enum | `Center` \| `Donut` \| `Edge-Loc` \| `Edge-Ring` \| `Loc` \| `Near-Full` \| `Scratch` \| `Random` \| `Normal` |
| `aliases` | list[str] | 문헌 속 별칭 사전. "edge ring defect" 같은 표현을 여기로 흡수 |
| `spatial_keywords` | list[str] | VLM 서술문이 패턴과 맞는지 확인하는 용도 — **연결(매칭)에 쓰면 안 됨** (Edge-Ring과 Donut이 둘 다 "ring"을 가짐) |
| `expected_zone` / `expected_shape` | list[str] | 같은 정합성 체크용. 무관하면 `["any"]` |

#### `(:Cause)` ⭐ — 유일하게 문헌에서 자라는 노드

| Property | Type | 설명 |
|---|---|---|
| `uid` | str | **유일 키** `"{step}:{name}"` — 예: `"ETCH:focus_ring_erosion"` |
| `name` | str | 대표 이름 |
| `step` | enum | 어느 공정에서 생기는 원인인지 |
| `cause_type` | enum | `Parameter` \| `Machine` \| `Material` \| `Method` \| `Man` (5M 분류) |
| `description` | str | **완결된 한 문장.** 나중에 가설 문장(`statement`)을 이 문장들을 이어 붙여 만들기 때문에 규약이 엄격함 |
| `aliases` | list[str] | 정규화 때 흡수한 원 표현들 |

- `uid`에 step이 들어가는 이유: 같은 `rf_power drift`라도 ETCH와 DEPO에서는 **다른 원인**이라서 (정상 범위부터 다름).
- 공정 단계를 알 수 없는 원인은 적재하지 않고 격리한다 (`OCCURS_IN` 필수).
- "Failure Mode냐 Root Cause냐"는 별도 라벨이 없다. **그래프에서의 위치로 판별**한다: 패턴에 직접 연결되면 표면(Failure Mode), 인과 사슬 끝이면 Root Cause.

#### `(:ProcessStep)` — 6종

`name` (`LITHO`|`ETCH`|`DEPO`|`CMP`|`CLEAN`|`EDS`), `aliases`. join key: `lot_history.step`

#### `(:ParameterType)` — 20종

`name`, `aliases`. join key: `telemetry.param`.
정상범위·단위는 **저장하지 않는다** — 조회 시점에 `query_telemetry` 도구가 돌려주기 때문 (그리고 정답 누출 방지, 원본 §2).

#### `(:DetectionMethod)` — MCP 도구 9종과 1:1

`name`, `mcp_tool`, `role`, `args_template`. `role`이 중요하다:

- **`verify` (5종)** — "이 원인이 맞는지" 가설별로 확인하는 도구. 가설에 실려 나감.
- **`pipeline` (4종)** — 가설과 무관하게 오케스트레이터가 항상 부르는 도구. 가설에 안 실림.

| `name` | `mcp_tool` | `role` | 뭘 확인하나 |
|---|---|---|---|
| `telemetry_check` | `query_telemetry` | verify | 공정 변수가 실제로 튀었나 |
| `maintenance_check` | `get_maintenance_history` | verify | 정비 이력에 단서가 있나 |
| `alarm_check` | `get_alarm_history` | verify | 알람이 울렸나 |
| `negative_control` | `get_normal_lot_ratio` | verify | 같은 장비의 정상 lot 비율 (반대근거) |
| `yield_change_point` | `detect_change_points` | verify | 수율이 언제부터 꺾였나 |
| `commonality` | `run_commonality_analysis` | pipeline | 불량 lot들의 공통 장비 |
| `lot_route` | `get_lot_history` | pipeline | lot이 거친 경로 |
| `timeline_check` | `get_lot_timeline` | pipeline | lot 시간축 |
| `wafer_map` | `get_wafer_map` | pipeline | 웨이퍼맵 원본 |

#### `(:Chunk)` — 문헌 조각

`chunk_id`(유일), `doc_id`, `title`, `text`, `embedding`. 모든 인과 주장의 "출처 원문"이 여기 있다.

### 1.2 질의 뷰 (`rca`)의 노드 3종

| 노드 | 용도 |
|---|---|
| `Hypothesis` | **가설 하나 = 노드 하나.** Text2Cypher가 보는 유일한 스키마 (형식은 §4) |
| `DefectPattern` | 9종 그대로 복사. VLM 판독 정합성 체크용 |
| `Chunk` | 근거 원문 회수 + 임베딩 폴백용 |

> **여기에 없는 것이 핵심이다.** `Cause`, `CAUSED_BY`, `RELATED_TO`는 `rca`에 존재하지 않는다. 인과 사슬은 이미 `Hypothesis` 안에 문자열 리스트로 평탄화돼 있다. 그래서 LLM이 그래프를 잘못 순회하는 사고 자체가 불가능하다 (검증 V9-1).

---

## 2. 관계 — "점들을 잇는 선"

한 문장이 그래프가 되는 예부터 보면 빠르다:

> *"Focus ring erosion **in the etch chamber** leads to elevated **RF power** and produces a characteristic **edge ring defect**."*

```
(Cause: focus_ring_erosion)
   ├─[:OCCURS_IN]──────────> (ProcessStep: ETCH)        "etch chamber에서"
   ├─[:INVOLVES_PARAMETER {direction:'high'}]─> (ParameterType: rf_power)
   └─(Cause: etch_rate_nonuniformity)
        ├─[:CAUSED_BY]──────> (focus_ring_erosion)       "erosion이 원인"
        └─[:MANIFESTS_AS]───> (DefectPattern: Edge-Ring)  "링 불량으로 발현"
```

### 2.1 빌드 그래프의 관계

**인과 백본 — 가설 생성의 재료**

| # | 관계 | 읽는 법 | 고유 Property |
|---|---|---|---|
| ① | `(:Cause)-[:MANIFESTS_AS]->(:DefectPattern)` | "이 원인은 이 패턴으로 나타난다" | `occurrence_prior` (`high`\|`mid`\|`low` — 문헌의 "commonly/rare" 서술) |
| ② | `(:Cause)-[:CAUSED_BY]->(:Cause)` | "이 원인의 배후에 저 원인이 있다" | 재귀 가능, 투영 시 깊이 2까지만 |
| ③ | `(:Cause)-[:OCCURS_IN]->(:ProcessStep)` | "이 원인은 이 공정에서 생긴다" | — (모든 Cause에 정확히 1개) |
| ④ | `(:Cause)-[:INVOLVES_PARAMETER]->(:ParameterType)` | "이 원인은 이 변수의 이상과 얽힌다" | `direction`: `high` \| `low` |

**조사·조치**

| # | 관계 | 읽는 법 | 생성 주체 |
|---|---|---|---|
| ⑤ | `(:Cause)-[:DETECTED_BY]->(:DetectionMethod)` | "이 원인은 이 도구로 확인한다" | **규칙 로더** (LLM 아님, 아래 규칙표) |
| ⑥ | `(:Cause)-[:MITIGATED_BY]->(:Action)` | "이 원인은 이렇게 조치한다" | LLM (2순위 기능) |

**프로버넌스**: `(:Chunk)-[:MENTIONS]->(:Cause)` — "이 문헌 조각이 이 원인을 언급했다"

**안전망**: `(:X)-[:RELATED_TO]->(:Y)` — LLM 추출물 중 ①~⑥ 어디에도 못 들어간 관계의 보관소. 버리지 않고 감사용으로 남기되, **`rca`로는 절대 넘어가지 않는다.**

**인과 엣지(①②⑤⑥)가 공통으로 갖는 Property**

| Property | Type | 설명 |
|---|---|---|
| `extraction_confidence` | float 1~5 | LLM이 매긴 추출 신뢰도. 중복 추출 시 평균. **2 미만은 투영 전 폐기** |
| `description` | str | 완결된 한 문장 — 가설 문장의 조립 부품 |
| `chunk_ids` | list[str] | 이 관계의 근거 문헌 조각 |
| `quotes` | list[str] | 근거 스니펫 (15단어 이내) |

**⑤ `DETECTED_BY`는 규칙으로 자동 부여** (도구가 9개뿐이라 LLM에 맡길 이유가 없음)

| Cause가 이런 조건이면 | 이 도구를 붙인다 |
|---|---|
| 공정 변수와 얽혀 있음 (④ 보유) | `telemetry_check` |
| `cause_type`이 Machine/Material | `maintenance_check`, `alarm_check` |
| `cause_type`이 Method/Man | `alarm_check` |
| 모든 Cause | `negative_control` (반대근거 확인은 필수) |
| 표면 노드 (① 보유) | `yield_change_point` |

### 2.2 질의 뷰의 관계 2종

| 관계 | 용도 |
|---|---|
| `(:Hypothesis)-[:FOR_PATTERN]->(:DefectPattern)` | 정합성 체크용 (코드의 고정 질의만 사용) |
| `(:Hypothesis)-[:EVIDENCED_BY]->(:Chunk)` | 근거 원문 회수용 (코드의 고정 질의만 사용) |

Text2Cypher에게 알려주는 스키마에는 이 둘도 **넣지 않는다.** LLM은 `Hypothesis` 노드 하나만 안다.

---

## 3. KG 노드 입력 형식 — "KG 노드가 받는 것"

앞 단계인 **VLM observer**의 판독 결과다. VLM은 순수 관측자로 계약돼 있다: 웨이퍼맵에서 **보이는 것만** 말하고, 원인 추론은 금지 (`description`에 "식각", "챔버" 같은 공정/원인 어휘가 나오면 계약 위반 → 후처리 밸리데이터가 잡아서 재생성).

```json
{
  "patterns": ["Edge-Ring"],
  "spatial": { "zone": "edge", "direction": "omni", "shape": "ring" },
  "description": "웨이퍼 가장자리를 따라 링 형태로 불량 die가 밀집한다.",
  "discriminative_evidence": "가장자리 전방위 연속 링 — 국소 군집(Edge-Loc)과 구분됨.",
  "severity": { "defect_die_ratio": 0.18 },
  "confidence": "high",
  "ambiguity": null
}
```

| 필드 | 누가 채우나 | KG 노드가 어디에 쓰나 |
|---|---|---|
| `patterns[]` | VLM (9종 enum) | **진입점.** 이 값으로 `Hypothesis.pattern`을 조회한다 |
| `spatial.zone/shape` | VLM (enum) | `DefectPattern.expected_zone/shape`와 대조 — 판독이 앞뒤가 맞는지 |
| `description` | VLM | `spatial_keywords`와 대조 (같은 정합성 체크) |
| `discriminative_evidence` | VLM | "왜 Edge-Loc이 아니라 Edge-Ring인가" — 가설 에이전트가 참조 |
| `severity.defect_die_ratio` | **코드가 계산** | VLM은 픽셀을 못 세므로 추정 금지. 심각도 참고치 |
| `confidence` | VLM (high/mid/low) | low면 `ambiguity` 기입 필수 |
| `ambiguity` | VLM | 애매한 케이스 → 임베딩 폴백 경로 판단 |

`patterns`가 9종을 벗어나거나 정합성 체크에 실패하면, 고정 질의 대신 `Chunk` 임베딩 검색(벡터 폴백)으로 빠진다.

> 정본 계약은 `KG_runtime_v0.2.md`. 필드가 다르면 그쪽이 우선.

---

## 4. KG 노드 출력 형식 — "KG 노드가 내보내는 것"

**가설 하나 = 딕셔너리 하나.** `rca`의 `Hypothesis` 노드가 그대로 행으로 나온다고 보면 된다. 아래 키는 전부 필수이며, 하나라도 빠지면 `normalize_rows()`가 예외를 던진다.

예시 — "Edge-Ring의 배후 원인이 포커스링 마모"라는 가설 하나:

| 필드 | Type | 값 예시 | 설명 |
|---|---|---|---|
| `hid` | str | `"Edge-Ring/etch_rate_nonuniformity/focus_ring_erosion"` | 가설 고유 ID (패턴+인과 경로) |
| `pattern` | str | `"Edge-Ring"` | 어느 패턴에 대한 가설인지 |
| `statement` | str | `"포커스링이 마모되면 … 링 형태의 불량으로 발현한다."` | 사람이 읽는 가설 문장 (description들을 이어 붙임, LLM 미사용) |
| `causal_path` | list[str] | `["Edge-Ring","etch_rate_nonuniformity","focus_ring_erosion"]` | 인과 경로 (패턴 → 표면 → 심층) |
| `surface_cause` | str | `"etch_rate_nonuniformity"` | 표면 원인 |
| `cause` | str | `"focus_ring_erosion"` | 이 가설이 지목하는 원인 |
| `cause_type` | str | `"Machine"` | 5M 분류 |
| `step_group` | str | `"ETCH"` | 의심 공정 — `lot_history.step`과 join |
| `depth` | int | `1` | 인과 사슬 깊이 (0=표면) |
| `param` | str \| null | `null` | 관련 공정 변수 — `telemetry.param`과 join |
| `direction` | str \| null | `null` | 변수 이상 방향 (`high`/`low`) |
| `how_to_verify` | str (JSON) | 아래 참조 | **이 가설을 검증할 도구 호출 지시서** |
| `prior` | str | `"high"` | 문헌상 흔한 정도 |
| `prior_score` | float | `0.9` | 위를 수치화 (high 0.9 / mid 0.5 / low 0.2) |
| `chain_confidence` | float | `4.1` | 경로상 **가장 약한** 고리의 추출 신뢰도 |
| `n_source_chunks` | int | `3` | 근거 문헌 조각 수 (중복 제거) |
| `evidence_chunk_ids` | list[str] | `["doc3#c17","doc7#c02"]` | 근거 조각 ID |
| `citations` | list[str] | `["Focus ring erosion leads to…"]` | 원문 스니펫 |
| `kg_version` | str | `"proj_20260709"` | 어느 투영 배치에서 나왔나 |

**`how_to_verify` 예시** — 가설 에이전트는 이 목록대로 MCP 도구를 부르면 된다:

```json
[
  {"tool": "get_maintenance_history",
   "args": {"equipment_id": "$suspect_eq", "time_range": "$window"}},
  {"tool": "get_alarm_history",
   "args": {"equipment_id": "$suspect_eq", "time_range": "$window"}},
  {"tool": "get_normal_lot_ratio",
   "args": {"equipment_id": "$suspect_eq", "time_range": "$window"}}
]
```

- `$suspect_eq`(의심 장비)와 `$window`(시간 창) **두 개만** 가설 에이전트가 채우면 된다. 나머지 변수는 투영 때 이미 치환됐거나, pipeline 도구용이라 여기 안 나온다.
- 목록에는 `verify` 도구만 실린다. `commonality` 같은 pipeline 도구는 오케스트레이터가 알아서 부른다.
- 여러 가설의 기본 정렬: `prior_score` 내림차순 → `chain_confidence` → `n_source_chunks` → `depth` 오름차순 (동점 처리 순서는 미결 §9-3).

---

> ⚠ **v0.6 반영 대기 (리뷰 지적)**: ① `hid`/`causal_path`를 `name` 대신 `uid` 기반으로 변경 예정 (다른 공정의 동명 Cause 충돌 방지), ② `INVOLVES_PARAMETER`·`OCCURS_IN`이 Cause당 정확히 1개인지 검증 추가 예정. 확정되면 §4 예시도 갱신할 것.
