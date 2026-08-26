# Offline Doctrine-Grounded Tutor
#
# Targets that touch the device use ORIN_HOST/ORIN_USER. Key auth only — no passwords
# in this file, ever.

ORIN_USER ?= vanguard
ORIN_HOST ?= 192.168.55.1
ORIN      := $(ORIN_USER)@$(ORIN_HOST)
REMOTE    ?= /opt/tutor
STAMP     := $(shell date +%Y-%m-%d-%H%M%S)

.PHONY: help eval tune-abstention ingest serve bench super-on super-off \
        backup device-info install-deps build-llama models verify-corpus clean

help:
	@echo "eval             run the eval harness, print the results table"
	@echo "tune-abstention  sweep the reranker threshold against eval/, write the tuned value"
	@echo "ingest           build the SQLite index from corpus/"
	@echo "serve            start llama.cpp server + FastAPI on the device"
	@echo "bench            P0 benchmark sweep across all power modes"
	@echo "super-on         switch to the Super nvpmodel profile (reversible)"
	@echo "super-off        revert to the stock nvpmodel profile"
	@echo "backup           clone the device NVMe to a compressed image (phase gate)"
	@echo "device-info      print pinned device facts for DECISIONS.md"

# --- eval ------------------------------------------------------------------

# The eval runs ON the device: it exercises the real pipeline, which needs the index and
# the three model servers. Running it here would measure nothing.
eval: push-src
	ssh $(ORIN) 'cd $(REMOTE) && python3 eval/run_eval.py \
	  --db $(REMOTE)/doctrine.sqlite --config $(REMOTE)/config/default.yaml \
	  --in-corpus $(REMOTE)/eval/in_corpus.jsonl \
	  --out-corpus $(REMOTE)/eval/out_of_corpus.jsonl \
	  --json-out $(REMOTE)/eval/last_run.json'
	@mkdir -p eval/runs
	@scp -q $(ORIN):$(REMOTE)/eval/last_run.json eval/runs/$(STAMP).json
	@echo ">>> saved eval/runs/$(STAMP).json"

tune-abstention: push-src
	ssh $(ORIN) 'cd $(REMOTE)/generation && python3 tune_thresholds.py \
	  --db $(REMOTE)/doctrine.sqlite --in-corpus $(REMOTE)/eval/in_corpus.jsonl \
	  --out-corpus $(REMOTE)/eval/out_of_corpus.jsonl --out $(REMOTE)/tuning.json'
	@scp -q $(ORIN):$(REMOTE)/tuning.json eval/tuning.json
	@echo ">>> tuned; update config/default.yaml abstention.reranker_score_threshold"

recall: push-src
	ssh $(ORIN) 'cd $(REMOTE) && python3 retrieval/measure_recall.py \
	  --db $(REMOTE)/doctrine.sqlite --eval $(REMOTE)/eval/in_corpus.jsonl'

