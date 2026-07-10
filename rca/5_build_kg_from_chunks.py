import os
import sys
import json
from pathlib import Path
from typing import Literal, Optional

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

NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5")


# =========================
# 2. KG 스키마 정의 (RCA)
# -------------------------
# 핵심: Cause(원인) 하나만 LLM이 문헌에서 자유롭게 만들고,
#       나머지 target(패턴/공정/변수)은 전부 고정 enum이다.
# =========================

ProcessStepName = Literal["LITHO", "ETCH", "DEPO", "CMP", "CLEAN", "EDS"]

CauseType = Literal["Parameter", "Machine", "Material", "Method", "Man"]  # 5M

DefectPatternName = Literal[
    "Center", "Donut", "Edge-Loc", "Edge-Ring", "Loc",
    "Near-Full", "Scratch", "Random", "Normal",
]

ParameterTypeName = Literal[
    "rf_power", "bias_voltage", "chamber_pressure", "gas_flow_cf4", "gas_flow_o2",
    "depo_gas_flow", "electrode_temp", "wafer_temp", "etch_time", "endpoint_time",
    "focus", "exposure_dose", "overlay", "develop_time", "depo_rate",
    "film_thickness", "slurry_flow", "polish_pressure", "platen_speed", "megasonic_power",
]

RelationshipKind = Literal[
    "MANIFESTS_AS",         # Cause -> DefectPattern
    "CAUSED_BY",            # Cause -> Cause
    "OCCURS_IN",            # Cause -> ProcessStep
    "INVOLVES_PARAMETER",   # Cause -> ParameterType
]


class CauseNode(BaseModel):
    """
    문헌에서 추출하는 유일한 개방 엔티티.
    uid는 반드시 "{step}:{name}" 형식 (예: "ETCH:focus_ring_erosion").
    같은 이름이라도 공정이 다르면 다른 원인이므로 step을 uid에 넣는다.
    """
    uid: str = Field(description='유일 키. 반드시 "{step}:{name}" 형식. 예: "ETCH:focus_ring_erosion"')
    name: str = Field(description="원인 이름(소문자 snake_case 권장). 예: focus_ring_erosion")
    step: ProcessStepName = Field(description="이 원인이 발생하는 공정 단계")
    cause_type: CauseType = Field(description="5M 분류")
    description: str = Field(description="완결된 한 문장. 나중에 가설 문장의 부품으로 이어 붙인다.")
    aliases: list[str] = Field(default_factory=list, description="문헌 속 별칭들")


class CausalRelationship(BaseModel):
    """
    source는 항상 Cause의 uid.
    target는 kind에 따라 다르다:
      - MANIFESTS_AS      -> DefectPattern 이름
      - CAUSED_BY         -> 다른 Cause의 uid
      - OCCURS_IN         -> ProcessStep 이름
      - INVOLVES_PARAMETER-> ParameterType 이름
    """
    kind: RelationshipKind
    source: str = Field(description="출발 Cause의 uid")
    target: str = Field(description="도착 노드 (kind에 따라 패턴/공정/변수 이름 또는 Cause uid)")

    direction: Optional[Literal["high", "low"]] = Field(
        default=None, description="INVOLVES_PARAMETER 전용: 변수 이상 방향"
    )
    occurrence_prior: Optional[Literal["high", "mid", "low"]] = Field(
        default=None, description="MANIFESTS_AS 전용: 문헌상 흔한 정도(commonly/rare)"
    )
    extraction_confidence: float = Field(
        description="추출 신뢰도 1~5. 애매하면 낮게."
    )
    description: str = Field(description="이 관계를 뒷받침하는 완결된 한 문장")
    quotes: list[str] = Field(default_factory=list, description="근거 원문 스니펫(짧게)")


class CauseGraph(BaseModel):
    nodes: list[CauseNode]
    relationships: list[CausalRelationship]


