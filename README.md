# CRAwLeR — Cross-Reference Aware Legal Retrieval

Pipeline and benchmarks (CRAwLeR-DK, CRAwLeR-PL) for evaluating cross-reference-aware context utilization in chunk retrieval over legal documents.

## Replicate

Experiments were run on macOS (control machine) with the LLM workloads served from a vLLM endpoint on CSC LUMI. The dataset construction pipeline and all evaluation can be reproduced by any host with an OpenAI-compatible vLLM endpoint serving the required model IDs and a working `uv` install.

### Setup

```bash
make create_environment           # uv venv --python 3.11
source ./.venv/bin/activate
make requirements                 # uv sync

# spaCy models used by the BM25 lemmatiser + stop-word removal
.venv/bin/python -m spacy download pl_core_news_sm
.venv/bin/python -m spacy download da_core_news_sm
```

Create a `.env` at the repo root with:

```
OPENROUTER_API_KEY=...
HF_TOKEN=...
VLLM_BASE_URL=http://localhost:8000/v1
```

### LLM endpoint (vLLM)

All LLM calls — `openai/gpt-oss-120b` for query generation + assurance, and `Qwen/Qwen3-235B-A22B-Instruct-2507-FP8` (strong) / `Qwen/Qwen3-30B-A3B-Instruct-2507` (weak) for chunk contextualisation — hit a vLLM server at `$VLLM_BASE_URL`. We host it on a LUMI compute node and tunnel locally:

```bash
caffeinate -s -t 36000 ssh -N -L 8000:nidNNNNNN:8000 jalocham@lumi.csc.fi &
```

External reproducers will need their own vLLM (or any OpenAI-compatible endpoint) serving the same model IDs, and override `VLLM_BASE_URL` accordingly.

### Data

> Currently the data is only available through the Google Drive which is only available to reviewers - instead of running the script below, please download the folder and place it below!

```bash
# only if you are having an access: currently no one
make sync_data_down               # gsutil rsync gs://maciej_dvc/data/ -> data/
```

This pulls raw documents, intermediate JSONLs, the final CRAwLeR-DK / CRAwLeR-PL datasets, and the result JSONLs that back every table in the paper, so the artifacts can be inspected without rerunning the LLM-heavy stages. Layout of the used datapoints:

```
data/
├── raw/
│   ├── polish_final_cluster_reparsed/{cluster_A,cluster_B,cluster_T}/<doc>/chunks.jsonl
│   └── danish_final_cluster_r10/{cluster_A,cluster_B,cluster_T}/<doc>/chunks.jsonl
└── processed/
    ├── polish_final_cluster_reparsed/             # CRAwLeR-PL main run (Qwen3-235B contextualiser)
    │   └── merged/                                # aggregated across clusters; final dataset lives here
    ├── danish_final_cluster_r10/                  # CRAwLeR-DK main run
    │   └── merged/
    ├── polish_final_cluster_reparsed_ablation/    # 2×2 ablation (Tables 5, 7)
    ├── danish_final_cluster_r10_ablation/         # 2×2 ablation (Tables 6, 8)
    └── polish_final_cluster_reparsed_waterfall/   # sliding-window variant (Table 4)
```

### Pipeline (CRAwLeR dataset construction)

**Stage 0 — Parse raw documents into chunks.**

Parse the raw legal HTML into chunked `.jsonl` files before running the evaluation pipeline. 

**Polish (CRAwLeR-PL)**
Run the parser script with your input and output paths:
```bash
python scripts/parserArtPolishFullDocument.py <input.html> <output.jsonl>
```

**Danish (CRAwLeR-DK)**
The script uses hardcoded paths. Open `scripts/stkparserRetsInformation.py` and edit the bottom block with your document's path and ID:
```python
if __name__ == "__main__":
    jsonl_data, total_candidates = parse_retsinformation_html('path/to/input.html', doc_id="your_doc_id") 
    # ...
    with open('path/to/output.jsonl', 'w', encoding='utf-8') as out_file:
```
Then execute it:
```bash
python scripts/stkparserRetsInformation.py
```

Place the chunked documents in a location of the specified format: `data/processed/{experiment_folder}/cluster_{A,B,T}/{document_name}/chunks.json`
For example, if parsing the `obligationtodefend` HTML using ELI API, once the `.json` document has been obtained, put it into: `data/processed/polish_final_cluster_reparsed_ablation/cluster_A/obligationtodefend/chunks.json`

