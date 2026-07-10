"""
7단계 — 가설 검증 모듈 (팹 DB 조회로 '검증' 인터페이스를 실제로 실행).

6_ask_graphrag.py 는 각 가설 끝에
    "검증: telemetry.param = '{parameter}' (방향 {direction})"
라는 인터페이스만 찍고 끝났다. 이 스크립트가 그 검증을 실제로 돌려
정상/이상 + 가설 지지/기각 판정을 돌려준다.

핵심 조인 흐름:
  1) lot_history 에서 (lot_id, step) -> 그 lot 이 쓴 equipment_id, 처리 시간창(ts_in~ts_out)
  2) telemetry 에서 (equipment_id, param, ts_in<=ts<=ts_out) -> 이 lot 의 계측값들
     (telemetry 에는 lot_id 가 없으므로 장비+시간창으로 lot 을 격리한다 — fab.md 설계)
  3) fab_model.yaml 의 정상범위와 비교 -> in_range / 방향 일치 여부로 지지 판정

demo(main): Center↔LOT-0001, Edge-Ring↔LOT-0003, Scratch↔LOT-0006 에 대해
Neo4j 에서 가설 경로(step, parameter, direction)를 뽑고 각각 verify() 를 호출해
'가설 → 계측값 vs 정상범위 → 지지/기각'을 한 줄로 출력한다.
"""

import os
import sys
import sqlite3
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Windows 콘솔(cp949)에서 유니코드 출력 크래시 방지
sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "fab" / "fab.db"
MODEL_PATH = BASE_DIR / "data" / "fab" / "fab_model.yaml"

load_dotenv(dotenv_path=BASE_DIR / ".env")

NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE")

# 정상범위 로드 (그라운드 트루스). param id -> {min, max, unit}
RANGES: dict[str, dict] = yaml.safe_load(MODEL_PATH.read_text(encoding="utf-8"))["parameters"]

# 6_ask_graphrag.py 의 HYPOTHESIS_QUERY 와 동일한 경로 형태(검증에 필요한 필드만 축약).
HYPOTHESIS_QUERY = """
MATCH (p:DefectPattern {id: $pattern})-[a:ARISES_IN]->(s:ProcessStep)
MATCH (fm:FailureMode)-[:OCCURS_IN]->(s)
MATCH (fm)-[cb:CAUSED_BY]->(c:Cause)
MATCH (c)-[ip:INVOLVES_PARAMETER]->(param:Parameter)
// 공정 정합성: 검증 변수는 그 공정에서 계측되는 것이어야 한다 (타 공정 변수 누수 차단)
WHERE s.id IN param.steps
RETURN s.id         AS step,
       c.id         AS cause,
       param.id     AS parameter,
       ip.direction AS direction
"""

# demo 케이스: (패턴, 용의 lot)
DEMO_CASES = [
    ("Center",    "LOT-0001"),
    ("Edge-Ring", "LOT-0003"),
    ("Scratch",   "LOT-0006"),
]


# =========================================================================
# 1. 검증 코어
# =========================================================================
def verify(lot_id: str, step: str, param: str, direction: str) -> dict:
    """
    한 가설(lot_id, step, param, direction)을 팹 DB로 검증한다.

    반환 dict:
      measured           : {n, min, max, mean} — 시간창 안 계측 요약
      normal_range       : {min, max}
      in_range           : bool  (방향별 대표값이 정상범위 안인가)
      direction          : 'high'|'low'
      supports_hypothesis: bool  (대표값이 예측 '같은 방향'으로 범위 밖인가)
      verdict_text       : 사람이 읽는 한 줄 판정
    """
    rng = RANGES.get(param)
    if rng is None:
        return {
            "measured": None, "normal_range": None, "in_range": None,
            "direction": direction, "supports_hypothesis": False,
            "verdict_text": f"정상범위 미정의 (fab_model.yaml 에 '{param}' 없음)",
        }
    lo, hi = rng["min"], rng["max"]

    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        # 1) 이 lot 이 이 step 에서 쓴 장비 + 처리 시간창
        cur.execute(
            "SELECT equipment_id, ts_in, ts_out FROM lot_history WHERE lot_id=? AND step=?",
            (lot_id, step),
        )
        hist = cur.fetchone()
        if hist is None:
            return {
                "measured": None, "normal_range": {"min": lo, "max": hi},
                "in_range": None, "direction": direction, "supports_hypothesis": False,
                "verdict_text": f"검증 불가: lot_history 에 {lot_id}/{step} 이력 없음",
            }
        equipment_id, ts_in, ts_out = hist

        # 2) 장비+파라미터+시간창으로 이 lot 의 계측값 격리
        cur.execute(
            """SELECT value FROM telemetry
               WHERE equipment_id=? AND param=? AND ts BETWEEN ? AND ?""",
            (equipment_id, param, ts_in, ts_out),
        )
        values = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

    if not values:
        return {
            "measured": None, "normal_range": {"min": lo, "max": hi},
            "in_range": None, "direction": direction, "supports_hypothesis": False,
            "verdict_text": f"검증 불가: {equipment_id}/{param} 텔레메트리 없음",
        }

    m_min, m_max = min(values), max(values)
    m_mean = sum(values) / len(values)
    measured = {"n": len(values), "min": round(m_min, 3),
                "max": round(m_max, 3), "mean": round(m_mean, 3)}

    # 3) 방향별 대표값으로 판정: high -> 최댓값, low -> 최솟값
    if direction == "high":
        rep = m_max
        out_same_dir = rep > hi
    elif direction == "low":
        rep = m_min
        out_same_dir = rep < lo
    else:
        rep = m_mean
        out_same_dir = (rep > hi) or (rep < lo)

    in_range = (lo <= rep <= hi)
    supports = bool(out_same_dir)

    dir_kr = {"high": "높음", "low": "낮음"}.get(direction, "이상")
    status = "이상" if not in_range else "정상"
    judge = "가설 지지" if supports else "가설 기각"
    verdict_text = (
        f"{status} / {judge} "
        f"(대표값 {round(rep, 3)} {dir_kr} vs 정상 {lo}~{hi}, "
        f"n={len(values)}, mean={round(m_mean, 3)})"
    )

    return {
        "measured": measured,
        "normal_range": {"min": lo, "max": hi},
        "in_range": in_range,
        "direction": direction,
        "supports_hypothesis": supports,
        "verdict_text": verdict_text,
    }


