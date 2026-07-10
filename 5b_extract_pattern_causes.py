"""
논문(PDF)에서 '불량 패턴 → 근본 원인'만 타깃 추출해 RCA 백본에 흡수한다.

배경:
- 5_build_kg_from_chunks.py 는 통제된 troubleshooting 문서(txt)로 백본을 만든다.
  거기서 Cause는 `FailureMode -CAUSED_BY-> Cause` 경로로만 붙는다.
- 그런데 학술 논문은 결함 패턴의 원인을 표로 **직접** 준다.
  (예: "Center 패턴 ← 불규칙 RF 동작 또는 비정상 액체 흐름")
  이 지식은 스키마에 담을 자리가 없어 5번에서 통째로 버려졌다.

그래서 여기서는 논문에서 (DefectPattern, Cause) 쌍만 뽑아 백본에 이어붙인다:

    (:DefectPattern)-[:ATTRIBUTED_TO {source:'literature'}]->(:Cause)

- DefectPattern 은 고정 8종(WM-811K, seeds/defect_patterns.json). 새로 만들지 않고 연결만.
- Cause 는 5번과 같은 :Cause 라벨을 공유한다(도메인 이중화 방지). 자유 텍스트.
- ATTRIBUTED_TO 에 source='literature' 를 달아 fab 검증 백본(ARISES_IN 경로)과 구분한다.

비용 절감: 패턴 이름이 실제로 등장하는 청크만 LLM에 보낸다(prefilter).

실행: 4_ingest_chunks_to_neo4j.py 로 Chunk/앵커가 적재된 뒤에 돌린다. (5번과 독립·순서 무관)
      특정 doc만: python 5b_extract_pattern_causes.py ref56_...
"""

import os
import re
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
# 1. 환경 변수 / 경로
# =========================

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

CHUNKS_PATH = BASE_DIR / "outputs" / "chunks.jsonl"
OUTPUT_PATH = BASE_DIR / "outputs" / "extracted_pattern_causes.jsonl"
SEEDS_DIR = BASE_DIR / "data" / "seeds"

NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5")

ONLY_DOC_IDS = set(sys.argv[1:])


# =========================
# 2. 패턴 별칭 인덱스 (5번과 동일 규칙)
# =========================

def _normalize_key(raw: str) -> str:
    return re.sub(r"[\s\-_]+", " ", raw.strip().lower())


def _load_pattern_seed() -> list[dict]:
    return json.loads((SEEDS_DIR / "defect_patterns.json").read_text(encoding="utf-8"))["nodes"]


def _build_alias_index(nodes: list[dict]) -> dict[str, str]:
    index: dict[str, str] = {}
    for node in nodes:
        canonical = node["id"]
        for surface in [canonical, node.get("name", canonical), *node.get("aliases", [])]:
            index[_normalize_key(surface)] = canonical
    return index


PATTERN_NODES = _load_pattern_seed()
DEFECT_PATTERN_INDEX = _build_alias_index(PATTERN_NODES)
PATTERN_IDS = [n["id"] for n in PATTERN_NODES]

# prefilter 용 표면형(패턴명/별칭)들. 청크 원문에 하나라도 단어 단위로 있으면 LLM에 보낸다.
PATTERN_SURFACES = sorted(DEFECT_PATTERN_INDEX.keys(), key=len, reverse=True)


def resolve_pattern(raw: str) -> Optional[str]:
    return DEFECT_PATTERN_INDEX.get(_normalize_key(raw))