# =========================
# 3. 프롬프트
# =========================

def build_prompt(chunk: dict) -> str:
    return f"""
다음은 반도체 웨이퍼 불량 원인분석(RCA) 문헌의 한 조각입니다.
이 조각에서 '원인(Cause)'과 그 인과관계를 지식그래프로 추출하세요.

청크 메타데이터:
- chunk_id: {chunk['chunk_id']}
- doc_id: {chunk.get('doc_id')}

추출 규칙:
- 원문에 명시된 내용만 추출하고, 추측하지 마세요.
- 의미 있는 원인이 없으면 nodes와 relationships를 빈 리스트로 반환하세요.
- Cause.uid는 반드시 "{{step}}:{{name}}" 형식. 예: "ETCH:focus_ring_erosion"
- description은 완결된 한국어 한 문장으로 쓰세요.

노드(Cause)만 자유롭게 만들 수 있고, 아래 target들은 고정 목록에서만 고르세요.

공정 단계(ProcessStep) 6종:
  LITHO, ETCH, DEPO, CMP, CLEAN, EDS

불량 패턴(DefectPattern) 9종:
  Center, Donut, Edge-Loc, Edge-Ring, Loc, Near-Full, Scratch, Random, Normal

공정 변수(ParameterType) 20종:
  rf_power, bias_voltage, chamber_pressure, gas_flow_cf4, gas_flow_o2,
  depo_gas_flow, electrode_temp, wafer_temp, etch_time, endpoint_time,
  focus, exposure_dose, overlay, develop_time, depo_rate,
  film_thickness, slurry_flow, polish_pressure, platen_speed, megasonic_power

5M 분류(cause_type): Parameter, Machine, Material, Method, Man

관계(kind) 4종:
- MANIFESTS_AS: (Cause) -> (DefectPattern)   "이 원인이 이 불량 패턴으로 발현한다" (occurrence_prior 채우기)
- CAUSED_BY:    (Cause) -> (Cause)            "이 원인의 배후에 저 원인이 있다" (target은 Cause uid)
- OCCURS_IN:    (Cause) -> (ProcessStep)      "이 원인은 이 공정에서 생긴다" (원인마다 정확히 1개)
- INVOLVES_PARAMETER: (Cause) -> (ParameterType)  "이 변수의 이상과 얽힌다" (direction 채우기)

중요:
- 모든 Cause에는 OCCURS_IN 관계가 정확히 하나 있어야 합니다.
- 각 관계에 extraction_confidence(1~5)와 근거 quotes를 채우세요.

원문:
{chunk['text']}
"""


# =========================
# 4. 추출 + 검증
# =========================

def extract_kg_from_chunk(structured_llm, chunk: dict) -> CauseGraph:
    return structured_llm.invoke(build_prompt(chunk))


def validate_kg(kg: CauseGraph) -> CauseGraph:
    """
    [Graph Pruning]
    - 모든 관계의 source는 이번 청크에서 추출된 Cause여야 한다.
    - CAUSED_BY는 target도 추출된 Cause여야 한다 (없는 노드를 가리키면 저장 불가).
    - MANIFESTS_AS/OCCURS_IN/INVOLVES_PARAMETER의 target은 enum이라 신뢰.
    - extraction_confidence 2 미만은 폐기(스펙 §2).
    """
    node_uids = {n.uid for n in kg.nodes}
    valid = []

    for rel in kg.relationships:
        if rel.source not in node_uids:
            continue
        if rel.extraction_confidence < 2:
            continue
        if rel.kind == "CAUSED_BY" and rel.target not in node_uids:
            continue
        valid.append(rel)

    return CauseGraph(nodes=kg.nodes, relationships=valid)


# =========================
# 5. Neo4j 저장 (kgbuild 그래프)
# =========================

def get_graph() -> Neo4jGraph:
    return Neo4jGraph(
        url=NEO4J_URI,
        username=NEO4J_USERNAME,
        password=NEO4J_PASSWORD,
        database=NEO4J_DATABASE,
    )


