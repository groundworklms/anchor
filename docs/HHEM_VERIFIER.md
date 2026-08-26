# HHEM grounding verifier (the premise-gate upgrade)

The premise gate (`src/generation/premise_gate.py`) refuses a question whose asserted
specific -- a count ("the seven phases"), an edition/year, a page, or an attribution -- is
not supported by the retrieved passages. It needs a verifier that judges *entailment*, not
similarity. D-056 proved the resident 2B cannot do this (it scores true and false counts
alike). D-057 replaced it with **Vectara HHEM-2.1-Open** (Flan-T5-base, 110M, Apache-2.0),
which discriminates cleanly (true count 0.94 vs false count 0.01).

This is an **optional upgrade**. With `premise_gate.hhem_url` set, the gate uses HHEM; set
it to `null` and the gate falls back to the 2B (weaker, but no extra service). The gate is
also **fail-open**: if the verifier is unreachable, `pipeline.ask` skips it and answers
normally -- a verifier outage never takes the tutor offline.

## One-time device setup (air-gapped Jetson Orin Nano)

The device has no network at inference. Setup packages/weights were pulled once through a
temporary reverse-SSH tunnel to a workstation's internet, then the model runs offline.

1. **torch was present but broken** -- `ImportError: libcudss.so.0`. Its own bundled CUDA
   libs were not on the loader path. The `tutor-verify` unit fixes this with:
   `LD_LIBRARY_PATH=~/.local/lib/python3.10/site-packages/nvidia/{cublas,cuda_nvrtc,cu12}/lib`
2. **transformers pin:** `pip install 'transformers==4.46.3' 'pillow>=10.2' sentencepiece`.
   HHEM-2.1-Open's `trust_remote_code` head does NOT import on transformers 5.x.
3. **Stage the model offline:** first load with network (through the tunnel) downloads
   `vectara/hallucination_evaluation_model` into `~/.cache/huggingface`; thereafter it runs
   with `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1` and no network.
4. **CPU-only:** `CUDA_VISIBLE_DEVICES=` -- keeps HHEM off the generator's GPU and drops
   its RSS from ~1450 MB (with a wasted CUDA context) to ~1040 MB.

## Service

`deploy/tutor-verify.service` -> `/etc/systemd/system/tutor-verify.service` (bakes in the
four env settings above), then `systemctl daemon-reload && systemctl enable --now
tutor-verify`. Serves loopback `127.0.0.1:8083`:

    GET  /health -> {"status":"ok","model":"..."}
    POST /verify {"claim":"...","spans":["passage",...]} -> {"support":0.0..1.0}   # per-span max

`scripts/verifier_server.py --self-test` loads the model and scores one supported / one
contradicted pair, then exits.

## Enable / disable

`config/default.yaml`:

```yaml
premise_gate:
  enabled: true
  hhem_url: http://127.0.0.1:8083/verify   # null -> 2B fallback; enabled:false -> gate off
  support_tau: 0.5
```

## Measured (D-057)

Premise gate + HHEM on the 82-case fixture: **8/21 fabrications caught** (including an
invented drug+dose and a stale-edition trap), **2/45 over-refusals**, **0/16 answer-traps
broken** -- a ~4:1 trade vs the 2B's 1:1. Memory: HHEM ~1040 MB CPU-only; full stack + HHEM
leaves ~676 MB available + swap. Latency: +~2-3s on a presupposition-bearing query (one
HHEM call), none on questions that assert no specific.
