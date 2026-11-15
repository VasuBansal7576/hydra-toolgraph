"""One client for Jev Choice, Noul, and Score.

The API key is read from JEV_API_KEY only. It is never logged, cached, or
returned. Token counts from the response usage object are logged instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from toolgraph.protocol import JEV_MODEL, JEV_URL, NOUL_CHUNK

_KEY_RE = re.compile("api" + "key_" + r"[A-Za-z0-9_]+")


def redact(text: str) -> str:
    cleaned = _KEY_RE.sub("[REDACTED]", text)
    key = os.environ.get("JEV_API_KEY", "")
    if key:
        cleaned = cleaned.replace(key, "[REDACTED]")
    return cleaned


class JevClient:
    def __init__(self, cache_path: Path, usage_log: Path) -> None:
        self.api_key = os.environ.get("JEV_API_KEY", "").strip()
        self.enabled = bool(self.api_key)
        self.tokens_in = 0
        self.tokens_out = 0
        self.calls = 0
        self.cache_hits = 0
        self.errors: list[dict[str, object]] = []
        self.cache_path = cache_path
        self.usage_log = usage_log
        self._cache: dict[str, dict] = {}
        if cache_path.exists():
            self._cache = json.loads(cache_path.read_text())
        self._lock = threading.Lock()
        self._sem = threading.Semaphore(4)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        usage_log.parent.mkdir(parents=True, exist_ok=True)

    def status(self) -> str:
        if self.enabled:
            return "enabled"
        return "skipped: no key"

    def _cache_key(self, payload: dict) -> str:
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _remember(self, key: str, value: dict) -> None:
        with self._lock:
            self._cache[key] = value
            if self.calls % 20 == 0:
                self.flush()

    def flush(self) -> None:
        tmp = self.cache_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._cache))
        tmp.replace(self.cache_path)

    def _call(self, state: str, questions: dict) -> dict | None:
        if not self.enabled:
            return None
        payload = {"model": JEV_MODEL, "state": state, "questions": questions}
        key = self._cache_key(payload)
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            with self._lock:
                self.cache_hits += 1
            return cached["answers"]
        body = json.dumps(payload).encode()
        last_error: dict[str, object] | None = None
        for attempt in range(4):
            request = urllib.request.Request(
                JEV_URL,
                data=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
            try:
                with self._sem:
                    with urllib.request.urlopen(request, timeout=60) as response:
                        raw = response.read().decode()
                        status = response.status
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode(errors="replace")
                status = exc.code
                last_error = {"status": status, "body": redact(raw)[:800]}
                if status in {408, 429, 500, 502, 503, 504} and attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
            except Exception as exc:  # network reset, timeout
                last_error = {"status": 0, "body": redact(str(exc))[:800]}
                if attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
            data = json.loads(raw)
            usage = data.get("usage") or {}
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            answers = data.get("answers") or {}
            with self._lock:
                self.calls += 1
                self.tokens_in += input_tokens
                self.tokens_out += output_tokens
                with self.usage_log.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "call": self.calls,
                                "http_status": status,
                                "input_tokens": input_tokens,
                                "output_tokens": output_tokens,
                            }
                        )
                        + "\n"
                    )
            self._remember(
                key,
                {
                    "answers": answers,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
            )
            return answers
        if last_error is not None:
            with self._lock:
                self.errors.append(last_error)
            print(
                f"jev_error status={last_error['status']} body={last_error['body']}",
                flush=True,
            )
        return None

    def choice_seeds(
        self,
        task: str,
        candidates: list[tuple[str, str]],
    ) -> list[tuple[str, float]] | None:
        """Return (tool, probability) best first, or None when the call failed."""
        if not self.enabled:
            return None
        criteria = {name: (description or name)[:180] for name, description in candidates[:255]}
        if not criteria:
            return []
        answers = self._call(
            state=task,
            questions={
                "entry": {
                    "type": "choice",
                    "instructions": (
                        "Which tool is the best entry point for the task? "
                        "Pick the tool that performs the action the user asked for. "
                        "Do not pick a low-level helper unless the user asked for that helper."
                    ),
                    "criteria": criteria,
                }
            },
        )
        if not answers or "entry" not in answers:
            return None
        probabilities = answers["entry"].get("probabilities") or {}
        ranked = sorted(probabilities.items(), key=lambda item: (-float(item[1]), item[0]))
        return [(name, float(prob)) for name, prob in ranked if name in criteria]

    def nouls(self, task: str, entry: str, tools: list[tuple[str, str]]) -> dict[str, float] | None:
        """Probability that each tool is required. None if every chunk failed."""
        if not self.enabled:
            return None
        pending = list(tools)
        scores: dict[str, float] = {}
        failed = False
        state = f"Task: {task}\nEntry tool already chosen: {entry}"
        for start in range(0, len(pending), NOUL_CHUNK):
            chunk = pending[start : start + NOUL_CHUNK]
            questions = {
                name: {
                    "type": "noul",
                    "instructions": (
                        f"{name} is required to complete the task, including when it "
                        f"only supplies an input the entry tool needs. "
                        f"Tool description: {(description or name)[:180]}"
                    ),
                }
                for name, description in chunk
            }
            answers = self._call(state=state, questions=questions)
            if not answers:
                failed = True
                continue
            for name, _description in chunk:
                answer = answers.get(name)
                if answer and "noul" in answer:
                    scores[name] = float(answer["noul"])
        if not scores and failed:
            return None
        return scores

    def score_miss(self, task: str, loaded: list[str], missing: list[str]) -> float | None:
        if not self.enabled:
            return None
        state = (
            f"Task: {task}\n"
            f"Loaded tools: {', '.join(loaded) if loaded else '(none)'}\n"
            f"Labelled tools that were not loaded: {', '.join(missing)}"
        )
        answers = self._call(
            state=state,
            questions={
                "gap": {
                    "type": "score",
                    "instructions": "Is a tool the task needs missing from the loaded set?",
                    "criteria": [
                        "The loaded tools already cover the task",
                        "A required helper is missing",
                        "The task cannot be completed without a missing prerequisite",
                    ],
                }
            },
        )
        if not answers or "gap" not in answers:
            return None
        return float(answers["gap"]["score"])
