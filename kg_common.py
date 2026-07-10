"""
공용 유틸리티 — 파이프라인 전 단계가 공유하는 것들을 한 곳에 모은다.

이전에는 Neo4j 연결·청크 로드·표기 정규화·앵커 별칭 인덱스가 4/5/5b/6/7에 복붙돼 있었다.
스키마나 정규화 규칙이 바뀌면 여러 파일을 동시에 고쳐야 했고, 파일마다 미묘하게 달라질 위험이 있었다.
여기로 모아 단일 출처(single source of truth)로 만든다.

제공:
- 환경/경로 상수, `get_graph()`               — Neo4j 연결
- `normalize_id`, `normalize_key`               — 표기 정규화
- `load_seed_nodes`, `build_alias_index`, `resolve` — 고정 vocab(앵커) 로딩·매핑
- `load_chunks`                                 — chunks.jsonl 로드(파일타입 필터 지원)
"""

import os
import re
import json
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from langchain_neo4j import Neo4jGraph


# =========================
# 경로 / 환경
# =========================

BASE_DIR = Path(__file__).resolve().parent
SEEDS_DIR = BASE_DIR / "data" / "seeds"
OUTPUTS_DIR = BASE_DIR / "outputs"
CHUNKS_PATH = OUTPUTS_DIR / "chunks.jsonl"

load_dotenv(dotenv_path=BASE_DIR / ".env")

NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5")
OPENAI_EMBED_MODEL = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")


def get_graph() -> Neo4jGraph:
    """공용 Neo4j 핸들. 모든 단계가 이걸 쓴다."""
    return Neo4jGraph(
        url=NEO4J_URI,
        username=NEO4J_USERNAME,
        password=NEO4J_PASSWORD,
        database=NEO4J_DATABASE,
    )


# =========================
# 표기 정규화
# =========================

def normalize_id(raw: str) -> str:
    """LLM이 흘린 표기 흔들림 흡수: 소문자 + 비영숫자 → 밑줄. 예: 'High Etch Rate' -> 'high_etch_rate'."""
    return re.sub(r"[^a-z0-9_]+", "_", raw.strip().lower()).strip("_")


def normalize_key(raw: str) -> str:
    """앵커 매칭용 키. 대소문자·하이픈·밑줄·연속 공백 차이를 흡수. 'Edge-Ring' == 'edge_ring' == 'edge ring'."""
    return re.sub(r"[\s\-_]+", " ", raw.strip().lower())


# =========================
# 고정 vocabulary (앵커) 로딩
# =========================

def load_seed_nodes(file_name: str) -> list[dict]:
    """data/seeds/<file_name> 의 nodes 배열."""
    path = SEEDS_DIR / file_name
    if not path.exists():
        raise FileNotFoundError(f"시드 파일을 찾을 수 없습니다: {path}")
    return json.loads(path.read_text(encoding="utf-8"))["nodes"]


def build_alias_index(file_name: str) -> dict[str, str]:
    """
    시드의 id/name/aliases 를 모아 `표면형(정규화) -> canonical id` 역인덱스를 만든다.
    LLM이 'etching', 'edge-ring' 처럼 흔들어도 canonical id로 갈아끼우는 데 쓴다.
    (spatial_keywords 는 여러 패턴에 동시에 걸려 매칭에 쓰지 않는다.)
    """
    index: dict[str, str] = {}
    for node in load_seed_nodes(file_name):
        canonical = node["id"]
        for surface in [canonical, node.get("name", canonical), *node.get("aliases", [])]:
            index[normalize_key(surface)] = canonical
    return index


def resolve(raw: str, index: dict[str, str]) -> Optional[str]:
    """표면형 하나를 canonical id로. 못 붙이면 None."""
    return index.get(normalize_key(raw))


# =========================
# 청크 로딩
# =========================

def load_chunks(path: Path = CHUNKS_PATH, file_type: Optional[str] = None) -> list[dict]:
    """
    chunks.jsonl 을 dict 리스트로. `file_type`('txt'|'pdf')을 주면 그 소스만 남긴다.
    (모든 필드를 채워 두므로 각 단계가 필요한 것만 골라 쓴다.)
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"chunks.jsonl 파일을 찾을 수 없습니다: {path}")

    chunks: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            md = row.get("metadata", {})
            ft = md.get("file_type")
            if file_type is not None and ft != file_type:
                continue
            chunks.append({
                "chunk_id": row["chunk_id"],
                "chunk_index": row.get("chunk_index"),
                "text": row["page_content"],
                "doc_id": md.get("doc_id"),
                "title": md.get("title"),
                "source": md.get("source"),
                "start_index": md.get("start_index"),
                "char_count": md.get("char_count"),
                "file_type": ft,
            })
    return chunks
