# SECS/GEM MCP 문서 ↔ KG 출력 정합성 검토

> 갱신: 2026-07-13 (MCP 문서 07-13 12:22 개정판 반영)
> 대조 기준:
> - `SECS GEM MCP 문서_v0 1.md` (2026-07-13 12:22 — §5 E2E 시나리오 3종·§6 Agent 결합 신설,
>   매핑표에서 Center-세정 노즐 행 및 Edge-bead removal 취소선 삭제)
> - `outputs/hypotheses.json` (schema **v2.4**, 가설 642건 — CLEAN·Edge-Ring-CMP 문서 반영) + `KG_output_명세.md`
> - `mapping_table.yaml` (원본 유지 — KG 매칭 키워드는 KG 모듈 소유)

---

## 1. 미해소 항목 (우선순위 순)

| # | 항목 | 내용 · 필요한 조치 |
|---|---|---|
| X1E | **E2E 시나리오의 정답 신호가 fab 어휘 밖 (X1 재격상)** | §5 시나리오 3종 중 2종의 **정답 신호 자체**가 seeds 20종·`fab.md`에 없음 — 5.1 Center `shower_flow`(+`pressure`), 5.3 Scratch `pad_usage_hours`·`motor_torque`·`slurry_particle`. 이대로면 정답 원인을 `[자동]` 경로로 잡을 수 없음. 시나리오가 mock 데이터 설계 서술이므로 **fab.db generator가 이 param들을 포함하도록 확장되는 것으로 읽힘** — 그렇다면 `fab.md`·`seeds/parameters.json`·5번 `Literal`·`mapping_table.yaml`의 `pad_usage_hours`까지 **일괄 동기 확장** 필요. **조치**: 확장 param 최종 목록 확정 후 KG 시드 갱신 (→ Q1 재격상) |
| X3 | **가설 수 × 루프 비용** | 가설 368건을 "후보 원인마다" 체인 실행하면 툴 호출 폭주 (E3/E4의 정신에 반함). 고유 검증 단위는 훨씬 적음 — Center 88 / Edge-Ring 31 / Scratch 16. **개정판에서 구체화**: §5 시나리오의 KG 질의는 **"후보 원인 3종"** 형태를 기대 (5.x 2단계), §6 Agent 결합도 "후보 원인 공정 확보" 수준. **조치**: `hypotheses.json`에 시나리오용 요약 후보 뷰(`candidates[]` — mapping_table 항목 단위) + 상세 `checks[]` 그룹 뷰 추가 (→ Q3) |
| X4 | **Alarm evidence 부재** | 알람은 KG에 노드/경로가 없어 B3 판정 ③("KG에 신호→결함 경로 있는가")에서 **모든 알람이 자동으로 교란 신호 판정**. **개정판 5.1이 이 위험을 실증**: 교란 알람 `HEATER_TEMP_DEV`는 KG 부재로 배제(의도대로)되지만, 같은 논리로 **관련 알람 `FLOW_LOW`도 배제될 것** — 시나리오는 이를 A4 보조 증거로 기대함. **조치**: `Alarm` evidence 노드(`fab_table='alarm'`, `[반자동]`) 추가 또는 B3 규칙 완화 (→ Q2) |
| N3 | **VLM 형상 분류 모듈 미구현** | VLM은 형상을 **자유 서술**로 출력 — 미지 패턴 진입(A0 분기 2)에는 서술 → shape/zone enum 분류기가 전제. 문서 추출(5번)과 같은 분류 계약이라 프롬프트 재사용 가능. **조치**: 모듈 소유·위치 결정 (→ Q5) |
| N4 | **문서 매핑표 취소선 vs `mapping_table.yaml` 불일치** | 문서에서 Center-`clean_nozzle_clog` 행이 삭제됐지만 yaml에는 잔존. 단, **의도일 수 있음** — 5.1 시나리오는 세정 노즐을 여전히 KG 후보(함정, B1으로 기각되는 H3)로 사용. "정답 설계에서 제외"와 "후보 지식에서 제외"는 다른 결정. **조치**: yaml 소유자(MCP/fab측)에 의도 확인 (→ Q7) |
| X7 | **Maintenance id ↔ T7 `parts` 매칭 불가** | `inspect_whether_residual_copper_...` 같은 id는 T7 반환의 "교체 부품" 텍스트와 자동 대조 불가. **조치**: Maintenance dedup 시 `parts_keyword` 정규화 속성 추가 |
| N1 | **재적재 간 앵커 비결정성** | 앵커 보강 패스로 실행 내 합집합은 확보했으나, **전체 재적재 간** 엣지 구성이 변동 (예: `Edge-Ring→CLEAN`이 실행에 따라 있다가 없다가). **조치(운영 규칙)**: agent 연동 테스트는 그래프 스냅샷 고정 + `meta.generated_at` 신선도 검사 |
| N2 | **`[근거없음]`이 상위 rank에 올 수 있음** | 순위가 tier 무관(의도된 설계 — 그럴듯함 ≠ 확인 용이성). **조치(agent 규칙)**: rank 순회가 아니라 **tier로 실행 계획을 세우고 rank는 tier 내 순서로** 사용 |
| X6잔여 | **KG 후보 분류 단계가 MCP 문서에 없음** | direct 가설(step=null)과 `[근거없음]` 가설의 처리 절차가 시나리오에 미명문화. **조치(문서 측)**: A0~A1 사이에 분류 단계 추가 — step 보유→표준 루프 / direct→step 미지정 T3 / 근거없음→evidence table 참고 정보 |

