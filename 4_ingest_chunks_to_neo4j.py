import sys

from langchain_neo4j import Neo4jGraph   # 타입 힌트용

import kg_common as kg

# Windows 콘솔(cp949)에서 em-dash 등 유니코드 출력 시 크래시 방지
sys.stdout.reconfigure(encoding="utf-8")

CHUNKS_PATH = kg.CHUNKS_PATH
SEEDS_DIR = kg.SEEDS_DIR

get_graph = kg.get_graph          # 공용 Neo4j 핸들
load_seed = kg.load_seed_nodes    # data/seeds/<file>.json -> nodes


# =========================
# 3. 제약 조건 생성
# -------------------------
# 모든 노드는 라벨별로 UNIQUE한 `id`를 유일 키로 갖는다 (schema.md §Node Types).
# 시드 앵커 3종 + 문헌에서 뽑는 3종(5번에서 생성) + Document/Chunk.
# MERGE가 중복 노드를 만들지 않도록 하는 안전장치.
# =========================

def create_constraints(graph: Neo4jGraph) -> None:
    statements = [
        "CREATE CONSTRAINT defect_pattern_id IF NOT EXISTS FOR (n:DefectPattern) REQUIRE n.id IS UNIQUE",
        "CREATE CONSTRAINT process_step_id   IF NOT EXISTS FOR (n:ProcessStep)   REQUIRE n.id IS UNIQUE",
        "CREATE CONSTRAINT parameter_id      IF NOT EXISTS FOR (n:Parameter)     REQUIRE n.id IS UNIQUE",
        "CREATE CONSTRAINT failure_mode_id   IF NOT EXISTS FOR (n:FailureMode)   REQUIRE n.id IS UNIQUE",
        "CREATE CONSTRAINT cause_id          IF NOT EXISTS FOR (n:Cause)         REQUIRE n.id IS UNIQUE",
        "CREATE CONSTRAINT equipment_id      IF NOT EXISTS FOR (n:Equipment)     REQUIRE n.id IS UNIQUE",
        "CREATE CONSTRAINT chunk_id_unique    IF NOT EXISTS FOR (c:Chunk)    REQUIRE c.id IS UNIQUE",
        "CREATE CONSTRAINT document_id_unique IF NOT EXISTS FOR (d:Document) REQUIRE d.id IS UNIQUE",
    ]
    for stmt in statements:
        graph.query(stmt)


# =========================
# 4. 시드 노드 적재 (앵커)
# -------------------------
# DefectPattern / ProcessStep / Parameter 는 문헌에서 뽑는 게 아니라
# 미리 정해진 고정 목록(enum)이다. data/seeds/*.json 을 읽어 그대로 MERGE 한다.
# 문헌이 이 id들을 언급하면 새로 만들지 않고 여기 연결한다(= 앵커).
#
# ProcessStep.id  ↔ fab의 lot_history.step
# Parameter.id    ↔ fab의 telemetry.param   (가설 검증 SQL의 join key)
# =========================

def seed_defect_patterns(graph: Neo4jGraph) -> None:
    nodes = load_seed("defect_patterns.json")
    graph.query(
        """
        UNWIND $nodes AS n
        MERGE (p:DefectPattern {id: n.id})
        SET p.name = n.name,
            p.aliases = n.aliases,
            p.spatial_keywords = n.spatial_keywords,
            p.expected_zone = n.expected_zone,
            p.expected_shape = n.expected_shape
        """,
        params={"nodes": nodes},
    )


def seed_process_steps(graph: Neo4jGraph) -> None:
    nodes = load_seed("process_steps.json")
    graph.query(
        """
        UNWIND $nodes AS n
        MERGE (s:ProcessStep {id: n.id})
        SET s.name = n.name,
            s.aliases = n.aliases
        """,
        params={"nodes": nodes},
    )


def seed_parameters(graph: Neo4jGraph) -> None:
    nodes = load_seed("parameters.json")
    graph.query(
        """
        UNWIND $nodes AS n
        MERGE (p:Parameter {id: n.id})
        SET p.name = n.name,
            p.steps = n.steps,
            p.aliases = n.aliases
        """,
        params={"nodes": nodes},
    )


def seed_all_anchors(graph: Neo4jGraph) -> None:
    seed_defect_patterns(graph)
    seed_process_steps(graph)
    seed_parameters(graph)


# chunks.jsonl 로드는 공용 kg.load_chunks 를 쓴다.
load_chunks = kg.load_chunks


# =========================
# 6. Document / Chunk 저장
# -------------------------
# (:Document)-[:HAS_CHUNK]->(:Chunk)
# =========================

def save_chunks(graph: Neo4jGraph, chunks: list[dict]) -> None:
    graph.query(
        """
        UNWIND $chunks AS row

        MERGE (d:Document {id: row.doc_id})
        SET d.title = row.title,
            d.source = row.source

        MERGE (c:Chunk {id: row.chunk_id})
        SET c.text = row.text,
            c.chunk_index = row.chunk_index,
            c.doc_id = row.doc_id,
            c.source = row.source,
            c.start_index = row.start_index,
            c.char_count = row.char_count

        MERGE (d)-[:HAS_CHUNK]->(c)
        """,
        params={"chunks": chunks},
    )


# =========================
# 7. 청크 순서 관계 (문서별)
# -------------------------
# 같은 문서 안에서만 chunk_index 순서대로 NEXT_CHUNK 를 잇는다.
# =========================

def create_next_chunk_relationships(graph: Neo4jGraph) -> None:
    graph.query(
        """
        MATCH (d:Document)-[:HAS_CHUNK]->(c:Chunk)
        WHERE c.chunk_index IS NOT NULL
        WITH d, c
        ORDER BY c.chunk_index ASC
        WITH d, collect(c) AS chunks
        WHERE size(chunks) > 1

        UNWIND range(0, size(chunks) - 2) AS i
        WITH chunks[i] AS cur, chunks[i + 1] AS nxt
        MERGE (cur)-[:NEXT_CHUNK]->(nxt)
        """
    )


# =========================
# 8. 확인용 출력
# =========================

def print_summary(graph: Neo4jGraph) -> None:
    def count(label: str) -> int:
        return graph.query(f"MATCH (n:{label}) RETURN count(n) AS c")[0]["c"]

    print("DefectPattern:", count("DefectPattern"))
    print("ProcessStep:", count("ProcessStep"))
    print("Parameter:", count("Parameter"))
    print("Document:", count("Document"))
    print("Chunk:", count("Chunk"))


# =========================
# 9. 실행
# =========================

def main() -> None:
    graph = get_graph()

    print("제약조건 생성...")
    create_constraints(graph)

    print("시드 앵커 적재...")
    seed_all_anchors(graph)

    chunks = load_chunks(CHUNKS_PATH)
    print("불러온 청크 수:", len(chunks))

    save_chunks(graph, chunks)
    create_next_chunk_relationships(graph)

    graph.refresh_schema()

    print("\n저장 완료")
    print_summary(graph)

    print("\nGraph schema:")
    print(graph.schema)


if __name__ == "__main__":
    main()
