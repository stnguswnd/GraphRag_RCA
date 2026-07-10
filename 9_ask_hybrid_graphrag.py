import os
from dotenv import load_dotenv

from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_neo4j import Neo4jGraph, Neo4jVector
from langchain_core.prompts import ChatPromptTemplate


# ============================================================
# 1. 환경 변수 로드
# ============================================================

load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4")
EMBEDDING_MODEL = os.getenv(
    "OPENAI_EMBEDDING_MODEL",
    "text-embedding-3-small",
)


# ============================================================
# 2. 7_build_hybrid_index.py에서 만든 인덱스 이름
# ------------------------------------------------------------#
# chunk_vector_index:
#   Chunk.embedding 속성을 대상으로 하는 vector index
#
# chunk_keyword_index:
#   Chunk.text 속성을 대상으로 하는 full-text keyword index
# ============================================================

VECTOR_INDEX_NAME = "chunk_vector_index"
KEYWORD_INDEX_NAME = "chunk_keyword_index"


# ============================================================
# 3. Neo4j 연결
# ------------------------------------------------------------
# Neo4jGraph는 여기서 상태 확인용으로 사용합니다.
# 실제 Hybrid Search는 Neo4jVector가 수행합니다.
# ============================================================

graph = Neo4jGraph(
    url=NEO4J_URI,
    username=NEO4J_USERNAME,
    password=NEO4J_PASSWORD,
    database=NEO4J_DATABASE,
)

# ============================================================
# 4. Embedding / LLM 준비
# ------------------------------------------------------------
# embeddings:
#   질문을 embedding으로 바꾸는 데 사용합니다.
#   7_build_hybrid_index.py에서 Chunk.embedding을 만들 때 사용한
#   embedding 모델과 같은 모델 사용
#
# llm:
#   검색된 context를 바탕으로 최종 답변을 생성합니다.
# ============================================================

embeddings = OpenAIEmbeddings(
    model=EMBEDDING_MODEL,
)

llm = ChatOpenAI(
    model=OPENAI_MODEL,
    temperature=0,
)

# ============================================================
# 5. Graph Expansion Query
# ------------------------------------------------------------
# [Hybrid GraphRAG 핵심]
#
# Neo4jVector가 먼저 Hybrid Search를 수행합니다.
#
# 1차 검색:
#   - Vector Search: Chunk.embedding 검색
#   - Keyword Search: Chunk.text 검색
#
# 그 결과로 검색된 Chunk가 retrieval_query 안에서
# node 변수로 전달됩니다.
#
# 이후 아래 Cypher가 node에서 출발해 그래프를 확장합니다.
#
# 2차 그래프 확장:
#   - 이전 Chunk
#   - 다음 Chunk
#   - Chunk가 언급한 KGEntity
#   - KGEntity 주변 관계
#
# 반환 규칙:
#   LangChain Neo4jVector의 retrieval_query는 반드시
#   text, score, metadata 세 컬럼을 반환해야 합니다.
# ============================================================

# [중요] 이 Cypher는 "하이브리드 검색이 끝난 뒤" 자동으로 실행된다.
# 하이브리드 검색으로 찾아낸 Chunk 하나하나가 여기서 'node' 라는 변수로 들어온다.
# (node = 이미 찾은 Chunk, score = 그 Chunk의 검색 점수. 둘 다 LangChain이 자동으로 넣어줌)
retrieval_query = """
// 찾은 Chunk(node)가 언급(MENTIONS)하는 KGEntity들을 그래프에서 이어붙인다.
// OPTIONAL = 연결된 엔티티가 없어도 Chunk는 버리지 않고 그대로 둔다.
OPTIONAL MATCH (node)-[:MENTIONS]->(entity:KGEntity)

WITH
    node,
    score,
    // 한 Chunk가 여러 엔티티를 언급할 수 있으므로 리스트로 모은다 (중복 제거 = DISTINCT)
    collect(DISTINCT {
        name: entity.name,
        type: entity.type
    }) AS entities

// ★ Neo4jVector 규칙: retrieval_query는 반드시 text / score / metadata 3개 컬럼을 반환해야 한다.
RETURN
    {
        chunk_text: node.text,   // 원문 Chunk 텍스트
        entities: entities       // 그 Chunk가 언급한 엔티티 목록 (= 그래프에서 확장한 정보)
    } AS text,                   // ← 이 text 전체가 나중에 Document.page_content 가 된다
    score,                       // ← 검색 점수
    {
        chunk_id: node.id,
        page_number: node.page_number,
        source: node.source
    } AS metadata                // ← 부가정보. Document.metadata 가 된다
"""


# ============================================================
# 6. Hybrid GraphRAG VectorStore 불러오기
# ------------------------------------------------------------
# from_existing_index:
#   이미 만들어진 vector index를 불러옵니다.
#   새 embedding을 다시 저장하지 않습니다.
#
# search_type="hybrid":
#   vector search + keyword search를 함께 사용합니다.
#
# retrieval_query:
#   hybrid search로 찾은 Chunk 주변의 graph context를 확장합니다.
# ============================================================

vector_store = Neo4jVector.from_existing_index(
    embedding=embeddings,          # 질문을 벡터로 바꿀 임베딩 모델 (7번과 동일해야 함)
    url=NEO4J_URI,
    username=NEO4J_USERNAME,
    password=NEO4J_PASSWORD,
    database=NEO4J_DATABASE,

    index_name=VECTOR_INDEX_NAME,           # 벡터(의미) 검색용 인덱스
    keyword_index_name=KEYWORD_INDEX_NAME,  # 키워드(단어) 검색용 인덱스

    search_type="hybrid",              # ★ 벡터 + 키워드 검색을 함께 사용
    retrieval_query=retrieval_query,   # ★ 검색 후 그래프를 확장하는 위의 Cypher를 붙인다 (8번과의 결정적 차이)
)