## 2. 결정 필요 사항

| # | 질문 | 연관 |
|---|---|---|
| Q3 | agent 루프 단위 — 가설별 vs 고유 검증 단위(`step`×`evidence`)별. `checks[]` 뷰를 KG가 제공할지 | X3 |
| Q2 | `Alarm` evidence 노드 추가 vs B3 규칙 완화 | X4 |
| Q5 | VLM 형상 서술 → enum 분류 모듈의 소유와 위치 (KG측 유틸 vs agent 내부) | N3 |
| Q6 | 실시간 단건 질의 모드(패턴 1건 입력) 필요 여부 — 현재는 배치 json에서 `questions[].pattern` 섹션 참조 | — |
| Q1 | **[재격상]** fab param 확장 목록 확정 — E2E 시나리오가 `shower_flow`·`pad_usage_hours`·`motor_torque`·`slurry_particle` 등을 전제. 확정되면 `fab.md`+`seeds/parameters.json`+5번 Literal 일괄 갱신 | X1E |
| Q7 | 매핑표 취소선(Center-세정)의 의도 — yaml에서도 제거인가, 함정 후보로 유지인가 | N4 |

## 3. 권장 착수 순서

1. **Q1 fab param 확장 목록 확정** (X1E) — E2E 시나리오 정답의 `[자동]` 검증 가능 여부를 좌우.
   확정 즉시 `fab.md` + `seeds/parameters.json` + 5번 Literal 동기화
2. Q3 `candidates[]`(시나리오용 후보 요약 뷰) + `checks[]` 그룹 뷰 — agent 루프 비용 확정 (STATUS ②와 병행)
3. Q2 Alarm evidence — 5.1이 관련 알람(`FLOW_LOW`)의 KG 연결을 실제로 요구
4. X7 `parts_keyword` — Maintenance dedup(P3)과 한 묶음
5. X6잔여(MCP 문서 측 — KG 후보 분류 단계 명문화) / Q7 매핑표 의도 확인 / Q5 VLM 분류기

---

## 4. 완화·운영 방침 확정 항목 (기록)

