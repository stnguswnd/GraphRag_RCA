"""
data/fab/fab.db (SQLite) 생성기 — 팹 목업 데이터.

fab.md 스키마(lot_history / telemetry / alarm / maintenance)를 그대로 만들고,
data/fab/fab_model.yaml 의 정상범위를 기준으로 텔레메트리를 샘플링한다.
정상범위 자체는 telemetry 에 저장하지 않는다(fab.md 규칙). 값만 넣는다.

KG 가설을 실제로 검증할 수 있도록 아래 4개 이상치를 '심어둔다':
  - LOT-0001 / DEPO / gas_flow    = HIGH  (정상 max 초과)  -> Center 가설
  - LOT-0003 / ETCH / etch_rate   = HIGH  (정상 max 초과)  -> Edge-Ring 가설
  - LOT-0006 / CMP  / down_force  = HIGH  (정상 max 초과)  -> Scratch 가설
  - LOT-0006 / CMP  / slurry_flow = LOW   (정상 min 미만)  -> Scratch(저방향) 가설

telemetry 에는 lot_id 컬럼이 없으므로(fab.md), 특정 lot 의 계측은
'그 lot 이 그 step 에서 쓴 장비 + ts_in~ts_out 시간창'으로 격리된다.
따라서 텔레메트리 ts 를 각 lot×step 처리 시간창 안에 찍는다.

재현성을 위해 random.seed(42).
"""

import sys
import random
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta

import yaml

# Windows 콘솔(cp949)에서 유니코드 출력 크래시 방지
sys.stdout.reconfigure(encoding="utf-8")

random.seed(42)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "fab.db"
MODEL_PATH = BASE_DIR / "fab_model.yaml"

# =========================================================================
# 0. 정상범위 로드 (그라운드 트루스)
# =========================================================================
RANGES: dict[str, dict] = yaml.safe_load(MODEL_PATH.read_text(encoding="utf-8"))["parameters"]

# 공정 스텝(장비군)별 파라미터 집합 — fab.md Quick Reference 그대로.
STEP_PARAMS: dict[str, list[str]] = {
    "LITHO": ["exposure_dose", "focus_offset", "stage_temp", "alignment_offset"],
    "ETCH":  ["rf_power", "chamber_pressure", "he_flow", "temperature", "etch_rate"],
    "DEPO":  ["rf_power", "chamber_pressure", "gas_flow", "susceptor_temp", "deposition_rate"],
    "CMP":   ["down_force", "slurry_flow"],
    "CLEAN": ["flow_rate", "megasonic_power", "chemical_temp", "rinse_time"],
    "EDS":   ["chuck_temp", "contact_resistance"],
}
STEP_ORDER = ["LITHO", "ETCH", "DEPO", "CMP", "CLEAN", "EDS"]

# 스텝별 장비 인스턴스 (라운드로빈 배정용).
STEP_EQUIPMENT: dict[str, list[str]] = {
    "LITHO": ["LITHO-01"],
    "ETCH":  ["ETCH-01", "ETCH-02", "ETCH-03"],
    "DEPO":  ["DEPO-01", "DEPO-02"],
    "CMP":   ["CMP-01", "CMP-02"],
    "CLEAN": ["CLEAN-01"],
    "EDS":   ["EDS-01"],
}
RECIPE = {s: f"RCP-{s}-1" for s in STEP_ORDER}

# 심어둘 이상치: (lot_id, step, param) -> 방향('high'|'low')
PLANTED = {
    ("LOT-0001", "DEPO", "gas_flow"):    "high",
    ("LOT-0003", "ETCH", "etch_rate"):   "high",
    ("LOT-0006", "CMP",  "down_force"):  "high",
    ("LOT-0006", "CMP",  "slurry_flow"): "low",
}

N_LOTS = 10
ROWS_PER_PARAM = 5          # lot×step×param 당 텔레메트리 행 수
TELEMETRY_INTERVAL_H = 2    # fab.md: 2시간 간격
BASE_TS = datetime(2026, 6, 1, 8, 0, 0)