# ============================================================
# 7. Hybrid GraphRAG 검색 함수
# ------------------------------------------------------------
# similarity_search_with_score()를 직접 호출합니다.
#
# 내부 동작:
# 1. 질문 embedding 생성
# 2. vector index 검색
# 3. keyword index 검색
# 4. 결과 병합
# 5. 검색된 Chunk를 node로 하여 retrieval_query 실행
# 6. graph-expanded context 반환
# ============================================================

def search_hybrid_graphrag(question: str, k: int = 5):
    # 이 한 줄이 5단계를 내부에서 순서대로 수행한다:
    #   1) 질문을 임베딩(벡터)으로 변환
    #   2) 벡터 인덱스 검색  (의미 유사)
    #   3) 키워드 인덱스 검색 (단어 일치)
    #   4) 두 결과 병합 (하이브리드)
    #   5) 찾은 Chunk를 node로 삼아 retrieval_query(그래프 확장) 실행
    # 반환: [(Document, score), ...]  ← Document.page_content 안에 chunk_text + entities가 들어있음
    results = vector_store.similarity_search_with_score(
        question,
        k=k,      # 가져올 Chunk 개수
    )

    return results


# ============================================================
# 8. 검색 결과를 LLM context로 변환
# ------------------------------------------------------------
# similarity_search_with_score() 반환 형태:
#
# [
#   (Document, score),
#   (Document, score),
#   ...
# ]
#
# retrieval_query에서 RETURN한 text가 Document.page_content가 됩니다.
# metadata는 Document.metadata에 들어갑니다.
# ============================================================

def format_context(results) -> str:
    if not results:
        return "검색된 문맥이 없습니다."

    formatted = []

    # (문서, 점수)를 하나씩 꺼내 번호(i)를 붙여 사람이 읽기 좋은 글로 만든다.
    # doc.page_content 안에는 원문 + 그래프에서 확장한 엔티티가 함께 들어있다.
    for i, (doc, score) in enumerate(results, start=1):
        formatted.append(
            f"""
[검색 결과 {i}]
검색 점수:
{score}

metadata:
{doc.metadata}

검색된 Chunk 및 Graph Context:
{doc.page_content}
"""
        )

    return "\n\n".join(formatted)


# ============================================================
# 9. 답변 생성 프롬프트
# ============================================================

prompt = ChatPromptTemplate.from_messages([
    (
        "system",
        """
너는 보험 약관 기반 Hybrid GraphRAG assistant다.

너는 다음 세 가지 검색 결과를 바탕으로 답변한다.
1. Vector Search로 찾은 의미적으로 유사한 Chunk
2. Keyword Search로 찾은 키워드 일치 Chunk
3. 검색된 Chunk 주변의 Knowledge Graph 관계

답변 규칙:
- 반드시 제공된 context에 근거해서 답변하라.
- context에 없는 내용은 추측하지 말고 모른다고 말하라.
"""
    ),
    (
        "human",
        """
질문:
{question}

검색된 context:
{context}
"""
    )
])


# ============================================================
# 10. 최종 질의응답 함수
# ------------------------------------------------------------#
# 1. Hybrid GraphRAG 검색
# 2. 검색 결과를 context로 변환
# 3. Prompt 생성
# 4. LLM 답변 생성
# ============================================================

def answer_question(question: str, k: int = 5, show_context: bool = False) -> str:
    # 1. 하이브리드 검색 + 그래프 확장을 한 번에 수행 (위 함수가 5단계를 내부 처리)
    #    ※ 8번과 달리 검색기가 "하나"다. 검색과 그래프 확장이 한 파이프라인으로 이어진다.
    search_results = search_hybrid_graphrag(question, k=k)

    # 2. 검색 결과(문서+점수)를 LLM이 읽을 수 있는 하나의 문자열 context로 변환
    context = format_context(search_results)

    # (선택) LLM에 넘기기 전에 실제 context를 눈으로 확인하고 싶을 때
    if show_context:
        print("\n" + "-" * 80)
        print("[검색 context]")
        print("-" * 80)
        print(context)

    # 3. 프롬프트의 빈 칸({question}, {context})을 실제 값으로 채운다
    messages = prompt.format_messages(
        question=question,
        context=context,
    )

    # 4. 채워진 프롬프트를 LLM에 보내 최종 답변 생성
    response = llm.invoke(messages)

    return response.content   # 답변 텍스트만 반환


# ============================================================
# 12. 테스트 질문
# ============================================================

questions = [
    "우리집 댕댕이가 의자를 파손해 보험금 청구하려 하는데 필요한 서류와 지급 절차는 어떻게 돼?",
    "우리집 강아지가 너무 짖어 시끄럽다고 보상하라는데 이 보험으로 커버 가능해?",
]


# 준비한 질문들을 하나씩 돌려가며 답변을 출력한다.
for question in questions:
    print("=" * 80)
    print("질문:", question)

    answer = answer_question(
        question,
        k=5,                 # Chunk 5개 검색
        show_context=True,   # 검색된 context도 함께 보고 싶으면 True (과정 이해에 도움)
    )

    print("\n답변:")
    print(answer)