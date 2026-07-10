"""
관측된 불량 패턴 → 근본 원인 가설 3건.

질문이 "{패턴} 결함 패턴이 나타나는 근본 원인은 무엇인가요?" 하나로 고정이므로
Cypher를 LLM에게 생성시키지 않는다(구 버전은 6_ask_graphrag_backup.py).
그래프 순회는 결정적으로 하고, LLM은 뽑아온 사실을 가설 문장으로 옮기기만 한다.

가설 1건 = DefectPattern -ARISES_IN-> ProcessStep <-OCCURS_IN- FailureMode
            -CAUSED_BY-> Cause -INVOLVES_PARAMETER-> Parameter

Parameter까지 이어지지 않는 경로는 가설로 치지 않는다. fab SQL로 검증할 수 없기 때문이다.
"""

import sys

from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from langchain_neo4j import Neo4jGraph   # 타입 힌트용

import kg_common as kg

# Windows 콘솔(cp949)에서 em-dash 등 유니코드 출력 시 크래시 방지
sys.stdout.reconfigure(encoding="utf-8")

OPENAI_MODEL = kg.OPENAI_MODEL
TOP_K = 3


# =========================
# 1. 가설 경로 조회 (결정적 Cypher)
# =========================

HYPOTHESIS_QUERY = """
MATCH (p:DefectPattern {id: $pattern})-[a:ARISES_IN]->(s:ProcessStep)
MATCH (fm:FailureMode)-[:OCCURS_IN]->(s)
MATCH (fm)-[cb:CAUSED_BY]->(c:Cause)
MATCH (c)-[ip:INVOLVES_PARAMETER]->(param:Parameter)
// 공정 정합성: 검증 변수는 그 공정에서 실제 계측되는 것이어야 한다.
// (병합된 FailureMode가 여러 공정에 걸쳐 타 공정 변수로 새는 것을 차단 — STATUS P3 방어)
WHERE s.id IN param.steps
RETURN s.id            AS step,
       fm.id           AS failure_mode,
       fm.name         AS failure_mode_name,
       c.id            AS cause,
       c.name          AS cause_name,
       c.description   AS cause_description,
       param.id        AS parameter,
       ip.direction    AS direction,
       a.occurrence_prior AS occurrence_prior,
       (coalesce(a.extraction_confidence, 3)
        + coalesce(cb.extraction_confidence, 3)
        + coalesce(ip.extraction_confidence, 3)) / 3.0 AS confidence,
       cb.quotes       AS quotes,
       a.chunk_ids     AS pattern_evidence
"""

PRIOR_RANK = {"high": 3, "mid": 2, "low": 1}


def fetch_hypotheses(graph: Neo4jGraph, pattern: str) -> list[dict]:
    rows = graph.query(HYPOTHESIS_QUERY, params={"pattern": pattern})

    # 같은 (원인, 검증변수) 쌍이 여러 공정 경로로 중복될 수 있다. 가장 강한 것만 남긴다.
    best: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row["cause"], row["parameter"])
        prior = PRIOR_RANK.get(row["occurrence_prior"], 1)
        row["_score"] = (prior, row["confidence"])
        if key not in best or row["_score"] > best[key]["_score"]:
            best[key] = row

    return sorted(best.values(), key=lambda r: r["_score"], reverse=True)[:TOP_K]


# =========================
# 1b. 문헌 기반 후보 원인 (논문 표에서 온 DefectPattern -ATTRIBUTED_TO-> Cause)
# -------------------------
# fab 검증 백본(ARISES_IN 완전경로)과 달리, 논문은 패턴의 원인을 직접 준다.
# 이 경로는 공정/고장모드를 거치지 않으므로 별도로 뽑아 '문헌 기반 후보'로 보고한다.
# Parameter까지 이어지면 fab 검증도 가능하다(OPTIONAL).
# =========================

# 문헌 원인(ATTRIBUTED_TO)은 5단계의 원인 표준화 덕에 백본 원인과 **같은 노드**로 합쳐져 있다.
# 따라서 그 Cause 가 INVOLVES_PARAMETER(검증변수)를 가지면 바로 딸려 나온다(별도 우회 불필요).
LITERATURE_QUERY = """
MATCH (p:DefectPattern {id: $pattern})-[a:ATTRIBUTED_TO]->(c:Cause)
OPTIONAL MATCH (c)-[ip:INVOLVES_PARAMETER]->(param:Parameter)
RETURN c.id                       AS cause,
       c.name                     AS cause_name,
       c.description              AS cause_description,
       a.source                   AS source,
       coalesce(a.extraction_confidence, 3) AS confidence,
       param.id                   AS parameter,
       ip.direction               AS direction
ORDER BY confidence DESC
"""


