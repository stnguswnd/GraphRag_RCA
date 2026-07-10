"""
5단계 — 통합 지식그래프 추출 (형식 무관 + 원인 표준화 내장).

■ 설계 원칙 (뿌리부터 제대로)
  - 소스 형식을 가리지 않는다. 로딩(2단계)만 txt/pdf별이고, 여기서부턴 청크는 그냥 텍스트다.
    troubleshooting 문서든 논문 표든, "향후 어떤 새 형식"이든 같은 추출기가 처리한다.
  - 한 청크에서 담을 수 있는 RCA 구조를 전부 뽑는다:
      FailureMode / Cause / Equipment
      + 관계 ARISES_IN, OCCURS_IN, CAUSED_BY, INVOLVES_PARAMETER, ATTRIBUTED_TO
    문서가 완전한 인과 사슬을 주면 사슬을, 표가 '패턴→원인'만 주면 그것을 뽑는다.
  - **원인 표준화(canonicalization)를 적재의 일부로 내장한다.** 표현이 달라도 같은 근본원인이면
    (예: 'rf_power_drift' ↔ 'irregular_rf_operation') 쓰기 전에 한 노드로 합친다.
    → txt·pdf가 처음부터 같은 Cause 를 공유한다. 사후 봉합(예전 5b/5c) 없이 그래프가 하나로 연결된다.

■ 3-pass
  1) 추출    — 관련 청크에서 엔티티/관계를 뽑아 메모리에 모은다.
  2) 표준화  — 전체 Cause 를 임베딩 후보 + LLM 판정으로 클러스터링해 canonical id 를 부여한다.
  3) 적재    — canonical id 로 Neo4j MERGE. 각 노드는 여러 소스의 근거(chunk_ids/quotes/aliases)를 누적한다.
"""

import re
import sys
import json
from pathlib import Path
from typing import Literal, Optional, get_args

import numpy as np
from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

import kg_common as kg

sys.stdout.reconfigure(encoding="utf-8")

OUTPUT_PATH = kg.OUTPUTS_DIR / "extracted_kg.jsonl"

# 원인 표준화 파라미터 (2단계 entity resolution)
CANON_FLOOR = 0.40   # 임베딩 후보 문턱(재현율). 정밀도는 LLM이 담당.
CANON_TOPK = 4       # 원인 하나당 검토할 이웃 수


# =========================================================================
# 1. 고정 vocabulary (앵커)
# =========================================================================
# 문헌에서 자유 생성하는 노드는 FailureMode/Cause/Equipment.
# DefectPattern/ProcessStep/Parameter 는 고정 목록이며 4번에서 시드로 적재된다.

ProcessStepId = Literal["LITHO", "ETCH", "DEPO", "CMP", "CLEAN", "EDS"]
DefectPatternId = Literal[
    "Center", "Donut", "Edge-Loc", "Edge-Ring", "Loc", "Near-Full", "Random", "Scratch",
]
ParameterId = Literal[
    "exposure_dose", "focus_offset", "stage_temp", "alignment_offset",
    "rf_power", "chamber_pressure", "he_flow", "temperature", "etch_rate",
    "gas_flow", "susceptor_temp", "deposition_rate",
    "down_force", "slurry_flow",
    "flow_rate", "megasonic_power", "chemical_temp", "rinse_time",
    "chuck_temp", "contact_resistance",
]

RelationshipKind = Literal[
    "ARISES_IN",           # DefectPattern -> ProcessStep   (패턴이 어느 공정을 의심케)
    "OCCURS_IN",           # FailureMode   -> ProcessStep   (고장이 일어나는 공정)
    "CAUSED_BY",           # FailureMode   -> Cause         (고장의 원인)
    "INVOLVES_PARAMETER",  # Cause         -> Parameter     (원인이 얽힌 검증변수)
    "ATTRIBUTED_TO",       # DefectPattern -> Cause         (문헌: 패턴의 원인 직결)
]

