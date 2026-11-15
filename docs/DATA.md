# Fetching ToolLinkOS data

The eval reads three JSON files under `data/`:

- `core_tools.json`
- `regular_tools.json`
- `instances.json`

Upstream: [Graph-RAG-Tool-Fusion-ToolLinkOS](https://github.com/EliasLumer/Graph-RAG-Tool-Fusion-ToolLinkOS) (MIT; see `data/UPSTREAM_LICENSE`).

Also mirrored on Hugging Face: [`eliaslumer/ToolLinkOS`](https://huggingface.co/datasets/eliaslumer/ToolLinkOS).

## From GitHub

```bash
git clone --depth 1 https://github.com/EliasLumer/Graph-RAG-Tool-Fusion-ToolLinkOS /tmp/ToolLinkOS
cp /tmp/ToolLinkOS/data/core_tools.json \
   /tmp/ToolLinkOS/data/regular_tools.json \
   /tmp/ToolLinkOS/data/instances.json \
   data/
```

If the upstream layout differs, copy the three files that contain the 50 core tools, 523 regular tools, and 1,569 instances into `data/` with those exact names.

## From Hugging Face

```bash
pip install huggingface_hub
python3 - <<'PY'
from huggingface_hub import hf_hub_download
from pathlib import Path
dst = Path("data")
dst.mkdir(exist_ok=True)
for name in ("core_tools.json", "regular_tools.json", "instances.json"):
    path = hf_hub_download("eliaslumer/ToolLinkOS", filename=name, repo_type="dataset")
    Path(dst / name).write_bytes(Path(path).read_bytes())
    print("wrote", name)
PY
```

## Sanity check

```bash
python3 - <<'PY'
from pathlib import Path
from toolgraph.data import load_dataset
tools, instances = load_dataset(Path("data"))
assert len(tools) == 573
assert len(instances) == 1569
print("ok", len(tools), len(instances))
PY
```