def fmt(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def sample_normal(param: str) -> float:
    """정상범위 안쪽(양끝 15% 마진 제외)에서 값 샘플링 -> 확실히 in-range."""
    lo, hi = RANGES[param]["min"], RANGES[param]["max"]
    span = hi - lo
    return round(random.uniform(lo + 0.15 * span, hi - 0.15 * span), 3)


def sample_anomaly(param: str, direction: str) -> float:
    """정상범위 밖으로 확실히 벗어난 값 (span 기반이라 음수 범위 파라미터도 안전)."""
    lo, hi = RANGES[param]["min"], RANGES[param]["max"]
    span = hi - lo
    if direction == "high":
        return round(random.uniform(hi + 0.20 * span, hi + 0.50 * span), 3)
    else:  # low
        return round(random.uniform(lo - 0.50 * span, lo - 0.20 * span), 3)


# =========================================================================
# 1. 스키마 생성
# =========================================================================
def create_schema(cur: sqlite3.Cursor) -> None:
    cur.executescript(
        """
        DROP TABLE IF EXISTS lot_history;
        DROP TABLE IF EXISTS telemetry;
        DROP TABLE IF EXISTS alarm;
        DROP TABLE IF EXISTS maintenance;

        CREATE TABLE lot_history (
            lot_id       TEXT,
            step         TEXT,
            equipment_id TEXT,
            chamber      TEXT,
            recipe_id    TEXT,
            ts_in        TEXT,
            ts_out       TEXT
        );
        CREATE TABLE telemetry (
            equipment_id TEXT,
            ts           TEXT,
            param        TEXT,   -- parameters.json 의 20개 id 와 정확히 일치해야 함
            value        REAL
        );
        CREATE TABLE alarm (
            equipment_id TEXT,
            lot_id       TEXT,   -- 교란 알람은 NULL
            ts           TEXT,
            alarm_id     INTEGER,
            text         TEXT
        );
        CREATE TABLE maintenance (
            equipment_id TEXT,
            ts           TEXT,
            type         TEXT,   -- PM(정기) / BM(돌발)
            parts        TEXT
        );
        """
    )


# =========================================================================
# 2. 데이터 생성
# =========================================================================
def build(cur: sqlite3.Cursor) -> dict[str, int]:
    lot_rows, tele_rows, alarm_rows, maint_rows = [], [], [], []

    for li in range(1, N_LOTS + 1):
        lot_id = f"LOT-{li:04d}"
        # lot 하나는 하루 안에 6스텝을 순서대로 통과. lot 마다 하루씩 밀어 시간창을 겹치지 않게.
        step_start = BASE_TS + timedelta(days=li - 1)

        for si, step in enumerate(STEP_ORDER):
            equipment_id = STEP_EQUIPMENT[step][(li - 1) % len(STEP_EQUIPMENT[step])]
            chamber = f"{equipment_id}-CH1"
            ts_in = step_start + timedelta(hours=si * 3)
            # 텔레메트리를 담을 만큼의 처리 시간창 확보
            ts_out = ts_in + timedelta(hours=(ROWS_PER_PARAM - 1) * TELEMETRY_INTERVAL_H + 1)

            lot_rows.append((lot_id, step, equipment_id, chamber, RECIPE[step], fmt(ts_in), fmt(ts_out)))

            # 이 lot×step 의 각 파라미터 텔레메트리 (ts_in~ts_out 시간창 안에 찍음)
            for param in STEP_PARAMS[step]:
                direction = PLANTED.get((lot_id, step, param))
                for k in range(ROWS_PER_PARAM):
                    ts = ts_in + timedelta(hours=k * TELEMETRY_INTERVAL_H)
                    if direction:
                        value = sample_anomaly(param, direction)
                    else:
                        value = sample_normal(param)
                    tele_rows.append((equipment_id, fmt(ts), param, value))

    # ---- alarm: 교란(1000번대) 몇 개 + 심어둔 이상치 지지(3000번대) 몇 개 ----
    alarm_rows.append(("ETCH-02", None, fmt(BASE_TS + timedelta(days=1, hours=4)), 1001, "chamber door interlock (nuisance)"))
    alarm_rows.append(("CLEAN-01", None, fmt(BASE_TS + timedelta(days=2, hours=6)), 1007, "DI water level low (nuisance)"))
    alarm_rows.append(("DEPO-01", "LOT-0001", fmt(BASE_TS + timedelta(days=0, hours=7)), 3002, "gas_flow high excursion"))
    alarm_rows.append(("ETCH-03", "LOT-0003", fmt(BASE_TS + timedelta(days=2, hours=4)), 3005, "etch_rate over upper spec"))
    alarm_rows.append(("CMP-01", "LOT-0006", fmt(BASE_TS + timedelta(days=5, hours=10)), 3011, "down_force high / slurry_flow low"))

    # ---- maintenance: PM/BM 몇 개 ----
    maint_rows.append(("ETCH-03", fmt(BASE_TS - timedelta(days=2)), "PM", "electrode / focus ring replace"))
    maint_rows.append(("DEPO-01", fmt(BASE_TS - timedelta(days=1)), "PM", "MFC calibration"))
    maint_rows.append(("CMP-01", fmt(BASE_TS + timedelta(days=6)), "BM", "slurry line clog clear"))
    maint_rows.append(("CLEAN-01", fmt(BASE_TS - timedelta(days=3)), "PM", "megasonic transducer check"))

    cur.executemany("INSERT INTO lot_history VALUES (?,?,?,?,?,?,?)", lot_rows)
    cur.executemany("INSERT INTO telemetry   VALUES (?,?,?,?)", tele_rows)
    cur.executemany("INSERT INTO alarm       VALUES (?,?,?,?,?)", alarm_rows)
    cur.executemany("INSERT INTO maintenance VALUES (?,?,?,?)", maint_rows)

    return {
        "lot_history": len(lot_rows),
        "telemetry": len(tele_rows),
        "alarm": len(alarm_rows),
        "maintenance": len(maint_rows),
    }


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        create_schema(cur)
        counts = build(cur)
        conn.commit()
    finally:
        conn.close()

    print(f"[OK] fab.db 생성 완료 -> {DB_PATH}")
    print("테이블별 행 수:")
    for t, n in counts.items():
        print(f"  - {t:12s}: {n}")
    print("\n심어둔 이상치:")
    for (lot, step, param), d in PLANTED.items():
        lo, hi = RANGES[param]["min"], RANGES[param]["max"]
        print(f"  - {lot} / {step:5s} / {param:14s} = {d.upper():4s} (정상 {lo}~{hi})")


if __name__ == "__main__":
    main()