DEFECT_PATTERN_IDS: set[str] = set(get_args(DefectPatternId))
PROCESS_STEP_IDS: set[str] = set(get_args(ProcessStepId))
PARAMETER_IDS: set[str] = set(get_args(ParameterId))

DEFECT_PATTERN_INDEX = kg.build_alias_index("defect_patterns.json")
PROCESS_STEP_INDEX = kg.build_alias_index("process_steps.json")
PARAMETER_INDEX = kg.build_alias_index("parameters.json")

# 관련성 게이트용 표면형: 패턴 또는 공정을 언급하는 청크만 추출 대상.
# (파라미터 별칭 'pressure'/'temperature' 등은 너무 흔해 게이트에 넣지 않는다 — 구조 앵커만.)
ANCHOR_SURFACES = sorted(
    set(DEFECT_PATTERN_INDEX) | set(PROCESS_STEP_INDEX),
    key=len, reverse=True,
)

# 공정 언급 근거 확인용(ARISES_IN 환각 차단)
STEP_SURFACES: dict[str, list[str]] = {}
for _surf, _canon in PROCESS_STEP_INDEX.items():
    STEP_SURFACES.setdefault(_canon, []).append(_surf)


def assert_enums_match_seeds() -> None:
    """Literal(정적 JSON schema용)과 시드가 어긋나면 즉시 터뜨린다."""
    for file_name, enum_ids in [
        ("defect_patterns.json", DEFECT_PATTERN_IDS),
        ("process_steps.json", PROCESS_STEP_IDS),
        ("parameters.json", PARAMETER_IDS),
    ]:
        seed_ids = {n["id"] for n in kg.load_seed_nodes(file_name)}
        if seed_ids != enum_ids:
            raise ValueError(
                f"{file_name} 과 Literal 불일치:\n"
                f"  시드에만: {sorted(seed_ids - enum_ids)}\n  코드에만: {sorted(enum_ids - seed_ids)}"
            )


def mentions_anchor(text: str) -> bool:
    hay = kg.normalize_key(text)
    return any(re.search(rf"\b{re.escape(s)}\b", hay) for s in ANCHOR_SURFACES)


def step_grounded_in(step_id: str, text: str) -> bool:
    hay = kg.normalize_key(text)
    return any(re.search(rf"\b{re.escape(s)}\b", hay) for s in STEP_SURFACES.get(step_id, []))


# 논문 방법론/분류 잡음(물리적 원인 아님). ATTRIBUTED_TO 원인에 특히 흔하다.
METHOD_NOISE_KEYWORDS = [
    "misclassif", "training data", "selecting training", "weighting", "weight scheme",
    "entropy", "voting", "c mean", "filtering", "classification", "classifier",
    "classify", "location aspect", "location and size", "locations are not fixed",
    "combining", "combine", "eye defect", "partial ring", "local zone",
]


def cause_is_noise(cause_id: str, cause_name: str) -> Optional[str]:
    if kg.resolve(cause_name, DEFECT_PATTERN_INDEX) or kg.resolve(cause_id, DEFECT_PATTERN_INDEX):
        return "원인이 불량 패턴 이름(동어반복)"
    text = kg.normalize_key(f"{cause_id} {cause_name}")
    for kw in METHOD_NOISE_KEYWORDS:
        if kw in text:
            return f"방법론/분류 잡음 '{kw}'"
    return None


# =========================================================================
# 2. 추출 스키마
# =========================================================================

class FailureModeNode(BaseModel):
    id: str = Field(description="유일 키. 소문자 snake_case. 예: post_etch_residue")
    name: str = Field(description="문헌 표현 그대로")
    description: str = Field(description="완결된 한국어 한 문장")
    aliases: list[str] = Field(default_factory=list)


class CauseNode(BaseModel):
    id: str = Field(description="유일 키. 소문자 snake_case. 예: high_etch_rate")
    name: str = Field(description="문헌 표현 그대로")
    description: str = Field(description="완결된 한국어 한 문장")
    aliases: list[str] = Field(default_factory=list)


