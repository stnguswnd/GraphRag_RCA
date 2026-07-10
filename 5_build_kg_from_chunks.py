import os
import re
import sys
import json
from pathlib import Path
from typing import Literal, Optional, get_args

from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Windows 콘솔(cp949)에서 em-dash 등 유니코드 출력 시 크래시 방지
sys.stdout.reconfigure(encoding="utf-8")

from langchain_openai import ChatOpenAI
from langchain_neo4j import Neo4jGraph


# =========================
# 1. 환경 변수 / 경로 설정
# =========================

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

CHUNKS_PATH = BASE_DIR / "outputs" / "chunks.jsonl"
OUTPUT_PATH = BASE_DIR / "outputs" / "extracted_kg.jsonl"
SEEDS_DIR = BASE_DIR / "data" / "seeds"

NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5")


# =========================
# 2. KG 스키마 정의 (schema.md)
# -------------------------
# 문헌에서 자유롭게 만드는 노드는 FailureMode / Cause / Equipment 셋.
# DefectPattern / ProcessStep / Parameter 는 고정 vocabulary(앵커)이며
# 4번에서 시드로 미리 적재된다. 여기서는 새로 만들지 않고 연결만 한다.
#
#   DefectPattern -[ARISES_IN]->  ProcessStep      (문서 A)
#   FailureMode   -[OCCURS_IN]->  ProcessStep      (문서 B)
#   FailureMode   -[CAUSED_BY]->  Cause            (문서 B)
#   Cause         -[INVOLVES_PARAMETER]-> Parameter (문서 B)
#   Equipment     -[PART_OF]->    ProcessStep      (규칙, LLM 아님)
# =========================

ProcessStepId = Literal["LITHO", "ETCH", "DEPO", "CMP", "CLEAN", "EDS"]

# seeds/defect_patterns.json 과 반드시 동일. id == VLM 출력 클래스
DefectPatternId = Literal["Center", "Scratch", "Edge-Ring"]

# seeds/parameters.json 과 반드시 동일. id == fab telemetry.param
ParameterId = Literal[
    "exposure_dose", "focus_offset", "stage_temp", "alignment_offset",
    "rf_power", "chamber_pressure", "he_flow", "temperature", "etch_rate",
    "gas_flow", "susceptor_temp", "deposition_rate",
    "down_force", "slurry_flow",
    "flow_rate", "megasonic_power", "chemical_temp", "rinse_time",
    "chuck_temp", "contact_resistance",
]

RelationshipKind = Literal[
    "ARISES_IN",            # DefectPattern -> ProcessStep
    "OCCURS_IN",            # FailureMode   -> ProcessStep
    "CAUSED_BY",            # FailureMode   -> Cause
    "INVOLVES_PARAMETER",   # Cause         -> Parameter
]

PROCESS_STEP_IDS: set[str] = set(get_args(ProcessStepId))
DEFECT_PATTERN_IDS: set[str] = set(get_args(DefectPatternId))
PARAMETER_IDS: set[str] = set(get_args(ParameterId))


def assert_enums_match_seeds() -> None:
    """
    위 Literal은 LLM에게 넘길 JSON schema를 정적으로 만들어야 해서 하드코딩돼 있다.
    시드 파일만 고치고 여기를 안 고치면, 문서의 해당 엔티티가 enum에 없어서
    validate_kg가 관계를 **조용히 버린다**. 시작하자마자 터뜨린다.
    """
    pairs = [
        ("defect_patterns.json", DEFECT_PATTERN_IDS),
        ("process_steps.json", PROCESS_STEP_IDS),
        ("parameters.json", PARAMETER_IDS),
    ]
    for file_name, enum_ids in pairs:
        data = json.loads((SEEDS_DIR / file_name).read_text(encoding="utf-8"))
        seed_ids = {n["id"] for n in data["nodes"]}
        if seed_ids != enum_ids:
            raise ValueError(
                f"{file_name} 과 이 파일의 Literal이 어긋납니다.\n"
                f"  시드에만 있음: {sorted(seed_ids - enum_ids)}\n"
                f"  코드에만 있음: {sorted(enum_ids - seed_ids)}\n"
                f"둘 중 하나를 고쳐 맞추세요. (프롬프트의 고정 목록도 함께)"
            )


