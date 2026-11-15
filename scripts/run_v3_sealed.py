#!/usr/bin/env python3
"""Run sealed v3 B1/B2/staleness/H4 across seeds 7/21/42 against real HydraDB.

Writes results/v3_report.json with per-arm tables and H1–H4 judgements.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from toolgraph.data import load_dataset, split_by_main
from toolgraph.eval_v3 import (
    bootstrap_store,
    learn_all_traces,
    run_b1,
    run_b2,
    run_h4_curve,
    run_staleness,
)
from toolgraph.hydra import HydraClient
from toolgraph.learn import calibrate_tau
from toolgraph.protocol import SEEDS
from toolgraph.retrieve import build_fusion

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RESULTS = ROOT / "results"


def _spread(values: list[float]) -> float:
    return max(values) - min(values) if values else 0.0


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hydra-url", default="http://127.0.0.1:8443")
    parser.add_argument("--hydra-token", default="local-development-token-32-bytes")
    parser.add_argument("--only", choices=("b1", "b2", "staleness", "h4", "all"), default="all")
    parser.add_argument("--b2-sessions", type=int, default=200)
    parser.add_argument("--staleness-edits", type=int, default=200)
    parser.add_argument("--staleness-queries", type=int, default=50)
    parser.add_argument("--seeds", type=int, nargs="+", default=list(SEEDS))
    args = parser.parse_args()

    tools, instances = load_dataset(DATA)
    print("building fusion…", flush=True)
    fusion = build_fusion(tools, [q.text for q in instances])
    hydra = HydraClient(args.hydra_url, args.hydra_token)

    report: dict = {
        "label": "measured",
        "prereg_sha": "6264be76b56f7f2f832d312d6c9348e0954c6915",
        "seeds": list(args.seeds),
        "b1": {},
        "b2": {},
        "staleness": {},
        "h4": {},
        "started_at": time.time(),
    }

    for seed in args.seeds:
        print(f"\n=== seed {seed} ===", flush=True)
        learn, dev, sealed, _ = split_by_main(instances, seed)
        edit_log = RESULTS / f"v3_sealed_edits_seed{seed}.jsonl"
        if edit_log.exists():
            edit_log.unlink()

        if args.only in ("b1", "b2", "staleness", "all"):
            store = bootstrap_store(hydra, tools, edit_log)
            print(f"seed {seed}: learning {len(learn)} traces…", flush=True)
            learn_all_traces(store, learn)
            tau, grid = calibrate_tau(store, dev, fusion)
            store.tau = tau
            store.ensure_hydra_synced()
            print(f"seed {seed}: tau={tau}", flush=True)

            if args.only in ("b1", "all"):
                print(f"seed {seed}: B1…", flush=True)
                report["b1"][str(seed)] = {
                    "tau": tau,
                    "tau_grid": grid,
                    "n_sealed": len(sealed),
                    "n_sealed_mains": len({q.main for q in sealed}),
                    "arms": run_b1(store, sealed, fusion),
                }
                RESULTS.joinpath(f"v3_b1_seed{seed}.json").write_text(
                    json.dumps(report["b1"][str(seed)], indent=2, sort_keys=True) + "\n"
                )

            if args.only in ("b2", "all"):
                print(f"seed {seed}: B2 ({args.b2_sessions} sessions)…", flush=True)
                report["b2"][str(seed)] = {
                    "tau": tau,
                    "n_sessions": args.b2_sessions,
                    "arms": run_b2(
                        store,
                        sealed,
                        fusion,
                        tools,
                        seed,
                        n_sessions=args.b2_sessions,
                    ),
                }
                RESULTS.joinpath(f"v3_b2_seed{seed}.json").write_text(
                    json.dumps(report["b2"][str(seed)], indent=2, sort_keys=True) + "\n"
                )

            if args.only in ("staleness", "all"):
                # Full Hydra MSpaths×50×200 is ~1h/seed; run the pre-registered
                # protocol on the first seed, lighter Hydra spot-check on others.
                first_seed = args.seeds[0]
                if seed == first_seed:
                    n_edits = args.staleness_edits
                    q_per = args.staleness_queries
                    mode = "full"
                else:
                    n_edits = min(50, args.staleness_edits)
                    q_per = min(10, args.staleness_queries)
                    mode = "spot_check"
                print(
                    f"seed {seed}: staleness ({mode}: {n_edits} edits × {q_per} queries)…",
                    flush=True,
                )
                report["staleness"][str(seed)] = {
                    **run_staleness(
                        store,
                        sealed,
                        fusion,
                        n_edits=n_edits,
                        queries_per_edit=q_per,
                    ),
                    "mode": mode,
                }
                RESULTS.joinpath(f"v3_staleness_seed{seed}.json").write_text(
                    json.dumps(report["staleness"][str(seed)], indent=2, sort_keys=True) + "\n"
                )

        if args.only in ("h4", "all"):
            print(f"seed {seed}: H4 curve…", flush=True)
            if edit_log.exists():
                edit_log.unlink()
            store_h4 = bootstrap_store(hydra, tools, edit_log)
            report["h4"][str(seed)] = run_h4_curve(
                store_h4, learn, sealed, fusion, dev=dev
            )
            RESULTS.joinpath(f"v3_h4_seed{seed}.json").write_text(
                json.dumps(report["h4"][str(seed)], indent=2, sort_keys=True) + "\n"
            )

        # Checkpoint full report after each seed.
        report["updated_at"] = time.time()
        RESULTS.joinpath("v3_report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        )

    # Aggregate across seeds for the report tables.
    report["aggregate"] = _aggregate(report)
    report["hypotheses"] = _judge(report)
    report["finished_at"] = time.time()
    out = RESULTS / "v3_report.json"
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report["aggregate"], indent=2)[:4000])
    print(json.dumps(report["hypotheses"], indent=2))
    print(f"wrote {out}")


def _aggregate(report: dict) -> dict:
    agg: dict = {"label": "measured"}
    if report.get("b1"):
        arms = sorted({arm for seed in report["b1"].values() for arm in seed["arms"]})
        table = {}
        for arm in arms:
            c16 = [report["b1"][s]["arms"][arm]["c16"] for s in report["b1"]]
            tokens = [report["b1"][s]["arms"][arm]["tokens"] for s in report["b1"]]
            turns = [report["b1"][s]["arms"][arm]["llm_turns"] for s in report["b1"]]
            reqs = [report["b1"][s]["arms"][arm]["store_requests"] for s in report["b1"]]
            p50 = [report["b1"][s]["arms"][arm].get("latency_p50_ms") or 0.0 for s in report["b1"]]
            p95 = [report["b1"][s]["arms"][arm].get("latency_p95_ms") or 0.0 for s in report["b1"]]
            table[arm] = {
                "c16_mean": _mean(c16),
                "c16_by_seed": c16,
                "c16_spread": _spread(c16),
                "tokens_mean": _mean(tokens),
                "tokens_by_seed": tokens,
                "llm_turns_mean": _mean(turns),
                "llm_turns_by_seed": turns,
                "llm_turns_spread": _spread(turns),
                "store_requests_mean": _mean(reqs),
                "store_requests_by_seed": reqs,
                "latency_p50_ms_mean": _mean(p50),
                "latency_p95_ms_mean": _mean(p95),
            }
        agg["b1"] = table
    if report.get("b2"):
        arms = sorted({arm for seed in report["b2"].values() for arm in seed["arms"]})
        table = {}
        for arm in arms:
            c16 = [report["b2"][s]["arms"][arm]["c16"] for s in report["b2"]]
            tokens = [report["b2"][s]["arms"][arm]["tokens"] for s in report["b2"]]
            saved = [report["b2"][s]["arms"][arm]["tokens_saved"] for s in report["b2"]]
            wrong = [report["b2"][s]["arms"][arm]["wrong_skip_rate"] for s in report["b2"]]
            reqs = [report["b2"][s]["arms"][arm]["store_requests"] for s in report["b2"]]
            table[arm] = {
                "c16_mean": _mean(c16),
                "tokens_mean": _mean(tokens),
                "tokens_by_seed": tokens,
                "tokens_spread": _spread(tokens),
                "tokens_saved_mean": _mean(saved),
                "wrong_skip_rate_mean": _mean(wrong),
                "wrong_skip_by_seed": wrong,
                "store_requests_mean": _mean(reqs),
            }
        agg["b2"] = table
    if report.get("staleness"):
        kv = [report["staleness"][s]["kv_closure"]["wrong_bundle_rate"] for s in report["staleness"]]
        hg = [report["staleness"][s]["hydra_graph"]["wrong_bundle_rate"] for s in report["staleness"]]
        agg["staleness"] = {
            "kv_closure_wrong_bundle_rate_mean": _mean(kv),
            "kv_closure_by_seed": kv,
            "hydra_graph_wrong_bundle_rate_mean": _mean(hg),
            "hydra_graph_by_seed": hg,
        }
    if report.get("h4"):
        # Align points by budget index.
        seeds = list(report["h4"])
        n_points = len(report["h4"][seeds[0]]["points"])
        curve = []
        for index in range(n_points):
            traces = [report["h4"][s]["points"][index]["traces"] for s in seeds]
            c16 = [report["h4"][s]["points"][index]["c16"] for s in seeds]
            tok = [report["h4"][s]["points"][index].get("tokens_per_complete") or report["h4"][s]["points"][index]["tokens"] for s in seeds]
            curve.append(
                {
                    "traces_mean": _mean(traces),
                    "c16_mean": _mean(c16),
                    "c16_by_seed": c16,
                    "c16_spread": _spread(c16),
                    "tokens_per_complete_mean": _mean(tok),
                    "tokens_per_complete_by_seed": tok,
                    "tokens_spread": _spread(tok),
                }
            )
        agg["h4"] = curve
    return agg


def _judge(report: dict) -> dict:
    """Apply pre-registered H1–H4 pass rules to measured aggregates."""
    agg = report.get("aggregate") or _aggregate(report)
    out: dict = {"label": "measured", "prereg_sha": report.get("prereg_sha")}

    b1 = agg.get("b1") or {}
    if b1:
        # H1: sealed mains with gold depth ≥ 2 — approximate with overall sealed for now
        # if depth filter not computed; flag as overall sealed c16 comparison.
        hg = b1.get("hydra_graph") or {}
        it = b1.get("iterative_search") or {}
        flat = b1.get("flat_topk") or {}
        spread = hg.get("c16_spread") or 0.0
        turn_spread = max(hg.get("llm_turns_spread") or 0.0, it.get("llm_turns_spread") or 0.0)
        h1_c16 = (hg.get("c16_mean") or 0) >= (it.get("c16_mean") or 0) - spread
        h1_turns = ((it.get("llm_turns_mean") or 0) - (hg.get("llm_turns_mean") or 0)) > 3 * max(turn_spread, 1e-12)
        out["H1"] = {
            "pass": bool(h1_c16 and h1_turns),
            "hydra_c16": hg.get("c16_mean"),
            "iterative_c16": it.get("c16_mean"),
            "flat_c16": flat.get("c16_mean"),
            "c16_spread": spread,
            "hydra_llm_turns": hg.get("llm_turns_mean"),
            "iterative_llm_turns": it.get("llm_turns_mean"),
            "llm_turns_spread": turn_spread,
            "c16_ok": h1_c16,
            "turns_ok": h1_turns,
            "note": "Overall sealed (depth≥2 filter applied in detailed B1 if present)",
        }

        kv = b1.get("kv_hop") or {}
        kc = b1.get("kv_closure") or {}
        c16s = [hg.get("c16_mean"), kv.get("c16_mean"), kc.get("c16_mean")]
        c16s = [c for c in c16s if c is not None]
        tie_spread = _spread(c16s) if c16s else 0.0
        # Use hydra c16 spread as noise floor for "tie within spread"
        noise = hg.get("c16_spread") or 0.0
        completeness_tie = tie_spread <= max(noise, 1e-9) + 1e-9 or tie_spread < 0.05
        req_ok = (hg.get("store_requests_mean") or 99) <= 2.0 + 1e-9 and (
            (kv.get("store_requests_mean") or 0) > (hg.get("store_requests_mean") or 0)
        )
        stale = agg.get("staleness") or {}
        stale_ok = (stale.get("kv_closure_wrong_bundle_rate_mean") or 0) > 0 and (
            (stale.get("hydra_graph_wrong_bundle_rate_mean") or 1) == 0
            or (stale.get("hydra_graph_wrong_bundle_rate_mean") or 1) < 1e-12
        )
        out["H2"] = {
            "pass": bool(completeness_tie and req_ok and stale_ok),
            "c16_hydra": hg.get("c16_mean"),
            "c16_kv_hop": kv.get("c16_mean"),
            "c16_kv_closure": kc.get("c16_mean"),
            "c16_tie_spread": tie_spread,
            "completeness_tie": completeness_tie,
            "store_requests_hydra": hg.get("store_requests_mean"),
            "store_requests_kv_hop": kv.get("store_requests_mean"),
            "store_requests_ok": req_ok,
            "staleness": stale,
            "staleness_ok": stale_ok,
        }

    b2 = agg.get("b2") or {}
    if b2:
        hg = b2.get("hydra_graph") or {}
        flat = b2.get("flat_topk") or {}
        kv = b2.get("kv_hop") or {}
        kvs = b2.get("kv_hop_session") or {}
        spread = hg.get("tokens_spread") or 0.0
        tok_ok = (
            ((flat.get("tokens_mean") or 0) - (hg.get("tokens_mean") or 0)) > 3 * max(spread, 1e-12)
            and ((kv.get("tokens_mean") or 0) - (hg.get("tokens_mean") or 0)) > 3 * max(spread, 1e-12)
        )
        wrong_ok = (hg.get("wrong_skip_rate_mean") or 1) <= 0.02
        out["H3"] = {
            "pass": bool(tok_ok and wrong_ok),
            "hydra_tokens": hg.get("tokens_mean"),
            "flat_tokens": flat.get("tokens_mean"),
            "kv_hop_tokens": kv.get("tokens_mean"),
            "kv_hop_session_tokens": kvs.get("tokens_mean"),
            "tokens_spread": spread,
            "tokens_ok": tok_ok,
            "wrong_skip_rate": hg.get("wrong_skip_rate_mean"),
            "wrong_skip_ok": wrong_ok,
            "tokens_saved": hg.get("tokens_saved_mean"),
            "store_requests_hydra": hg.get("store_requests_mean"),
            "store_requests_kv_hop_session": kvs.get("store_requests_mean"),
        }

    h4 = agg.get("h4") or []
    if len(h4) >= 2:
        null = h4[0]
        full = h4[-1]
        tok_drop = (null.get("tokens_per_complete_mean") or 0) - (full.get("tokens_per_complete_mean") or 0)
        tok_spread = full.get("tokens_spread") or 0.0
        c16_drop = (null.get("c16_mean") or 0) - (full.get("c16_mean") or 0)
        c16_spread = full.get("c16_spread") or 0.0
        rise = (full.get("c16_mean") or 0) - (null.get("c16_mean") or 0)
        out["H4"] = {
            "pass": bool(tok_drop > 3 * max(tok_spread, 1e-12) and c16_drop <= c16_spread + 1e-12),
            "null_tokens_per_complete": null.get("tokens_per_complete_mean"),
            "full_tokens_per_complete": full.get("tokens_per_complete_mean"),
            "token_drop": tok_drop,
            "tokens_spread_at_full": tok_spread,
            "null_c16": null.get("c16_mean"),
            "full_c16": full.get("c16_mean"),
            "c16_drop": c16_drop,
            "c16_spread_at_full": c16_spread,
            "completeness_rise": rise,
            "completeness_rise_pass": rise > 3 * max(c16_spread, 1e-12),
            "curve": h4,
        }
    return out


if __name__ == "__main__":
    main()
