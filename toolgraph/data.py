"""ToolLinkOS loader and the learn/dev/sealed split."""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from pathlib import Path

from toolgraph.protocol import DEV_FRACTION, LEARN_FRACTION

NAME_RE = re.compile(r"[A-Za-z0-9_]+")
TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    kind: str
    params: tuple[dict, ...]
    depends_on: tuple[dict, ...]

    def base_text(self) -> str:
        parts = [self.name.replace("_", " "), self.description]
        for param in self.params:
            parts.append(str(param.get("name", "")))
            parts.append(str(param.get("description", "")))
        return ". ".join(part.strip() for part in parts if part and str(part).strip())


@dataclass(frozen=True)
class Instance:
    idx: int
    text: str
    main: str
    gold: frozenset[str]


def load_dataset(data_dir: Path) -> tuple[list[Tool], list[Instance]]:
    core = json.loads((data_dir / "core_tools.json").read_text())
    regular = json.loads((data_dir / "regular_tools.json").read_text())
    raw_tools = list(core) + list(regular)
    tools: list[Tool] = []
    seen: set[str] = set()
    for raw in raw_tools:
        name = raw["name"]
        if not NAME_RE.fullmatch(name):
            raise ValueError(f"tool name is not a safe identifier: {name!r}")
        if name in seen:
            raise ValueError(f"duplicate tool {name}")
        seen.add(name)
        deps = []
        for dep in raw.get("depends_on") or []:
            dep_name = dep["name"]
            if not NAME_RE.fullmatch(dep_name):
                raise ValueError(f"dependency name is not a safe identifier: {dep_name!r}")
            deps.append(dep)
        tools.append(
            Tool(
                name=name,
                description=raw.get("description") or "",
                kind=raw.get("func_type") or "regular",
                params=tuple(raw.get("parameters") or []),
                depends_on=tuple(deps),
            )
        )
    tools.sort(key=lambda tool: tool.name)
    known = {tool.name for tool in tools}
    instances: list[Instance] = []
    for idx, raw in enumerate(json.loads((data_dir / "instances.json").read_text())):
        gold = frozenset(raw["golden_function_names"])
        missing = gold - known
        if missing:
            raise ValueError(f"query {idx} labels unknown tools: {sorted(missing)[:5]}")
        main = raw["main_golden_function_name"]
        if main not in gold:
            raise ValueError(f"query {idx} main tool is outside the gold set")
        instances.append(Instance(idx=idx, text=raw["user_query"], main=main, gold=gold))
    return tools, instances


def split_indices(n: int, seed: int) -> tuple[list[int], list[int], list[int]]:
    """Query-level split. Leaks sibling gold sets on ToolLinkOS; appendix only."""
    order = list(range(n))
    random.Random(seed).shuffle(order)
    n_learn = int(n * LEARN_FRACTION)
    n_dev = int(n * DEV_FRACTION)
    learn = order[:n_learn]
    dev = order[n_learn : n_learn + n_dev]
    sealed = order[n_learn + n_dev :]
    if set(learn) & set(dev) or set(learn) & set(sealed) or set(dev) & set(sealed):
        raise RuntimeError("split overlap")
    if len(learn) + len(dev) + len(sealed) != n:
        raise RuntimeError("split dropped a query")
    return learn, dev, sealed


def split_by_main(
    instances: list[Instance],
    seed: int,
) -> tuple[list[Instance], list[Instance], list[Instance], dict[str, str]]:
    """Headline split: all queries of a main stay in the same learn/dev/sealed bucket.

    Measured fact on ToolLinkOS: every main has exactly 3 queries with an
    identical gold set, so a query-level split leaks sibling answers.
    """
    by_main: dict[str, list[Instance]] = {}
    for query in instances:
        by_main.setdefault(query.main, []).append(query)
    mains = sorted(by_main)
    random.Random(seed).shuffle(mains)
    n_learn = int(len(mains) * LEARN_FRACTION)
    n_dev = int(len(mains) * DEV_FRACTION)
    learn_mains = set(mains[:n_learn])
    dev_mains = set(mains[n_learn : n_learn + n_dev])
    sealed_mains = set(mains[n_learn + n_dev :])
    if learn_mains & dev_mains or learn_mains & sealed_mains or dev_mains & sealed_mains:
        raise RuntimeError("main-split overlap")
    if len(learn_mains) + len(dev_mains) + len(sealed_mains) != len(mains):
        raise RuntimeError("main-split dropped a main")

    def ordered(bucket: set[str]) -> list[Instance]:
        out: list[Instance] = []
        # Stable within-main order; across mains follow the shuffled main list.
        for main in mains:
            if main not in bucket:
                continue
            out.extend(sorted(by_main[main], key=lambda row: row.idx))
        return out

    assignment = {
        main: (
            "learn"
            if main in learn_mains
            else "dev"
            if main in dev_mains
            else "sealed"
        )
        for main in mains
    }
    return ordered(learn_mains), ordered(dev_mains), ordered(sealed_mains), assignment


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())