# =========================
# 2.1 앵커 표기 정규화 (canonicalization)
# -------------------------
# LLM은 프롬프트에 canonical id 목록을 받고도 'edge-ring', 'etch' 처럼 표기를 흔든다.
# 시드의 aliases를 역인덱스로 만들어, 버리기 전에 한 번 canonical id로 갈아끼운다.
#
# aliases만 쓰고 spatial_keywords는 쓰지 않는다.
# 후자는 여러 패턴에 동시에 걸려(예: 'ring'이 Edge-Ring/Donut 양쪽) 매칭에 못 쓴다.
# =========================

def _normalize_key(raw: str) -> str:
    """대소문자·하이픈·밑줄·연속 공백 차이를 흡수한다. 'Edge-Ring' == 'edge_ring' == 'edge ring'"""
    return re.sub(r"[\s\-_]+", " ", raw.strip().lower())


def _build_alias_index(file_name: str) -> dict[str, str]:
    data = json.loads((SEEDS_DIR / file_name).read_text(encoding="utf-8"))
    index: dict[str, str] = {}
    for node in data["nodes"]:
        canonical = node["id"]
        for surface in [canonical, node.get("name", canonical), *node.get("aliases", [])]:
            index[_normalize_key(surface)] = canonical
    return index


DEFECT_PATTERN_INDEX = _build_alias_index("defect_patterns.json")
PROCESS_STEP_INDEX = _build_alias_index("process_steps.json")
PARAMETER_INDEX = _build_alias_index("parameters.json")


def resolve_anchor(raw: str, index: dict[str, str]) -> Optional[str]:
    """앵커 표기 하나를 canonical id로. 못 붙이면 None(호출부가 사유를 남기고 버린다)."""
    return index.get(_normalize_key(raw))


# canonical ProcessStep id -> 그 공정을 가리키는 모든 표기 (근거 확인용 역방향 맵)
STEP_SURFACES: dict[str, list[str]] = {}
for _surface, _canonical in PROCESS_STEP_INDEX.items():
    STEP_SURFACES.setdefault(_canonical, []).append(_surface)


def step_is_grounded_in(step_id: str, chunk_text: str) -> bool:
    """
    이 청크 원문이 해당 공정을 실제로 언급하는가.

    LLM은 공정 이름이 하나도 없는 서론 문단에서도 ARISES_IN을 지어낸다(목록 첫 항목인
    LITHO를 자리채움으로 고름). 프롬프트로는 안 막혀서 여기서 결정적으로 거른다.
    """
    haystack = _normalize_key(chunk_text)
    return any(
        re.search(rf"\b{re.escape(surface)}\b", haystack)
        for surface in STEP_SURFACES.get(step_id, [])
    )


class FailureModeNode(BaseModel):
    """공정 내부의 고장 모드. 예: post-etch residue, metal corrosion."""
    id: str = Field(description="유일 키. 소문자 snake_case. 예: post_etch_residue")
    name: str = Field(description="문헌에 쓰인 그대로의 고장 모드 이름. 예: excessive post-etch residue")
    description: str = Field(description="완결된 한국어 한 문장")
    aliases: list[str] = Field(default_factory=list, description="문헌 속 별칭들")


class CauseNode(BaseModel):
    """고장 모드의 근본 원인. 예: high etch rate, nonuniform etch process."""
    id: str = Field(description="유일 키. 소문자 snake_case. 예: high_etch_rate")
    name: str = Field(description="문헌에 쓰인 그대로의 원인 이름. 예: incorrect process parameter (high etch rate)")
    description: str = Field(description="완결된 한국어 한 문장. 나중에 가설 문장의 부품으로 이어 붙인다.")
    aliases: list[str] = Field(default_factory=list, description="문헌 속 별칭들")


class EquipmentNode(BaseModel):
    """장비 인스턴스. 문헌이 구체적 장비를 지목할 때만."""
    id: str = Field(description="장비 식별자. 문헌 표기 그대로. 예: ETCH-03")
    name: str = Field(description="장비 이름. 보통 id와 같다.")
    equip_group: ProcessStepId = Field(description="이 장비가 속한 공정군")