**Stages 1–3 — Generate, filter, assure queries.** The central script [scripts/lumi_generate_queries_and_assurance.sh](scripts/lumi_generate_queries_and_assurance.sh) chains all stages; uncomment each block and run sequentially. It uses [prompts/query_generation_prompt_r9.j2](prompts/query_generation_prompt_r9.j2) (Appendix B.1) and [prompts/assurance_prompt_r10.j2](prompts/assurance_prompt_r10.j2) (Appendix B.2). Set the language by editing the two paths at the top:

```bash
RAW_BASE="./data/raw/polish_final_cluster_reparsed"        # or danish_final_cluster_r10
PROCESSED_BASE="./data/processed/polish_final_cluster_reparsed"
```

Then:

```bash
nohup caffeinate -s -t 28800 bash ./scripts/lumi_generate_queries_and_assurance.sh &
```
Inside the script there is `ssh` port-forwarding command - if you are already port forwarding, skip it.

Stages inside the script:


| Stage | What | Inputs → Outputs |
|---|---|---|
| 1 | LLM query generation (`openai/gpt-oss-120b`, medium reasoning, T=1) via [scripts/generate_queries.sh](scripts/generate_queries.sh) | `chunks.jsonl` → `queries.jsonl` per cluster |
| Merge | [scripts/merge.sh](scripts/merge.sh) concatenates per-cluster outputs (**Don't concatenate augmented chunks if not created, see the next section**) | per-cluster `queries.jsonl` → `merged/queries.jsonl` |
| 1b | Drop generation failures (`lcr/cli/filter_queries.py drop-failures`) | `queries.jsonl` → `successful_queries.jsonl` |
| 2 (initial eval) | Adversarial baseline: BGE-M3 dense ([scripts/eval_gpu.sh](scripts/eval_gpu.sh)) + BM25 ([scripts/eval_bm25.sh](scripts/eval_bm25.sh)) over plain chunks | `successful_queries.jsonl` → `chunks_{bge-m3_dense,bm25}_results.jsonl` |
| 2a | [scripts/rank_filter_queries.sh](scripts/rank_filter_queries.sh) keeps queries where both retrievers ranked the target > 10 | → `hard_queries.jsonl` |
| 2b | LLM assurance (`gpt-oss-120b`, high reasoning) with the Appendix B.2 prompt | → `assurance_results.jsonl` + `assurance_results.html` |
| 2c | [scripts/assurance_filter_queries.sh](scripts/assurance_filter_queries.sh) drops hard queries that failed assurance | → `contextual_queries.jsonl` (final dataset) |
| 3 | Re-evaluate dense + BM25 against the contextualised chunks (**run the next section first**) | `augmented_chunks.jsonl` + `contextual_queries.jsonl` → `augmented_chunks_{bge-m3_dense,bm25}_results.jsonl` |

The final CRAwLeR datasets are:

- [data/processed/polish_final_cluster_reparsed/merged/contextual_queries.jsonl](data/processed/polish_final_cluster_reparsed/merged/contextual_queries.jsonl) — **CRAwLeR-PL** (300 queries; Table 1)
- [data/processed/danish_final_cluster_r10/merged/contextual_queries.jsonl](data/processed/danish_final_cluster_r10/merged/contextual_queries.jsonl) — **CRAwLeR-DK** (158 queries; Table 1)

### Anthropic-style contextual retrieval — main baseline (Tables 2, 3)

Each chunk is augmented in-place with an LLM-written description of where it sits in its document. Implemented in [lcr/anthropic_preprocessor.py](lcr/anthropic_preprocessor.py); the prompt is in Appendix B.3.

Polish (CRAwLeR-PL, Table 3):

```bash
nohup caffeinate -s -t 36000 bash ./scripts/lumi_anthropic.sh &
```

Danish (CRAwLeR-DK, Table 2):

```bash
nohup caffeinate -s -t 36000 bash ./scripts/lumi_anthropic_danish.sh &
```

Both scripts call [scripts/anthropic_contextualise.sh](scripts/anthropic_contextualise.sh) under the hood and write `augmented_chunks.jsonl` per cluster, then merge to `merged/augmented_chunks.jsonl`. Stage 3 of [scripts/lumi_generate_queries_and_assurance.sh](scripts/lumi_generate_queries_and_assurance.sh) then re-runs dense + BM25 retrieval against the augmented chunks to produce the Recall@k numbers in Tables 2 and 3:

- [data/processed/polish_final_cluster_reparsed/merged/augmented_chunks_bge-m3_dense_results.jsonl](data/processed/polish_final_cluster_reparsed/merged/augmented_chunks_bge-m3_dense_results.jsonl)
- [data/processed/polish_final_cluster_reparsed/merged/augmented_chunks_bm25_results.jsonl](data/processed/polish_final_cluster_reparsed/merged/augmented_chunks_bm25_results.jsonl)
- [data/processed/danish_final_cluster_r10/merged/augmented_chunks_bge-m3_dense_results.jsonl](data/processed/danish_final_cluster_r10/merged/augmented_chunks_bge-m3_dense_results.jsonl)
- [data/processed/danish_final_cluster_r10/merged/augmented_chunks_bm25_results.jsonl](data/processed/danish_final_cluster_r10/merged/augmented_chunks_bm25_results.jsonl)

Appendix E (impact of contextualisation on non-contextual queries) re-runs the same retrieval against [non_contextual_queries.jsonl](data/processed/polish_final_cluster_reparsed/merged/non_contextual_queries.jsonl).

### Sliding-window local contextualisation (Table 4)

Variant of Anthropic contextualisation that splits the document into 32k-token windows with 8k overlap, contextualises per window, then aggregates. Implemented in [lcr/waterfall_preprocessor.py](lcr/waterfall_preprocessor.py); the aggregation prompt is in Appendix B.4. Evaluated only on the `obligationtodefend` document.

```bash
nohup caffeinate -s -t 36000 bash ./scripts/lumi_waterfall.sh &
```

Then assemble the comparison set and run [scripts/sota.sh](scripts/sota.sh) (verbatim from session history):

> Warning: the consolidate is not part of the paper, so no need to run it.
> We recommend against running it in its current version: in our case, majority of the chunks failed. Likely due to timeout.

```bash
mkdir -p data/processed/polish_final_cluster_reparsed_waterfall/compare
cd data/processed/polish_final_cluster_reparsed_waterfall/compare
ln ../cluster_A/obligationtodefend/augmented_chunks.jsonl \
   ./augmented_chunks_obligation_to_defend_waterfall_append.jsonl
ln ../cluster_A_consolidate/obligationtodefend/augmented_chunks.jsonl \
   ./augmented_chunks_obligation_to_defend_waterfall_consolidate.jsonl
ln ../../polish_final_cluster_reparsed/cluster_A/obligationtodefend/augmented_chunks.jsonl \
   ./augmented_chunks_obligation_to_defend.jsonl
cp ../../polish_final_cluster_reparsed/merged/contextual_queries.jsonl \
   ./contextual_queries.jsonl
cd -
./scripts/sota.sh > data/processed/polish_final_cluster_reparsed_waterfall/sota.log
```

Outputs land under [data/processed/polish_final_cluster_reparsed_waterfall/compare/results/](data/processed/polish_final_cluster_reparsed_waterfall/compare/results/).

### Ablation — contextualiser × retriever (Tables 5, 6 + Appendix C Tables 7, 8)

The 2×2 ablation swaps the contextualising LLM (`Qwen3-235B-A22B-Instruct-2507-FP8` ↔ `Qwen3-30B-A3B-Instruct-2507`) against the retriever (`BAAI/bge-m3` ↔ `intfloat/multilingual-e5-small`). Steps below apply to Polish; repeat with `danish_final_cluster_r10_ablation` for Danish.

**1. Generate the 30B contextualisation** with [scripts/lumi_anthropic.sh](scripts/lumi_anthropic.sh) / [scripts/lumi_anthropic_danish.sh](scripts/lumi_anthropic_danish.sh) — both already point `PROCESSED_BASE` at the `_ablation` tree and set `CONTEXTUALISATION_MODEL=Qwen/Qwen3-30B-A3B-Instruct-2507`.

**2. Assemble the merged dir** (verbatim from session history):

```bash
./scripts/merge_chunks.sh ./data/processed/polish_final_cluster_reparsed_ablation \
                          ./data/processed/polish_final_cluster_reparsed_ablation/merged \
                          augmented_chunks.jsonl
cd ./data/processed/polish_final_cluster_reparsed_ablation/merged
mv augmented_chunks.jsonl augmented_chunks30B.jsonl
ln ../../polish_final_cluster_reparsed/merged/augmented_chunks.jsonl ./augmented_chunks235B.jsonl
ln ../../polish_final_cluster_reparsed/merged/contextual_queries.jsonl ./contextual_queries.jsonl
cd -
```

**3. Run the 2×2 grid** with [scripts/ablation.sh](scripts/ablation.sh):

```bash
./scripts/ablation.sh ./data/processed/polish_final_cluster_reparsed_ablation \
                    > ./data/processed/polish_final_cluster_reparsed_ablation/log512.txt
./scripts/ablation.sh ./data/processed/danish_final_cluster_r10_ablation \
                    > ./data/processed/danish_final_cluster_r10_ablation/log512.txt
```

Outputs are `qwen3_{30B,235B}_{me5,bge-m3}_results.jsonl` under each `merged/`.

**Tables 5, 6 (main body)** use BGE-M3 capped to 512 tokens to keep input length comparable with multilingual-e5-small. **Tables 7, 8 (Appendix C)** are the same runs with BGE-M3 at its native 8192-token cap; reproduce by editing the `--batch-size` / max-length in [lcr/cli/eval.py](lcr/cli/eval.py) (or remove the cap inside [scripts/ablation.sh](scripts/ablation.sh)) and re-running. The Appendix C tables confirm the difference is negligible.

### Manual analysis (Section 5.2, 5.3.1, Appendix D, Appendix E)

### For the Polish dataset: 
See [data/processed/polish_final_cluster_reparsed/merged/triple_analysis.html](data/processed/polish_final_cluster_reparsed/merged/triple_analysis.html)

28 queries sampled for the manual analysis: 
* financingeducation_{688,653,652,960}
* healthcarepublicfunds_{638,1208,743,1024}
* obligationtodefend_{2223,1727,2095,1846}
* police_{1249,368,1146,1271}
* socialassistance_{242,361,1179,1603}
* socialinsurancesystem_{1052,622,367,1815}
* statefireservice_{1636,1711,594,757}


### For the Danish dataset: 
See [data/processed/danish_final_cluster_r10/compare_test.html](data/processed/danish_final_cluster_r10/compare_test.html)

28 queries sampled for the manual analysis:
* aaregnskabsloven_{§138a_stk3,§137n_stk2,§138a_stk4,§97a_stk4}
* almenboligloven_{§58b_stk1,§98d_stk4,§169_stk2,§179a_stk4}
* barnetslov_{§49_stk3,§201_stk1,§139_stk5,§13_stk5}
* erhversfondsloven_{§99_stk3,§109_stk3,§101_stk1,§119_stk3}
* selskabsloven_{§5_stk1,§305_stk2,§318b_stk1,§37_stk2}
* serviceloven_{§82b_stk2,§137e_stk2,§97_stk8,§73_stk2}
* straffeloven_{§132b_stk5,§152c_stk1,§236_stk10,§183_stk2}

These files contain all the queries, including the ones used for the manual analysis.

The HTML inspection views used for the 28-query manual audit per dataset (Section 5.2) and the failure analysis (Section 5.3.1) are produced by [lcr/visualisation/visualise_jsonl.py](lcr/visualisation/visualise_jsonl.py) (called automatically by Stage 2b of the central script) and [lcr/visualisation/visualise_compare.py](lcr/visualisation/visualise_compare.py).

Regenerate any of these:

```bash
python3 lcr/visualisation/visualise_jsonl.py \
    data/processed/danish_final_cluster_r10/merged/augmented_chunks_bge-m3_dense_results.jsonl

python3 lcr/visualisation/visualise_compare.py \
    data/processed/danish_final_cluster_r10/merged/contextual_queries.jsonl \
    data/processed/danish_final_cluster_r10/merged/augmented_chunks_bge-m3_dense_results.jsonl \
    data/processed/danish_final_cluster_r10/merged/augmented_chunks_bm25_results.jsonl \
    -o data/processed/danish_final_cluster_r10/compare_test.html
```

### Figures (notebooks)

| Paper figure | Notebook | Output |
|---|---|---|
| **Figs 13, 14** — token distance from target chunk to farthest context chunk | [notebooks/polish_distance_analysis.ipynb](notebooks/polish_distance_analysis.ipynb), [notebooks/danish_distance_analysis.ipynb](notebooks/danish_distance_analysis.ipynb) | [figures/token_distances_polish.png](figures/token_distances_polish.png), [figures/token_distances_danish.png](figures/token_distances_danish.png) |
| **Figs 22, 23** — chunk token distributions per document | [notebooks/dataset_token_analysis.ipynb](notebooks/dataset_token_analysis.ipynb) | [figures/polish_token_distributions_4x2.png](figures/polish_token_distributions_4x2.png), [figures/danish_token_distributions_4x2.png](figures/danish_token_distributions_4x2.png) |
| **Figs 24, 25** — document similarity heatmaps (paraphrase-multilingual-MiniLM-L12-v2) | [notebooks/average_embedding.ipynb](notebooks/average_embedding.ipynb) | [figures/polish_mini_cosine_final.png](figures/polish_mini_cosine_final.png), [figures/danish_mini_cosine_final.png](figures/danish_mini_cosine_final.png) |

Restart and run.