class EquipmentNode(BaseModel):
    id: str = Field(description="장비 식별자. 문헌 표기 그대로. 예: ETCH-03")
    name: str
    equip_group: ProcessStepId


class Relationship(BaseModel):
    """
    kind 별 (source, target):
      ARISES_IN          : DefectPattern id -> ProcessStep id
      OCCURS_IN          : FailureMode id   -> ProcessStep id
      CAUSED_BY          : FailureMode id   -> Cause id
      INVOLVES_PARAMETER : Cause id         -> Parameter id
      ATTRIBUTED_TO      : DefectPattern id -> Cause id
    """
    kind: RelationshipKind
    source: str
    target: str
    direction: Optional[Literal["high", "low"]] = Field(default=None, description="INVOLVES_PARAMETER 전용")
    occurrence_prior: Optional[Literal["high", "mid", "low"]] = Field(default=None, description="ARISES_IN 전용")
    extraction_confidence: float = Field(description="1~5")
    description: str = Field(description="근거 한 문장")
    quotes: list[str] = Field(default_factory=list)


class RcaGraph(BaseModel):
    failure_modes: list[FailureModeNode]
    causes: list[CauseNode]
    equipment: list[EquipmentNode]
    relationships: list[Relationship]


# =========================================================================
# 3. 프롬프트 (형식 무관 단일)
# =========================================================================

def build_prompt(chunk: dict) -> str:
    return f"""
다음은 반도체 웨이퍼 불량 원인분석(RCA) 문헌의 한 조각입니다. (troubleshooting 매뉴얼일 수도, 학술 논문 표일 수도 있음)
이 조각이 담고 있는 RCA 지식을 **있는 만큼** 지식그래프로 추출하세요. 없는 건 억지로 만들지 마세요.

뽑을 것:
- FailureMode: 공정 내부의 고장 모드 (예: post-etch residue, overlay misregistration)
- Cause: 그 배후 근본 원인 (예: high etch rate, irregular RF operation)
- Equipment: 문헌이 'ETCH-03'처럼 구체 장비를 지목할 때만
- 관계:
  · ARISES_IN          (DefectPattern -> ProcessStep)  "이 불량 패턴은 이 공정을 의심케 한다"
  · OCCURS_IN          (FailureMode   -> ProcessStep)  "이 고장은 이 공정에서 일어난다" (고장마다 정확히 1개)
  · CAUSED_BY          (FailureMode   -> Cause)        "이 고장의 원인은 저것"
  · INVOLVES_PARAMETER (Cause         -> Parameter)    "이 원인은 이 변수 이상과 얽힘" (direction 채우기)
  · ATTRIBUTED_TO      (DefectPattern -> Cause)        "논문 표: 이 패턴의 원인은 저것" (공정/고장 없이 원인 직결)

아래 세 목록은 **고정**입니다. 새로 만들지 말고 목록 안에서 정확한 문자열로만 매핑하세요. 없으면 그 관계는 만들지 마세요.
  ProcessStep: LITHO, ETCH, DEPO, CMP, CLEAN, EDS
  DefectPattern(WM-811K 8종): Center, Donut, Edge-Loc, Edge-Ring, Loc, Near-Full, Random, Scratch
  Parameter(20): exposure_dose, focus_offset, stage_temp, alignment_offset, rf_power, chamber_pressure,
    he_flow, temperature, etch_rate, gas_flow, susceptor_temp, deposition_rate, down_force, slurry_flow,
    flow_rate, megasonic_power, chemical_temp, rinse_time, chuck_temp, contact_resistance

규칙:
- 원문에 명시된 것만. 노드 id 는 소문자 snake_case. description 은 완결된 한국어 한 문장.
- 공정 변수 자체(rf_power 등)를 Cause 로 만들지 마세요. 변수는 INVOLVES_PARAMETER 의 target 으로만.
  "etch rate too high"처럼 이상 방향이 붙은 서술만 Cause 입니다.
- 불량 패턴(Center/Scratch/...)은 DefectPattern 이지 FailureMode/Cause 가 아닙니다. ARISES_IN/ATTRIBUTED_TO 의 source 로만 씁니다.
- ARISES_IN 은 원문에 그 공정 이름이 실제 등장할 때만.
- ATTRIBUTED_TO 는 논문이 "이 패턴 ← 이런 원인"을 표/문장으로 줄 때. 그 Cause 도 causes 목록에 넣으세요.
  분류/방법론 용어(misclassification, training data 등)나 패턴 이름 자체를 Cause 로 넣지 마세요.
- 각 관계에 extraction_confidence(1~5)와 근거 quotes 를 채우세요.

청크: chunk_id={chunk['chunk_id']}, doc_id={chunk.get('doc_id')}

원문:
{chunk['text']}
"""