def save_kg_to_neo4j(graph: Neo4jGraph, kg: CauseGraph, chunk: dict) -> None:
    nodes = [n.model_dump() for n in kg.nodes]
    rels = [r.model_dump() for r in kg.relationships]

    if not nodes:
        return

    # (1) Cause 노드 저장 + 이 청크가 언급했음을 기록
    graph.query(
        """
        MATCH (c:Chunk {id: $chunk_id})
        WITH c
        UNWIND $nodes AS n
        MERGE (cause:Cause {uid: n.uid})
        SET cause.name = n.name,
            cause.step = n.step,
            cause.cause_type = n.cause_type,
            cause.description = n.description,
            cause.aliases = n.aliases
        MERGE (c)-[:MENTIONS]->(cause)
        """,
        params={"chunk_id": chunk["chunk_id"], "nodes": nodes},
    )

    # (2) OCCURS_IN : Cause -> ProcessStep (앵커)
    graph.query(
        """
        UNWIND $rels AS r
        WITH r WHERE r.kind = 'OCCURS_IN'
        MATCH (c:Cause {uid: r.source})
        MATCH (s:ProcessStep {name: r.target})
        MERGE (c)-[:OCCURS_IN]->(s)
        """,
        params={"rels": rels},
    )

    # (3) INVOLVES_PARAMETER : Cause -> ParameterType (앵커, direction)
    graph.query(
        """
        UNWIND $rels AS r
        WITH r WHERE r.kind = 'INVOLVES_PARAMETER'
        MATCH (c:Cause {uid: r.source})
        MATCH (pt:ParameterType {name: r.target})
        MERGE (c)-[rel:INVOLVES_PARAMETER]->(pt)
        SET rel.direction = r.direction
        """,
        params={"rels": rels},
    )

    # (4) MANIFESTS_AS : Cause -> DefectPattern (occurrence_prior + 근거)
    graph.query(
        """
        UNWIND $rels AS r
        WITH r WHERE r.kind = 'MANIFESTS_AS'
        MATCH (c:Cause {uid: r.source})
        MATCH (p:DefectPattern {name: r.target})
        MERGE (c)-[rel:MANIFESTS_AS]->(p)
        SET rel.occurrence_prior = r.occurrence_prior,
            rel.extraction_confidence = r.extraction_confidence,
            rel.description = r.description,
            rel.chunk_ids = CASE
                WHEN rel.chunk_ids IS NULL THEN [$chunk_id]
                WHEN NOT $chunk_id IN rel.chunk_ids THEN rel.chunk_ids + [$chunk_id]
                ELSE rel.chunk_ids END,
            rel.quotes = r.quotes
        """,
        params={"rels": rels, "chunk_id": chunk["chunk_id"]},
    )

    # (5) CAUSED_BY : Cause -> Cause (근거)
    graph.query(
        """
        UNWIND $rels AS r
        WITH r WHERE r.kind = 'CAUSED_BY'
        MATCH (c1:Cause {uid: r.source})
        MATCH (c2:Cause {uid: r.target})
        MERGE (c1)-[rel:CAUSED_BY]->(c2)
        SET rel.extraction_confidence = r.extraction_confidence,
            rel.description = r.description,
            rel.chunk_ids = CASE
                WHEN rel.chunk_ids IS NULL THEN [$chunk_id]
                WHEN NOT $chunk_id IN rel.chunk_ids THEN rel.chunk_ids + [$chunk_id]
                ELSE rel.chunk_ids END,
            rel.quotes = r.quotes
        """,
        params={"rels": rels, "chunk_id": chunk["chunk_id"]},
    )


# =========================
# 6. DETECTED_BY 규칙 부여 (LLM 아님)
# -------------------------
# 도구가 9개뿐이라 규칙으로 붙인다 (스펙 §2.1 ⑤).
# 모든 청크 처리 후 한 번만 실행하면 된다.
# =========================

