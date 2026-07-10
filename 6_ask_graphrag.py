"""
관측된 불량 패턴 → 근본 원인 가설 3건.

질문이 "{패턴} 결함 패턴이 나타나는 근본 원인은 무엇인가요?" 하나로 고정이므로
Cypher를 LLM에게 생성시키지 않는다(구 버전은 6_ask_graphrag_backup.py).
그래프 순회는 결정적으로 하고, LLM은 뽑아온 사실을 가설 문장으로 옮기기만 한다.

가설 1건 = DefectPattern -ARISES_IN-> ProcessStep <-OCCURS_IN- FailureMode
            -CAUSED_BY-> Cause -INVOLVES_PARAMETER-> Parameter

Parameter까지 이어지지 않는 경로는 가설로 치지 않는다. fab SQL로 검증할 수 없기 때문이다.
"""

import os
import sys
import json
import collections
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Windows 콘솔(cp949)에서 em-dash 등 유니코드 출력 시 크래시 방지
sys.stdout.reconfigure(encoding="utf-8")

from langchain_openai import ChatOpenAI
from langchain_neo4j import Neo4jGraph

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
SEEDS_DIR = BASE_DIR / "data" / "seeds"

NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5")

# 기본값은 "탐색된 모든 가설을 다 낸다".
# 굳이 잘라 보고 싶으면 환경변수로: TOP_K=3 python 6_ask_graphrag.py
_top_k = os.getenv("TOP_K")
TOP_K = int(_top_k) if _top_k else None

# 문장 합성을 한 번에 다 넘기면 프롬프트가 터진다. 배치로 나눠 부른다.
SYNTHESIS_BATCH = 12


# =========================
# 1. 가설 경로 조회 (결정적 Cypher)
# =========================

# evidence 3종을 :Evidence 슈퍼라벨로 한 번에 잡는다.
# Parameter 만 telemetry 조인으로 자동 판정되고, Maintenance/Recipe 는 조회만 자동이다(반자동).
HYPOTHESIS_QUERY = """
MATCH (p:DefectPattern {id: $pattern})-[a:ARISES_IN]->(s:ProcessStep)
MATCH (fm:FailureMode)-[:OCCURS_IN]->(s)
MATCH (fm)-[cb:CAUSED_BY]->(c:Cause)
MATCH (c)-[vb:VERIFIED_BY]->(e:Evidence)
RETURN s.id            AS step,
       fm.id           AS failure_mode,
       fm.name         AS failure_mode_name,
       c.id            AS cause,
       c.name          AS cause_name,
       c.description   AS cause_description,
       e.id            AS evidence,
       e.name          AS evidence_name,
       CASE
         WHEN e:Parameter   THEN 'Parameter'
         WHEN e:Maintenance THEN 'Maintenance'
         WHEN e:Recipe      THEN 'Recipe'
         ELSE 'Evidence'
       END             AS evidence_label,
       e.fab_table     AS fab_table,
       vb.direction    AS direction,
       a.occurrence_prior AS occurrence_prior,
       (coalesce(a.extraction_confidence, 3)
        + coalesce(cb.extraction_confidence, 3)
        + coalesce(vb.extraction_confidence, 3)) / 3.0 AS confidence,
       cb.quotes       AS quotes,
       cb.chunk_ids    AS chunk_ids
"""

# 문헌이 공정을 거치지 않고 패턴 -> 원인을 바로 말한 경우(ref56 Table 1).
# 공정을 모르므로 Parameter 자동 검증에 닿지 못한다. evidence가 없을 수도 있다.
DIRECT_QUERY = """
MATCH (p:DefectPattern {id: $pattern})-[at:ATTRIBUTED_TO]->(c:Cause)
OPTIONAL MATCH (c)-[vb:VERIFIED_BY]->(e:Evidence)
RETURN 'direct'        AS route,
       NULL            AS step,
       NULL            AS failure_mode,
       '(문헌 직결)'    AS failure_mode_name,
       c.id            AS cause,
       c.name          AS cause_name,
       c.description   AS cause_description,
       coalesce(e.id, '근거없음')    AS evidence,
       coalesce(e.name, '문헌 서술') AS evidence_name,
       CASE
         WHEN e:Parameter   THEN 'Parameter'
         WHEN e:Maintenance THEN 'Maintenance'
         WHEN e:Recipe      THEN 'Recipe'
         ELSE 'None'
       END             AS evidence_label,
       coalesce(e.fab_table, '-') AS fab_table,
       vb.direction    AS direction,
       NULL            AS occurrence_prior,
       coalesce(at.extraction_confidence, 3) AS confidence,
       at.quotes       AS quotes,
       at.chunk_ids    AS chunk_ids
"""