# =========================================================================
# 4. 추출 + 검증(가지치기)
# =========================================================================

def validate_kg(kg_obj: RcaGraph, chunk_text: str, dropped: list[str]) -> RcaGraph:
    for fm in kg_obj.failure_modes:
        fm.id = kg.normalize_id(fm.id)
    for c in kg_obj.causes:
        c.id = kg.normalize_id(c.id)

    fm_ids = {fm.id for fm in kg_obj.failure_modes}
    cause_ids = {c.id for c in kg_obj.causes}
    cause_name = {c.id: c.name for c in kg_obj.causes}
    valid: list[Relationship] = []

    for rel in kg_obj.relationships:
        raw = f"{rel.kind} {rel.source!r}->{rel.target!r}"
        if rel.extraction_confidence < 2:
            dropped.append(f"{raw}: 신뢰도<2")
            continue
        src, tgt = rel.source.strip(), rel.target.strip()

        if rel.kind == "ARISES_IN":
            src = kg.resolve(src, DEFECT_PATTERN_INDEX)
            tgt = kg.resolve(tgt, PROCESS_STEP_INDEX)
            if not src or not tgt:
                dropped.append(f"{raw}: 앵커 매핑 실패"); continue
            if not step_grounded_in(tgt, chunk_text):
                dropped.append(f"{raw}: 공정 '{tgt}' 원문 언급 없음(환각)"); continue
        elif rel.kind == "OCCURS_IN":
            src = kg.normalize_id(src); tgt = kg.resolve(tgt, PROCESS_STEP_INDEX)
            if src not in fm_ids or not tgt:
                dropped.append(f"{raw}: FM 미추출/공정 매핑 실패"); continue
        elif rel.kind == "CAUSED_BY":
            src, tgt = kg.normalize_id(src), kg.normalize_id(tgt)
            if src not in fm_ids or tgt not in cause_ids:
                dropped.append(f"{raw}: FM/Cause 미추출"); continue
            noise = cause_is_noise(tgt, cause_name.get(tgt, ""))
            if noise:
                dropped.append(f"{raw}: {noise}"); continue
        elif rel.kind == "INVOLVES_PARAMETER":
            src = kg.normalize_id(src); tgt = kg.resolve(tgt, PARAMETER_INDEX)
            if src not in cause_ids or not tgt:
                dropped.append(f"{raw}: Cause 미추출/변수 매핑 실패"); continue
        elif rel.kind == "ATTRIBUTED_TO":
            src = kg.resolve(src, DEFECT_PATTERN_INDEX); tgt = kg.normalize_id(tgt)
            if not src or tgt not in cause_ids:
                dropped.append(f"{raw}: 패턴 매핑 실패/Cause 미추출"); continue
            noise = cause_is_noise(tgt, cause_name.get(tgt, ""))
            if noise:
                dropped.append(f"{raw}: {noise}"); continue
        else:
            dropped.append(f"{raw}: 알 수 없는 kind"); continue

        rel.source, rel.target = src, tgt
        valid.append(rel)

    # 어떤 FailureMode(CAUSED_BY)도, 어떤 패턴(ATTRIBUTED_TO)도 가리키지 않는 Cause 는 고아 → 버린다.
    linked_causes = {r.target for r in valid if r.kind in ("CAUSED_BY", "ATTRIBUTED_TO")}
    for c in kg_obj.causes:
        if c.id not in linked_causes:
            dropped.append(f"Cause {c.id!r}: 고아(연결된 상위 없음)")
    causes = [c for c in kg_obj.causes if c.id in linked_causes]
    valid = [r for r in valid if r.kind != "INVOLVES_PARAMETER" or r.source in linked_causes]

    return RcaGraph(failure_modes=kg_obj.failure_modes, causes=causes,
                    equipment=kg_obj.equipment, relationships=valid)


