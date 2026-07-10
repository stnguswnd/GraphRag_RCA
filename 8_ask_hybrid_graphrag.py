import os
from dotenv import load_dotenv

from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_neo4j import Neo4jGraph, Neo4jVector, GraphCypherQAChain
from langchain_core.prompts import PromptTemplate, ChatPromptTemplate


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
# ------------------------------------------------------------
# 반드시 7번 파일에서 생성한 이름과 같아야 합니다.
# ============================================================

VECTOR_INDEX_NAME = "chunk_vector_index"
KEYWORD_INDEX_NAME = "chunk_keyword_index"


# ============================================================
# 3. Neo4j 연결
# ============================================================

graph = Neo4jGraph(
    url=NEO4J_URI,
    username=NEO4J_USERNAME,
    password=NEO4J_PASSWORD,
    database=NEO4J_DATABASE,
)


# ============================================================
# 4. Embedding / LLM 준비
# ============================================================

embeddings = OpenAIEmbeddings(
    model=EMBEDDING_MODEL,
)

llm = ChatOpenAI(
    model=OPENAI_MODEL,
    temperature=0,
)

# ============================================================
# 5. Vector + Keyword Retriever 준비
# ------------------------------------------------------------
#
# 역할:
# - Chunk.embedding 기반 Vector Search
# - Chunk.text 기반 Keyword Search
# - 두 결과를 합쳐 관련 Chunk 반환
# ============================================================

# from_existing_index: 7번에서 "이미 만들어 둔" 인덱스를 그대로 불러온다.
# (여기서 embedding을 새로 만들지 않는다. 검색만 할 것이므로 기존 인덱스에 붙기만 함)
vector_store = Neo4jVector.from_existing_index(
    embedding=embeddings,          # 질문을 벡터로 바꿀 때 쓰는 임베딩 모델 (7번과 반드시 동일해야 함)
    url=NEO4J_URI,
    username=NEO4J_USERNAME,
    password=NEO4J_PASSWORD,
    database=NEO4J_DATABASE,

    index_name=VECTOR_INDEX_NAME,           # 의미(벡터) 검색용 인덱스
    keyword_index_name=KEYWORD_INDEX_NAME,  # 단어(키워드) 검색용 인덱스

    search_type="hybrid",   # ★ 벡터 검색 + 키워드 검색을 동시에 돌려 결과를 합친다 = "하이브리드"
)


def search_vector_keyword(question: str, k: int = 5):
    """
    [검색기 1] Vector Search + Keyword Search로 관련 Chunk 조회
    - 질문과 의미가 비슷하거나(벡터) 단어가 겹치는(키워드) 원문 Chunk를 k개 찾아온다.
    """

    # similarity_search_with_score: 검색 결과를 (문서, 유사도점수) 쌍의 리스트로 돌려준다.
    # 예) [(Document(원문...), 0.83), (Document(원문...), 0.79), ...]
    result = vector_store.similarity_search_with_score(
        question,
        k=k,        # 몇 개까지 가져올지 (기본 5개)
    )

    print("result : " , result)   # 디버깅용: 실제로 뭐가 검색됐는지 눈으로 확인
    return result


def format_vector_keyword_context(results) -> str:
    """
    검색기 1의 결과(문서+점수 리스트)를 LLM이 읽을 수 있는 "하나의 긴 문자열"로 변환한다.
    LLM은 파이썬 객체가 아니라 텍스트만 읽을 수 있으므로, 이렇게 사람이 보기 좋은 글로 풀어준다.
    """

    if not results:   # 검색 결과가 아예 없으면
        return "Vector + Keyword 검색 결과가 없습니다."

    formatted = []   # 각 검색 결과를 하나씩 글로 만들어 담을 리스트

    # results 안의 (문서, 점수)를 하나씩 꺼내 번호(i)를 붙여가며 반복
    for i, (doc, score) in enumerate(results, start=1):
        # doc.metadata   = 어느 페이지/출처인지 등 부가정보
        # doc.page_content = 실제 원문 Chunk 텍스트
        formatted.append(
            f"""
[Hybrid 검색 결과 {i}]
score:
{score}

metadata:
{doc.metadata}

chunk_text:
{doc.page_content}
"""
        )

    return "\n\n".join(formatted)