class Relationship(BaseModel):
    """
    kind 별 (source, target) 규약:
      - ARISES_IN          : DefectPattern id  -> ProcessStep id
      - OCCURS_IN          : FailureMode id    -> ProcessStep id
      - CAUSED_BY          : FailureMode id    -> Cause id
      - INVOLVES_PARAMETER : Cause id          -> Parameter id
    """
    kind: RelationshipKind
    source: str = Field(description="출발 노드의 id")
    target: str = Field(description="도착 노드의 id")

    direction: Optional[Literal["high", "low"]] = Field(
        default=None, description="INVOLVES_PARAMETER 전용: 변수 이상 방향"
    )
    occurrence_prior: Optional[Literal["high", "mid", "low"]] = Field(
        default=None, description="ARISES_IN 전용: 문헌상 흔한 정도(commonly/rare)"
    )
    extraction_confidence: float = Field(description="추출 신뢰도 1~5. 애매하면 낮게.")
    description: str = Field(description="이 관계를 뒷받침하는 완결된 한 문장")
    quotes: list[str] = Field(default_factory=list, description="근거 원문 스니펫(짧게)")


class RcaGraph(BaseModel):
    failure_modes: list[FailureModeNode]
    causes: list[CauseNode]
    equipment: list[EquipmentNode]
    relationships: list[Relationship]


# =========================
# 3. 프롬프트
# =========================

def build_prompt(chunk: dict) -> str:
    return f"""
다음은 반도체 웨이퍼 불량 원인분석(RCA) 문헌의 한 조각입니다.
이 조각에서 고장 모드(FailureMode), 원인(Cause), 장비(Equipment)와 그 관계를 지식그래프로 추출하세요.

청크 메타데이터:
- chunk_id: {chunk['chunk_id']}
- doc_id: {chunk.get('doc_id')}

추출 규칙:
- 원문에 명시된 내용만 추출하고, 추측하지 마세요.
- 의미 있는 내용이 없으면 모든 리스트를 빈 리스트로 반환하세요.
- 노드 id는 소문자 snake_case. 예: post_etch_residue, high_etch_rate
- description은 완결된 한국어 한 문장으로 쓰세요.
- 공정 변수 자체(rf_power, etch_rate 등)를 Cause로 만들지 마세요.
  변수는 INVOLVES_PARAMETER의 target으로만 씁니다.
  "etch rate too high"처럼 이상 방향이 붙은 서술만 Cause입니다.
- FailureMode(증상/고장 모드)와 Cause(그 배후 원인)를 섞지 마세요.
  예: "excessive post-etch residue"는 FailureMode, "nonuniform etch process"는 Cause.

아래 세 목록은 고정입니다. 새로 만들지 말고 목록 안에서만 고르세요.
해당하는 항목이 목록에 없으면 그 관계는 추출하지 마세요.

공정 단계(ProcessStep) 6종:
  LITHO, ETCH, DEPO, CMP, CLEAN, EDS

불량 패턴(DefectPattern) 3종:
  Center, Scratch, Edge-Ring
  (웨이퍼맵 상의 공간 패턴만 해당. "circular ring"→Edge-Ring, "bulls eye"→Center,
   "linear defect"/"scuff mark"→Scratch)

공정 변수(Parameter) 20종:
  exposure_dose, focus_offset, stage_temp, alignment_offset,
  rf_power, chamber_pressure, he_flow, temperature, etch_rate,
  gas_flow, susceptor_temp, deposition_rate,
  down_force, slurry_flow,
  flow_rate, megasonic_power, chemical_temp, rinse_time,
  chuck_temp, contact_resistance

관계(kind) 4종:
- ARISES_IN:          (DefectPattern) -> (ProcessStep)  "이 불량 패턴은 이 공정을 의심케 한다" (occurrence_prior 채우기)
- OCCURS_IN:          (FailureMode)   -> (ProcessStep)  "이 고장 모드는 이 공정에서 일어난다" (고장 모드마다 정확히 1개)
- CAUSED_BY:          (FailureMode)   -> (Cause)        "이 고장 모드의 원인은 저것이다"
- INVOLVES_PARAMETER: (Cause)         -> (Parameter)    "이 원인은 이 변수의 이상과 얽힌다" (direction 채우기)

중요:
- source/target에는 반드시 노드의 id를 쓰세요.
  고정 목록의 값은 **위에 적힌 문자열 그대로** 대소문자까지 정확히 옮기세요.
  예: 'Edge-Ring' (O) / 'edge-ring' (X), 'ETCH' (O) / 'etching' (X)
- ARISES_IN은 **원문에 공정 이름이 실제로 등장할 때만** 만드세요.
  공정이 언급되지 않은 서론·요약 문단에서는 ARISES_IN을 추측해 만들지 마세요.
- 불량 패턴(Center/Scratch/Edge-Ring)은 DefectPattern이지 FailureMode가 아닙니다.
  'scratch_pattern' 같은 FailureMode를 만들지 마세요. 패턴은 ARISES_IN의 source로만 씁니다.
  FailureMode는 공정 내부의 고장(post-etch residue, overlay misregistration 등)입니다.
- 모든 FailureMode에는 OCCURS_IN 관계가 정확히 하나 있어야 합니다.
- Equipment는 문헌이 "ETCH-03"처럼 구체적 장비를 지목할 때만 만드세요. PART_OF는 만들지 마세요(규칙으로 자동 생성).
- 각 관계에 extraction_confidence(1~5)와 근거 quotes를 채우세요.

원문:
{chunk['text']}
"""