| # | 항목 | 확정된 방침 |
|---|---|---|
| X1 | **파라미터 어휘 불일치 — 출력 처리 방침** (⚠ 어휘 확장 자체는 X1E로 재격상됨) | (a) 어휘 밖 신호는 `[근거없음]` 유지 — 자동/반자동으로 새지 않음. (b) '지식 없음'이 아니라 '계측 없음'이므로 **C2(부분 커버리지)로 처리** — agent는 `verification.unverifiable_signals` 또는 `mapping.param_in_fab_vocab=false`를 evidence table "부족한 데이터"란에 기록. (c) 버려지던 신호명 **보존** — `Cause.unverifiable_signals` → 출력 (실측: Cause 18개, 가설 20건). 이 방침은 어휘가 확장돼도 잔여 밖 신호에 계속 적용됨 |
| X2 | **클래스×원인 매핑표 커버리지** | **해소 (07-13).** 매핑표(취소선 제외 8항목)의 모든 패턴→공정 조합이 KG에 존재 — 누락 0. 마지막 공백이던 `Edge-Ring→CMP`는 `edgering-cmp.txt`(Xie & Boning, MIT/MRS 2005 + 큐레이션 메타데이터 "Related Defect: Edge Ring")로 확보. 근거 강도 주의: 본문은 기전(edge over-polish→주변부 불균일)까지만 말하고 패턴 연결은 메타데이터 큐레이션임. retaining ring(부품)↔ring(패턴) 어휘 함정은 프롬프트 가드로 차단 확인. CLEAN 계열은 `...TABLE+CLEAN.md`로 해소 (재구성 표, provenance 주의 명기) |
| X5 | **시나리오 체인 라우팅** | **해소 (07-13).** 출력에 `scenario_hint` 필드 신설 — Parameter→A3, Recipe→A5, Maintenance→`consumable`(추출 시 LLM 판단, 노드 속성)이면 A6·아니면 A2, 근거없음→null. 소급 노드는 키워드 휴리스틱(pad/brush/slurry/filter/conditioner) 임시 판정, 재추출 시 정식 값으로 대체. 실측 분포: A2 209 / A3 97 / A5 32 / A6 44 / null 260 |
| X8 | **표기 규약** | **해소 (07-13, KG측이 문서 수정).** MCP 문서의 식별자 표기를 코드에 정렬 — `step=증착/식각/세정` → `step=DEPO/ETCH/CLEAN`(11건), `params=[RF_power]` → `[rf_power]`. 산문(한국어 서술)은 유지. T8의 `metric=수율` 같은 툴 자체 계약값은 X8 범위 밖으로 보존 |
| X9 | 수치 prior 출처 | 정본 = `mapping_table.yaml`의 `prob`. KG는 `mapping.prob`로 노출만 |
| X10 | KG 질의 인터페이스 | 계약 = `hypotheses.json` + `KG_output_명세.md`. agent는 `questions[].pattern`으로 자기 섹션을 읽음. 잔여는 Q6 |

## 5. 정합 확인 (충돌 없음)

| # | 항목 | 근거 |
|---|---|---|
| OK-1 | 3클래스 명칭 일치 — VLM = MCP(A0) = KG `DefectPattern.id` (`Center`/`Scratch`/`Edge-Ring`) | 시드 계약 |
| OK-2 | T5 호출 인자 공급 — `verification.fab_table` + `path.evidence` + `direction` (E3 "전체 덤프 금지" 충족) | 출력 구조 |
| OK-3 | D3 Faithfulness 요건 — `sentence` + `path` + `provenance`(경로 전체 chunk_ids) | 출력 구조 |
| OK-4 | E2 원칙 — KG 출력에 인스턴스 주장 없음. 문헌 근거(provenance)와 큐레이션 근거(`mapping`) 구분 | 설계 |
| OK-5 | C4 스코프 일치 — 후공정·RTP 등은 `[근거없음]` 격리 | 검증 등급 |
| OK-6 | B1(negative evidence)은 KG와 독립 — 출력이 방해하지 않음 | — |
| OK-7 | Scratch 원인 실제 겹침 — 패드 마모·슬러리 대입자가 문서 매핑표와 KG 양쪽에 존재 | 교과서 CMP 표 |
| OK-8 | MCP 3.1 매핑표와 직접 연동 — `mapping_table.yaml`을 오버레이로 소비, `prob`·`citation`·신호가 가설에 실림 | 매핑 오버레이 |
| OK-9 | 순위가 측정값 기반 — `(occurrence_prior, evidence_docs, evidence_chunks)`. "그럴듯한 순서"(rank)와 "확인 방법"(tier)이 독립 | v2.4 출력 |
| OK-10 | 미지 패턴 대응 기반 — 형상 관측을 shape/zone enum으로 분류하면 `SpatialSignature`부터 순회 가능 (A0 분기 2 지원) | v2.4 형상 레이어 |
| OK-11 | **Edge-bead removal 취소선 삭제** — KG가 스코프 밖으로 버려온 판단과 문서가 일치하게 됨 | 개정판 §3.1 |
| OK-12 | **Center-세정 후보의 정답 설계 제외** — KG에 해당 문헌 근거가 없던 실상과 일치 (단 yaml 잔존 여부는 N4/Q7) | 개정판 §3.1 |
| OK-13 | **§6 Agent 결합 1단계와 부합** — "KG/문헌에서 후보 원인 공정 확보"는 `hypotheses.json`의 `pattern`→`path.step`/`cause`로 충족 | 개정판 §6 |