# =========================================================================
# 5. 원인 표준화 (canonicalization) — 적재의 일부
# =========================================================================
# 전체 추출에서 나온 Cause 들을 임베딩 후보 + LLM 판정으로 클러스터링해,
# 같은 근본원인은 하나의 canonical id 로 합친다. txt/pdf 가릴 것 없이 여기서 통합된다.

class SameCauseDecision(BaseModel):
    same_ids: list[str] = Field(default_factory=list,
                                description="주어진 원인과 '같은 물리적 근본원인'인 후보 id 들. 없으면 빈 리스트.")


def _embed(texts: list[str]) -> np.ndarray:
    vecs = np.array(OpenAIEmbeddings(model=kg.OPENAI_EMBED_MODEL).embed_documents(texts), dtype=np.float32)
    return vecs / np.clip(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9, None)


class _UF:
    """
    검증변수 제약을 클러스터 단위로 강제하는 union-find.
    각 클러스터는 서로 다른 Parameter 를 최대 1개만 가질 수 있다. 병합 결과 2개 이상이 되면 거부한다.
    (파라미터 없는 논문 원인을 징검다리로 gas_flow↔chamber_pressure 가 이어지는 전이 누수를 차단.)
    """
    def __init__(self, ids, params: dict):
        self.p = {i: i for i in ids}
        self.cp = {i: set(params.get(i, set())) for i in ids}   # root -> param 집합

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]; x = self.p[x]
        return x

    def union(self, a, b) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        combined = self.cp[ra] | self.cp[rb]
        if len(combined) > 1:        # 서로 다른 검증변수 → 병합 금지
            return False
        self.p[ra] = rb
        self.cp[rb] = combined
        return True


def _param_str(params: set) -> str:
    return f"[검증변수 {sorted(params)}]" if params else "[검증변수 미상]"