# One push so the device always runs the committed code, never a stale hand-copied file.
push-src:
	@ssh $(ORIN) 'mkdir -p $(REMOTE)/eval $(REMOTE)/config $(REMOTE)/retrieval \
	  $(REMOTE)/generation $(REMOTE)/ingest'
	@scp -q src/common/chunk_text.py $(ORIN):$(REMOTE)/retrieval/
	@scp -q src/common/chunk_text.py $(ORIN):$(REMOTE)/ingest/
	@scp -q src/retrieval/*.py $(ORIN):$(REMOTE)/retrieval/
	@scp -q src/generation/*.py $(ORIN):$(REMOTE)/generation/
	@scp -q src/ingest/*.py $(ORIN):$(REMOTE)/ingest/
	@scp -q eval/run_eval.py eval/in_corpus.jsonl eval/out_of_corpus.jsonl \
	  $(ORIN):$(REMOTE)/eval/
	@scp -q config/default.yaml $(ORIN):$(REMOTE)/config/
	@echo ">>> pushed src to $(ORIN):$(REMOTE)"

# --- corpus / index --------------------------------------------------------

# `verify_manifest.py` was referenced here for weeks and has never existed in any
# commit, so `make ingest` could not have run. The screener is `inspect_corpus.py`; the
# manifest builder is `build_manifest.py`.
verify-corpus:
	python3 src/ingest/inspect_corpus.py --manifest corpus/manifest.json

# Ingest is two steps, and the chunker was missing from this target entirely.
# build_index.py takes --chunks/--db; it has never accepted --config.
chunk:
	python3 src/ingest/chunker.py --manifest corpus/manifest.json \
	  --out corpus/chunks.jsonl --report corpus/chunk_report.json

ingest: verify-corpus chunk
	python3 src/ingest/build_index.py --chunks corpus/chunks.jsonl \
	  --db doctrine.sqlite

verify-citations:
	python3 src/ingest/verify_citations.py --sample 120

test:
	python3 -m pytest tests -q

# --- device ----------------------------------------------------------------

device-info:
	@ssh $(ORIN) 'echo "== module =="; \
	  sudo -n cat /sys/bus/i2c/devices/0-0050/eeprom 2>/dev/null | strings | head -1 || true; \
	  cat /proc/device-tree/compatible | tr -d "\0"; echo; \
	  echo "== l4t =="; head -1 /etc/nv_tegra_release; \
	  echo "== cuda =="; /usr/local/cuda/bin/nvcc --version | tail -2; \
	  echo "== power =="; nvpmodel -q | head -2; \
	  echo "== nvpmodel conf =="; ls -l /etc/nvpmodel.conf; \
	  echo "== mem =="; free -h | head -2'

services:
	scp scripts/install_services.sh $(ORIN):$(REMOTE)/
	ssh $(ORIN) 'sudo sh $(REMOTE)/install_services.sh'
	@echo ">>> units enabled; they now start the tutor at boot"

boot-test:
	@echo ">>> rebooting and timing cold boot to a usable tutor"
	@python3 scripts/boot_test.py --host $(ORIN_HOST) --user $(ORIN_USER)

install-deps:
	ssh $(ORIN) 'sudo apt-get update && sudo apt-get install -y \
	  cmake ninja-build build-essential libcurl4-openssl-dev python3-pip'

build-llama:
	ssh $(ORIN) 'set -e; \
	  export PATH=/usr/local/cuda/bin:$$PATH; \
	  mkdir -p $(REMOTE) && cd $(REMOTE); \
	  [ -d llama.cpp ] || git clone https://github.com/ggml-org/llama.cpp; \
	  cd llama.cpp && git rev-parse HEAD > $(REMOTE)/llama_cpp_commit.txt; \
	  rm -rf build; \
	  cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="87" \
	    -DCMAKE_BUILD_TYPE=Release -G Ninja; \
	  cmake --build build --config Release -j$$(nproc); \
	  echo "PINNED COMMIT: $$(cat $(REMOTE)/llama_cpp_commit.txt)"'

# --- power profile ---------------------------------------------------------
# Reversible symlink swap. The Super conf ships with L4T 36.4.3; no firmware flash.
# See DECISIONS.md D-012.

# SUPERSEDED, kept for the record. D-012 believed this was a durable reversible swap;
# D-016 found /etc/systemd/nvpower.sh relinks nvpmodel.conf from the device tree at every
# boot, and D-019 measured the actual gain at ZERO -- 691 vs 718 tok/s with byte-identical
# clocks. Do not use these expecting a speedup.
super-on:
	ssh $(ORIN) 'sudo ln -sf /etc/nvpmodel/nvpmodel_p3767_0003_super.conf /etc/nvpmodel.conf && \
	  ls -l /etc/nvpmodel.conf && grep "^< POWER_MODEL" /etc/nvpmodel.conf'
	@echo ">>> reboot required, then: ssh $(ORIN) 'sudo nvpmodel -m 2'  (MAXN_SUPER)"

super-off:
	ssh $(ORIN) 'sudo ln -sf /etc/nvpmodel/nvpmodel_p3767_0003.conf /etc/nvpmodel.conf && \
	  ls -l /etc/nvpmodel.conf && grep "^< POWER_MODEL" /etc/nvpmodel.conf'
	@echo ">>> reboot required, then: ssh $(ORIN) 'sudo nvpmodel -m 0'  (15W stock)"

# --- benchmark -------------------------------------------------------------

bench:
	scp scripts/p0_bench.py $(ORIN):$(REMOTE)/
	ssh $(ORIN) 'cd $(REMOTE) && sudo python3 p0_bench.py --out $(REMOTE)/bench_$(STAMP).json'
	scp $(ORIN):$(REMOTE)/bench_$(STAMP).json bench/
	python3 scripts/bench_table.py bench/bench_$(STAMP).json --stamp "$(STAMP)" > bench/BENCHMARKS.md
	@echo ">>> wrote bench/BENCHMARKS.md"

# --- backup ----------------------------------------------------------------
# Phase-gate guardrail: clone the boot drive at every phase gate.

backup:
	@mkdir -p backups/$(STAMP)
	ssh $(ORIN) 'sudo dd if=/dev/nvme0n1 bs=4M status=progress | zstd -T0 -3 -c' \
	  > backups/$(STAMP)/nvme0n1_full.img.zst
	@echo ">>> wrote backups/$(STAMP)/nvme0n1_full.img.zst"

# --- serve -----------------------------------------------------------------

serve:
	ssh $(ORIN) 'cd $(REMOTE) && ./scripts/serve.sh'

clean:
	rm -rf __pycache__ src/**/__pycache__ eval/runs