# =========================
# 4. 추출 + 검증
# =========================

def extract_kg_from_chunk(structured_llm, chunk: dict) -> RcaGraph:
    return structured_llm.invoke(build_prompt(chunk))


def normalize_id(raw: str) -> str:
    """LLM이 흘린 표기 흔들림 흡수: 소문자 + 공백/하이픈 → 밑줄."""
    return re.sub(r"[^a-z0-9_]+", "_", raw.strip().lower()).strip("_")


def validate_kg(
    kg: RcaGraph,
    dropped: Optional[list[str]] = None,
    chunk_text: str = "",
) -> RcaGraph:
    """
    [Graph Pruning]
    - FailureMode/Cause id를 정규화한 뒤 관계의 source/target을 같은 규칙으로 맞춘다.
    - 앵커(DefectPattern/ProcessStep/Parameter) 표기는 시드 aliases로 canonical id에 갈아끼운다.
      LLM이 'edge-ring', 'etching' 처럼 흔들어도 살린다. 못 붙이면 사유를 남기고 버린다.
    - 이번 청크에서 추출되지 않은 FailureMode/Cause를 가리키는 관계는 버린다.
      (없는 노드를 가리키면 Cypher MATCH가 실패해 조용히 유실되므로 미리 자른다)
    - extraction_confidence 2 미만은 폐기.

    dropped 리스트를 넘기면 버린 관계의 사유가 쌓인다(조용한 유실 방지).
    """
    log = dropped if dropped is not None else []

    for fm in kg.failure_modes:
        fm.id = normalize_id(fm.id)
    for c in kg.causes:
        c.id = normalize_id(c.id)

    fm_ids = {fm.id for fm in kg.failure_modes}
    cause_ids = {c.id for c in kg.causes}

    valid: list[Relationship] = []

    for rel in kg.relationships:
        raw = f"{rel.kind} {rel.source!r} -> {rel.target!r}"

        if rel.extraction_confidence < 2:
            log.append(f"{raw}: 신뢰도 {rel.extraction_confidence} < 2")
            continue

        src, tgt = rel.source.strip(), rel.target.strip()

        if rel.kind == "ARISES_IN":
            src = resolve_anchor(src, DEFECT_PATTERN_INDEX)
            tgt = resolve_anchor(tgt, PROCESS_STEP_INDEX)
            if src is None or tgt is None:
                log.append(f"{raw}: 앵커 매핑 실패 (DefectPattern/ProcessStep)")
                continue
            if chunk_text and not step_is_grounded_in(tgt, chunk_text):
                log.append(f"{raw}: 청크 원문에 공정 '{tgt}' 언급 없음 (환각)")
                continue
        elif rel.kind == "OCCURS_IN":
            src = normalize_id(src)
            tgt = resolve_anchor(tgt, PROCESS_STEP_INDEX)
            if src not in fm_ids or tgt is None:
                log.append(f"{raw}: FailureMode 미추출 또는 ProcessStep 매핑 실패")
                continue
        elif rel.kind == "CAUSED_BY":
            src, tgt = normalize_id(src), normalize_id(tgt)
            if src not in fm_ids or tgt not in cause_ids:
                log.append(f"{raw}: FailureMode/Cause가 이 청크에서 추출되지 않음")
                continue
        elif rel.kind == "INVOLVES_PARAMETER":
            src = normalize_id(src)
            tgt = resolve_anchor(tgt, PARAMETER_INDEX)
            if src not in cause_ids or tgt is None:
                log.append(f"{raw}: Cause 미추출 또는 Parameter 매핑 실패")
                continue
        else:
            log.append(f"{raw}: 알 수 없는 kind")
            continue

        rel.source, rel.target = src, tgt
        valid.append(rel)

    # 어떤 FailureMode도 가리키지 않는 Cause는 그래프에서 도달할 수 없다(고아).
    # 그 Cause를 버리면 거기서 출발하던 INVOLVES_PARAMETER도 같이 버려야 한다.
    linked_causes = {r.target for r in valid if r.kind == "CAUSED_BY"}
    for c in kg.causes:
        if c.id not in linked_causes:
            log.append(f"Cause {c.id!r}: 어떤 FailureMode도 가리키지 않는 고아")
    causes = [c for c in kg.causes if c.id in linked_causes]
    valid = [
        r for r in valid
        if r.kind != "INVOLVES_PARAMETER" or r.source in linked_causes
    ]

    return RcaGraph(
        failure_modes=kg.failure_modes,
        causes=causes,
        equipment=kg.equipment,
        relationships=valid,
    )