def canonicalize_causes(all_causes: dict[str, dict], cause_params: dict[str, set], llm) -> dict[str, str]:
    """
    all_causes  : cause_id -> {name, description, count}
    cause_params: cause_id -> {INVOLVES_PARAMETER 로 걸린 변수 id 집합}
    반환: cause_id -> canonical_id 매핑.

    병합 규칙: LLM이 '같은 근본원인'이라 판정 + **검증변수가 충돌하지 않을 때만** 합친다.
    두 원인이 서로 다른 fab 변수(gas_flow vs chamber_pressure 등)에 걸리면, 범주가 같아도
    다른 원인이다(검증 대상이 다르므로). 이 구조적 제약이 과병합을 막는다.
    """
    ids = list(all_causes)
    if len(ids) < 2:
        return {i: i for i in ids}

    texts = [f"{all_causes[i]['name']}. {all_causes[i]['description']}" for i in ids]
    vecs = _embed(texts)
    sim = vecs @ vecs.T
    uf = _UF(ids, cause_params)

    print(f"[표준화] 원인 {len(ids)}개 클러스터링 (floor={CANON_FLOOR})")
    for i, cid in enumerate(ids):
        order = sorted(range(len(ids)), key=lambda j: sim[i][j], reverse=True)
        cands = [ids[j] for j in order if j != i and sim[i][j] >= CANON_FLOOR][:CANON_TOPK]
        if not cands:
            continue
        lines = "\n".join(
            f"  - id={c} | {all_causes[c]['name']}: {all_causes[c]['description']} {_param_str(cause_params.get(c, set()))}"
            for c in cands
        )
        prompt = f"""반도체 RCA에서 아래 '기준 원인'과 **같은 물리적 근본원인**을 뜻하는 후보만 고르세요.

[기준 원인] id={cid} | {all_causes[cid]['name']}: {all_causes[cid]['description']} {_param_str(cause_params.get(cid, set()))}

[후보]
{lines}

엄격 규칙:
- 표현만 다르고 **정확히 같은 메커니즘**이면 매칭. (예: 'irregular RF operation' = 'RF power drift')
- **서로 다른 공정 변수에 얽힌 원인은 다른 원인입니다.** 범주가 비슷해도(둘 다 '유량'/'압력'/'온도')
  검증변수가 다르면(gas_flow vs chamber_pressure vs slurry_flow; 또는 stage_temp vs susceptor_temp vs chuck_temp)
  고르지 마세요.
- 애매하면 고르지 마세요(과병합보다 미병합이 낫다)."""
        try:
            dec = llm.with_structured_output(SameCauseDecision, method="json_schema").invoke(prompt)
        except Exception as e:
            print("  (판정 실패, 건너뜀)", e); continue
        for sid in dec.same_ids:
            if sid in all_causes and sid != cid:
                uf.union(cid, sid)   # 검증변수 충돌 시 내부에서 거부됨(클러스터 제약)

    # 클러스터별 대표(canonical) 선정: 가장 많이 등장(count) → 이름 짧은 순.
    clusters: dict[str, list[str]] = {}
    for i in ids:
        clusters.setdefault(uf.find(i), []).append(i)
    mapping: dict[str, str] = {}
    merged = 0
    for members in clusters.values():
        rep = sorted(members, key=lambda i: (-all_causes[i]["count"], len(i)))[0]
        for m in members:
            mapping[m] = rep
        if len(members) > 1:
            merged += len(members) - 1
            names = ", ".join(all_causes[m]["name"][:22] for m in members)
            print(f"  ▶ {rep}  ⇐  {names}")
    print(f"[표준화] {len(ids)}개 → {len(clusters)}개 (원인 {merged}개 병합)")
    return mapping


# =========================================================================
# 6. Neo4j 적재 (canonical id 로)
# =========================================================================

_CHUNK_IDS = """
    rel.chunk_ids = CASE
        WHEN rel.chunk_ids IS NULL THEN [$chunk_id]
        WHEN NOT $chunk_id IN rel.chunk_ids THEN rel.chunk_ids + [$chunk_id]
        ELSE rel.chunk_ids END
"""