def normalize_id(raw: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", raw.strip().lower()).strip("_")


# 논문 방법론/분류 관련 잡음. 물리적 공정 원인이 아니므로 원인으로 치지 않는다.
# (방법론 논문 ref57/ref58이 패턴을 '분류 라벨'로만 언급하며 흘리는 것들)
METHOD_NOISE_KEYWORDS = [
    "misclassif", "training data", "selecting training", "weighting", "weight scheme",
    "entropy", "voting", "c mean", "filtering", "classification", "classifier",
    "classify", "location aspect", "location and size", "locations are not fixed",
    "combining", "combine", "eye defect", "partial ring", "local zone",
]


def cause_noise_reason(cause_id: str, cause_name: str) -> Optional[str]:
    """물리적 원인이 아니면 사유를 돌려준다(버림 대상). 정상이면 None."""
    # 동어반복: '원인'이 사실 알려진 불량 패턴 이름 (예: Center<-Center, Scratch<-scratch)
    if resolve_pattern(cause_name) or resolve_pattern(cause_id):
        return "원인이 불량 패턴 이름(동어반복)"
    text = _normalize_key(f"{cause_id} {cause_name}")
    for kw in METHOD_NOISE_KEYWORDS:
        if kw in text:
            return f"방법론/분류 잡음 '{kw}'"
    return None


def chunk_mentions_pattern(text: str) -> bool:
    hay = _normalize_key(text)
    return any(re.search(rf"\b{re.escape(s)}\b", hay) for s in PATTERN_SURFACES)


# =========================
# 3. 추출 스키마
# =========================

class PatternCause(BaseModel):
    """논문이 말하는 '이 불량 패턴의 원인'."""
    pattern: str = Field(description="불량 패턴 이름. 아래 고정 8종 중 하나로만.")
    cause_id: str = Field(description="원인 유일키. 소문자 snake_case. 예: irregular_rf_operation")
    cause_name: str = Field(description="문헌에 쓰인 원인 이름 그대로")
    cause_description: str = Field(description="완결된 한국어 한 문장")
    confidence: float = Field(description="추출 신뢰도 1~5. 애매하면 낮게.")
    quotes: list[str] = Field(default_factory=list, description="근거 원문 스니펫(짧게)")


class PatternCauseExtract(BaseModel):
    items: list[PatternCause]


# =========================
# 4. 프롬프트
# =========================

def build_prompt(chunk: dict) -> str:
    return f"""
다음은 반도체 웨이퍼 불량을 다루는 학술 문헌의 한 조각입니다.
여기서 **"어떤 불량 패턴이 어떤 원인으로 생기는가"** 정보만 뽑으세요. (패턴 → 원인)

특히 결함 패턴과 그 원인(source of defects)을 나열한 **표**가 핵심입니다.
표의 각 행 = (패턴, 원인) 쌍. 한 패턴에 원인이 여러 개면 각각 별도 항목으로 만드세요.

불량 패턴은 아래 8종(WM-811K) 중 하나로만 매핑하세요. 목록에 없으면 그 행은 건너뜁니다.
  Center, Donut, Edge-Loc, Edge-Ring, Loc, Near-Full, Random, Scratch

규칙:
- 원문(특히 표)에 명시된 것만. 추측 금지. 없으면 items 를 빈 리스트로.
- pattern 은 위 8종의 정확한 문자열로. (예: 'Edge-Ring' O, 'edge ring' X)
- cause_id 는 소문자 snake_case (예: irregular_rf_operation, unusual_liquid_flow).
- cause_name 은 문헌 표현 그대로, cause_description 은 완결된 한국어 한 문장.
- 공정 변수 이름 자체(rf_power 등)가 아니라, 이상 서술("불규칙한 RF 동작")을 원인으로 담으세요.
- 각 항목에 confidence(1~5)와 근거 quotes 를 채우세요.

청크 메타: chunk_id={chunk['chunk_id']}, doc_id={chunk.get('doc_id')}

원문:
{chunk['text']}
"""


# =========================
# 5. 추출 + 검증
# =========================

def extract(structured_llm, chunk: dict) -> PatternCauseExtract:
    return structured_llm.invoke(build_prompt(chunk))


def validate(ext: PatternCauseExtract, dropped: Optional[list[str]] = None) -> list[dict]:
    """패턴을 고정 8종으로 정규화, 신뢰도<2 폐기. 살아남은 항목을 dict 리스트로."""
    log = dropped if dropped is not None else []
    out = []
    seen: set[tuple[str, str]] = set()   # (pattern, cause_id) 중복 제거
    for it in ext.items:
        raw = f"{it.pattern!r} -> {it.cause_id!r}"
        if it.confidence < 2:
            log.append(f"{raw}: 신뢰도 {it.confidence} < 2")
            continue
        pattern = resolve_pattern(it.pattern)
        if pattern is None:
            log.append(f"{raw}: 패턴 '{it.pattern}' 을 8종에 매핑 실패")
            continue
        cause_id = normalize_id(it.cause_id)
        if not cause_id:
            log.append(f"{raw}: cause_id 비었음")
            continue
        noise = cause_noise_reason(cause_id, it.cause_name)
        if noise:
            log.append(f"{raw}: {noise}")
            continue
        key = (pattern, cause_id)
        if key in seen:
            log.append(f"{raw}: 중복")
            continue
        seen.add(key)
        out.append({
            "pattern": pattern,
            "cause_id": cause_id,
            "cause_name": it.cause_name,
            "cause_description": it.cause_description,
            "confidence": it.confidence,
            "quotes": it.quotes,
        })
    return out


# =========================
# 6. Neo4j 저장
# =========================

def get_graph() -> Neo4jGraph:
    return Neo4jGraph(
        url=NEO4J_URI,
        username=NEO4J_USERNAME,
        password=NEO4J_PASSWORD,
        database=NEO4J_DATABASE,
    )


def save(graph: Neo4jGraph, items: list[dict], chunk: dict) -> None:
    if not items:
        return
    graph.query(
        """
        MATCH (ch:Chunk {id: $chunk_id})
        UNWIND $items AS it
        MERGE (cause:Cause {id: it.cause_id})
          ON CREATE SET cause.name = it.cause_name, cause.description = it.cause_description
        MERGE (ch)-[:MENTIONS]->(cause)
        WITH it, cause
        MATCH (dp:DefectPattern {id: it.pattern})
        MERGE (dp)-[r:ATTRIBUTED_TO]->(cause)
        SET r.source = 'literature',
            r.extraction_confidence = it.confidence,
            r.quotes = it.quotes,
            r.chunk_ids = CASE
                WHEN r.chunk_ids IS NULL THEN [$chunk_id]
                WHEN NOT $chunk_id IN r.chunk_ids THEN r.chunk_ids + [$chunk_id]
                ELSE r.chunk_ids END
        """,
        params={"chunk_id": chunk["chunk_id"], "items": items},
    )


def append_result_to_jsonl(output_path: Path, chunk: dict, items: list[dict]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    row = {"chunk_id": chunk["chunk_id"], "doc_id": chunk.get("doc_id"), "items": items}
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
                "text": row["page_content"],
                "doc_id": metadata.get("doc_id"),
                "file_type": metadata.get("file_type"),
            })
    return chunks