PRIOR_RANK = {"high": 3, "mid": 2, "low": 1}


# =========================
# 1.1 검증 등급 (verification tier)
# -------------------------
# 가르는 축은 "fab.db에 데이터가 있느냐"가 아니다. 셋 다 fab 테이블에 붙어 있다.
# 진짜 축은 **에이전트가 스스로 채택/기각을 판정할 수 있느냐**다.
#
#   자동   Parameter   : Parameter.id == telemetry.param 이라 결정적으로 조인된다.
#                        fab_model.yaml의 정상범위와 비교해 기계적으로 판정한다.
#                        -> hypothesis agent가 쿼리 작성부터 결론까지 끝낸다.
#
#   반자동 Maintenance : maintenance 테이블을 조회할 수는 있다. 그러나 Maintenance.id는
#                        조인 키가 아니라 필터 힌트이고(parts 컬럼이 자유 텍스트),
#                        어느 행이 그 정비인지·지연됐는지는 규칙으로 못 정한다.
#          Recipe      : lot_history.recipe_id로 실제 레시피는 읽는다.
#                        그러나 **기대값이 KG에 없어** 비교 대상이 없다.
#                        -> agent가 근거 데이터를 뽑아 오고, 판정은 사람이 한다.
#
#   근거없음           : evidence 노드 자체가 없다. ATTRIBUTED_TO로만 붙은 Cause가 여기다.
#                        (예: surface_damage_by_humans, RTP 관련 원인 — fab 6스텝 밖)
#                        -> fab 데이터로 손댈 수 없다. 문헌 서술로만 남는다.
#
# 순위에서 자동 > 반자동 > 근거없음 순으로 올린다.
# =========================

TIER_AUTO, TIER_SEMI, TIER_NONE = 2, 1, 0

TIER_OF_LABEL = {
    "Parameter": TIER_AUTO,
    "Maintenance": TIER_SEMI,
    "Recipe": TIER_SEMI,
}

TIER_TAG = {TIER_AUTO: "자동", TIER_SEMI: "반자동", TIER_NONE: "근거없음"}

LEGEND = """검증 등급 — 'fab.db에 있느냐'가 아니라 '에이전트가 스스로 판정할 수 있느냐'로 가릅니다.
  [자동]     Parameter. telemetry.param과 결정적으로 조인되고 정상범위로 판정 가능. 에이전트가 결론까지 냅니다.
  [반자동]   Maintenance / Recipe. fab 테이블 조회는 되지만 조인 키나 기대값이 없어 판정은 사람 몫입니다.
  [근거없음] 검증 신호가 없는 문헌 서술. fab 데이터로 확인할 수 없습니다."""


def fetch_hypotheses(graph: Neo4jGraph, pattern: str) -> list[dict]:
    rows = graph.query(HYPOTHESIS_QUERY, params={"pattern": pattern})
    for row in rows:
        row["route"] = "step"
    rows += graph.query(DIRECT_QUERY, params={"pattern": pattern})

    # 완전히 같은 경로만 합친다. (원인, 검증신호)로만 묶으면 서로 다른 공정·고장 모드를 거친
    # 별개의 가설이 하나로 뭉개진다. 예: 같은 rf_power가 ETCH와 DEPO 양쪽에서 나올 수 있다.
    best: dict[tuple, dict] = {}
    for row in rows:
        row["tier"] = TIER_OF_LABEL.get(row["evidence_label"], TIER_NONE)
        key = (row["route"], row["step"], row["failure_mode"], row["cause"], row["evidence"])
        prior = PRIOR_RANK.get(row["occurrence_prior"], 1)
        row["_score"] = (row["tier"], prior, row["confidence"])
        if key not in best or row["_score"] > best[key]["_score"]:
            best[key] = row

    ranked = sorted(best.values(), key=lambda r: r["_score"], reverse=True)
    return ranked if TOP_K is None else ranked[:TOP_K]


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
- 주어진 사실만 쓰고 새로운 원인이나 검증 신호를 지어내지 마세요.
- 각 문장에 공정, 고장 모드, 근본 원인, 검증 방법을 모두 담으세요.
- "{pattern} 패턴은 ... 로 보이며, ...를 확인해야 합니다" 같은 가설 어투로 쓰세요.
- 검증 신호가 [자동]이면 direction이 high일 때 "값이 높은지", low면 "값이 낮은지" 확인하라고 쓰세요.
- 검증 신호가 [반자동] Maintenance면 "정비 이력을 확인해야 합니다",
  [반자동] Recipe면 "사용된 레시피를 확인해야 합니다"로 쓰세요.