# =========================================================================
# 2. 가설 경로 조회 (Neo4j). 실패 시 하드코딩 폴백으로 데모는 계속 돈다.
# =========================================================================
FALLBACK_HYPOTHESES = {
    "Center":    [("DEPO", "gas_flow", "high"), ("DEPO", "susceptor_temp", "high"),
                  ("DEPO", "chamber_pressure", "high")],
    "Edge-Ring": [("ETCH", "etch_rate", "high"), ("ETCH", "rf_power", "high"),
                  ("ETCH", "chamber_pressure", "high")],
    "Scratch":   [("CMP", "down_force", "high"), ("CMP", "slurry_flow", "low"),
                  ("CMP", "megasonic_power", "high")],
}


def fetch_hypothesis_rows(graph, pattern: str) -> list[tuple[str, str, str]]:
    """(step, parameter, direction) 튜플 목록. 중복 제거, 입력 순서 보존."""
    rows = graph.query(HYPOTHESIS_QUERY, params={"pattern": pattern})
    seen, out = set(), []
    for r in rows:
        key = (r["step"], r["parameter"], r["direction"])
        if key in seen or r["parameter"] is None:
            continue
        seen.add(key)
        out.append(key)
    return out


# =========================================================================
# 3. 데모
# =========================================================================
def main() -> None:
    if not DB_PATH.exists():
        print(f"[!] {DB_PATH} 가 없습니다. 먼저 data/fab/generate_fab.py 를 실행하세요.")
        return

    # Neo4j 연결 시도 (실패해도 폴백 가설로 데모 진행)
    graph = None
    try:
        from langchain_neo4j import Neo4jGraph
        graph = Neo4jGraph(url=NEO4J_URI, username=NEO4J_USERNAME,
                           password=NEO4J_PASSWORD, database=NEO4J_DATABASE)
        graph.query("RETURN 1")
        print("[i] Neo4j 연결 성공 — 그래프에서 가설 경로를 조회합니다.\n")
    except Exception as e:
        print(f"[i] Neo4j 미연결({type(e).__name__}) — 하드코딩 폴백 가설로 데모를 진행합니다.\n")
        graph = None

    for pattern, lot in DEMO_CASES:
        print("=" * 88)
        print(f"관측 패턴: {pattern}  |  용의 lot: {lot}")
        print(f"질문: {pattern} 결함의 근본 원인 가설을 팹 텔레메트리로 검증한다.")
        print("-" * 88)

        if graph is not None:
            rows = fetch_hypothesis_rows(graph, pattern)
        else:
            rows = FALLBACK_HYPOTHESES.get(pattern, [])

        # 검증 대상이 이 용의 lot 이 실제로 통과한 step 만 남긴다(다른 step 은 스킵).
        rows = [r for r in rows if lot_passed_step(lot, r[0])]

        for step, param, direction in rows:
            res = verify(lot, step, param, direction)
            mark = "◎ 지지" if res["supports_hypothesis"] else "· 기각"
            print(
                f"[{mark}] {pattern} -> {step} -> {param} (예상 {direction})"
            )
            print(
                f"        검증: telemetry.param='{param}' | {res['verdict_text']}"
            )
        print()


def lot_passed_step(lot_id: str, step: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM lot_history WHERE lot_id=? AND step=? LIMIT 1", (lot_id, step)
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


if __name__ == "__main__":
    main()