# =========================
# 8. 실행
# =========================

def main() -> None:
    chunks = load_chunks(CHUNKS_PATH)

    if ONLY_DOC_IDS:
        chunks = [c for c in chunks if c["doc_id"] in ONLY_DOC_IDS]
        print("대상 doc_id 필터:", ONLY_DOC_IDS)
    else:
        chunks = [c for c in chunks if c.get("file_type") == "pdf"]
        print("대상: PDF 논문 청크만")

    total = len(chunks)
    # prefilter: 패턴 이름이 등장하는 청크만 LLM에 보낸다
    targets = [c for c in chunks if chunk_mentions_pattern(c["text"])]
    print(f"전체 {total}청크 중 패턴 언급 {len(targets)}청크만 추출 대상 (prefilter)")

    graph = get_graph()

    llm = ChatOpenAI(model=OPENAI_MODEL, temperature=0)
    structured_llm = llm.with_structured_output(PatternCauseExtract, method="json_schema")

    if OUTPUT_PATH.exists():
        OUTPUT_PATH.unlink()

    totals = {"items": 0, "dropped": 0}
    pairs_by_pattern: dict[str, int] = {}

    for i, chunk in enumerate(targets, start=1):
        print("=" * 80)
        print(f"[{i}/{len(targets)}] {chunk['chunk_id']}")

        ext = extract(structured_llm, chunk)
        dropped: list[str] = []
        items = validate(ext, dropped)

        save(graph, items, chunk)
        append_result_to_jsonl(OUTPUT_PATH, chunk, items)

        for it in items:
            pairs_by_pattern[it["pattern"]] = pairs_by_pattern.get(it["pattern"], 0) + 1
            print(f"   · {it['pattern']} <- {it['cause_id']}")
        for r in dropped:
            print("   버림:", r)

        totals["items"] += len(items)
        totals["dropped"] += len(dropped)

    graph.refresh_schema()

    print("\n완료")
    print(f"  적재된 (패턴→원인) 쌍: {totals['items']}")
    print(f"  패턴별: {pairs_by_pattern}")
    print(f"  버림: {totals['dropped']}")
    print("결과 저장:", OUTPUT_PATH)


if __name__ == "__main__":
    main()
