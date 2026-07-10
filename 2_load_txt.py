import re
import sys
import json
import unicodedata
from pathlib import Path

from langchain_core.documents import Document
import pdfplumber

# Windows 콘솔(cp949)에서 em-dash 등 유니코드 출력 시 크래시 방지
sys.stdout.reconfigure(encoding="utf-8")


# =========================
# 1. 경로 설정
# =========================

BASE_DIR = Path(__file__).resolve().parent          # 프로젝트 루트

DOCS_DIR = BASE_DIR / "data" / "docs"
OUTPUT_DIR = BASE_DIR / "outputs"
OUTPUT_PATH = OUTPUT_DIR / "parsed_docs.jsonl"


# =========================
# 2. 텍스트 정제
# -------------------------
# .txt는 PDF보다 깨끗하지만, 최소한의 정규화는 해준다.
# =========================

def clean_text(text: str) -> str:
    if not text:
        return ""

    # 유니코드 정규화 (NFC)
    text = unicodedata.normalize("NFC", text)

    # 줄바꿈 통일
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # 탭/연속 공백 정리
    text = re.sub(r"[ \t]+", " ", text)

    # 3줄 이상 연속 빈 줄은 2줄로
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


# =========================
# 3. PDF 텍스트 추출 (컬럼 인식)
# -------------------------
# 학술 논문은 2단 편집 + 테두리 없는 표가 흔하다. 단순 텍스트 추출(pypdf 등)은
# 좌/우 컬럼을 한 줄로 섞어버려 표의 "패턴 | 원인" 행이 깨진다.
# 그래서 pdfplumber로 페이지를 좌/우 컬럼으로 나눠 각각 위→아래로 읽는다.
#
# - 2단 판별: 가운데 거터(mid)를 걸치는 단어가 거의 없으면 2단으로 본다.
# - x_tolerance=1.0: 글자 사이가 이만큼 벌어지면 공백을 넣는다.
#   (이 PDF들은 공백 문자를 인코딩하지 않아 'Irregularradiofrequency'처럼 붙는데, 이걸 복원)
# 텍스트가 없는 스캔 이미지 PDF는 추출 결과가 비어 조용히 걸러진다.
# 페이지 사이는 빈 줄로 구분해 3_split.py의 문단 우선 청킹이 자연스럽게 끊게 한다.
# =========================

X_TOLERANCE = 1.0        # 글자 간격이 이 이상이면 공백 삽입 (붙은 단어 복원)
STRADDLE_RATIO = 0.05    # 가운데를 걸치는 단어 비율이 이 미만이면 2단으로 판단


def _extract_page_text(page) -> str:
    """페이지 하나의 텍스트. 2단 편집이면 좌 컬럼 전체 → 우 컬럼 전체 순서로 읽는다."""
    mid = page.width / 2
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)

    two_col = False
    if words:
        straddle = sum(1 for w in words if w["x0"] < mid < w["x1"])
        left = [w for w in words if w["x1"] <= mid]
        right = [w for w in words if w["x0"] >= mid]
        two_col = (
            straddle < STRADDLE_RATIO * len(words)
            and len(left) > 10 and len(right) > 10
        )

    if two_col:
        left_text = page.crop((0, 0, mid, page.height)).extract_text(x_tolerance=X_TOLERANCE) or ""
        right_text = page.crop((mid, 0, page.width, page.height)).extract_text(x_tolerance=X_TOLERANCE) or ""
        return (left_text.strip() + "\n\n" + right_text.strip()).strip()

    return (page.extract_text(x_tolerance=X_TOLERANCE) or "").strip()


def extract_pdf_text(path: Path) -> str:
    try:
        with pdfplumber.open(str(path)) as pdf:
            pages = []
            for i, page in enumerate(pdf.pages):
                try:
                    text = _extract_page_text(page)
                except Exception as e:
                    print(f"  (경고: {path.name} p{i} 추출 실패 {e})")
                    text = ""
                if text.strip():
                    pages.append(text)
    except Exception as e:
        print(f"  (건너뜀: PDF 열기 실패 {e}) {path.name}")
        return ""

    return "\n\n".join(pages)


# =========================
# 4. 문헌 로드
# -------------------------
# 파일 하나 = 문헌 하나(Document 하나).
# doc_id는 파일 이름(확장자 제외), title은 문서 첫 줄로 잡는다.
#
# .txt / 확장자 없는 텍스트 / .pdf 를 받는다.
# (확장자 없는 텍스트 파일도 받는다 — 과거에 그런 문헌이 있었고, 조용히 누락됐었다.)
# 내용이 빈 파일(텍스트 없는 스캔 PDF 포함)은 건너뛴다.
# =========================

TEXT_SUFFIXES = {"", ".txt"}
PDF_SUFFIXES = {".pdf"}
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | PDF_SUFFIXES


def load_documents(docs_dir: Path) -> list[Document]:
    if not docs_dir.exists():
        raise FileNotFoundError(f"문헌 폴더를 찾을 수 없습니다: {docs_dir}")

    paths = sorted(
        p for p in docs_dir.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
    )

    if not paths:
        raise FileNotFoundError(f"문헌 파일이 없습니다: {docs_dir}")

    docs = []

    for path in paths:
        suffix = path.suffix.lower()

        if suffix in PDF_SUFFIXES:
            file_type = "pdf"
            raw = extract_pdf_text(path)
        else:
            file_type = "txt"
            raw = path.read_text(encoding="utf-8")

        cleaned = clean_text(raw)

        if not cleaned:
            print(f"  (건너뜀: 내용이 비어 있음) {path.name}")
            continue

        # 첫 줄을 제목으로 사용 (없으면 파일명)
        first_line = cleaned.split("\n", 1)[0].strip()
        title = first_line if first_line else path.stem

        docs.append(
            Document(
                page_content=cleaned,
                metadata={
                    "doc_id": path.stem,      # 예: "etch_rca_guide"
                    "title": title,
                    "source": path.name,      # 예: "etch_rca_guide.txt"
                    "file_type": file_type,   # "txt" | "pdf"
                },
            )
        )

    if not docs:
        raise ValueError(f"읽을 수 있는 문헌이 없습니다: {docs_dir}")

    return docs


# 이전 이름 유지 (다른 코드가 참조할 수 있으므로)
load_txt_documents = load_documents


# =========================
# 5. JSONL 저장
# -------------------------
# 다음 단계(3_split.py)가 다시 읽기 쉽도록 JSONL로 저장.
# =========================

def save_documents_to_jsonl(docs: list[Document], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as f:
        for doc in docs:
            row = {
                "doc_id": doc.metadata["doc_id"],
                "page_content": doc.page_content,
                "metadata": doc.metadata,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


# =========================
# 6. 실행
# =========================

def main() -> None:
    print("문헌 폴더:", DOCS_DIR)

    docs = load_documents(DOCS_DIR)
    print("불러온 문헌 수:", len(docs))

    for doc in docs:
        print(f"- [{doc.metadata['file_type']}] {doc.metadata['doc_id']} ({doc.metadata['title']})")

    docs = [
        Document(page_content=clean_text(d.page_content), metadata=d.metadata)
        for d in docs
    ]

    save_documents_to_jsonl(docs, OUTPUT_PATH)
    print("\n저장 완료:", OUTPUT_PATH)

    print("\n미리보기")
    print("=" * 80)
    print(docs[0].metadata)
    print(docs[0].page_content[:500])


if __name__ == "__main__":
    main()