# =========================
# 5. Neo4j 저장
# =========================

def get_graph() -> Neo4jGraph:
    return Neo4jGraph(
        url=NEO4J_URI,
        username=NEO4J_USERNAME,
        password=NEO4J_PASSWORD,
        database=NEO4J_DATABASE,
    )


# 관계 속성에 이 청크를 근거로 덧붙이는 조각 (중복 없이 append)
_CHUNK_IDS_SET = """
            rel.chunk_ids = CASE
                WHEN rel.chunk_ids IS NULL THEN [$chunk_id]
                WHEN NOT $chunk_id IN rel.chunk_ids THEN rel.chunk_ids + [$chunk_id]
                ELSE rel.chunk_ids END
"""


def save_kg_to_neo4j(graph: Neo4jGraph, kg: RcaGraph, chunk: dict) -> None:
    failure_modes = [n.model_dump() for n in kg.failure_modes]
    causes = [n.model_dump() for n in kg.causes]
    equipment = [n.model_dump() for n in kg.equipment]
    rels = [r.model_dump() for r in kg.relationships]

    chunk_id = chunk["chunk_id"]

    # (1) FailureMode 노드 + 이 청크가 언급했음을 기록
    if failure_modes:
        graph.query(
            """
            MATCH (c:Chunk {id: $chunk_id})
            UNWIND $nodes AS n
            MERGE (fm:FailureMode {id: n.id})
            SET fm.name = n.name,
                fm.description = n.description,
                fm.aliases = n.aliases
            MERGE (c)-[:MENTIONS]->(fm)
            """,
            params={"chunk_id": chunk_id, "nodes": failure_modes},
        )

    # (2) Cause 노드
    if causes:
        graph.query(
            """
            MATCH (c:Chunk {id: $chunk_id})
            UNWIND $nodes AS n
            MERGE (cause:Cause {id: n.id})
            SET cause.name = n.name,
                cause.description = n.description,
                cause.aliases = n.aliases
            MERGE (c)-[:MENTIONS]->(cause)
            """,
            params={"chunk_id": chunk_id, "nodes": causes},
        )

    # (3) Equipment 노드 + PART_OF (equip_group에서 규칙으로 파생)
    if equipment:
        graph.query(
            """
            MATCH (c:Chunk {id: $chunk_id})
            UNWIND $nodes AS n
            MERGE (e:Equipment {id: n.id})
            SET e.name = n.name,
                e.equip_group = n.equip_group
            MERGE (c)-[:MENTIONS]->(e)
            WITH e, n
            MATCH (s:ProcessStep {id: n.equip_group})
            MERGE (e)-[:PART_OF]->(s)
            """,
            params={"chunk_id": chunk_id, "nodes": equipment},
        )

    if not rels:
        return

    # (4) ARISES_IN : DefectPattern -> ProcessStep  (문서 A)
    graph.query(
        f"""
        UNWIND $rels AS r
        WITH r WHERE r.kind = 'ARISES_IN'
        MATCH (p:DefectPattern {{id: r.source}})
        MATCH (s:ProcessStep {{id: r.target}})
        MERGE (p)-[rel:ARISES_IN]->(s)
        SET rel.occurrence_prior = r.occurrence_prior,
            rel.extraction_confidence = r.extraction_confidence,
            rel.description = r.description,
            rel.quotes = r.quotes,
        {_CHUNK_IDS_SET}
        """,
        params={"rels": rels, "chunk_id": chunk_id},
    )

    # (5) OCCURS_IN : FailureMode -> ProcessStep  (앵커)
    graph.query(
        f"""
        UNWIND $rels AS r
        WITH r WHERE r.kind = 'OCCURS_IN'
        MATCH (fm:FailureMode {{id: r.source}})
        MATCH (s:ProcessStep {{id: r.target}})
        MERGE (fm)-[rel:OCCURS_IN]->(s)
        SET rel.extraction_confidence = r.extraction_confidence,
            rel.description = r.description,
            rel.quotes = r.quotes,
        {_CHUNK_IDS_SET}
        """,
        params={"rels": rels, "chunk_id": chunk_id},
    )

    # (6) CAUSED_BY : FailureMode -> Cause
    graph.query(
        f"""
        UNWIND $rels AS r
        WITH r WHERE r.kind = 'CAUSED_BY'
        MATCH (fm:FailureMode {{id: r.source}})
        MATCH (c:Cause {{id: r.target}})
        MERGE (fm)-[rel:CAUSED_BY]->(c)
        SET rel.extraction_confidence = r.extraction_confidence,
            rel.description = r.description,
            rel.quotes = r.quotes,
        {_CHUNK_IDS_SET}
        """,
        params={"rels": rels, "chunk_id": chunk_id},
    )

    # (7) INVOLVES_PARAMETER : Cause -> Parameter  (검증 종착점, direction)
    graph.query(
        f"""
        UNWIND $rels AS r
        WITH r WHERE r.kind = 'INVOLVES_PARAMETER'
        MATCH (c:Cause {{id: r.source}})
        MATCH (p:Parameter {{id: r.target}})
        MERGE (c)-[rel:INVOLVES_PARAMETER]->(p)
        SET rel.direction = r.direction,
            rel.extraction_confidence = r.extraction_confidence,
            rel.description = r.description,
            rel.quotes = r.quotes,
        {_CHUNK_IDS_SET}
        """,
        params={"rels": rels, "chunk_id": chunk_id},
    )