# ============================================================
# 6. GraphCypherQAChain 준비
# ------------------------------------------------------------
#
# 역할:
# - 사용자 질문을 Cypher로 변환
# - Neo4j 그래프 구조를 직접 조회
# - KGEntity, 관계, evidence, 연결 Chunk 등을 검색
# ============================================================

CYPHER_GENERATION_TEMPLATE = """
당신은 Neo4j Cypher 전문가입니다.
사용자의 질문에 답하기 위한 Cypher만 생성하세요.

규칙:
- 읽기 전용 쿼리만 생성하세요.
- CREATE, MERGE, DELETE, SET, REMOVE, DROP 사용 금지.
- 반환 결과는 최대 10개로 제한하세요.
- 긴 Chunk.text 전체를 너무 많이 반환하지 마세요.
- 필요한 경우 name, type, evidence, page_number, chunk_id, source 정도만 반환하세요.
- 백틱(`)을 사용하지 마세요.
- Cypher 코드만 출력하세요. 설명하지 마세요.

스키마:
{schema}

질문:
{question}
"""

# 위 템플릿을 실제 프롬프트 객체로 만든다.
# {schema}(그래프 구조 설명)와 {question}(사용자 질문) 두 칸이 나중에 채워진다.
cypher_prompt = PromptTemplate(
    input_variables=["schema", "question"],
    template=CYPHER_GENERATION_TEMPLATE,
)

# GraphCypherQAChain: "질문 → Cypher 자동 생성 → Neo4j 실행 → 결과로 답변" 을 한 번에 해주는 체인
graph_chain = GraphCypherQAChain.from_llm(
    llm=llm,
    graph=graph,
    cypher_prompt=cypher_prompt,      # 위에서 만든 Cypher 생성용 프롬프트
    verbose=True,                     # 생성된 Cypher를 콘솔에 출력 (디버깅에 유용)
    validate_cypher=True,             # 만들어진 Cypher가 스키마상 말이 되는지 검사
    allow_dangerous_requests=True,    # LLM이 만든 쿼리를 실제 DB에 실행하는 것을 허용 (주의: 프롬프트로 읽기전용 강제)
    top_k=10,                         # 결과를 최대 10개까지만
)


def search_graph_cypher(question: str) -> dict:
    """
    [검색기 2] GraphCypherQAChain으로 그래프 구조를 직접 조회한다.
    질문을 Cypher(그래프 질의 언어)로 번역해 실행하므로,
    "필요서류 → 절차 → 제외조건" 같은 '관계'를 따라가는 질문에 강하다.

    반환 dict에는 보통 다음이 들어있다:
    - result: 그래프 조회 결과를 바탕으로 만든 1차 자연어 답변
    - intermediate_steps: 실제로 생성된 Cypher와 DB raw 결과 (과정 추적용)
    """
    # invoke에 {"query": 질문} 형태로 넣으면 체인이 알아서 Cypher 생성→실행→요약까지 수행
    result = graph_chain.invoke({"query": question})

    print("graphrag_result : ", result)   # 디버깅용 출력

    return result


def format_graph_context(graph_search_result: dict) -> str:
    """
    GraphCypherQAChain 결과를 LLM에게 전달할 문자열 context로 변환합니다.
    """

    # 체인 결과 dict에서 자연어 답변("result" 키)만 꺼낸다. 없으면 빈 문자열.
    graph_answer = graph_search_result.get("result", "")

    formatted = []
    formatted.append(graph_answer)

    return "\n".join(formatted)


# ============================================================
# 7. 최종 답변 생성 프롬프트
# ------------------------------------------------------------
# 두 독립 검색 결과를 모두 LLM에게 전달합니다.
#
# 1. Vector + Keyword 검색 결과
# 2. GraphCypherQAChain 검색 결과
#
# LLM은 두 결과를 비교/종합해서 최종 답변합니다.
# ============================================================

