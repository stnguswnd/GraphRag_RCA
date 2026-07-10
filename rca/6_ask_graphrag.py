import os
import sys
from dotenv import load_dotenv

from langchain_openai import ChatOpenAI
from langchain_neo4j import Neo4jGraph, GraphCypherQAChain
from langchain_core.prompts import PromptTemplate

# Windows 콘솔(cp949)에서 em-dash 등 유니코드 출력 시 크래시 방지
sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5")

graph = Neo4jGraph(
    url=NEO4J_URI,
    username=NEO4J_USERNAME,
    password=NEO4J_PASSWORD,
    database=NEO4J_DATABASE,
)
graph.refresh_schema()

llm = ChatOpenAI(
    model=OPENAI_MODEL,
    temperature=0,
)


# ============================================================
# Cypher 생성 프롬프트
# ------------------------------------------------------------
# RCA 그래프 구조를 LLM에게 알려줘서 Cypher를 더 잘 만들게 한다.
#   (:Cause)-[:MANIFESTS_AS]->(:DefectPattern)
#   (:Cause)-[:CAUSED_BY]->(:Cause)
#   (:Cause)-[:OCCURS_IN]->(:ProcessStep)
#   (:Cause)-[:INVOLVES_PARAMETER]->(:ParameterType)
#   (:Cause)-[:DETECTED_BY]->(:DetectionMethod)
#   (:Chunk)-[:MENTIONS]->(:Cause)
# ============================================================

CYPHER_GENERATION_TEMPLATE = """
당신은 Neo4j Cypher 전문가입니다.
반도체 웨이퍼 불량 원인분석(RCA) 지식그래프에 대한 질문에 답하기 위한 Cypher만 생성하세요.

그래프 핵심 구조:
- (:Cause)-[:MANIFESTS_AS]->(:DefectPattern)      원인이 어떤 불량 패턴으로 나타나는지 (표면 원인)
- (:Cause)-[:CAUSED_BY]->(:Cause)                 원인의 배후 원인 (근본 원인으로 거슬러 올라감)
- (:Cause)-[:OCCURS_IN]->(:ProcessStep)           원인이 발생하는 공정 단계
- (:Cause)-[:INVOLVES_PARAMETER]->(:ParameterType) 관련 공정 변수 (direction: high/low)
- (:Cause)-[:DETECTED_BY]->(:DetectionMethod)     원인 확인용 도구 (role: verify/pipeline)
- (:Chunk)-[:MENTIONS]->(:Cause)                  근거 문헌 조각

규칙:
- 읽기 전용 쿼리만 생성하세요. CREATE, MERGE, DELETE, SET, REMOVE 금지.
- 불량 패턴 이름은 DefectPattern.name 으로 매칭하세요 (예: 'Edge-Ring').
- "근본 원인/root cause"을 물으면 CAUSED_BY를 따라가세요. 깊이는 최대 2까지만 (*1..2).
- "어떻게 확인/검증"을 물으면 DETECTED_BY와 DetectionMethod.mcp_tool 를 반환하세요.
- 반환 결과는 최대 10개로 제한하세요.
- 백틱(`)을 사용하지 마세요.
- Cypher 코드만 출력하세요. 설명하지 마세요.

스키마:
{schema}

질문:
{question}
"""

cypher_prompt = PromptTemplate(
    input_variables=["schema", "question"],
    template=CYPHER_GENERATION_TEMPLATE,
)

chain = GraphCypherQAChain.from_llm(
    llm=llm,
    graph=graph,
    cypher_prompt=cypher_prompt,
    verbose=True,
    validate_cypher=True,
    allow_dangerous_requests=True,
    top_k=10,
)


questions = [
    "Edge-Ring 불량의 가능한 원인은 뭐야?",
    "Edge-Ring 불량의 근본 원인(root cause)까지 거슬러 올라가면 뭐가 있어?",
    "focus_ring_erosion 원인은 어떤 도구로 확인해?",
    "Scratch 불량은 어느 공정에서 생길 수 있어?",
    "Donut 불량과 관련된 공정 변수는 뭐야?",
]

for question in questions:
    print("=" * 80)
    print("질문:", question)

    try:
        result = chain.invoke({"query": question})
        print("\n답변:")
        print(result["result"])
    except Exception as e:
        print("\n에러 발생:")
        print(type(e).__name__)
        print(e)