# =========================
# 6. 추출 결과 JSONL 저장
# =========================

def append_result_to_jsonl(output_path: Path, chunk: dict, kg: RcaGraph) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "chunk_id": chunk["chunk_id"],
        "doc_id": chunk.get("doc_id"),
        "kg": kg.model_dump(),
    }
    with output_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# =========================
# 7. chunks.jsonl 로드
# =========================

def load_chunks(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"chunks.jsonl 파일을 찾을 수 없습니다: {path}")
    chunks = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            metadata = row.get("metadata", {})
            chunks.append({
                "chunk_id": row["chunk_id"],
                "chunk_index": row["chunk_index"],
                "text": row["page_content"],
                "doc_id": metadata.get("doc_id"),
            })
    return chunks


# =========================
# 8. 실행
# =========================

def main() -> None:
    assert_enums_match_seeds()

    chunks = load_chunks(CHUNKS_PATH)
    print("처리할 청크 수:", len(chunks))

    graph = get_graph()

    llm = ChatOpenAI(model=OPENAI_MODEL, temperature=0)
    structured_llm = llm.with_structured_output(RcaGraph, method="json_schema")

    # 재실행 시 결과 파일 초기화
    if OUTPUT_PATH.exists():
        OUTPUT_PATH.unlink()

    totals = {"failure_modes": 0, "causes": 0, "equipment": 0, "relationships": 0}
    total_dropped = 0

    for i, chunk in enumerate(chunks, start=1):
        print("=" * 80)
        print(f"[{i}/{len(chunks)}] {chunk['chunk_id']}")
        print(chunk["text"][:160].replace("\n", " "))

        kg = extract_kg_from_chunk(structured_llm, chunk)

        dropped: list[str] = []
        kg = validate_kg(kg, dropped, chunk_text=chunk["text"])

        print(
            "FailureMode:", len(kg.failure_modes),
            "| Cause:", len(kg.causes),
            "| Equipment:", len(kg.equipment),
            "| 관계:", len(kg.relationships),
        )
        for reason in dropped:
            print("  버림:", reason)
        total_dropped += len(dropped)

        save_kg_to_neo4j(graph, kg, chunk)
        append_result_to_jsonl(OUTPUT_PATH, chunk, kg)

        totals["failure_modes"] += len(kg.failure_modes)
        totals["causes"] += len(kg.causes)
        totals["equipment"] += len(kg.equipment)
        totals["relationships"] += len(kg.relationships)

    graph.refresh_schema()

    print("\n완료")
    for key, value in totals.items():
        print(f"총 추출 {key}: {value}")
    print("총 버린 관계/노드:", total_dropped)
    print("결과 저장:", OUTPUT_PATH)

    print("\nGraph schema:")
    print(graph.schema)


if __name__ == "__main__":
    main()