def fetch_literature_causes(graph: Neo4jGraph, pattern: str, limit: int = 6) -> list[dict]:
    rows = graph.query(LITERATURE_QUERY, params={"pattern": pattern})
    best: dict[str, dict] = {}
    for row in rows:
        cur = best.get(row["cause"])
        if cur is None or (row["parameter"] and not cur["parameter"]):
            best[row["cause"]] = row
    return list(best.values())[:limit]


# =========================
# 2. 가설 문장 생성 (LLM은 여기서만 쓴다)
# =========================

class Hypotheses(BaseModel):
    hypotheses: list[str] = Field(
        description="가설 문장 리스트. 입력으로 준 경로 순서를 그대로 유지한다. 번호는 붙이지 않는다."
    )


SYNTHESIS_PROMPT = """
반도체 웨이퍼 결함 근본원인 분석(RCA) 결과를 보고합니다.

관측된 불량 패턴: {pattern}

지식그래프에서 아래 {n}개의 인과 경로를 찾았습니다.
각 경로를 한국어 가설 문장 하나로 옮기세요.

경로:
{paths}

작성 규칙:
- 경로 하나당 문장 하나. 입력 순서를 그대로 유지하세요.
- 주어진 사실만 쓰고 새로운 원인이나 변수를 지어내지 마세요.
- 각 문장에 공정, 고장 모드, 근본 원인, 검증할 변수를 모두 담으세요.
- "{pattern} 패턴은 ... 로 보이며, ...를 확인해야 합니다" 같은 가설 어투로 쓰세요.
- direction이 high면 "값이 높은지", low면 "값이 낮은지"를 확인하라고 쓰세요.
- 문장 앞에 번호를 붙이지 마세요.
"""


def describe_path(row: dict) -> str:
    direction = {"high": "높음", "low": "낮음"}.get(row["direction"], "이상 여부")
    return (
        f"- 공정: {row['step']}\n"
        f"  고장 모드: {row['failure_mode_name']} ({row['failure_mode']})\n"
        f"  근본 원인: {row['cause_name']} ({row['cause']})\n"
        f"  원인 설명: {row['cause_description']}\n"
        f"  검증 변수: {row['parameter']} (예상 방향: {direction})\n"
        f"  패턴→공정 근거 강도: {row['occurrence_prior']}, 평균 추출 신뢰도: {row['confidence']:.1f}"
    )


def synthesize(llm, pattern: str, rows: list[dict]) -> list[str]:
    paths = "\n".join(describe_path(r) for r in rows)
    prompt = SYNTHESIS_PROMPT.format(pattern=pattern, n=len(rows), paths=paths)
    result = llm.with_structured_output(Hypotheses, method="json_schema").invoke(prompt)
    return result.hypotheses


# =========================
# 3. 실행
# =========================

def load_pattern_ids() -> list[str]:
    return [n["id"] for n in kg.load_seed_nodes("defect_patterns.json")]


def main() -> None:
    graph = kg.get_graph()
    llm = ChatOpenAI(model=OPENAI_MODEL, temperature=0)

    for pattern in load_pattern_ids():
        print("=" * 80)
        print(f"질문: {pattern} 결함 패턴이 나타나는 근본 원인은 무엇인가요?")
        print()

        rows = fetch_hypotheses(graph, pattern)

        if not rows:
            print("fab 검증 가설 없음. 그래프에 이 패턴의 완전한 경로")
            print("(DefectPattern→ProcessStep→FailureMode→Cause→Parameter)가 없습니다.")
            print()
        else:
            if len(rows) < TOP_K:
                print(f"(경고: 완전한 경로가 {len(rows)}개뿐이라 가설 {len(rows)}건만 냅니다)")
                print()

            for i, (sentence, row) in enumerate(zip(synthesize(llm, pattern, rows), rows), start=1):
                print(f"{i}. {sentence}")
                print(
                    f"   근거: {pattern} -[ARISES_IN]-> {row['step']}"
                    f" <-[OCCURS_IN]- {row['failure_mode']}"
                    f" -[CAUSED_BY]-> {row['cause']}"
                    f" -[INVOLVES_PARAMETER]-> {row['parameter']}"
                )
                print(f"   검증: telemetry.param = '{row['parameter']}' (방향 {row['direction']})")
                print()

        # 문헌 기반 후보 원인 (논문 표에서 온 것. fab 검증 백본과 구분해서 보고)
        lit = fetch_literature_causes(graph, pattern)
        if lit:
            print(f"[문헌 기반 후보 원인] (논문 표 근거, {len(lit)}건)")
            for row in lit:
                verify = (
                    f"telemetry.param='{row['parameter']}' (방향 {row['direction']})"
                    if row["parameter"] else "연결된 검증 변수 없음(정성적 단서)"
                )
                print(f"   · {row['cause_name']} ({row['cause']})")
                print(f"     {pattern} -[ATTRIBUTED_TO/{row['source']}]-> {row['cause']} | 검증: {verify}")
            print()


if __name__ == "__main__":
    main()
