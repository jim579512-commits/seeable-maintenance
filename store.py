"""簡單的 JSON 檔資料儲存（展示用）。所有修改都在鎖內完成並立刻寫檔。"""
import copy
import json
import os
import threading


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.state = None

    def load_or_seed(self, seed_fn, schema=1):
        """讀取既有資料；資料格式版本不同（程式改版）時，自動重建示範資料。"""
        with self.lock:
            if os.path.exists(self.path):
                try:
                    with open(self.path, encoding="utf-8") as f:
                        self.state = json.load(f)
                    if self.state.get("schema") == schema:
                        return False
                except (OSError, ValueError):
                    pass
            self.state = seed_fn()
            self.save()
            return True

    def reset(self, seed_fn):
        with self.lock:
            self.state = seed_fn()
            self.save()

    def save(self):
        with self.lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def mutate(self, fn):
        """在鎖內修改狀態並存檔；fn 的回傳值原樣傳回。"""
        with self.lock:
            result = fn(self.state)
            self.save()
            return result

    def next_id(self, prefix):
        with self.lock:
            seq = self.state.setdefault("seq", {})
            seq[prefix] = seq.get(prefix, 0) + 1
            return f"{prefix}{seq[prefix]:03d}"
