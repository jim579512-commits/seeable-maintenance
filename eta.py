"""預估完工時間（ETA）模型。

示範用：以「模擬的服務廠工時資料」訓練隨機森林。正式導入時，把 simulate() 換成服務廠實際的
工單工時紀錄（每個階段的開始／結束時間）即可，特徵與預測流程不用改。
沒有 scikit-learn 時自動改用同一套規則直接估算。
"""
import random

try:
    import numpy as np
    from sklearn.ensemble import RandomForestRegressor

    HAVE_SK = True
except Exception:  # noqa: BLE001  沒裝 sklearn 也要能跑
    HAVE_SK = False

FEATURES = ["標準工時", "追加工時", "廠內在修台數", "時段", "目前階段", "本階段已經過分鐘", "是否油電"]
WASH_MIN = 15


PARALLEL = 0.7  # 定期保養通常兩位技師同時作業，實際經過時間約為標準工時的七成


def stage_durations(std_min, addon_min, load, hour):
    """各階段的「真實」耗時（分鐘）：0 接車等候、1 檢查、2 作業、3 品檢、4 洗車。"""
    work = (std_min + addon_min) * PARALLEL
    queue = 5 + load * 2.5 + (8 if 9 <= hour <= 11 else 0)
    return [queue, 0.15 * work, 0.65 * work, 0.10 * work + 5, WASH_MIN]


def rule_remaining(std_min, addon_min, load, hour, stage, elapsed, is_hybrid):
    if stage >= 5:
        return 0.0
    d = stage_durations(std_min, addon_min, load, hour)
    if is_hybrid:
        d[2] += 5
    rest = max(0.0, d[stage] - elapsed) + sum(d[stage + 1:5])
    return rest


def simulate(n=4000, seed=42):
    rng = random.Random(seed)
    X, y = [], []
    for _ in range(n):
        std_min = rng.uniform(40, 160)
        addon_min = rng.choice([0, 0, 0, 10, 20, 30, 40, 60])
        load = rng.randint(1, 12)
        hour = rng.randint(8, 17)
        stage = rng.randint(0, 4)
        is_hybrid = rng.random() < 0.5
        d = stage_durations(std_min, addon_min, load, hour)
        elapsed = rng.uniform(0, d[stage] * 1.3)
        true_rest = rule_remaining(std_min, addon_min, load, hour, stage, elapsed, is_hybrid)
        # 真實世界的雜訊：等零件、技師被叫走、洗車排隊……
        true_rest = max(0.0, true_rest * rng.uniform(0.85, 1.2) + rng.gauss(0, 5))
        X.append([std_min, addon_min, load, hour, stage, elapsed, 1 if is_hybrid else 0])
        y.append(true_rest)
    return X, y


class EtaModel:
    def __init__(self):
        self.model = None
        self.mae = None
        self.n_train = 0
        if HAVE_SK:
            try:
                self._train()
            except Exception:  # noqa: BLE001
                self.model = None

    def _train(self):
        X, y = simulate()
        split = int(len(X) * 0.8)
        model = RandomForestRegressor(n_estimators=120, max_depth=12, random_state=42, n_jobs=1)
        model.fit(np.array(X[:split]), np.array(y[:split]))
        pred = model.predict(np.array(X[split:]))
        self.mae = float(np.mean(np.abs(pred - np.array(y[split:]))))
        self.model = model
        self.n_train = split

    def info(self):
        if self.model is not None:
            return {
                "name": "隨機森林（模擬資料訓練）",
                "mae": round(self.mae, 1),
                "n_train": self.n_train,
                "features": FEATURES,
            }
        return {"name": "規則估算（未安裝 scikit-learn）", "mae": None, "n_train": 0, "features": FEATURES}

    def remaining(self, std_min, addon_min, load, hour, stage, elapsed, is_hybrid):
        if stage >= 5:
            return 0.0
        if self.model is not None:
            val = float(self.model.predict(np.array([[std_min, addon_min, load, hour, stage, elapsed, 1 if is_hybrid else 0]]))[0])
        else:
            val = rule_remaining(std_min, addon_min, load, hour, stage, elapsed, is_hybrid)
        return max(5.0, val)

    def remaining_many(self, rows):
        """一次預估多筆（線上預約比較各廠時段時用，比逐筆快很多）。rows＝[(標準工時, 追加工時, 在修台數, 時段, 階段, 已經過, 是否油電), ...]"""
        if self.model is None or not rows:
            return [self.remaining(*r) for r in rows]
        X = np.array([[r[0], r[1], r[2], r[3], r[4], r[5], 1 if r[6] else 0] for r in rows])
        return [0.0 if r[4] >= 5 else max(5.0, float(v)) for v, r in zip(self.model.predict(X), rows)]