- 검증 신호가 [근거없음]이면 "fab 데이터로는 확인할 수 없어 문헌 근거로만 남습니다"라고 덧붙이세요.
- 경로가 "문헌 직결"이면 공정을 언급하지 말고, 문헌이 이 패턴의 원인으로 지목했다고 쓰세요.
- 문장 앞에 번호를 붙이지 마세요.
"""


def describe_path(row: dict) -> str:
    label = row["evidence_label"]
    tag = TIER_TAG[row["tier"]]
    if label == "Parameter":
        direction = {"high": "높음", "low": "낮음"}.get(row["direction"], "이상 여부")
        verify = f"[{tag}] 계측 변수 {row['evidence']} (예상 방향: {direction})"
    elif label == "Maintenance":
        verify = f"[{tag}] 정비 이력 조회: {row['evidence_name']} — 판정은 사람이"
    elif label == "Recipe":
        verify = f"[{tag}] 레시피 조회: {row['evidence_name']} — 기대값이 없어 판정은 사람이"
    else:
        verify = f"[{tag}] 문헌 서술만 있음. fab 데이터로 확인 불가"

    if row["route"] == "direct":
        head = "- 경로: 문헌이 패턴에서 원인을 바로 지목 (공정 미상)\n"
    else:
        head = (
            f"- 공정: {row['step']}\n"
            f"  고장 모드: {row['failure_mode_name']} ({row['failure_mode']})\n"
        )

    return (
        f"{head}"
        f"  근본 원인: {row['cause_name']} ({row['cause']})\n"
        f"  원인 설명: {row['cause_description']}\n"
        f"  검증 신호: [{label}] {verify}\n"
        f"  평균 추출 신뢰도: {row['confidence']:.1f}"
    )


def _fallback_sentence(pattern: str, row: dict) -> str:
    """LLM이 문장을 덜 돌려줬을 때. 가설을 조용히 버리느니 사실만 이어 붙인다."""
    where = "문헌이 직접 지목" if row["route"] == "direct" else f"{row['step']} 공정"
    return (
        f"{pattern} 패턴은 {where}의 {row['cause_name']}이(가) 원인으로 보입니다. "
        f"검증 신호: {row['evidence']} [{TIER_TAG[row['tier']]}]"
    )


def synthesize(llm, pattern: str, rows: list[dict]) -> list[str]:
    """
    경로가 수십 개일 수 있으므로 배치로 나눠 부른다.
    LLM이 배치 크기와 다른 개수를 돌려줘도 가설이 유실되지 않도록 길이를 맞춘다.
    """
    structured = llm.with_structured_output(Hypotheses, method="json_schema")
    sentences: list[str] = []

    for start in range(0, len(rows), SYNTHESIS_BATCH):
        batch = rows[start:start + SYNTHESIS_BATCH]
        paths = "\n".join(describe_path(r) for r in batch)
        prompt = SYNTHESIS_PROMPT.format(pattern=pattern, n=len(batch), paths=paths)

        try:
            got = structured.invoke(prompt).hypotheses
        except Exception as exc:                      # noqa: BLE001
            print(f"  (경고: 문장 합성 실패, 사실만 출력합니다 — {type(exc).__name__})")
            got = []

        if len(got) != len(batch):
            print(f"  (경고: 문장 {len(got)}개 / 경로 {len(batch)}개 — 부족분은 자동 생성)")
        got = got[:len(batch)]
        got += [_fallback_sentence(pattern, r) for r in batch[len(got):]]
        sentences.extend(got)

    return sentences


# =========================
# 3. 실행
# =========================

def load_pattern_ids() -> list[str]:
    data = json.loads((SEEDS_DIR / "defect_patterns.json").read_text(encoding="utf-8"))
    return [n["id"] for n in data["nodes"]]


def main() -> None:
    graph = Neo4jGraph(
        url=NEO4J_URI,
        username=NEO4J_USERNAME,
        password=NEO4J_PASSWORD,
        database=NEO4J_DATABASE,
    )
    llm = ChatOpenAI(model=OPENAI_MODEL, temperature=0)

    print(LEGEND)
    print()

    report = {
        "meta": {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "model": OPENAI_MODEL,
            "neo4j_database": NEO4J_DATABASE,
            "top_k": TOP_K,
            "question_template": "{pattern} 결함 패턴이 나타나는 근본 원인은 무엇인가요?",
            "tier_legend": {
                "자동": "Parameter. telemetry.param과 결정적 조인 + 정상범위 판정. agent가 결론까지.",
                "반자동": "Maintenance/Recipe. fab 테이블 조회는 되지만 판정은 사람 몫.",
                "근거없음": "evidence 없는 문헌 서술. fab 데이터로 확인 불가.",
            },
            "score_note": "score = (tier, occurrence_prior, confidence) 내림차순. "
                          "tier 외 성분은 LLM 자기평가라 신뢰도 낮음 (STATUS.md P2).",
        },
        "questions": [],
    }

    for pattern in load_pattern_ids():
        print("=" * 80)
        print(f"질문: {pattern} 결함 패턴이 나타나는 근본 원인은 무엇인가요?")
        print()

        rows = fetch_hypotheses(graph, pattern)

        entry = {
            "pattern": pattern,
            "question": f"{pattern} 결함 패턴이 나타나는 근본 원인은 무엇인가요?",
            "counts": {},
            "hypotheses": [],
        }
        report["questions"].append(entry)

        if not rows:
            print("가설 없음. 그래프에 이 패턴의 경로가 없습니다.")
            print("(공정 경유: DefectPattern→ProcessStep→FailureMode→Cause→Evidence)")
            print("(문헌 직결: DefectPattern→Cause)")
            print()
            continue

        by_tier = collections.Counter(TIER_TAG[r["tier"]] for r in rows)
        by_route = collections.Counter(r["route"] for r in rows)
        entry["counts"] = {
            "total": len(rows),
            "by_tier": dict(by_tier),
            "by_route": dict(by_route),
        }
        summary = ", ".join(f"{TIER_TAG[t]} {by_tier[TIER_TAG[t]]}건"
                            for t in (TIER_AUTO, TIER_SEMI, TIER_NONE) if by_tier[TIER_TAG[t]])
        print(f"가설 {len(rows)}건 — {summary}")
        print(f"  (공정 경유 {by_route['step']}건, 문헌 직결 {by_route['direct']}건)")
        if TOP_K is not None:
            print(f"  (TOP_K={TOP_K} 환경변수가 설정돼 상위 {TOP_K}건만 출력합니다)")
        print()

        for i, (sentence, row) in enumerate(zip(synthesize(llm, pattern, rows), rows), start=1):
            print(f"{i}. {sentence}")

            if row["route"] == "direct":
                trail = f"{pattern} -[ATTRIBUTED_TO]-> {row['cause']}"
                if row["evidence_label"] != "None":
                    trail += f" -[VERIFIED_BY]-> ({row['evidence_label']}) {row['evidence']}"
            else:
                trail = (
                    f"{pattern} -[ARISES_IN]-> {row['step']}"
                    f" <-[OCCURS_IN]- {row['failure_mode']}"
                    f" -[CAUSED_BY]-> {row['cause']}"
                    f" -[VERIFIED_BY]-> ({row['evidence_label']}) {row['evidence']}"
                )
            print(f"   근거: {trail}")

            if row["tier"] == TIER_AUTO:
                print(
                    f"   검증: [자동] agent가 판정. {row['fab_table']}.param = '{row['evidence']}'"
                    f" 를 정상범위와 비교 (예상 이탈 방향: {row['direction']})"
                )
            elif row["tier"] == TIER_SEMI:
                print(
                    f"   검증: [반자동] agent가 {row['fab_table']} 테이블을 조회, 판정은 사람이"
                    f" — {row['evidence_name']}"
                )
            else:
                print("   검증: [근거없음] fab 데이터에 연결되지 않음. 문헌 서술로만 존재합니다")
            print()

            entry["hypotheses"].append({
                "rank": i,
                "sentence": sentence,
                "tier": TIER_TAG[row["tier"]],
                "route": row["route"],                     # step=공정 경유, direct=문헌 직결
                "path": {
                    "pattern": pattern,
                    "step": row["step"],
                    "failure_mode": row["failure_mode"],
                    "cause": row["cause"],
                    "evidence": None if row["tier"] == TIER_NONE else row["evidence"],
                    "evidence_label": row["evidence_label"],
                },
                "verification": {
                    "fab_table": None if row["fab_table"] == "-" else row["fab_table"],
                    "direction": row["direction"],
                },
                "score": {
                    "tier": row["tier"],
                    "occurrence_prior": row["occurrence_prior"],
                    "confidence": row["confidence"],
                },
                "detail": {
                    "failure_mode_name": row["failure_mode_name"],
                    "cause_name": row["cause_name"],
                    "cause_description": row["cause_description"],
                },
                "provenance": {
                    "chunk_ids": row.get("chunk_ids") or [],
                    "quotes": row.get("quotes") or [],
                },
            })

    out_path = BASE_DIR / "outputs" / "hypotheses.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    total = sum(len(q["hypotheses"]) for q in report["questions"])
    print("=" * 80)
    print(f"JSON 저장: {out_path}  (가설 {total}건)")


if __name__ == "__main__":
    main()
