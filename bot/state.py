"""Состояние бота в JSON-файле, который workflow коммитит обратно в репозиторий."""
from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime, timedelta

DEFAULT_STATE: dict = {
    "version": 1,
    "seen": {},                 # url новости -> когда впервые увидели
    "announced_events": {},     # url события -> когда опубликовали анонс
    "bootstrapped_sources": [], # источники, по которым уже собран "базовый" список ссылок
    "event_cache": {},          # url события -> извлечённые данные страницы
    "tg_offset": 0,             # offset для getUpdates
    "pending": None,            # текущий черновик на модерации
    "last_draft_date": None,
    "history": [],
}


def load(path: str) -> dict:
    state = copy.deepcopy(DEFAULT_STATE)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            state.update(json.load(f))
    return state


def save(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def _prune_map(mapping: dict, cutoff: datetime, key=lambda v: v) -> dict:
    out = {}
    for k, v in mapping.items():
        try:
            if datetime.fromisoformat(key(v)) >= cutoff:
                out[k] = v
        except (TypeError, ValueError):
            continue
    return out


def prune(state: dict, now: datetime) -> None:
    state["seen"] = _prune_map(state["seen"], now - timedelta(days=90))
    state["announced_events"] = _prune_map(state["announced_events"], now - timedelta(days=60))
    state["event_cache"] = _prune_map(state["event_cache"], now - timedelta(days=10),
                                      key=lambda v: v.get("fetched", ""))
    state["history"] = state["history"][-60:]
