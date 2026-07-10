import sys
import json
from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

# Windows 콘솔(cp949)에서 em-dash 등 유니코드 출력 시 크래시 방지
sys.stdout.reconfigure(encoding="utf-8")


# =========================
# 1. 경로 설정
# =========================

BASE_DIR = Path(__file__).resolve().parent

INPUT_PATH = BASE_DIR / "outputs" / "parsed_docs.jsonl"
OUTPUT_PATH = BASE_DIR / "outputs" / "chunks.jsonl"


# =========================
# 2. parsed_docs.jsonl 로드
# =========================

def load_parsed_documents(input_path: Path) -> list[Document]:
    """
    2_load_txt.py가 저장한 parsed_docs.jsonl을 Document 리스트로 복원
    """
    if not input_path.exists():
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {input_path}")

    docs = []

    with input_path.open("r", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            docs.append(
                Document(
                    page_content=row["page_content"],
                    metadata=dict(row.get("metadata", {})),
                )
            )

    return docs


# =========================
# 3. 청킹
# -------------------------
# 문단(빈 줄) → 줄 → 문장 순으로 잘라 문맥이 최대한 안 끊기게 한다.
# 기술 문헌은 문단 단위 의미가 강해서 "\n\n"을 최우선 separator로 둔다.
# =========================

def chunk_documents(docs: list[Document]) -> list[Document]:
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=80,
        add_start_index=True,
        keep_separator=True,
        separators=[
            "\n\n",   # 문단
            "\n",     # 줄
            ". ",     # 문장
            " ",
            "",
        ],
    )

    chunks = text_splitter.split_documents(docs)

    # 문서별 청크 순번을 매기기 위한 카운터
    per_doc_index: dict[str, int] = {}
    cleaned_chunks = []

    for chunk in chunks:
        text = chunk.page_content.strip()

        # 너무 짧은 청크는 버린다
        if len(text) < 20:
            continue

        doc_id = chunk.metadata.get("doc_id", "unknown")

        # 문서 안에서 0,1,2... 로 증가하는 순번
        idx = per_doc_index.get(doc_id, 0)
        per_doc_index[doc_id] = idx + 1

        # 사람이 읽기 좋은 chunk_id: "etch_rca_guide#c00"
        chunk_id = f"{doc_id}#c{idx:02d}"

        chunk.metadata["chunk_id"] = chunk_id
        chunk.metadata["chunk_index"] = idx
        chunk.metadata["char_count"] = len(text)
        chunk.page_content = text

        cleaned_chunks.append(chunk)

    return cleaned_chunks


# =========================
# 4. JSONL 저장
# =========================

def save_chunks_to_jsonl(chunks: list[Document], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as f:
        for chunk in chunks:
            row = {
                "chunk_id": chunk.metadata["chunk_id"],
                "chunk_index": chunk.metadata["chunk_index"],
                "page_content": chunk.page_content,
                "metadata": chunk.metadata,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


# =========================
# 5. 실행
# =========================

def main() -> None:
    docs = load_parsed_documents(INPUT_PATH)
    print("불러온 문헌 수:", len(docs))

    chunks = chunk_documents(docs)
    print("생성된 청크 수:", len(chunks))

    save_chunks_to_jsonl(chunks, OUTPUT_PATH)
    print("저장 완료:", OUTPUT_PATH)

    print("\n미리보기")
    print("=" * 80)
    for chunk in chunks[:3]:
        print("chunk_id:", chunk.metadata["chunk_id"])
        print("doc_id:", chunk.metadata.get("doc_id"))
        print("char_count:", chunk.metadata["char_count"])
        print(chunk.page_content[:300])
        print("-" * 80)


if __name__ == "__main__":
    main()