def write_chunk(graph, kg_obj: RcaGraph, chunk: dict, canon: dict[str, str], canon_meta: dict[str, dict]) -> None:
    cid = chunk["chunk_id"]

    def cmap(x): return canon.get(x, x)

    if kg_obj.failure_modes:
        graph.query("""
            MATCH (ch:Chunk {id:$cid}) UNWIND $ns AS n
            MERGE (fm:FailureMode {id:n.id})
            SET fm.name=n.name, fm.description=n.description, fm.aliases=n.aliases
            MERGE (ch)-[:MENTIONS]->(fm)
        """, params={"cid": cid, "ns": [n.model_dump() for n in kg_obj.failure_modes]})

    # Cause 는 canonical id 로. 대표 노드에 이름/설명/별칭(합쳐진 표면형들)을 싣는다.
    canon_causes = {}
    for c in kg_obj.causes:
        rep = cmap(c.id)
        meta = canon_meta.get(rep, {"name": c.name, "description": c.description})
        canon_causes[rep] = {"id": rep, "name": meta["name"], "description": meta["description"],
                             "aliases": meta.get("aliases", [])}
    if canon_causes:
        graph.query("""
            MATCH (ch:Chunk {id:$cid}) UNWIND $ns AS n
            MERGE (c:Cause {id:n.id})
            SET c.name=n.name, c.description=n.description, c.aliases=n.aliases
            MERGE (ch)-[:MENTIONS]->(c)
        """, params={"cid": cid, "ns": list(canon_causes.values())})

    if kg_obj.equipment:
        graph.query("""
            MATCH (ch:Chunk {id:$cid}) UNWIND $ns AS n
            MERGE (e:Equipment {id:n.id}) SET e.name=n.name, e.equip_group=n.equip_group
            MERGE (ch)-[:MENTIONS]->(e)
            WITH e,n MATCH (s:ProcessStep {id:n.equip_group}) MERGE (e)-[:PART_OF]->(s)
        """, params={"cid": cid, "ns": [n.model_dump() for n in kg_obj.equipment]})

    rels = []
    for r in kg_obj.relationships:
        d = r.model_dump()
        # Cause 를 참조하는 필드를 canonical id 로 치환한다.
        if r.kind == "CAUSED_BY":             # target = Cause
            d["target"] = cmap(r.target)
        elif r.kind == "INVOLVES_PARAMETER":  # source = Cause
            d["source"] = cmap(r.source)
        elif r.kind == "ATTRIBUTED_TO":       # target = Cause
            d["target"] = cmap(r.target)
        rels.append(d)

    def run(kind, cypher):
        graph.query(cypher, params={"rels": rels, "chunk_id": cid})

    run("ARISES_IN", f"""UNWIND $rels AS r WITH r WHERE r.kind='ARISES_IN'
        MATCH (p:DefectPattern {{id:r.source}}),(s:ProcessStep {{id:r.target}})
        MERGE (p)-[rel:ARISES_IN]->(s)
        SET rel.occurrence_prior=r.occurrence_prior, rel.extraction_confidence=r.extraction_confidence,
            rel.description=r.description, rel.quotes=r.quotes, {_CHUNK_IDS}""")
    run("OCCURS_IN", f"""UNWIND $rels AS r WITH r WHERE r.kind='OCCURS_IN'
        MATCH (fm:FailureMode {{id:r.source}}),(s:ProcessStep {{id:r.target}})
        MERGE (fm)-[rel:OCCURS_IN]->(s)
        SET rel.extraction_confidence=r.extraction_confidence, rel.description=r.description,
            rel.quotes=r.quotes, {_CHUNK_IDS}""")
    run("CAUSED_BY", f"""UNWIND $rels AS r WITH r WHERE r.kind='CAUSED_BY'
        MATCH (fm:FailureMode {{id:r.source}}),(c:Cause {{id:r.target}})
        MERGE (fm)-[rel:CAUSED_BY]->(c)
        SET rel.extraction_confidence=r.extraction_confidence, rel.description=r.description,
            rel.quotes=r.quotes, {_CHUNK_IDS}""")
    run("INVOLVES_PARAMETER", f"""UNWIND $rels AS r WITH r WHERE r.kind='INVOLVES_PARAMETER'
        MATCH (c:Cause {{id:r.source}}),(p:Parameter {{id:r.target}})
        MERGE (c)-[rel:INVOLVES_PARAMETER]->(p)
        SET rel.direction=r.direction, rel.extraction_confidence=r.extraction_confidence,
            rel.description=r.description, rel.quotes=r.quotes, {_CHUNK_IDS}""")
    run("ATTRIBUTED_TO", f"""UNWIND $rels AS r WITH r WHERE r.kind='ATTRIBUTED_TO'
        MATCH (p:DefectPattern {{id:r.source}}),(c:Cause {{id:r.target}})
        MERGE (p)-[rel:ATTRIBUTED_TO]->(c)
        SET rel.source='literature', rel.extraction_confidence=r.extraction_confidence,
            rel.description=r.description, rel.quotes=r.quotes, {_CHUNK_IDS}""")


# =========================================================================
# 7. 실행
# =========================================================================