final_prompt = ChatPromptTemplate.from_messages([
    (
        "system",
        """
너는 보험 약관 기반 Hybrid GraphRAG assistant다.

너에게는 두 종류의 검색 결과가 제공된다.

1. Vector + Keyword 검색 결과
   - 질문과 의미적으로 유사하거나 키워드가 일치하는 Chunk
   - 원문 근거를 확인하는 데 유용하다.

2. GraphCypherQAChain 검색 결과
   - Neo4j Knowledge Graph 구조를 기반으로 조회한 결과
   - 조항, 필요서류, 지급절차, 보상 제외 조건 등 관계를 확인하는 데 유용하다.

답변 규칙:
- 반드시 제공된 검색 결과에 근거해서 답변하라.
- 검색 결과에 없는 내용은 추측하지 말고 모른다고 말하라.
"""
    ),
    (
        "human",
        """
질문:
{question}

[1] Vector + Keyword 검색 context:
{vector_keyword_context}

[2] GraphCypherQAChain 검색 context:
{graph_context}
"""
    )
])


# ============================================================
# 8. 최종 Hybrid GraphRAG 질의응답 함수
# ------------------------------------------------------------
# 이 함수가 전체 구조를 실행합니다.
#
# 1. Vector + Keyword Retriever 실행
# 2. GraphCypherQAChain 실행
# 3. 두 결과를 문자열 context로 변환
# 4. LLM 최종 답변 생성
# ============================================================

def answer_question(
    question: str,
    hybrid_k: int = 5,          # 검색기 1이 가져올 Chunk 개수
    show_context: bool = False, # True면 LLM에 넘기기 전 검색 결과를 화면에 출력
) -> str:
    # ------------------------------------------------------------
    # 검색 1: Vector + Keyword Retriever (원문 근거 찾기)
    # ------------------------------------------------------------
    vector_keyword_results = search_vector_keyword(   # (1) 관련 Chunk 검색
        question,
        k=hybrid_k,
    )

    vector_keyword_context = format_vector_keyword_context(  # (2) 결과를 글(문자열)로 변환
        vector_keyword_results,
    )

    # ------------------------------------------------------------
    # 검색 2: GraphCypherQAChain (관계 구조 찾기)
    #   ※ 8번의 핵심: 검색 1과 검색 2를 "서로 독립적으로" 따로 돌린다.
    # ------------------------------------------------------------
    graph_search_result = search_graph_cypher(question)      # (3) 그래프를 Cypher로 조회

    graph_context = format_graph_context(graph_search_result) # (4) 결과를 글(문자열)로 변환

    # ------------------------------------------------------------
    # 두 검색 결과 확인용 출력
    # ------------------------------------------------------------
    if show_context:
        print("\n" + "-" * 80)
        print("[Vector + Keyword 검색 context]")
        print("-" * 80)
        print(vector_keyword_context)

        print("\n" + "-" * 80)
        print("[GraphCypherQAChain 검색 context]")
        print("-" * 80)
        print(graph_context)

    # ------------------------------------------------------------
    # 최종 답변 생성
    #   두 검색 결과(원문 근거 + 그래프 관계)를 프롬프트의 빈 칸에 채워 넣고
    #   LLM이 둘을 비교/종합해서 하나의 답을 만들게 한다. (= late fusion, 나중에 합치기)
    # ------------------------------------------------------------
    messages = final_prompt.format_messages(
        question=question,
        vector_keyword_context=vector_keyword_context,  # [1] 원문 근거
        graph_context=graph_context,                    # [2] 그래프 관계
    )

    response = llm.invoke(messages)   # LLM에게 최종 답변 요청

    return response.content           # LLM이 만든 답변 텍스트만 반환


# ============================================================
# 9. 테스트 질문
# ============================================================

questions = [
    "우리집 댕댕이가 의자를 파손해 보험금 청구하려 하는데 필요한 서류와 지급 절차는 어떻게 돼?",
    "우리집 강아지가 너무 짖어서 윗집에서 피해보상을 하라는데, 가입한 보험으로 처리 될까?",
]


# 준비한 질문들을 하나씩 돌려가며 최종 답변을 출력한다.
for question in questions:
    print("=" * 80)
    print("질문:", question)


    answer = answer_question(
        question,
        hybrid_k=5,          # 검색기 1이 Chunk 5개 검색
        show_context=True,   # 두 검색기의 context를 함께 보고 싶으면 True (과정 이해에 도움)
    )

    print("\n최종 답변:")
    print(answer)