def apply_detection_rules(graph: Neo4jGraph) -> None:
    # 규칙1: 공정 변수와 얽힘(④ 보유) → telemetry_check
    graph.query(
        """
        MATCH (c:Cause)-[:INVOLVES_PARAMETER]->(:ParameterType)
        MATCH (m:DetectionMethod {name: 'telemetry_check'})
        MERGE (c)-[:DETECTED_BY]->(m)
        """
    )
    # 규칙2: cause_type이 Machine/Material → maintenance_check, alarm_check
    graph.query(
        """
        MATCH (c:Cause) WHERE c.cause_type IN ['Machine', 'Material']
        MATCH (m:DetectionMethod) WHERE m.name IN ['maintenance_check', 'alarm_check']
        MERGE (c)-[:DETECTED_BY]->(m)
        """
    )
    # 규칙3: cause_type이 Method/Man → alarm_check
    graph.query(
        """
        MATCH (c:Cause) WHERE c.cause_type IN ['Method', 'Man']
        MATCH (m:DetectionMethod {name: 'alarm_check'})
        MERGE (c)-[:DETECTED_BY]->(m)
        """
    )
    # 규칙4: 모든 Cause → negative_control
    graph.query(
        """
        MATCH (c:Cause)
        MATCH (m:DetectionMethod {name: 'negative_control'})
        MERGE (c)-[:DETECTED_BY]->(m)
        """
    )
    # 규칙5: 표면 노드(MANIFESTS_AS ① 보유) → yield_change_point
    graph.query(
        """
        MATCH (c:Cause)-[:MANIFESTS_AS]->(:DefectPattern)
        MATCH (m:DetectionMethod {name: 'yield_change_point'})
        MERGE (c)-[:DETECTED_BY]->(m)
        """
    )


# =========================
# 7. 추출 결과 JSONL 저장
# =========================

def append_result_to_jsonl(output_path: Path, chunk: dict, kg: CauseGraph) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "chunk_id": chunk["chunk_id"],
        "doc_id": chunk.get("doc_id"),
        "kg": kg.model_dump(),
    }
    with output_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# =========================
# 8. chunks.jsonl 로드
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
# 9. 실행
# =========================

def main() -> None:
    chunks = load_chunks(CHUNKS_PATH)
    print("처리할 청크 수:", len(chunks))

    graph = get_graph()

    llm = ChatOpenAI(model=OPENAI_MODEL, temperature=0)
    structured_llm = llm.with_structured_output(CauseGraph, method="json_schema")

    # 재실행 시 결과 파일 초기화
    if OUTPUT_PATH.exists():
        OUTPUT_PATH.unlink()

    total_nodes = 0
    total_rels = 0

    for i, chunk in enumerate(chunks, start=1):
        print("=" * 80)
        print(f"[{i}/{len(chunks)}] {chunk['chunk_id']}")
        print(chunk["text"][:160].replace("\n", " "))

        kg = extract_kg_from_chunk(structured_llm, chunk)
        kg = validate_kg(kg)

        print("추출 Cause 수:", len(kg.nodes), "| 관계 수:", len(kg.relationships))

        save_kg_to_neo4j(graph, kg, chunk)
        append_result_to_jsonl(OUTPUT_PATH, chunk, kg)

        total_nodes += len(kg.nodes)
        total_rels += len(kg.relationships)

    print("\nDETECTED_BY 규칙 부여...")
    apply_detection_rules(graph)

    graph.refresh_schema()

    print("\n완료")
    print("총 추출 Cause 수:", total_nodes)
    print("총 추출 관계 수:", total_rels)
    print("결과 저장:", OUTPUT_PATH)

    print("\nGraph schema:")
    print(graph.schema)


if __name__ == "__main__":
    main()