def _tally(kg_obj: RcaGraph, all_causes: dict, cause_params: dict) -> None:
    for c in kg_obj.causes:
        slot = all_causes.setdefault(c.id, {"name": c.name, "description": c.description, "count": 0})
        slot["count"] += 1
    for r in kg_obj.relationships:
        if r.kind == "INVOLVES_PARAMETER":
            cause_params.setdefault(r.source, set()).add(r.target)


def collect_from_extraction(targets, structured) -> tuple[list, dict, dict]:
    """pass 1: 청크마다 LLM 추출 + 검증. 결과를 캐시(jsonl)에도 남긴다."""
    if OUTPUT_PATH.exists():
        OUTPUT_PATH.unlink()
    extracted, all_causes, cause_params = [], {}, {}
    for i, chunk in enumerate(targets, 1):
        print(f"[추출 {i}/{len(targets)}] {chunk['chunk_id']}", flush=True)
        kg_obj = validate_kg(structured.invoke(build_prompt(chunk)), chunk["text"], [])
        extracted.append(({"chunk_id": chunk["chunk_id"]}, kg_obj))
        _tally(kg_obj, all_causes, cause_params)
        with OUTPUT_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"chunk_id": chunk["chunk_id"], "kg": kg_obj.model_dump()}, ensure_ascii=False) + "\n")
    return extracted, all_causes, cause_params


def collect_from_cache() -> tuple[list, dict, dict]:
    """추출을 건너뛰고 캐시(jsonl)에서 복원한다. (표준화/적재만 다시 돌릴 때)"""
    extracted, all_causes, cause_params = [], {}, {}
    for line in OUTPUT_PATH.open(encoding="utf-8"):
        row = json.loads(line)
        kg_obj = RcaGraph(**row["kg"])
        extracted.append(({"chunk_id": row["chunk_id"]}, kg_obj))
        _tally(kg_obj, all_causes, cause_params)
    return extracted, all_causes, cause_params


def main() -> None:
    assert_enums_match_seeds()
    resume = "resume" in sys.argv[1:]
    graph = kg.get_graph()

    # ---- pass 1: 추출 (또는 캐시 복원) ----
    if resume and OUTPUT_PATH.exists():
        print(f"[resume] 추출 캐시 재사용: {OUTPUT_PATH} (재추출 생략)")
        extracted, all_causes, cause_params = collect_from_cache()
    else:
        chunks = kg.load_chunks()
        targets = [c for c in chunks if mentions_anchor(c["text"])]
        print(f"전체 {len(chunks)}청크 중 앵커 언급 {len(targets)}청크가 추출 대상 (형식 무관 게이트)")
        structured = ChatOpenAI(model=kg.OPENAI_MODEL, temperature=0).with_structured_output(
            RcaGraph, method="json_schema")
        extracted, all_causes, cause_params = collect_from_extraction(targets, structured)
    print(f"추출된 고유 원인 {len(all_causes)}개")

    # ---- pass 2: 원인 표준화 ----
    llm = ChatOpenAI(model=kg.OPENAI_MODEL, temperature=0)
    canon = canonicalize_causes(all_causes, cause_params, llm)
    canon_meta: dict[str, dict] = {}   # canonical_id -> {name, description, aliases}
    for cid, rep in canon.items():
        m = canon_meta.setdefault(rep, {"name": all_causes[rep]["name"],
                                        "description": all_causes[rep]["description"], "aliases": []})
        if cid != rep:
            m["aliases"].append(all_causes[cid]["name"])

    # ---- pass 3: 적재 ----
    print("\n[적재] canonical id 로 Neo4j 기록")
    for chunk, kg_obj in extracted:
        write_chunk(graph, kg_obj, chunk, canon, canon_meta)

    graph.refresh_schema()
    n_cause = graph.query("MATCH (c:Cause) RETURN count(c) AS n")[0]["n"]
    print(f"\n완료. Cause 노드 {n_cause}개 (표준화 후). 결과: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
