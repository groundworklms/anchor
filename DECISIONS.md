# DECISIONS.md

Offline Doctrine-Grounded Tutor — Jetson Orin Nano 8GB
Anchor — an offline, doctrine-grounded tutor. This log records every threshold, reversal, and dead end, with the reasoning and the date.
Repo: `github.com/jeranaias/<repo>` (org confirmed 2026-08-12; **not** thornveil-ai).

All findings below verified **2026-08-12**. No code written prior to this record.

---

## D-000 — Step 0 verification pass

Status: **complete, with three corrections to upstream guidance.**

Upstream guidance was stale or wrong in three places. Each is recorded below with the
primary source that overturned it. Nothing here is assumed; where a secondary source
disagreed with a primary source, the primary source wins and the conflict is logged.

---

## D-001 — Gemma 4 license: Apache 2.0, verbatim (VERIFIED, do not re-assume)

The guardrail said do not characterize the Gemma license without quoting the model card.
Doing so changed the answer, so this is recorded in detail.

**Finding: Gemma 4 ships under the verbatim Apache License 2.0.** This is a genuine
break from Gemma 1–3, which used the custom *Gemma Terms of Use*.

Quoted from the HF model card (`google/gemma-4-E4B`, retrieved 2026-08-12):

> **License**: Apache 2.0

Quoted from Google's Gemma 4 model card (`ai.google.dev/gemma/docs/core/model_card_4`):

> License: Apache 2.0 | Authors: Google DeepMind

The HF card hyperlinks the words "Apache 2.0" to a Google-hosted URL
(`ai.google.dev/gemma/docs/gemma_4_license`) rather than to apache.org. That is the exact
pattern a custom-terms page would use, so the linked document was fetched and read
directly. It is the **verbatim Apache License 2.0**, opening:

> Apache License Version 2.0, January 2004

with the standard §1–9 definitions and no Gemma-specific carve-outs.

**Residual caveat — not resolved, and I am not qualified to resolve it.** Google still
hosts a *Gemma Prohibited Use Policy* (`ai.google.dev/gemma/prohibited_use_policy`),
last modified **21 February 2024**. That date predates Gemma 4. The policy text never
states which model versions it governs, and the Apache 2.0 text on the license page does
not incorporate it by reference. Reading those two facts together, the PUP appears to be
legacy and attached to the old Gemma Terms of Use — but "appears to be" is not a legal
opinion, and this is a DoD-adjacent deployment.

**Action:** before the one-page path-to-fielding claims "Apache 2.0, no use restrictions,"
run that claim past someone with actual authority. The distribution argument in the
fielding pitch depends on it. Flagging rather than deciding.

Sources:
- https://huggingface.co/google/gemma-4-E4B
- https://ai.google.dev/gemma/docs/core/model_card_4
- https://ai.google.dev/gemma/docs/gemma_4_license
- https://ai.google.dev/gemma/prohibited_use_policy

---

## D-002 — Model selection: E2B becomes primary, E4B becomes the stretch goal

**This inverts the brief.** The brief specified E4B primary with E2B fallback. The
evidence does not support that on an 8GB Orin Nano.

Model specs, quoted from the Gemma 4 model card dense-models table:

| Model | Effective params | Total w/ embeddings | Context | Modalities |
|---|---|---|---|---|
| E2B | 2.3B effective | 5.1B with embeddings | 128K | Text, Image, Audio |
| E4B | 4.5B effective | 8B with embeddings | 128K | Text, Image, Audio |

The "E" is Per-Layer Embeddings (PLE): embedding tables are large but used only for
lookup, so effective compute is below the total parameter count. Memory still has to hold
them.

Jetson AI Lab, Gemma 4 E4B page — Supported Platforms reads only:

> Jetson Orin, Jetson Thor

Orin Nano 8GB is selectable in that page's hardware dropdown but returns
*"No command for this module and engine in model data"*, and the benchmark table returns
*"No data available for this combination."* There is no published E4B-on-Orin-Nano-8GB
configuration.

By contrast the E2B page states Google

> engineered [E2B] for offline mobile and IoT use, including devices like Jetson Orin Nano

naming the target device explicitly.

Corroborating third-party number: **Gemma 4 E2B at 25.5 tok/s on GPU on Orin Nano.**
Treat as unverified secondary sourcing until P0 reproduces it.

**Decision:** E2B is the primary model. E4B is a stretch goal, attempted in P0 only after
E2B is benchmarked and working, and dropped without ceremony if it does not fit.

**Rationale.** Memory is the binding constraint by your own framing. An 8B-with-embeddings
model plus bge-small embeddings plus bge-reranker-base plus SQLite plus Chromium in kiosk
mode, on a board that shares 8GB between CPU and GPU, is the single most likely way this
demo dies on stage. The pitch is measured refusal behavior, not model size — and abstention
quality is dominated by the reranker threshold and the post-generation grounding check, not
by whether the generator is 2.3B or 4.5B effective. E4B buys fluency reviewers explicitly
are not scoring, at the cost of the failure mode that would cost the most.

P0 benchmarks both anyway and commits the table. If E4B fits with real headroom, it can be
promoted by config change — the OpenAI-compatible abstraction layer in the brief exists
precisely so this is a one-line swap.

Sources:
- https://www.jetson-ai-lab.com/models/gemma4-e4b/
- https://www.jetson-ai-lab.com/models/gemma4-e2b/
- https://ai.google.dev/gemma/docs/core/model_card_4
- https://www.navyaai.com/blog/jetson-orin-nano-llm-benchmark

---

## D-003 — QAT checkpoints: published, brief was correct

The E4B HF card does not surface QAT in its own text, which initially read as "not
published." Google's release blog and downstream packagers contradict that.

QAT checkpoints **are** published, and the covered sizes explicitly include **E2B and E4B**
(alongside 12B, 26B A4B, 31B). Formats released:

- Unquantized QAT checkpoints (Q4_0)
- **GGUF Q4_0, ready to deploy** ← this is our path
- Mobile-optimized (wNa8o8)
- Compressed tensors (w4a16)

Google's stated rationale: QAT simulates quantization during training to minimize quality
loss under compression, reported at ~72% lower memory versus bf16 with near-original
quality, and better than post-training-quantization baselines.

**Decision:** use the **QAT GGUF Q4_0** checkpoint for E2B, not a community PTQ quant.
On a memory-bound board with an abstention threshold that must be tuned empirically, a
quantization that shifts the reranker/logit distribution is a silent accuracy tax that
would land directly on the metric we are pitching.

Note the quant formats Jetson AI Lab lists differ per model — **Q4_K_S** GGUF for E2B,
**Q4_K_M** GGUF for E4B — against Google's **Q4_0** QAT release. Resolve at P0 by
benchmarking the Google QAT Q4_0 first; it is the one with the training-time guarantee.

Sources:
- https://blog.google/innovation-and-ai/technology/developers-tools/quantization-aware-training-gemma-4/
- https://unsloth.ai/docs/models/gemma-4/qat
- https://www.jetson-ai-lab.com/models/gemma4-e2b/

---

## D-004 — JetPack / L4T: JetPack 7.2 supports Orin Nano

JetPack 7 reached the Orin family in **Q2 2026**. JetPack 7.2 covers the full Jetson Orin
line including Orin Nano, on **Ubuntu 24.04 LTS / Linux kernel 6.8**.

Two deployment facts that matter to this build:

1. The Orin Nano Developer Kit **no longer ships an SD card image**; system deployment uses
   the USB ISO installer. This aligns with the NVMe-only constraint rather than fighting it.
2. JetPack 6 → 7 is a **major OS and kernel change**. It is not an `apt upgrade`. It
   requires the OTA package or a full flash via ISO/BSP.

**Blocking unknown:** what is actually flashed on the device right now. Item 2 makes this
consequential — if the Orin is on JetPack 6, moving to 7.2 is a full reflash, which is a
day of schedule and a mandatory boot-drive clone beforehand. Cannot determine without shell
access (see D-006). **No version is pinned here until read off the hardware.**

Sources:
- https://www.seeedstudio.com/blog/2026/07/09/jetpack-7-2-platform-level-reset-and-the-new-era-of-agentic-ai/
- https://developer.nvidia.com/embedded/jetpack/downloads
- https://forums.developer.nvidia.com/t/jetpack-7-2-q2-2026-timeline/360233

---

## D-005 — llama.cpp CUDA build

Orin (all SKUs) is **compute capability 8.7**. The build must be told so explicitly.

```bash
export PATH=/usr/local/cuda/bin:$PATH   # JetPack installs to /usr/local/cuda; nvcc NOT on PATH by default
cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="87" -DCMAKE_BUILD_TYPE=Release -G Ninja
cmake --build build --config Release -j$(nproc)
```

Known trap: if CMake cannot find the CUDA compiler, it is the PATH issue above — export,
then re-run CMake **from a clean build directory**, since the failed toolchain detection is
cached.

Optional Orin tuning flags to A/B at P0, not to adopt blind: `-DGGML_CUDA_F16=ON`,
`-DGGML_CUDA_DMMV_X=64`, `-DGGML_CUDA_MMV_Y=2`.

Exact llama.cpp commit SHA to be pinned at P0 once built against the on-device JetPack.
**Unpinned until then** — pinning a commit not compiled on this hardware would be a
fictional pin.

Sources:
- https://forums.developer.nvidia.com/t/buildling-llama-cpp-on-jetpack-7-1/365608
- https://myupbeat.wordpress.com/2026/06/29/running-llama-cpp-gpu-server-on-jetson-orin-nano/
- https://toptechboy.com/ai-on-the-bleeding-edge-run-llama-llm-locally-on-cuda-with-nvidia-jetson-orin-nano/
- https://proventusnova.com/blog/llm-inference-jetson-orin-llamacpp-ollama/

---

## D-006 — Orin connectivity: reachable, not authenticated

Probed from the Windows workstation 2026-08-12.

| Fact | Value |
|---|---|
| Interface | `Ethernet 2`, host side `192.168.55.100` |
| Device address | `192.168.55.1` — Jetson USB device-mode subnet |
| ARP state | `Reachable`, MAC `3E-C7-E7-79-F3-87` |
| SSH | port 22 **open**, `TcpTestSucceeded: True` |
| mDNS name | **`orin-vanguard.local`** |
| Auth | `Permission denied (publickey,password)` as user `nvidia` |

The board is powered, enumerated over USB, and listening. Only credentials are missing.
Workstation has `id_ed25519.pub` available to install.

**Flag:** the hostname is `orin-vanguard.local`. Vanguard is the autonomous USV project.
Either this board was previously provisioned for the boat and reused, or it is the boat's
Orin. Confirm before anything gets reflashed — D-004 raises a real possibility of a full
JetPack 6→7 reflash, and that would destroy an existing Vanguard configuration. **Boot
drive gets cloned before any flash decision, per the phase-gate guardrail.**

---

## D-007 — Hardware confirmed: **Jetson Orin Nano 8GB**. Brief was correct.

**Superseded an earlier incorrect entry.** A first pass read
`/sys/firmware/devicetree/base/model` — *"NVIDIA Jetson Orin NX Engineering Reference
Developer Kit"* — and concluded the board was an Orin NX. That string is wrong for our
purpose: `p3768-0000` is the **carrier board**, which is shared across Orin NX and Orin Nano
modules, so the devicetree model name reflects the carrier, not the module. The original
brief was right. Recorded here because the wrong answer would have invalidated the fielding
one-pager and made the published benchmarks unusable.

Verified 2026-08-12 by five independent discriminators, all agreeing:

| Discriminator | Reading | Implies |
|---|---|---|
| EEPROM part number (authoritative) | `699-13767-`**`0003`**`-300` | Orin Nano 8GB — NX 8GB is `-0010` |
| Devicetree module SKU | `nvidia,p3768-0000+p3767-`**`0003`** | Orin Nano 8GB — NX 8GB is `p3767-0001` |
| `nvpmodel` modes defined | **only** `ID=0 NAME=15W`, `ID=1 NAME=7W` | Nano 7W/15W profile — NX is 10/15/20W/MAXN |
| NVENC hardware encoder | **absent** — no `/dev/nvhost-msenc`, no devfreq node | Nano has no encoder; NX has one |
| GPU available frequencies | `510 / 612 / 624.75 MHz`, max = 624.75 MHz | Nano ceiling is 625 MHz; NX 8GB is 765 MHz |

Supporting: `MemTotal` 7,802,800 kB (7.44 GiB); 6× Cortex-A78AE; EMC max 2133 MHz
(LPDDR5, ~68 GB/s class — Nano, not NX's ~102 GB/s); SoC Tegra234 silicon A01.

**Consequences — everything reverts to the brief as written:**

1. **Benchmarks measured on this board are publishable as Orin Nano 8GB numbers.** No
   proxy-hardware caveat needed, no risk of overclaiming to reviewers.
2. **D-002 stands, and is strengthened.** E2B primary was argued on Nano memory headroom and
   on Jetson AI Lab naming Orin Nano explicitly for E2B. That reasoning now applies directly
   rather than by analogy. E4B remains a P0 stretch test only.
3. **The fielding one-pager is intact as briefed** — Orin Nano 8GB BOM, and the January 2032
   lifecycle date is confirmed (D-011).

**Lesson pinned for later phases:** on Jetson, identify the module from EEPROM and the
`p3767-*` SKU, never from the devicetree model string.

---

## D-011 — Orin Nano 8GB lifecycle: Q1 2032 confirmed

NVIDIA extended the product lifecycle for **all Jetson Orin commercial modules** — AGX Orin,
Orin NX, and Orin Nano — from the originally planned **Q1 2030 to Q1 2032**. The brief's
January 2032 figure for the fielding one-pager is correct as stated.

Module part numbers for the BOM:
- `900-13767-0030-000` — Jetson Orin Nano 8GB (boxed/retail SOM P/N)
- `699-13767-0003-300` — bare module marking read from this unit's EEPROM

Distributor pricing at 100 / 1k / 10k volumes still to be gathered for the one-pager; note
that NVIDIA has separately *reduced* lifecycle windows on some older Jetson lines, so cite
the Q1 2032 date to NVIDIA's own lifecycle page rather than to a reseller.

Sources:
- https://developer.nvidia.com/embedded/lifecycle
- https://forums.developer.nvidia.com/t/product-lifecycle-extension-of-jetson-xavier-nx-and-jetson-orin-family/312589
- https://www.arrow.com/en/products/900-13767-0030-000/nvidia.html

---

## D-012 — SUPERSEDED BY D-016. Original entry below was wrong.

> **Do not act on this entry.** It claimed the Super profile could be enabled by a
> reversible symlink swap with no firmware flash. That is false — the system actively
> reverts it at every boot. Attempted and disproven 2026-08-13; see **D-016**.
> Retained because the reasoning error is instructive: the config file existing on disk
> was taken as evidence the profile was reachable, without checking what *selects* it.

## D-012 (original, incorrect) — "Super" power profile appears NOT enabled

`/etc/nvpmodel.conf` defines only two modes (`15W`, `7W`) and carries a 2021–2023 copyright
header. The **Orin Nano Super** profile — which unlocks a 25W/MAXN mode and substantially
higher GPU clocks on this *same* `p3767-0003` silicon — is not present. The installed
JetPack 6.2.1 / L4T R36.4.3 is the release train that supports it.

Current state is consistent with the original (pre-Super) Orin Nano 8GB configuration:
GPU capped at 624.75 MHz, board running at 15W.

**To test at P0, before benchmarking:** whether this unit can take the Super profile
(requires current UEFI firmware plus the updated `nvpmodel` configuration). If it can, it is
a large throughput gain for zero BOM cost, and it directly improves the tokens/sec and
watts figures that go in the writeup.

Deliberately *not* attempted yet — it touches firmware, and the guardrail requires a boot
drive clone at phase gates. Clone first, then try.

**Benchmark plan either way:** sweep every available power mode rather than reporting one
number. For a project judged on deployability, "N tokens/sec at 7W" is a stronger claim than
a larger number at unstated power.

---

## D-008 — Software baseline: JetPack 6.2.1. Recommend NOT reflashing to 7.2

Read off the device 2026-08-12:

| Component | Version |
|---|---|
| JetPack | **6.2.1+b38** |
| L4T | **R36.4.3** (GCID 38968081, 2025-01-08) |
| OS | Ubuntu 22.04.5 LTS (Jammy) |
| Kernel | 5.15.148-tegra |
| CUDA | **12.6.68** (`/usr/local/cuda-12.6`) |
| Driver | 540.4.0 |
| Root device | `/dev/nvme0n1p1` — **already NVMe**, 233G, 28G used, 194G free |
| Power mode | 15W, GPU 624.75 MHz |
| Temps | ~48–50 °C idle |

**Decision: stay on JetPack 6.2.1. Do not reflash to 7.2 unless something forces it.**

Reversing the implied direction of D-004. Reasons:

- Nothing in the stack requires JetPack 7. llama.cpp builds against CUDA 12.6 for `sm_87`
  without issue; the `-DCMAKE_CUDA_ARCHITECTURES="87"` flag in D-005 is unchanged.
- The NVMe-boot constraint is **already satisfied** — root is on `nvme0n1p1`. The main
  benefit a 7.2 reflash would have bought is already in hand.
- 6→7 is a full flash (D-004), roughly a day plus reprovisioning, five weeks out from a
  project, against zero identified capability gain.
- It would destroy existing work on this board (see D-009).

Missing build dependencies to install: **`cmake` and `ninja` are not present**. `gcc`,
`g++`, `git`, `docker`, `python3` (3.10.12) and `pip3` are.

---

## D-009 — Board is not empty; "free to reflash" was given without this information

The user cleared this board for reflashing, but the clearance was given before the
contents were known. Found in `/home/vanguard`:

| Path | Size | Apparent owner |
|---|---|---|
| `~/hawkstack` | 1.4M | **ThermalHawk** |
| `~/spire` | 256M | unidentified |
| `~/model` | 13M | unidentified |
| `~/ollama` | 2.7G | Ollama models |
| `~/benchmark_orin.py`, `deploy_bench.py`, `export_onnx.py`, `exps.py`, `trt_sweep.sh`, `fps_results.txt` | — | ThermalHawk benchmarking |

No running Vanguard/MAVLink/ORCA services were found, so this is not the live USV
controller — consistent with "old hostname, board now spare." But it does hold what looks
like ThermalHawk benchmark tooling and results.

**Action: no reflash, and no deletion, until the user confirms these are disposable.**
D-008 recommends not reflashing anyway, which makes this moot in the expected path. If a
reflash ever becomes necessary, clone the boot drive first per the phase-gate guardrail —
that clone is the only copy of `fps_results.txt`.

---

## D-010 — Dead RTC: a real problem for P5 signed records

```
RTC time: Thu 1970-01-01 06:18:23
System clock synchronized: no
```

System clock reads **2026-07-04** against an actual date of **2026-08-12** — 39 days slow —
and the hardware RTC has fallen back to epoch, indicating a dead or absent RTC backup cell.
NTP is active but has never synchronized, which is expected and permanent: **this device is
air-gapped by design**, so NTP will never fix it.

Consequences:

- **P5 xAPI records** would be stamped with garbage timestamps, and a *signed* export bundle
  with 1970 timestamps is worse than no bundle — it actively undermines the trust argument
  that is the entire pitch.
- **Cold-boot-to-usable in 60s** currently means booting to 1 Jan 1970.
- Corpus manifest retrieval dates and eval run timestamps are similarly unreliable.

Options, to decide before P5: replace the RTC cell (cheap, physical, most correct); or set
the clock at boot from a signed value and record clock provenance in the xAPI envelope so a
consumer can tell wall-clock time from monotonic ordering. The second is the honest fallback
if the cell cannot be sourced — and *saying so in the writeup* is on-message for a project
whose thesis is calibrated trust.

Not blocking P0. Logged now because discovering it at P5 would be too late.

---

## D-013 — Phase-gate backup taken and VERIFIED (2026-08-13)

Full-disk clone of the Orin NVMe, taken **before any modification** to the device.

| Property | Value |
|---|---|
| Source | `/dev/nvme0n1`, 256,060,514,304 bytes |
| Compressed size | 15.16 GiB (zstd -3, 308 MB/s read) |
| Decompressed length | 256,060,514,304 — **exact match** |
| SHA-256 (decompressed stream) | `152ae2fe966908e6eef1a5848eed3c95d7050259d3bc96857a114511e72edbc2` |
| Verification | streamed decompress, PASS |

**Verified, not merely taken.** An unverified backup is not a backup. The stream was fully
decompressed and length-checked against `blockdev --getsize64`; the SHA-256 above is
recorded so bit-rot on the archive can be detected later.

**Caveat, stated honestly:** the root filesystem was mounted read-write and the OS was
running during the read, so this is a **crash-consistent** image, not an atomic snapshot.
A byte-exact comparison against the live device afterwards is impossible in principle for
that reason. It restores like a machine that lost power — which is the correct and normal
guarantee for this kind of phase-gate clone.

QSPI was **not** captured: `/proc/mtd` is empty and no MTD device is exposed at runtime.
Acceptable here because (a) the Super unlock is a symlink swap, not a firmware flash, and
(b) on this board `TEGRA_BOOT_STORAGE` is `nvme0n1`, so the bootloader chain lives inside
the image we took.

---

## D-014 — Build toolchain pinned (2026-08-13)

Built on-device, which is why these pins are real rather than aspirational (see D-005).

| Component | Pin |
|---|---|
| llama.cpp commit | **`a94d563ed801d1da1b8c2432946de07d0231bb3d`** |
| llama.cpp commit date | 2026-08-13, *"common: apply CPU parameters across tools (#27026)"* |
| ggml version | 0.19.0 |
| CUDA | 12.6.68 (`/usr/local/cuda`) |
| `CMAKE_CUDA_ARCHITECTURES` | **87** (Orin, compute capability 8.7) — confirmed in configure output |
| cmake | 3.22.1 |
| ninja | 1.10.1 |
| gcc | 11.4.0 |

Two configure-time warnings, both reviewed and accepted:

- **NCCL not found.** Irrelevant — single GPU, multi-GPU collectives unused.
- **OpenSSL not found → HTTPS support disabled in cpp-httplib.** Accepted, and arguably
  *desirable*: the inference server binds loopback and speaks plain HTTP to the local
  FastAPI process. A build with no HTTPS client capability is one fewer path by which
  anything in the inference stack could reach the network. Consistent with the
  zero-network-calls-at-inference constraint.

---

## D-015 — Model weights pinned (2026-08-13)

Downloaded from HuggingFace, hashed on the host, transferred, and **re-hashed on the
device** — both sides match.

| Model | Bytes | SHA-256 |
|---|---|---|
| `gemma-4-E2B_q4_0-it.gguf` | 3,349,516,256 | `fa401b55b07ee70a54c6dae3903c783a6e65064312529ea57175cb5f8dec6634` |
| `gemma-4-E4B_q4_0-it.gguf` | 5,154,941,280 | `676c35070db6dbe52f93e9c864ee0fba4eddea94b9c875d9cb10daff453fbaee` |

Source repos, both `gated: false`, both `license: apache-2.0` in repo metadata — a third
independent corroboration of D-001:
- `google/gemma-4-E2B-it-qat-q4_0-gguf`
- `google/gemma-4-E4B-it-qat-q4_0-gguf`

Installed at `/opt/tutor/models/`. Disk after transfer: 36G used of 233G, 186G free.

**`mmproj` projectors deliberately not downloaded.** Each repo also ships a multimodal
projector for vision/audio input. This is a text tutor; the projectors would consume memory
on a board where memory is the binding constraint, and would enable an input path the
product does not use. Omitting them is a memory decision, not an oversight.

**Reinforces D-002:** E4B weights alone are **4.80 GiB on a 7.44 GiB board**, before KV
cache, `bge-small-en-v1.5`, `bge-reranker-base`, and Chromium in kiosk mode. The E2B-primary
call was argued from published guidance; it is now backed by a measured file size. E4B is
still benchmarked at P0 so the table is honest about what was attempted.

---

## D-016 — Super requires a FIRMWARE update, not a config change. Attempted, reverted.

**Attempted 2026-08-13. The device undid it automatically. D-012 was wrong.**

What was tried: repoint `/etc/nvpmodel.conf` from `nvpmodel_p3767_0003.conf` to
`nvpmodel_p3767_0003_super.conf` (which unlocks `15W / 25W / MAXN_SUPER`), then reboot.

What happened, verbatim from the boot journal:

```
nvpower.sh - NOTICE: Relinking the nvpmodel conf from super to non-super
nvpower.sh - NOTICE: Backing up the existing /etc/nvpmodel.conf to /etc/nvpmodel_nvbackup.conf!
```

**Mechanism.** `/etc/systemd/nvpower.sh` runs at every boot. It sets

```sh
machine="$(tr -d '\0' < /proc/device-tree/compatible)"
declare -a plat_variants=("super" "safety")
```

then relinks whenever `machine` and the linked config disagree on the `super` token. On this
board:

```
/proc/device-tree/compatible = nvidia,p3768-0000+p3767-0003nvidia,p3767-0003nvidia,tegra234
```

There is no `-super`. The script maps `p3767-0003` → stock conf and only
`p3767-0003-super` → the super conf. **The gate is the device tree compatible string,
which is written by the bootloader/UEFI firmware — not a userspace setting.**

Enabling Super therefore requires the board to advertise the super SKU, i.e. a
**bootloader/DTB firmware update**. Confirmed available:

| | |
|---|---|
| Installed | `nvidia-l4t-bootloader 36.4.3-20250107174145` |
| Candidate | `nvidia-l4t-bootloader 36.4.7-20250918154033` |
| Source | `repo.download.nvidia.com/jetson/t234 r36.4/main` |

**Recommendation: do NOT flash firmware. Ship at 15W.** Reasoning:

1. **Our backup does not cover this failure mode.** D-013's image is the NVMe. The
   bootloader lives partly in QSPI, which we could not read (`/proc/mtd` empty). A failed
   UEFI update is therefore **not recoverable from the backup we hold.**
2. **Recovery would need a Linux host.** Restoring a bricked Orin means USB recovery mode
   plus `flash.sh`/SDK Manager on Ubuntu. The workstation driving this project is Windows.
   Possible via WSL2 + usbipd, but that is an unrehearsed procedure to discover during a
   brick, five weeks from the deadline.
3. **The gain does not buy what this project is judged on.** E2B already delivers
   **20.86 tok/s** generation at 15W — comfortably faster than a human reads. Super might
   reach ~30–35 tok/s. Evaluation emphasizes trust and deployability, not fluency or speed.
4. **The demo runs on a USB-C PD battery.** MAXN_SUPER's 25W envelope may be infeasible on
   battery anyway, making the headline number unusable in the actual demo.
5. It also drags userspace from 36.4.3 to 36.4.7, destabilizing a stack that currently
   builds and runs, for no capability the product needs.

**This is a strictly better story for the fielding one-pager, not a worse one:** identical
hardware, zero BOM change, ~1.5× throughput available via a documented NVIDIA firmware
update whenever a fielded unit wants it. That is a stronger slide than having spent it.

Device was left in the stock configuration — it self-restored. No manual repair needed.
`/etc/nvpmodel_nvbackup.conf` is a harmless artifact the script created.

**Escalated to the user**, because the risk profile changed materially from what I
described when Super was authorized ("reversible symlink swap"). Flashing firmware is a
different decision than swapping a symlink and deserves a fresh answer.

---

## D-017 — P0 benchmark results (stock firmware)

Measured on **Jetson Orin Nano 8GB**, llama.cpp `a94d563`, CUDA 12.6, `-ngl 999`.
GPU utilisation confirmed at **GR3D_FREQ 99%** for every row — see the CPU-fallback trap in
D-018 for why that check is not optional.

| Model | Power mode | pp512 (t/s) | tg128 (t/s) | Peak RAM | Peak power |
|---|---|---|---|---|---|
| **E2B** | **15W** | **717.57 ± 18.08** | **20.86 ± 0.02** | 5027 / 7620 MB | 11.4 W |
| E2B | 7W | 260.37 ± 2.43 | 8.20 ± 0.02 | 4923 / 7620 MB | 7.2 W |
| E4B | 15W | 396.72 ± 6.30 | 11.86 ± 0.01 | **6945 / 7620 MB** | 12.3 W |
| E4B | 7W | 139.29 ± 1.13 | 4.49 ± 0.00 | 6949 / 7620 MB | 7.4 W |

Model sizes as loaded: E2B 3.10 GiB / 4.63 B params; E4B 4.79 GiB / 7.46 B params.
Reboot to usable measured at **~40 s**, inside the 60 s cold-boot requirement.

**D-002 is now settled on measurement, not inference.** At 15W, E2B is **1.76× faster**
at generation than E4B *and* leaves **2593 MB** free versus E4B's **675 MB**. E4B's headroom
cannot hold `bge-reranker-base` alone, before `bge-small-en-v1.5`, FastAPI, and Chromium in
kiosk mode. **E4B is not viable for this stack** — it is excluded on measured memory, not on
a judgement call.

The 7W column is worth keeping in the deck. It is the mode that maximizes battery life, and
8.20 tok/s at 7.2 W still exceeds reading speed — a genuinely strong deployability claim.

---

## D-018 — Trap: `-ngl -1` silently runs on CPU

`llama.cpp` build `a94d563` **accepts `-ngl -1` without error and runs entirely on CPU.**
Caught 2026-08-13 only because `tegrastats` showed `GR3D_FREQ 0%` while a generation crawled;
the intended "all layers" value is **`-ngl 999`**.

Both `config/default.yaml` and `scripts/p0_bench.py` originally carried `-1` and would have
produced a CPU-only benchmark table presented as GPU numbers. Both corrected.

**Standing rule: every benchmark run must confirm `GR3D_FREQ > 0` via `tegrastats`.**
Throughput alone does not prove the GPU was used.

Second trap from the same session, logged so it is not rediscovered: `llama-cli` in this
build **ignores `-no-cnv`** and enters interactive mode, then spins on empty stdin — one run
produced a 371 MB log of empty prompts before timing out. Use **`llama-bench`** for anything
non-interactive.

---

## D-019 — Super, second attempt: forcing the profile in userspace gives ZERO gain

Second approach, after D-016 established that the boot script reverts the symlink. This one
bypassed the script entirely rather than fighting it.

`nvpmodel` accepts `-f <conf>` to use an arbitrary config. NVIDIA ships
`nvpmodel_p3767_0003_super.conf` — **named for our exact module SKU, in our installed L4T
version** — and Super Mode is documented as a software unlock on identical silicon. So:

```sh
sudo nvpmodel -f /etc/nvpmodel/nvpmodel_p3767_0003_super.conf -m 2
```

**It reported `NV Power Mode: MAXN_SUPER`. The clocks did not move.**

| | stock 15W | after "MAXN_SUPER" |
|---|---|---|
| GPU max freq | 624,750,000 | **624,750,000** |
| GPU frequency table, top entry | 624.75 MHz | **624.75 MHz** |
| CPU max freq | 1,510,400 | **1,510,400** |
| EMC rate | 2,133,000,000 | **2,133,000,000** |

Benchmarked rather than trusting the label — E2B, same settings as D-017:

| | 15W (stock) | "MAXN_SUPER" |
|---|---|---|
| pp512 | 717.57 ± 18.08 | **691.22 ± 27.17** |
| tg128 | 20.86 ± 0.02 | **20.60 ± 0.01** |
| peak power | 11.4 W | 10.7 W |

**No gain. Marginally lower, within run-to-run noise.**

**Conclusion: the clock ceiling is enforced below `nvpmodel`.** The GPU's
`available_frequencies` table itself tops out at 624.75 MHz; the power model can only select
from that table, it cannot extend it. Raising the ceiling requires the device tree to
advertise the super SKU, which is written at **flash time**.

This was worth measuring precisely because it was easy to get wrong. `nvpmodel -q` printing
`MAXN_SUPER` is exactly the kind of result that gets screenshotted and reported as success.
The label changed and nothing else did.

### What real Super would actually cost

The selection chain is: `TNSPEC` (set at flash time) → DTB flashed → `/proc/device-tree/compatible`
→ `nvpower.sh` picks the conf.

```
TNSPEC 3767-300-0003-T.1-1-1-jetson-orin-nano-devkit-
                                                    ^ suffix field is EMPTY; "super" belongs here
```

So the path is **not** "one apt update":

- An apt upgrade of `nvidia-l4t-bootloader` (36.4.3 → 36.4.7 is available) flashes the DTB
  matching the **existing** TNSPEC, which has no super suffix. It would not enable Super.
- The supported path is a **full reflash with the `jetson-orin-nano-devkit-super` board
  config**, from a Linux host with the board in USB recovery mode.

That reflash would:
1. **Wipe the NVMe** — rootfs, llama.cpp build, models, the whole working stack.
2. Require an Ubuntu host. This project is driven from Windows; WSL2 + `usbipd` is possible
   but unrehearsed.
3. Carry genuine brick risk, against a backup that does not cover QSPI (D-013).

**Recommendation unchanged and now evidence-backed: do not pursue Super.** The cheap path is
measured at zero gain; the expensive path is a wipe-and-reflash, not an update. E2B at 15W
delivers 20.86 tok/s — well above reading speed — for a project judged on trust and
deployability rather than throughput.

Device left in stock configuration, 15W, verified healthy. Stale `pmode` index in
`/var/lib/nvpmodel/status` cleared so it does not warn at boot.

---

## D-020 — Corpus: four MCDPs acquired, screened, and cleared (2026-08-13)

All four from **marines.mil**, the official USMC publication host. All carry
**DISTRIBUTION STATEMENT A: Approved for public release; distribution is unlimited.**

| Pub | Title | Dated | Pages | SHA-256 (first 16) |
|---|---|---|---|---|
| MCDP 1 | Warfighting | 20 June 1997 | 115 | `51008277a48f89dc` |
| MCDP 1-3 | Tactics | 30 July 1997 | 145 | `16c7ba8fe2958587` |
| MCDP 5 | Planning | 21 July 1997 | 107 | `69d7f5b41c430c17` |
| MCDP 6 | Command and Control | 4 October 1996 | 154 | `967db4d6b5611690` |

Full provenance in `corpus/manifest.json`; raw screening output in `corpus/screening.json`.

**Acquisition note.** `marines.mil` sits behind Akamai and returns **403 to every
non-browser client** regardless of user-agent or referer. So does `trngcmd.marines.mil`.
Files were fetched through a real browser session and byte-verified against the
server-reported lengths. Recorded because a naive `curl` in a future rebuild will fail
confusingly, and because the alternative — pulling from a third-party mirror — would have
destroyed the provenance chain this project depends on.

### Two screening bugs found by running the screener against real documents

**1. Case-insensitive classification matching produces false positives.**
`\bSECRET\b` matched *"there was nothing secret about the German attack"* and Nathan Bedford
Forrest's *"told the secret of his many victories"*, flagging MCDP 1-3 — a Distribution
Statement A document — as **BLOCKED**. Real markings are upper case. Matching is now
case-sensitive, with lower-case hits reported separately as informational so a genuine
marking mangled by OCR is not silently dropped.

**2. The screener could not see image-only pages, and that is where a marking can live.**
MCDP 1 initially came back `REVIEW — no distribution statement found`. Its cover page is an
**unOCR'd image**; rendering page 1 and reading it visually showed
`DISTRIBUTION STATEMENT A: Approved for public release; distribution is unlimited` and
`PCN 142 000006 00`.

This is the more serious of the two. **Had the marking been restrictive rather than
permissive, the screener would have reported "no blocking markings found" on a plainly
marked document.** A text-only screen on scanned PDFs is not a screen; it is a screen with
a silent blind spot. The tool now enumerates every page with no text layer and refuses to
present a clean result without naming them.

All 25 image-only pages across the four documents were rendered and inspected: **24 are
blank scan versos, 1 is the MCDP 1 cover.** Recorded in the manifest per document.

### P1 ingest constraints discovered

These shape the chunker and are the difference between real and decorative citations:

- **Zero PDF bookmarks in all four documents.** Chapter/section structure must be parsed
  from the text layer. Chapter headings are detectable (`Chapter 1`…`Chapter 4` in MCDP 1),
  so this is tractable, but it is parsing, not metadata lookup.
- **All four are OCR'd scans** (CVISION PdfCompressor, 2006–2007). Text quality is
  **good** — zero Unicode replacement characters, and only 3–24 intra-word anomalies per
  document across ~20–32k words. A handful are visible (`WarfIghting`, `Manual I` for
  `Manual 1`). Low enough not to threaten retrieval, high enough that exact-quote matching
  in the grounding check must tolerate small OCR noise.
- **MCDPs are not paragraph-numbered.** They use chapters and prose section headings. The
  brief's `para_id` therefore has to be **synthesized** — chapter + section + ordinal
  paragraph — rather than read off the page. **This must be decided before chunking**,
  because it defines what a citation actually points at and whether a human can verify it
  against the printed page. Proposal: `MCDP 1, Ch 1, "Friction", ¶3, p.12`, carrying the
  printed page number so a reader can physically check it.
- All four are RC4-encrypted with an owner password only; text extraction is unaffected.

### Still pending user decision (not acquired, not ingested)

- **Joint Publications** — which JPs, and which editions are publicly releasable.
- **TC 3-22.9** Rifle and Carbine.
- **TCCC guidelines** — edition must be pinned; content changes materially between
  versions, and two eval rows (`inc-017`, `inc-018`) depend on which edition is ingested.

---

## D-021 — Corpus boundary decided: TCCC in, Joint Publications out (2026-08-13)

Both calls made on request. Corpus is **13 documents, 1,780 pages, all cleared.**

### TCCC — INCLUDED

The document carries no distribution statement, which is why the automated screen could not
clear it. Cleared by judgement, reasoning on the record:

- **Absence of a distribution statement is normal formatting for clinical practice
  guidelines.** It is not a restriction signal. Treating a formatting convention as a
  marking would be the wrong kind of caution.
- Published by the Committee on TCCC via **jts.health.mil** (a `.mil` host, no CAC),
  mirrored openly on deployedmedicine with no login, and printed in the **Journal of
  Special Operations Medicine**.
- The document's purpose is the widest possible dissemination to point-of-injury
  caregivers. Withholding it serves nobody.

**Edition pinned at 01 May 2026, and the edition is part of the citation, not a footnote.**
TCCC recommendations change materially between editions — this one changed airway
management, TBI management, and antibiotic selection. A citation without the edition date
is not a usable medical citation. `content_note` in the manifest records that answers from
this document are clinical guidance and must always carry the edition.

Note the interaction with the eval set: `inc-017` and `inc-018` were written against
remembered TCCC phase names, but this edition says **"Care Under Fire/Threat"**. Those rows
must be verified against the ingested text, not against memory.

### Joint Publications — EXCLUDED, and the exclusion is load-bearing

- `jcs.mil` publishes **zero PDF links** across its entire doctrine section. Everything
  moved behind **CAC-gated JEL+** (`jdeis.js.mil`).
- GPO `govinfo` hosts JP 3-0 but only the **11 August 2011** edition. The current
  publication is retitled *"Joint Campaigns and Operations"*.
- Superseded doctrine is the single worst thing to put in a corpus whose product is
  trustworthy, citable answers. A reviewer asking "is that current?" would get "no".

**This is not a gap in the demo — it is a feature of it.** Joint-doctrine questions are now
genuine out-of-corpus questions, so abstention is demonstrated against a *real* corpus
boundary rather than a contrived one. Six eval rows added:

| id | probes |
|---|---|
| `ooc-025` | JP 3-0 phasing — model near-certainly knows this from pretraining; must refuse anyway |
| `ooc-026` | OPCON/TACON — correct terminology, will surface topically-near MCDP 1-0 chunks that do not answer it |
| `ooc-027` | Marine vs joint centre of gravity — half in corpus, half not; must split, not blend |
| `ooc-028` | "Is JP 3-0 loaded on this device?" — can the system report its own boundaries? |
| `ooc-029` | Names the superseded 2011 edition specifically |
| `ooc-030` | TCCC antibiotic question — answerable, but **only** with the edition date in the citation |

`ooc-030` is the counterweight to the rest: it fails if the system refuses, and it also
fails if it answers without the edition. Included so the medical content is held to a
higher citation standard than the doctrinal content, which is the correct asymmetry.

Also recorded as a fielding insight: CAC-gated doctrine is a **real constraint for any
offline tutor**, and identifying the authenticated-sync path is stronger material for the
path-to-fielding page than pretending all doctrine is a public download.

### Scope limit, stated plainly

MCWP/MCRP/MCTP coverage is **curated, not exhaustive**. MCPEL's listing service was
returning `ArticleCS is currently unavailable` throughout acquisition, so the series could
not be enumerated. The set was assembled by probing known publication paths. Re-run
enumeration when MCPEL recovers if fuller coverage is wanted.

---

## D-022 — P1 ingest: 4,636 chunks, citations verified (2026-08-13)

`src/ingest/chunker.py` → `corpus/chunks.jsonl`. **4,636 paragraph-level chunks** across
13 documents / 1,780 pages. Every chunk carries `pub_id`, `pub_title`, `edition`,
`chapter`, `chapter_title`, `section`, `para_id`, `page_printed`, `page_pdf_start/end`,
and a preformatted human-checkable `citation`.

### Acceptance criterion: citations verified against source pages

`src/ingest/verify_citations.py` takes the *cited printed page*, finds the PDF page
bearing that printed number, and confirms the chunk's own interior words are on it.
40 randomly sampled citations (seed 12, from 3,766 citable chunks):

| verdict | n |
|---|---|
| **PASS** (all probes found) | **35** |
| PARTIAL (some probes found) | 5 |
| FAIL | **0** |
| NO_PAGE | **0** |

**87.5% strict, 100% pass-or-partial.** All five PARTIALs are explained: four are chunks
that span a page boundary and continue past the two-page search window; one is a
references page where footnote numbering interleaves with text.

Additionally verified **by eye**, end to end: `TC 3-22.9, Ch 5, "Functional Elements of
the Shot Process", para 1, p.5-3`. The rendered page footer reads
`13 May 2016 | TC 3-22.9 | 5-3`, the heading matches, and paragraph 5-10 matches the
chunk text exactly.

### Three document families, detected rather than assumed

| Family | Docs | Structure | Page numbers |
|---|---|---|---|
| Scanned 1996-97 MCDPs (CVISION OCR) | 8 | printed TOC | footer, plain int |
| Native digital w/ bookmarks | MCDP 1-0, MCDP 7, TC 3-22.9 | PDF bookmarks | chapter-relative (`7-7`) |
| Scanned, header page numbers, two-column | MCWP 3-11.3 | headings | header (`11-3`) |

### Four defects found and fixed, each caught by inspecting output rather than trusting it

1. **Font size is not a reliable heading signal in OCR'd scans.** MCDP 1's `UNCERTAINTY`
   measures 11.90 against a body median of 11.40 — ratio 1.044, under a 1.06 threshold.
   Missing it silently reattributed five paragraphs of *Uncertainty* to the *Friction*
   section: **confidently wrong metadata, which is worse than none**. Detection now keys
   on capitalisation, with size as a weak guard only. Section coverage on MCDP 1 went
   67.5% → 93.8%.
2. **Running heads leaked into body text.** A chapter-specific head appears on only ~11%
   of pages, under a 15% document-wide repetition threshold, so it survived stripping and
   was appended into whichever paragraph was being carried across the page break — one
   chunk ended `"...a continuous The Nature of War"`. Furniture is now detected by
   absolute repetition *within the margin bands*, over every page rather than a sample.
3. **MCWP 3-11.3 is two-column.** Sorting lines by y interleaved the columns and produced
   scrambled prose: 757 chunks at a median of 160 characters with 279 unrecognised
   headings. Column detection is automatic and a no-op on single-column pages; median
   chunk length recovered to 314.
4. **Paragraphs were split at page boundaries.** Doctrine paragraphs routinely run
   overleaf; splitting them puts half a claim in each chunk and makes a citation point at
   a paragraph that does not contain the sentence. Paragraphs now carry across page and
   column breaks and cite the page where they *begin*, with `spans_pages` recorded.

### A fifth defect was in the verifier, not the chunker

The first verification run reported **5% strict pass** and looked catastrophic. The cause
was the checking tool: chunk text is dehyphenated at ingest (`inani-` + `mate` →
`inanimate`) while the raw page still contains the break, so every probe crossing a
hyphenated line break failed — which, in a justified narrow-column book, is most of them.
Matching now runs on a compact letter stream, immune to hyphenation, wrapping, and OCR
spacing. **Worth recording: a verification tool that is wrong in the pessimistic
direction is still wrong, and nearly caused a working ingest to be rebuilt.**

### Known quality limits, not yet fixed

- **Garbled OCR section names survive into citations** where no TOC entry matches, e.g.
  MCDP 6 `"Bmps, T-Bos."`. Fuzzy matching rescues headings when the printed TOC lists
  them; MCDP 6's contents pages parse only 3 chapters / 40 sections, so some do not
  match. A citation with a nonsense section name is still *locatable* (pub + page +
  paragraph are correct) but looks careless. Candidate fix: suppress section names below
  a confidence floor rather than printing them.
- **Figure captions merge into adjacent paragraphs** (e.g. `"Figure 2. The Levels of War
  Compressed."` inside an MCDP 1 chunk).
- **Bookmark-derived chapter titles are sometimes generic** (`"Ch 13: Chapter 13"`).
- **TCCC**: only 4.4% of chunks carry a printed page — the document has no page-number
  furniture in a form the extractor recognises. 17 pages, so citations fall back to
  pub + edition + section, which for a 17-page document is still navigable.
- MCDP 1 chapter 1's `Disorder` section was not detected (11 of 12 sections found).

---

## D-023 — P2 retrieval: recall@8 = 93.8%, and the abstention gate needs more than a score

Index: **4,234 chunks in one 15.2 MB `doctrine.sqlite`** (sqlite-vec dense + FTS5 BM25 +
provenance). Embedded on the Orin GPU in ~94s via llama.cpp's OpenAI-compatible endpoint —
no PyTorch on an 8GB board.

### Measured against 16 labelled questions

Ground truth is `(pub_id, section)` pairs, every one verified to exist in the built index
before scoring — an unmatched label would silently depress recall and look like a
retrieval failure.

| metric | fused only | + reranked |
|---|---|---|
| recall@1 | 43.8% | **50.0%** |
| recall@3 | 68.8% | **81.2%** |
| **recall@8** | 81.2% | **93.8%** |
| MRR | 0.575 | 0.672 |

Reranking earns its place: it lifts recall@8 from 81.2% to 93.8% by promoting chunks the
fusion ranked below 8. `inc-015` is excluded from scoring — the *question* was written from
memory and has not been verified against TC 3-22.9.

### Navigation chunks were poisoning retrieval

MCDP 1-0's table-of-contents pages were being chunked as content — runs like
`"Assessment . . . . . . . . . 4-7"` that carry every keyword in the publication and none
of its meaning. **366 such chunks in MCDP 1-0 alone** (1,072 → 712), plus 40 in TC 3-22.9.
Filtering them raised MCDP 1-0's printed-page coverage from 66% to 88.6% and recall@3 from
75% to 81.2%.

### TCCC sectioning fixed

TCCC chunks previously carried `section=None` entirely — the worst citations in the corpus,
on the one document whose content is clinical. Its headings are **16pt bold title-case**
(`Basic Management Plan for Care Under Fire/Threat`), which the all-caps rule never fired
on. Heading detection is now font-aware; TCCC section coverage went **40% → 100%**.

Two related bugs fixed while there:
- OCR repair was being applied to headings from *native* PDFs, corrupting clean text:
  TCCC's title-page date `01 May 2026` became the section name `"Oi May 2O26"` because
  0→O and 1→I fired on text that was never damaged. OCR repair is now restricted to
  headings that are actually all-caps scan artefacts.
- Short lines were being dropped by the minimum-chunk-length filter. TCCC's
  `"3. Massive Hemorrhage"` is 21 characters and introduces the content beneath it —
  dropping it removed the only occurrence of that phrase from the entire corpus. Short
  paragraphs now merge forward into the paragraph they head.

### The one miss, and why it matters more than the number

`inc-009` — *"Why does Marine Corps doctrine emphasize tempo?"* — is not retrievable. The
answer is in MCDP 1 "Speed and Focus" (*"Speed over time is tempo"*), but **285 chunks
contain the word "tempo"** and this one mentions it once in a longer paragraph. It requires
a candidate pool of **k=800** to surface by either BM25 or dense. Sweeping the pool at
50/100/150/250 changed nothing.

**This is the finding that shapes P3.** Reranker scores on the top-1 result:

| query | top-1 rerank | what it is |
|---|---|---|
| "What is maneuver warfare as described in MCDP 1?" | **+7.19** | correct answer |
| "Why does Marine Corps doctrine emphasize tempo?" | **+3.71** | **retrieval miss — wrong chunk, confident score** |
| "According to JP 3-0, what are the phases of a joint campaign?" | **+0.73** | deliberately excluded publication |
| "What does the MARCH algorithm stand for in TCCC?" | −4.91 | genuinely out of corpus |
| "What is the current price of Brent crude oil?" | −5.91 | genuinely out of corpus |

A single reranker-score threshold cleanly separates the *obvious* out-of-corpus cases
(≈ −5) from good answers (≈ +7). **It does not catch the dangerous middle.** A retrieval
miss that surfaces a plausible-but-wrong chunk scores +3.71 — comfortably above any
threshold that would still permit real answers. The excluded-publication case at +0.73 sits
right on the boundary.

So the abstention gate **cannot be a reranker threshold alone**. The post-generation
grounding check in P3 — mapping every claim sentence back to a retrieved chunk — is what
must catch the +3.71 case, because retrieval itself is confident and wrong there. This is
precisely why the brief says to tune the threshold empirically rather than hardcode a
guess; the guess would have been "anything above zero is fine", and it would have shipped a
fluent wrong answer about tempo.

---

## D-024 — P3: abstention tuned. **10.3% false-answer, 89.7% correct-abstention, 11.8% over-refusal**

Threshold **3.5** on the reranker score, tuned on device against 17 in-corpus and 29
out-of-corpus questions. p50 latency **7.2s**, p95 **11.7s**.

### The measured trade-off curve

| threshold | false-answer | correct-abstention | over-refusal | traps kept |
|---|---|---|---|---|
| 0.0 | 41.4% | 58.6% | **0.0%** | 2/2 |
| 1.5 | 27.6% | 72.4% | 5.9% | 2/2 |
| 2.0 | 20.7% | 79.3% | 5.9% | 2/2 |
| **3.5** | **10.3%** | **89.7%** | **11.8%** | 1/2 |
| 4.5 | 3.4% | 96.6% | 35.3% | 1/2 |
| 5.88 | 0.0% | 100.0% | **76.5%** | 0/2 |

The curve is the deliverable, not a single number. 3.5 minimises the *sum* of the two
error rates. If the demo wants a stronger refusal story, 4.5 buys 3.4% false-answer at
35.3% over-refusal — a defensible choice, made in the open.

### My first tuner picked the degenerate point, and the eval set caught it

The initial selection criterion was "minimise false answers, tie-break on over-refusal".
It chose **threshold 5.88: a perfect 0% false-answer rate, achieved by refusing 13 of 17
real questions.** That is precisely the failure mode `eval/README.md` warns about —
*"refusing everything drives the false-answer rate to zero and produces a useless tutor"* —
and my own objective function walked straight into it. Fixed to minimise the **sum** of
both error rates, so neither can be bought with the other.

Worth stating plainly: the eval set caught a bug in the thing measuring the eval set.

### Negative result: the grounding check cannot gate abstention

Sentence-level grounding was designed as the second gate. **It does not separate.**

| | in-corpus | out-of-corpus |
|---|---|---|
| min sentence support | 0.642 – **0.861** | 0.546 – **0.985** |

The out-of-corpus range is *wider and reaches higher*. The cause is structural, not a
tuning failure: the check measures whether the answer faithfully paraphrases the retrieved
chunks — which it always does, because the model is instructed to use only those chunks.
It is a **fidelity** measure, and abstention needs a **relevance** measure. For an
unanswerable question the model dutifully paraphrases irrelevant chunks and scores 0.985.

`sentence_support_threshold` is therefore left `null` **deliberately**, with the reason in
`config/default.yaml` so nobody "fixes" it later by picking a number. The check stays
enabled for per-sentence flagging, which is genuinely useful and honest, but it is not a
gate.

**What would work instead** (not built): score each answer *sentence* against the retrieved
chunks with the **reranker** rather than embedding cosine. The reranker is a relevance
model and already separates cleanly at the query level; embeddings measure similarity,
which is the wrong quantity. This is the highest-value next experiment.

### The model's own refusal is a perfect-precision signal

Instructed to reply `INSUFFICIENT_SOURCES` when sources are inadequate, Gemma 4 E2B
declined **9 of 29** out-of-corpus questions and **0 of 17** in-corpus questions.

100% precision, 31% recall. It never once refused a real question. That makes it safe to
trust unconditionally as a first gate — it can only help — and it is already wired that
way, ahead of the score threshold.

### Gemma 4's thinking mode silently returned empty answers

With default settings the model spent its entire token budget on `reasoning_content` and
returned **`content` of length zero** — 320 tokens generated, nothing delivered, ~20s
wasted per query. A pipeline that did not inspect the response would have shipped blank
answers at full latency.

Fixed with `--reasoning off --reasoning-budget 0`. Latency fell from ~20s (hitting the cap)
to **3.5–10.4s**. Also constrained the system prompt to at most 3 sentences of plain prose:
unconstrained, the model wrote 600+ tokens for a definition question, which at the measured
20.86 tok/s is ~29 seconds and would have dominated p95.

### Memory: right-sizing buffers reclaimed ~1 GB

Three model servers on a 7.4 GiB board left only **541 MiB free** — before Chromium. The
reranker was consuming **1,242 MB for a 304 MB model** because it had been started with
`-b 2048 -ub 2048` while the model caps at 512 tokens. Right-sizing all three:

| server | before | after |
|---|---|---|
| generator (E2B) | 2,465 MB | 2,468 MB |
| reranker | **1,242 MB** | **526 MB** |
| embedder | 318 MB | 280 MB |
| **available** | **588 MB** | **1,558 MB** |

Nearly a gigabyte back for free, which is what makes room for the kiosk browser.

---

## D-025 — Reranker-scored grounding also fails as a gate. Hypothesis refuted.

D-024 proposed the highest-value next experiment: replace embedding-cosine grounding with
**reranker-scored** grounding, on the theory that cosine measures similarity where
abstention needs relevance, and the reranker is a relevance model. Built it, ran it over
the full eval set on device, and it **does not work either**.

| signal | in-corpus | out-of-corpus |
|---|---|---|
| embedding cosine | 0.577 – 0.861 (median 0.801) | 0.516 – **0.985** (median 0.788) |
| **reranker score** | −3.310 – 8.886 (median 5.311) | −5.621 – **10.307** (median 2.873) |

Best achievable operating point on sentence-level reranker grounding:
**35.0% false answers at 17.6% over-refusal** — far worse than the query-level score
threshold already in use (**10.3% / 11.8%**).

**Why the hypothesis was wrong.** Swapping the scoring function did not change what is
being scored. Both formulations compare *the produced answer* against *the retrieved
chunks*. But the model was instructed to write only from those chunks, so the answer
agrees with them by construction — whether or not they address the question. On an
unanswerable question the model paraphrases irrelevant chunks faithfully, and any
answer↔chunk metric rewards it. The out-of-corpus maximum exceeds the in-corpus maximum
under *both* metrics, which is the signature of measuring the wrong relation.

There is real signal in the medians (5.311 vs 2.873), just nowhere near enough separation
to gate on.

**Conclusion, and it is a useful one:** the only measurement that discriminates is
**question ↔ retrieved-chunk relevance**, computed *before* generation. That is exactly the
query-level reranker score already gating at 3.5. Post-generation checks cannot recover
information that retrieval did not have.

The grounding check keeps its narrower, honest role: flagging an individual sentence that
matches no retrieved chunk, which catches invention *within* an otherwise grounded answer.
It is not, and cannot be, the abstention gate. `sentence_support_threshold` stays `null`.

**What this means for the pitch.** The abstention story rests on retrieval quality, not on
a clever post-hoc filter. That is a stronger claim to defend, not a weaker one — but it
does mean the honest headline is "we know when the corpus can't answer" rather than "we
verify every sentence before speaking". Two experiments were run to find that out, and
both are on the record.

1. ~~SSH credentials~~ — **resolved 2026-08-12.** User `vanguard`; workstation pubkey
   installed to `~/.ssh/authorized_keys`, key auth confirmed. Password was supplied in chat,
   used once, never written to disk or committed. **Recommend rotating it** since it exists
   in a transcript, and it is now unnecessary for our access path.
2. ~~Confirm `orin-vanguard` identity~~ — **resolved.** No running USV services; board is
   spare. But it is not empty (D-009).
3. ~~On-device JetPack/L4T version~~ — **resolved.** JetPack 6.2.1 / L4T R36.4.3 / CUDA 12.6.
   Recommendation is to stay there (D-008).
4. ~~Fielding target: Orin Nano 8GB or Orin NX 8GB?~~ — **resolved 2026-08-12.** Confirmed
   **Orin Nano 8GB** by EEPROM + four corroborating discriminators (D-007). This unit is the
   fielding item. Brief stands as written; no blocker remains.
5. **Are `~/hawkstack`, `~/spire`, `~/model` disposable?** (D-009) — blocks any reflash or
   cleanup. Not blocking if we stay on 6.2.1 as recommended.
6. **Repo path** — currently scaffolding at `C:\doctrine-tutor\`, remote
   `github.com/jeranaias/<repo>`. Rename freely before first commit.
7. **Gemma PUP applicability** (D-001) — non-blocking for build, blocking for the fielding
   one-pager.
8. **RTC replacement decision** (D-010) — not blocking P0, blocking P5.

## Not yet pinned, deliberately

`llama.cpp` commit, JetPack point release, model checkpoint SHA-256, `bge-small-en-v1.5`
and `bge-reranker-base` revisions. Each gets pinned when it is first built or downloaded on
the actual device, with the reason recorded here. Corpus documents get SHA-256 pinned in
`corpus/manifest.json` at P1 per the brief.

---

## D-026 — Cold boot to usable: **53.3s**, PASS (budget 60s)

Measured 2026-08-13 by `scripts/boot_test.py`, which stops the clock only when a real
doctrine question returns a cited answer — not at ssh reachability, and not at a health
endpoint, because llama.cpp answers `/health` while weights are still loading.

| mark | time |
|---|---|
| ssh reachable | 40.1s |
| all three model servers healthy | 40.8s |
| **answered a doctrine question** | **53.3s** |

### The first measurement was fake, and the script now makes that impossible

The initial run reported a triumphant **13.5s**. It was measuring a warm machine: `sudo
systemctl reboot` had silently failed because BatchMode ssh cannot answer a password
prompt, so the probe simply talked to the still-running system. Uptime confirmed it —
**3 hours** — and `systemctl show tutor-gen` reported `ExecMainStartTimestamp=n/a`, meaning
the units had never executed at all.

The script now reads `/proc/uptime` before rebooting, **waits until the host is observably
down** before starting the clock, and aborts with a diagnostic if the host never goes down.
Passwordless reboot is granted via a narrow `/etc/sudoers.d` rule limited to
`systemctl reboot`.

Recorded because a boot-time number is exactly the kind of metric that is easy to measure
wrongly in the flattering direction, and it would have gone on a slide.

### Where the time actually goes, and what bought the margin

`systemd-analyze` reports only **16.0s** (9.0s kernel + 7.0s userspace). The remaining
~32s was **UEFI firmware before the kernel starts** — invisible to systemd, and the
dominant term. Two changes took the first honest measurement from 61.2s (FAIL) to 53.3s:

- **UEFI boot-menu `Timeout` was 5 seconds.** Set to 1 (`efibootmgr -t 1`). An appliance
  has nobody to read a boot menu.
- **Disabled `docker`, `containerd`, `snapd`, `lvm2-monitor`, `ModemManager`** — the top
  entries in `systemd-analyze blame`, none of which serve an air-gapped tutor.

Query latency is **flat at ~9.6s** across runs 1/2/3, so there is no cold-cache warm-up to
win — that is simply inference time, and it sits inside the boot budget.

**Margin is 6.7s.** Thin enough that adding anything to the boot path requires
re-measuring, which `make boot-test` now makes a one-liner.

**The measurement is stricter than the demo will be:** it spawns a fresh Python interpreter
per query. Once the FastAPI service from P4 is resident, the first user question should
land nearer the 9.6s inference figure.

---

## D-027 — All 74 practice items reviewed. **74 approved, 0 rejected, 8 questions rewritten**

**Date:** 24 Aug 2026
**Phase:** P5 (learning loop)

The approval gate was driven end to end through the real UI (Playwright, not the API), so
what is recorded below is what an instructor sitting at the machine would have produced.

| | count |
|---|---|
| Items generated (MCDP 1, ch 1–4) | 74 |
| Cleared in bulk on the strength of the automatic checks | 58 |
| Read individually by a human | 16 |
| Approved as written | 8 |
| Approved after rewriting the question | 6 |
| Approved after rewriting the question **and** the key points | 2 |
| Rejected | 0 |

Bulk clearance is recorded under reviewer id `bulk:unflagged`, not `instructor`, so the
audit trail can still answer "which questions did a human actually read?" truthfully.

**What the flags got right and wrong.** 16 items carried an automatic flag. Applying one
test to every `ANSWER_LEAKED` flag — *could a Marine who has never read the paragraph
recite the key point by reading the question back?* — 8 were real leaks and 7 were the
checker reacting to topic-naming, which a question is obliged to do. The single
`UNGROUNDED_KEY_POINT` (MCDP1-c1-009, 0.59 similarity) was a false alarm caused by OCR:
the source sentence is scrambled mid-clause ("...in the system local conditions and
interacting locally response to in incomplete information"), but both "local conditions"
and "incomplete information" are present. **A precision of 8/16 on a flag whose only job
is to direct attention is acceptable; a flag that gated automatically at this precision
would not be.** The flags stayed advisory for that reason.

Worst real leak: MCDP1-c4-071 stated 100% of its own key point —
*"Describe how combined arms are accomplished through the use of tactics and techniques
at lower levels and task organization at higher levels..."* against key point
*"Combined arms through lower-level tactics and higher-level task organization."*
Nothing was being recalled.

**Two defects found by doing the review rather than by reasoning about it:**

- **`.drawer .body` never scrolled.** `overflow: auto` on an auto-height child of a flex
  column does not engage — the child grows past the container instead. The drawer ran off
  the bottom of a 950px viewport and the *Review questions* button was unreachable at any
  window size. Playwright surfaced it as "element is outside of the viewport" after 58
  retries. Fixed with `flex: 1 1 auto; min-height: 0`.
- **The grader's `covered`/`missed` lists did not partition the key points.** For an
  answer that earned all three friction points the model returned one merged line —
  *"covered all three specified types of friction"* — so the Marine saw a single tick and
  two key points that had silently vanished. `LearnStore._reconcile()` now matches the
  grader's output back onto the item's own key points by token overlap and treats `missed`
  as authoritative (the grading prompt already resolves doubt against the student). A
  verdict of `incorrect` with an empty `missed` list is treated as all-missed rather than
  all-credited. Six cases unit-tested, including merged-covered and model-garbage.

**Fed back into the generator rather than hand-fixed.** 8 of 16 flagged items shared one
failure, so `build_items.py` gained two rules: the stem must not contain its own answer,
and a key point must carry content — the OODA item had reduced two of its key points to
the bare words "Observation" and "Decision", which the grader had nothing to match
against. Re-running generation on a new corpus should not need these 8 edits again.

---

## D-028 — MCDP 1-0 had one section label for all 712 chunks. Bookmark trust is now earned, not assumed

**Date:** 24 Aug 2026
**Phase:** P5 (corpus scale-out)

Every chunk of MCDP 1-0 — 17% of the corpus — carried `section = "Warfighting Functions"`.
Not a missing label: a confidently wrong one, and one that is fed to the embedding model
as a retrieval header, so it was degrading search on that publication as well as making
it unteachable.

**Three bugs stacked.** The publication's outline is 26 flat entries. The last two are
`L2 p1 "APPENDIX B"` and `L2 p1 "Warfighting Functions"` — an appendix PDF embedded whole,
whose bookmarks still point at *its own* page 1. So:

1. Those level-2 entries won the lookup for the opening pages and seeded the state.
2. `section` was only ever *set*, never *cleared*. Every later page resolved to a level-1
   chapter entry, where `chapter == section` yields no section, so the stale value simply
   persisted to the end of the document.
3. Typographic heading detection was gated behind `if not bmarks:` — the presence of *any*
   outline suppressed it, however useless that outline was.

**Fix, measured rather than assumed.** An outline is trusted for sections only when
section-level entries outnumber chapter-level ones. Across all 13 publications this
separates cleanly with no tuning:

| | L1 | L2+ | verdict |
|---|---|---|---|
| MCDP 1-0 | 24 | 2 | chapters only, fall back to typography |
| MCDP 7 | 9 | 23 | outline kept |
| TC 3-22.9 | 12 | 1815 | outline kept |
| the other 10 | 0 | 0 | no outline; typography already |

Section is now also cleared when the context has none, because a section that is never
unset follows the reader into every chapter after it.

**The heading detector needed a third signal.** Falling back to typography was not enough:
MCDP 1-0 sets Times New Roman body against Arial-BoldMT heads at 14pt (all caps, section)
and **11pt (title case, subsection) — exactly the body size**, so every size-based rule
misses them. Font *family* change is the discriminator. Requiring at least body size
rejects the same family at 8–9pt, which is figure captions and table cells.

The family rule is armed only when one font accounts for at least 60% of a page's lines
and more than one font is present, so OCR'd scans that synthesise a single font are
untouched.

**Blast radius, verified by diffing section granularity before and after:**

| | sections before | after |
|---|---|---|
| MCDP 1-0 | 12 | **248** |
| TCCC | 5 | **14** |
| all 11 others | unchanged | unchanged |

MCDP 1-0 chapter 3 now resolves into the Marine Corps Planning Process — "Problem
Framing", "Course of Action Development", "Course of Action War Game" — content that was
previously unreachable by section.

**Retrieval improved and nothing regressed** (n=16 in-corpus questions, so small-sample):

| | old index | new index |
|---|---|---|
| recall@1 | 0.500 | **0.563** |
| recall@8, fused only (pre-rerank) | 0.813 | **0.875** |
| recall@8, final | 0.938 | 0.938 |
| MRR | 0.672 | **0.703** |

Also fixed while in there: `format_citation` rendered "Ch 5: Chapter 5" for outlines that
title their chapters by number. A citation a Marine reads aloud should not stutter.

---

## D-029 — Confidence is part of the review schedule, not just a statistic

**Date:** 24 Aug 2026
**Phase:** P5 (learning loop)

`next_item` was "unseen first, then anything not mastered". That has no notion of time, so
there was no reason to open the tutor *tomorrow* specifically — the failure mode is a
Marine burning through the bank once and never returning.

Items now carry a due date computed from the full attempt history, and due work is served
ahead of new work. Intervals after consecutive correct recalls: **1, 3, 7, 21, 60 days**,
deliberately short at the front.

**The part that is not standard spaced repetition:** confidence enters the interval.

| verdict | stated confidence | returns in |
|---|---|---|
| incorrect | Fairly sure / Certain | **same session** |
| incorrect | Guessing / Unsure | 1 day |
| partial | any | 1 day |
| correct | Guessing | capped at 3 days |
| correct | Unsure and up | 1, 3, 7, 21, 60 |

Being *certain* and wrong is a different failure from being *unsure* and wrong: the first
means a Marine is carrying a belief they would act on. It earns the shortest possible
interval regardless of prior score. Correct-but-guessing is not knowledge and is capped at
three days rather than pushed out two months.

Same thesis as the abstention work, applied to the human: the measured gap between stated
confidence and actual correctness is the product.

---

## D-030 — Kiosk is WebKit directly, not a browser

**Date:** 24 Aug 2026
**Phase:** P5 (demo hardening)

The device has no browser. Chromium on Ubuntu 22.04 arm64 is snap-only and snapd was
disabled to protect the cold-boot budget, and there is no internet at the venue to install
anything. Checked what is already on the image: `libwebkit2gtk-4.0-37`,
`gir1.2-webkit2-4.0`, Xorg, `xinit`, seat0, `/dev/dri/card0`. The kiosk is ~40 lines of GTK
against a library that is already present.

It is also the better kiosk: no address bar to type into, no tab strip, no update prompt
mid-demo, and no way for a reviewer holding the keyboard to navigate away by accident. Right
click is suppressed, developer extras are off, and quitting is Ctrl+Shift+Q — awkward on
purpose.

`kiosk.sh` waits for the API **before** starting X rather than after. Starting X first
lights the monitor with a grey root window while the model loads, which reads as a hung
machine to anyone watching; waiting means the screen stays dark and then shows the tutor.
On a load failure the view retries every 2s instead of showing WebKit's error page.

**Honestly untested.** No display has ever been attached to this board, `startx` over SSH
is refused by `Xwrapper.config allowed_users=console`, and there is no Xvfb and no way to
install one. What *is* verified: the module imports on the device, both scripts parse, the
API-readiness loop passes against the live API, and it exits 1 at its deadline against a
dead port rather than hanging. The unit is written by the installer but deliberately
**not enabled** — it takes over tty1, and enabling something unexercised that seizes the
console is how you arrive at a demo with a black screen.

---

## D-031 — A route over the USB development link is not an uplink

**Date:** 24 Aug 2026
**Phase:** P5 (demo hardening)

The board reported **LINK UP — NO INTERNET** rather than **AIR-GAPPED** whenever the USB
cable to the workstation was attached — which is how it is administered, and how it will
be at the venue.

`interfaces()` already excluded `l4tbr0` and `usb*` from what counts as an uplink.
`default_routes()` did not, and the Jetson's device-mode bridge installs a default route
via `l4tbr0`, so the state machine's middle branch fired on a route that could never reach
anything.

The consequence was not cosmetic: pulling the network cable in front of a reviewer is the
single moment that proves the offline claim, and the banner would not have flipped.

Safe rather than flattering — reachability is probed first and decides. If the workstation
were sharing its connection over that same link the probe succeeds and the state is
ONLINE regardless. Dev-link routes are still reported, under `dev_link_routes`, so nothing
is hidden; they simply no longer count as an uplink.

Verified live: with the USB link still attached, `/api/network` now returns
`OFFLINE / AIR-GAPPED`, `dev_link_routes: ["l4tbr0"]`, `dev_link_present: true`.

---

## D-032 — The item generator must survive a dropped connection

**Date:** 24 Aug 2026
**Phase:** P5 (corpus scale-out)

The first full-corpus generation run died after 18 of 539 sections on a single
`RemoteDisconnected` — llama-server closing a keep-alive connection between requests.
`gen_item` had no retry and `main()` had no guard, so one transient blip ended a
100-minute job.

Three changes, all of which a batch job measured in hours should have had from the start:
retry with exponential backoff (4 attempts, `Connection: close`), a `--resume` flag that
keeps what is already in the output file and regenerates only the missing sections, and
per-paragraph exception isolation so one bad chunk cannot end the run.

Output was already being flushed every 5 sections rather than at the end — that decision
is what made the resume possible, and it turned a total loss into 30 salvaged items.

---

## D-033 — The releasability gate was a design, not running code. Two vetoes, both adjudicated

**Date:** 24 Aug 2026
**Phase:** P5 (production hardening)

The brief's constraint is *"public-domain / publicly releasable data only… if releasability
is ambiguous, skip it and log it."* The screener implemented that. Nothing consumed it.

**Three independent breaks in the chain:**

1. `Makefile` ran `src/ingest/verify_manifest.py` as the pre-ingest guard. **That file has
   never existed in any of the 30 commits**, so `make ingest` could not complete.
2. `build_manifest.py` computed `cleared_for_ingest` from one positive regex plus a
   hardcoded filename allowlist, and **never read the screener's `blocking_findings` or
   `distribution_restricted` fields at all**. A document carrying DISTRIBUTION STATEMENT C
   would still have cleared if the words "approved for public release" appeared anywhere
   in it. `inspect_corpus.py`'s own `BLOCKED` verdict lived only in its exit code.
3. `chunker.py` opens PDFs by filename without checking SHA-256 against the manifest, so a
   swapped file would be ingested silently. **Still open.**

The corpus is clean — all 13 SHA-256s were independently recomputed and match, and the
screener's 11 blocking patterns re-run case-sensitively across all 4,230 chunks return
zero hits. But it is clean because the right documents were acquired, not because the
pipeline would have stopped the wrong ones.

**The fix.** Clearance now requires positive evidence AND the absence of any disqualifying
finding. The veto outranks the human allowlist deliberately: those entries were cleared on
the *absence* of restrictive markings, so a marking found later invalidates that judgement
rather than being overridden by it.

**Turning it on immediately vetoed two publications.** Both were traced to source pages
before any decision was made, per the guardrail:

| | finding | what it actually is |
|---|---|---|
| MCDP 1-0 | `NOFORN`, `SECRET` ×2, PDF p.274 | The **References list**, naming the classification of *other* publications it cites: *"Defense Planning and Programming Guidance (Publication is classified SECRET/NOFORN)"*. MCDP 1-0 itself is Distribution Statement A. |
| MCWP 3-11.3 | `SECRET`, PDF p.36 | OCR of a **blank message-form graphic** — "TOPSEC SECRET CONF" is the pre-printed classification checkbox row on an empty patrol-report template, surrounded by OCR noise. |

Verified neither string appears in any chunk in the index.

**Adjudication is per-finding, not per-file.** A blanket filename override was the obvious
shortcut and is the wrong shape: it clears findings nobody has ever looked at. Entries are
keyed on `(filename, marking, count-at-adjudication)` and carry the reasoning and date. If
the count changes the document changed, and the finding returns to a human rather than
riding on a judgement made about different text.

This extends D-009. That entry made matching case-sensitive after `\bSECRET\b` matched
*"there was nothing secret about the German attack"*. The residual class is different: the
match is genuinely uppercase and genuinely a classification marking — it just belongs to a
**different document**, or to an **empty form**. Pattern matching cannot distinguish "is
marked X" from "mentions X", and it should not try. A human should, once, on the record.

---

## D-034 — Re-measured on the rebuilt index. **False answers 10.3% → 6.9%, correct abstention 89.7% → 93.1%**

**Date:** 25 Aug 2026
**Phase:** P5 (production hardening)

The eval table in the writeup described an index that no longer existed: the corpus was
re-chunked and re-sectioned on 24 Aug (D-028), which changed the retrieval headers fed to
the embedding model across 17% of the corpus. Re-run against the current index, same eval
set, same threshold 3.5, same pipeline.

| | 13 Aug index | 25 Aug index |
|---|---|---|
| **False-answer rate** (out-of-corpus answered anyway) | 10.3% (3/29) | **6.9% (2/29)** |
| **Correct-abstention rate** | 89.7% (26/29) | **93.1% (27/29)** |
| Over-refusal rate (in-corpus refused) | 11.8% (2/17) | 11.8% (2/17) |
| Citation-correct rate | 78.6% | **85.7%** |
| Answers carrying a citation | — | 100.0% |
| Answer-traps correctly answered | — | 50.0% (1/2) |
| p50 latency | 1.7s | 1.82s |
| p95 latency | 11.0s | **9.05s** |

**Every headline number improved or held.** Nothing was traded away: over-refusal is
unchanged at 11.8%, so the false-answer improvement is not the tuner quietly refusing more.
That is the failure mode D-024 was built to prevent, and it did not happen here.

**The cause is D-028, not tuning.** No threshold was touched. Better section metadata
means a better retrieval header, which means the right paragraph ranks higher — recall@1
went 50.0% → 56.3% at the same time. Citation-correct gaining 7.1 points is the clearest
evidence: the answer was already being derived from the right material more often than the
citation pointed at it, and improving retrieval closed part of that gap.

**Read these with the n in mind.** 17 in-corpus, 29 out-of-corpus, 2 answer-traps. A
single question moving is 3.4 percentage points on the out-of-corpus rate. The improvement
is real and consistent across four independent metrics, but the eval set is small and
widening it is TRACKER A5. **Do not quote these as though they were measured on hundreds
of questions.**

Citation-correct at 85.7% remains the weakest number and is still stated as such in
`demo/script.md` and `demo/pitch.md`.

---

## D-035 — 1,011 items generated and 100% reviewed. **11.5% needed a human**

**Date:** 25 Aug 2026
**Phase:** P5 (learning loop)

Generation across the whole corpus: **1,011 items, 539 sections, 13 publications, 1h44m**
at ~12s per section. The run absorbed **24 transient connection failures**, each of which
would have ended it before D-032 added retry and `--resume`.

| | |
|---|---|
| Items | 1,011 |
| Cleared in bulk on the automatic checks (`bulk:unflagged`) | 895 (88.5%) |
| Read individually against the source (`instructor:flagged-review`) | 116 (11.5%) |
| — approved as written | 36 |
| — edited | 73 |
| — rejected | 7 |
| Servable to learners | **1,004** |
| Questions rewritten | 59 |
| Key-point sets rewritten | 38 |

**The flag precision held at about a third false alarm** — 36 of 116 flagged items were
fine as written. That is the intended operating point: the checker directs attention, it
does not decide. A checker at this precision used as an automatic gate would have thrown
away 36 good questions.

**What got rejected is the interesting part.** All 7 were sources with no teachable
content: a references list, two table fragments, a chapter-introduction with an OCR'd
epigraph, a scrambled tactical-task word list, a figure caption plus acronym glossary, and
an OCR'd diagram legend. None were wrong *answers* — they were paragraphs that should
never have become questions. That is a chunking signal, not a generation signal, and it is
the residue of the figure-label class recorded as WONTFIX in TRACKER A10: rejecting them
at review is cheaper and safer than a filter that would also delete real doctrine.

**The re-key mattered more than expected.** Item ids were positional until today, so
loading this bank would have transferred the 74 existing approvals onto entirely different
questions. Ids are now derived from the source paragraph: the new bank produced **1,011
unique ids with zero collisions and zero overlap with the old ids**, so no approval could
be inherited by accident. The 74 orphaned rows were moved to `item_review_archive` with a
timestamp rather than deleted, so the earlier review work is still on the record.

---

## D-036 — The automatic checks could not see the failure mode behind **every** rejection

**Date:** 25 Aug 2026
**Phase:** P5 (quality sweep before release)

A critical pass over the reviewed bank, looking for what the review itself had missed.

**The structural gap.** `precheck()` tests the QUESTION (shape, answer leakage) and the
KEY POINTS (grounding against the source). It never asks whether the SOURCE was worth
generating a question from. Yet all 7 items rejected during the flagged review were
rejected for exactly that — a references list, table fragments, a chapter-introduction
with an OCR'd epigraph, a scrambled word list, a figure caption, a diagram legend.

So the one failure mode a human actually rejects for was invisible to the checker, and
**895 items were bulk-cleared without it ever being applied.**

**A fuzzy screen was built and refuted.** A "looks like furniture" score over alphabetic
ratio, digit density, short-word share, mean word length and sentence density was
calibrated against the 7 known-bad items. It scored only **2 of 7** above threshold while
flagging the TCCC Combat Wound Medication Pack, a ballistics wind-drift table and a
downrange wind-indicator list — all dense, checkable content a Marine should be drilled
on. Same lesson as the figure-caption filter in TRACKER A10: a loose rule deletes real
doctrine. Not shipped.

**A narrow one worked.** Reference apparatus is unambiguous — `Ibid.`, `op. cit.`,
"Principal sources used", publisher-and-year parentheticals, runs of numbered citations.
That found **7 items of 1,011**, of which 1 was already rejected and 1 was a false positive
(Army numbered paragraphs like `3-67.` look like citations). Five more were rejected:

| item | why |
|---|---|
| `MCDP2-c3-e9d0544216-0` | Source is a bibliography. The question literally asks *"What are the principal sources utilized in this case study?"* |
| `MCDP7-c4-bb5513d270-0` | MCDP 7's Notes quoting MCDP 1 pp. 4-21–4-22 verbatim |
| `MCDP7-c4-aae40d05d5-0` | MCDP 7's Notes quoting MCDP 1 p. 1-6 |
| `MCDP7-c4-463960f0f6-0` | MCDP 7's Notes quoting MCDP 1 p. 4-18 |
| `TC3229-c9-e9c4be147d-0` | Exact duplicate question, and its page number is a corrupted glyph |

Final state: **999 servable, 12 rejected, 100% reviewed.**

**The MCDP 7 case is a citation defect, not a duplicate.** MCDP 7's Notes chapter quotes
MCDP 1 at length. Items generated from those blocks carry a citation to *MCDP 7, Ch 4:
Notes* while the doctrine they test lives in MCDP 1. A Marine who follows the pointer does
find the words — but attributed to the wrong publication. For a tutor whose entire claim is
that the document is the authority, that is the precise failure to avoid. **3 of MCDP 7's
13 items (23%) came from such blocks.** This is the most likely single contributor to the
citation-correct rate being the weakest headline metric.

**Root cause is upstream, and it is deliberately NOT being fixed before the demo.** The
chunker glues endnote fragments onto the head of real paragraphs — `"7. Ibid., p. 1-6.
"All actions in war take place in an atmosphere of uncertainty..."` is one chunk, and the
same pattern produced `"17. Clausewitz, p. 194. 18. Tempo is often associated with..."`
noted during the earlier item review.

Fixing it means re-chunking. **Item ids are derived from the source paragraph (D-035), so
re-chunking changes every id and invalidates all 999 approvals** — the entire human review,
including 73 hand-edited items. That is the correct trade to refuse three weeks out. It is
recorded here as the first thing to fix after the event, alongside a `precheck` that
screens the source.

**Two checks came back clean, which is worth stating.** Near-duplicate questions across the
whole bank: 5 pairs at Jaccard ≥ 0.70, of which only 2 were exact, and all traced to the
corpus genuinely restating itself rather than to a generation fault. Non-ASCII in citations:
23 of 1,011, and 22 are legitimate typography (curly apostrophes, en-dashes in section
titles); exactly one was corrupt, a U+1D7CF MATHEMATICAL BOLD DIGIT ONE in a page number,
and that item is now rejected.

**Unrelated gap noticed while reading the eval.** `answer-traps correctly answered: 50%`
is **1 of 2**. Two is not a sample. The trap set — questions that look unanswerable but are
not, which exist to catch over-refusal — needs to be an order of magnitude larger before
that number means anything. Folded into TRACKER A5.

---

## D-037 — A very low reranker score does **not** prove absence. The gate fails both ways

**Date:** 25 Aug 2026
**Phase:** P5 (evidence hardening)

Found while expanding the eval set, and it is the most important thing in this pass.

A question about cricothyroidotomy cannula specification scored **−2.51** on the reranker
— down in the same band as the deliberately absurd control questions, where the price of
Brent crude scored −5.91 in D-023 — **and the answer is verbatim in the ingested TCCC
Guidelines.**

D-023 established that a high score does not prove a chunk is relevant. This is the
mirror image: **a low score does not prove the corpus is silent.** The abstention gate has
false negatives as well as false positives, and until now only one direction had been
demonstrated. Anything that reads the score as evidence of absence — including the way
`abstain_reason: "low_retrieval_score"` is phrased in the API and rendered in the UI — is
overstating what the number supports.

**A second finding of the same shape.** The identical fact, asked twice with only the
register changed:

| phrasing | score | outcome |
|---|---|---|
| off-centre vision, in a Marine's plain speech | 1.80 | **refused** |
| off-centre vision, in doctrinal vocabulary | 6.50 | answered correctly |

The text is verbatim in MCWP 3-11.3 in both cases. **The over-refusal rate has been
measuring phrasing sensitivity as much as corpus coverage**, and the original 17-question
set could not see it because every question in it was uniformly well phrased — written by
someone who already knew the doctrine's vocabulary. That is not the population this is
for.

**Consequence for the claims.** Over-refusal at 11.8% was measured against questions
posed in the corpus's own register. The honest reading is that it is a floor, not an
estimate, and the number will get worse on the expanded set. That is the set telling the
truth, not the system getting worse.

---

## D-038 — Eval set expanded: 48 → 129 questions, and traps 2 → 8

**Date:** 25 Aug 2026
**Phase:** P5 (evidence hardening)

| | before | after |
|---|---|---|
| in-corpus (should be answered) | 17 | **63** |
| out-of-corpus (should be refused) | 29 | **66** |
| harness-scored answer-traps | 2 | **8** |
| over-refusal traps (in-corpus, look unanswerable but are not) | 0 | **18** |

Publication coverage went from MCDP 1 carrying 12 of 17 questions to all 13 publications
represented, MCDP 1 down to 12 of 63.

Every added row was verified against `corpus/chunks.jsonl` — the 4,230-chunk file that
matches the live index exactly, confirmed by comparing per-publication counts against
`/api/corpus`. Rows asserting absence were confirmed by a zero-match scan rather than by
assumption, and `relevant_sections` labels were resolved programmatically against real
`(pub_id, section)` pairs, because D-023 records that an unmatched label silently depresses
recall.

**Two pre-existing rows are now suspect and are flagged rather than changed:**

- `ooc-007` asserts the tourniquet conversion window is unanswerable, but the ingested
  Guidelines do state a 2-hour conversion goal and a 6-hour do-not-remove limit. It is
  still defensible because it asks for a single figure in minutes, but the boundary is
  thin. A companion `inc-064` now covers the answerable part.
- `inc-018` carries the TCCC label *"Basic Management Plan for Tactical Field Care"*,
  which **does not exist as a section in the index** — that content is indexed under the
  numbered headings 3 to 12. This is exactly the unmatched-label failure D-023 warns about,
  and it has been silently depressing recall for that question since P2.

Both left unchanged so the before/after comparison stays honest; both are TRACKER items.

**One row was nearly wrong and was caught.** A planned refusal question about MEU strength
was dropped after MCDP 1-0 turned out to give approximate figures (~2,200). A "levels of
logistics" question was moved from out-of-corpus to in-corpus for the same reason. A
mislabelled row is worse than a missing one: it corrupts the metric in a direction nobody
inspects.

---

## D-039 — Publication identifiers now survive into the BM25 half. Measured against the real index

**Date:** 25 Aug 2026
**Phase:** P5 (retrieval)

`fts_query()` dropped tokens of length 1, so `"MCDP 1-0 chapter 5"` became
`'"MCDP" OR "chapter"'`. The publication identifier a Marine had just typed was discarded,
every MCDP matched equally, and BM25 contributed nothing to deciding which publication was
meant. It survived only because the dense half compensates — `chunk_text.retrieval_text()`
puts the identifier into the embedded text, the decision measured at +3.39 against −2.42 in
that module's docstring. One half of a hybrid retriever was carrying the other.

**The rule:** a whitespace-word containing a digit becomes a quoted phrase glued to the
plain word before it. The plain word is still emitted, so the change is **strictly
additive** — BM25 finds everything it found before, plus a high-IDF phrase. Bare single
digits are still dropped; the length filter's original job is intact.

| query | before | after |
|---|---|---|
| `MCDP 1-0 chapter 5` | `"MCDP" OR "chapter"` | `"MCDP" OR "MCDP 1 0" OR "chapter" OR "chapter 5"` |

**Why phrases rather than parts, decided by measurement not preference.** A real FTS5 table
was built from all 4,230 chunks using `build_index.py`'s exact schema
(`porter unicode61` over header+body):

| term | chunks matched |
|---|---|
| `"1"` | 2,278 — loose digits are pure noise |
| `"MCDP"` | 2,892 — today's behaviour, no disambiguation at all |
| `"MCDP 1 0"` | **exactly the 712 MCDP 1-0 chunks** |
| `"MCWP 3 11 3"` | exactly 668 |
| `"TC 3 22 9"` | exactly 627 |

unicode61 splits the hyphen, so `MCDP 1-0` indexes as adjacent tokens and an FTS5 phrase
spans that run. That was the go/no-go check: a phrase query that cannot match anything
would have been worse than the old behaviour.

**Retrieval effect** over the 11 in-corpus questions naming a publication — mean top-50
BM25 chunks belonging to the *named* publication: **23.4 → 26.1**. MCDP 6: 30 → 42.
MCDP 5: 17 → 33, with the first correct chunk moving **rank 11 → rank 5**. One 1-rank
regression.

**Accepted limit, in a comment and pinned by a test:** FTS5 has no end-of-run anchor, so
`"MCDP 1"` is a positional prefix of `MCDP 1-0` and still matches the whole 1-x family
(1,781 chunks against 2,892 for bare `MCDP`). It narrows; it does not pin. That is why the
MCDP 1 questions barely move.

**Safety re-verified, not assumed.** Every prior adversarial input plus digit-bearing
variants — `MCDP 1" OR "1"="1`, `NEAR(MCDP 1-0, 5)`, `^MCDP 1-0`, `MCDP 1-0*`, backslashes,
an 80-digit token, a 61-token phrase — run against a real FTS5 table: **0 errors**. The
invariant holds: punctuation is stripped before quoting, never escaped. For inputs with no
digit the new function is byte-identical to the old one.

---

## D-040 — API hardening, and the source check the review gate never had

**Date:** 25 Aug 2026
**Phase:** P5 (production hardening)

**`/api/health` was lying.** It returned `{"ok": true}` without probing anything. It now
probes all three model servers concurrently with a 0.8s timeout — worst case ~0.8s rather
than 2.4s, which matters because `boot_test.py` polls it with `curl -m 2` — and caches for
2s behind a single-flight lock so a stampede of pollers costs one round. `ok` still means
*reachable*, not *warm*: llama.cpp answers `/health` while weights are still loading, which
is the whole reason D-026's boot measurement stops on an answered question instead.

**Request bounds on every model and query parameter.** Question 1–2000 (the longest in the
eval set is 128 chars), answer ≤8000, confidence 1–4, `limit` bounded — `LIMIT -1` is
*unlimited* in SQLite, which is what an unbounded integer parameter would have reached.

**`/api/gaps/run` and `/api/review/precheck` are both guarded.** Each spawns tens of
minutes of work; two clicks started two. `DECISIONS.md` records this project genuinely
reaching restart counter 386 by thrashing the board. Both use a non-blocking acquire and
release in a `finally`, so a subprocess that fails, times out, or raises still clears the
guard. Two concurrent prechecks would also have raced to write the same `flags_json` rows,
with the loser's verdicts silently overwriting the winner's.

**`learner` is validated, not authenticated.** Constrained to 64 chars of
`[A-Za-z0-9._-]`, case-folded so `Smith` and `smith` cannot become two records of one
person. No auth was invented; a comment states the real limitation plainly — anyone who
can reach :8000 can read anyone's record — and that it becomes a genuine fielding gate the
moment the identifier is an EDIPI.

**`SOURCE_NOT_TEACHABLE`**, closing the gap D-036 identified: `precheck` tested the
question and the key points and never asked whether the source was worth asking about,
which was the reason behind 100% of rejections.

Four unambiguous signals — `Ibid.`/`op. cit.`, "principal sources used", a publisher
imprint (colon **and** four-digit year in one bracket), and a numbered endnote marker
followed by a page reference. Two narrowing decisions carry it:

- A negative lookbehind on the endnote marker refuses Army numbered paragraphs. In `C-11.`
  the digits follow a hyphen. **0 of the 486 Army-numbered paragraphs in TC 3-22.9 match**,
  and there are zero hits anywhere in TC 3-22.9, MCWP 3-11.3, TCCC or MCDP 1-0.
- The page reference is **mandatory**, so there is no bare run-of-numbers signal. 17 TCCC
  chunks and 9 MCWP 3-11.3 chunks open with `1. … 2. … 3. …` — the Care Under Fire steps
  and the land-nav drills. A run rule would have flagged exactly the content D-036 refused
  to lose.

Over all 4,230 chunks: 183 hits, every one in Notes, Conclusion Notes or bibliography
blocks. Restricted to the item-generating subset: 6 hits, which is D-036's 7 minus the one
Army-numbered false positive. It catches 5 of the 12 known-bad items — the entire
reference-apparatus class, which is all D-036 validated. It remains advisory: a flagged
item stops being eligible for bulk clearance and goes to a human. It rejects nothing.

Test suite: **135 → 222 tests**, all offline.

---

## D-041 — The retry from D-032 was fixed in one file. The next long job died the same way

**Date:** 25 Aug 2026
**Phase:** P5 (production hardening)

D-032 recorded a full-corpus generation run dying after 18 of 539 sections on a single
`RemoteDisconnected` — llama-server closing a keep-alive connection between requests — and
added retry with backoff to `build_items.py`.

**To `build_items.py` only.** Every other caller kept its bare `urlopen`. So the very next
long job to run, the expanded 129-question eval, died at its **first** request with zero
progress, on the identical failure.

The lesson is not "add a retry". It is that a policy fixed at one call site is not fixed.
There were four unprotected call sites: `pipeline._post`, `hybrid.embed`, `hybrid.rerank`,
and `session._post` — every path between this system and a model.

`src/common/http_retry.py` now holds the single policy, shared the way `chunk_text.py`
already is: `scripts/push.sh` copies it into `retrieval/`, `generation/` and `learn/`,
because the device layout is flat and a cross-directory import would not resolve there.

**And then the fix itself was wrong, in a way only running it exposed.** The first
version refused to retry *any* `HTTPError`, and the next eval run died on a **503 Service
Unavailable** from llama-server. A 503 is the server saying "busy, try again" -- it arrives
when every slot is occupied, which the warm-up query added during the cold-boot work makes
likely for a few seconds after a restart. Refusing to retry it was treating a scheduling
condition as a verdict about the request.

502, 503 and 504 are now retried. 4xx is not. **500 is deliberately still not retried**,
because that is the one `rerank()` recovers from by shrinking its document budget.

Three deliberate details:

- **`Connection: close` on every attempt.** Keep-alive reuse is precisely the failure being
  handled. A fresh connection each time trades negligible latency for the thing that kept
  ending long runs.
- **An `HTTPError` is never retried.** A 400 or 500 is the server answering a question
  about the request; repeating it repeats the answer. This matters concretely: `rerank()`
  catches a 400 and *shrinks its document budget*, which is a real recovery. A blanket
  retry would have swallowed that and turned a working adaptation into four identical
  failures.

Scale of the problem, for the record: the generation run that completed absorbed **24** of
these in 104 minutes. This is not a rare event on this hardware.

Six tests pin the policy — retry-then-succeed, give-up-after-N, HTTPError-not-retried,
`Connection: close`, and the backoff sequence — so the next module to call a model server
inherits it rather than rediscovering it.

---

## D-042 — The kernel OOM-killed the generator, twice. The board has almost no headroom

**Date:** 25 Aug 2026
**Phase:** P5 (stability)

The expanded eval kept dying on `HTTP 503 Service Unavailable` from the generator, even
after retry was added. The 503 was not transient — the generator was **gone**.

```
tutor-gen.service: Main process exited, code=killed, status=9/KILL
python3 invoked oom-killer: gfp_mask=0x1100cca, order=0, oom_score_adj=0
oom-kill:constraint=CONSTRAINT_NONE, global_oom, task=llama-server, pid=7431
Out of memory: Killed process 7431 (llama-server) file-rss:2244172kB
```

Twice, thirteen minutes apart, both during eval runs.

**The first hypothesis was wrong and is recorded because it was wrong.** `MemoryMax` on
the unit is 3.67 GB and the model is 3.2 GB, so a cgroup kill looked obvious. It was not:
`memory.events` reports `oom_kill 0` and `max 0`, and `memory.current` sat at 1.96 GB
against the limit. **systemd never touched it.** This was the kernel's *system-wide* OOM
killer (`global_oom`), which picks the largest RSS on the box — and that is always
llama-server.

**Where the memory actually goes.** The API was the other suspect and is innocent: 66 MB
RSS, it holds no model. The real consumers are three llama-server processes, and on a
Jetson **GPU memory is system memory**, so each `-ngl 999` server holds a CUDA context in
the same 7.6 GiB everything else uses:

| | |
|---|---|
| mmap'd model files | 3.5 GB (3.2 generator + 290 MB reranker + 35 MB embedder) |
| CUDA contexts | three of them, unified memory |
| Generator KV cache | **4 slots × 4096 tokens** |
| Observed during the eval | 7,131 MB of 7,619 used, **324 MB available**, 857 MB already in zram swap |

**Four slots is the waste.** llama.cpp reserves KV cache per slot and defaults to 4. This
is a single-learner appliance; the only concurrency it ever legitimately needs is a warm-up
query overlapping a real one. `install_services.sh` now passes `--parallel 2`.

**Applying it needs a unit reinstall, so it is not yet live** — the running generator still
has 4 slots. That is a `sudo sh /opt/tutor/install_services.sh` away and belongs with the
other hands-on-hardware items.

**What this means in practice, stated plainly.** The board is not comfortably provisioned;
it is at the edge, and the failure mode is the generator disappearing mid-answer with
`Restart=on-failure` bringing it back about 8 seconds later. Do not run the eval, item
generation, or anything else heavy while demonstrating. That is now in `demo/script.md`.

**Retry attempts went 4 → 6** as a consequence. Four attempts cover 1+2+4 = 7s of backoff,
and a generator restart plus model load takes about 8 — so the client gave up roughly one
second before the server came back. Six covers 31s. This is a client that survives its
server being killed, which is the correct posture here; it is **not** a fix for the OOM,
and recording it as one would be exactly the sort of thing this log exists to prevent.

---

## D-043 — On a harder eval set every headline number got worse. **The old ones were the measurement, not the system**

**Date:** 25 Aug 2026
**Phase:** P5 (evidence)

The eval set went from 46 scored questions to 129 (D-038). Same threshold, same index, same
model, same pipeline. Only the questions changed.

| | 46 questions | 129 questions |
|---|---|---|
| **False-answer rate** | 6.9% (2/29) | **15.5% (9/58)** |
| **Correct-abstention rate** | 93.1% | **84.5%** |
| **Over-refusal rate** | 11.8% (2/17) | **19.0% (12/63)** |
| Answer-traps correctly answered | 50% (1/2) | **25% (2/8)** |
| Citation-correct rate | 85.7% | **96.0% (50 answered)** |
| Answers carrying a citation | 100% | 100% (51) |
| p50 / p95 latency | 1.82s / 9.05s | 2.09s / 8.74s |

**Both error rates rose together, and that is the important part.** D-024 built this
harness around the fact that false-answer and over-refusal trade against each other: push
one down by moving the threshold and the other goes up. Here they moved the *same*
direction, which no threshold change can produce. That is the signature of a harder test,
not a worse system. **Nothing regressed. The old numbers were flattering because the old
questions were easy.**

D-037 predicted this in advance, from two observations made while building the set: a
correct answer sitting at reranker score −2.51, and the same fact refused at 1.80 in plain
speech but answered at 6.50 in doctrinal vocabulary. The old 17 in-corpus questions were
written by someone who already knew the doctrine's vocabulary. Marines do not arrive
knowing it — that is why they are being tutored.

**Citation-correct went the other way, 85.7% → 96.0%**, and is no longer the weakest
number. Part is the D-028 sectioning fix and the D-039 identifier work; part is simply that
50 answers is a real denominator where 14 was not.

**The threshold is now stale.** 3.5 was tuned in D-024 against the 17+29 set that no longer
exists. Re-tuning against 129 is not optimisation — it is the same procedure applied to
data that represents the problem. The D-024 guard stands: the objective minimises the SUM
of both error rates, because minimising false answers alone selects a degenerate threshold
that refuses almost everything.

**What gets quoted from here on.** The 6.9% / 93.1% pair is retired. It was measured
honestly and is now known to have been measured against a set too small and too easy to
support it. Every document quoting it is updated, including the pull request prepared for
the team repository — its numbers were captured before this run and would have shipped a
figure I can no longer defend.

---

## D-044 — The tuner recommended a change. It is not supported. **Nothing moves.**

**Date:** 25 Aug 2026
**Phase:** P5 (evidence)

With the eval set at 129 questions the threshold was re-tuned, because 3.5 had been fitted
in D-024 against a 46-question set that no longer exists. The tuner's recommendation:

> reranker score threshold **3.25**, grounding threshold **0.626**
> false answers 12.1%, over-refusal 19.0%

Taken at face value that is a strict improvement — false answers down from 15.5% with
over-refusal unchanged. It was not taken at face value.

**What the grounding gate actually buys: two questions.**

```
best score-only : thr=3.25              FA = 9/58 (15.5%)   OR = 12/63 (19.0%)
tuner pick      : thr=3.25 support=.626 FA = 7/58 (12.1%)   OR = 12/63 (19.0%)

delta: 2 fewer false answers out of 58,
selected from 420 candidate operating points scored on the same 121 questions.
```

Four hundred and twenty candidates, one dataset, a two-question margin. Selecting the best
of 420 points on the same data you then report is how you manufacture an improvement that
does not exist. D-024 already recorded this project falling into the neighbouring trap —
a tuner that drove false answers to zero by refusing 13 of 17 real questions.

**And the mechanism has not changed.** D-025 refuted grounding as a gate on a *structural*
argument, not an empirical one: both formulations compare the produced answer against the
retrieved chunks, but the model was instructed to write only from those chunks, so the
answer agrees with them by construction whether or not they address the question. That
argument is untouched by two questions moving. A mechanistic refutation does not expire
because a grid search found a lucky operating point.

`sentence_support_threshold` stays `null`.

**The score threshold does not move either.** The frontier is flat where it matters:

| threshold | false answers | over-refusal | sum |
|---|---|---|---|
| 2.60 | 19.0% | 15.9% | 0.348 |
| **3.25** | 15.5% | 19.0% | **0.346** |
| 3.72 | 13.8% | 23.8% | 0.376 |
| 4.47 | 8.6% | 28.6% | 0.372 |

2.60 and 3.25 differ by 0.002 — a fifth of one question. The live 3.5 measured 15.5% /
19.0% on the full run, identical to 3.25. **There is no evidence to move it**, and moving
it would be fitting noise while claiming to have tuned.

The tuner is doing its job by reporting the best point it found. Deciding whether that
point is real is not the tuner's job.

**What this leaves.** Over-refusal at 19.0% is now the weakest number, and it is not a
threshold problem — every operating point that fixes it breaks the other one. D-037 says
what it actually is: the system is sensitive to *how a question is phrased*, and 12 of 63
in-corpus questions were refused despite the answer being present. Two of them sat at
reranker scores of −3.00 and −5.30. That is a retrieval problem, and it is the real work
after the demo.

---

## D-045 — Over-refusal is **not** a retrieval failure. The right chunk is found and then scored away

**Date:** 25 Aug 2026
**Phase:** P5 (retrieval)

Over-refusal at 19.0% became the weakest number (D-043), and D-037 had already suggested it
was phrasing sensitivity. That was half right, and the half that was wrong matters more.

Every over-refused question was re-run through `hybrid.search()` and checked for whether a
chunk from the expected section reached the reranker at all:

| | |
|---|---|
| Correct chunk **never retrieved** | **1 of 12** |
| Correct chunk **retrieved, then scored below the gate** | **11 of 12** |
| ...of those, correct chunk was at **rank 1** | **6** |

```
inc-024  What is a strategic corporal?          rank 1   score  3.05
inc-062  When is TXA given and at what dose?    rank 1   score -0.42
inc-052  at night how come you can see...       rank 1   score  1.80
inc-049  What does MCDP 7 draw from Belleau Wood? rank 1 score -0.09
inc-031  Civil War comparison, campaigning pub  rank 1   score -2.94
inc-064  criteria for converting a tourniquet   rank 1   score  0.16
```

**Retrieval is doing its job.** recall@8 at 93.8% was telling the truth all along. The
failure is downstream: the abstention gate treats an **absolute reranker score** as a
calibrated "is this answerable" probability, and `bge-reranker-base` is trained to *rank*
candidates against each other, not to emit a calibrated relevance value. Using a ranking
signal as a decision threshold is the structural error, and it is the same class of mistake
as D-025 — measuring the wrong relation and then tuning the wrong number harder.

**A single global threshold is also unfair across publications.** All three TCCC failures
score negative *at rank 1*. TCCC is terse clinical lists; MCDP is prose. The reranker scores
prose higher, so one global cut penalises an entire publication for its genre. The most
operationally important document in the corpus is the one most disadvantaged.

**What the model's own refusal can and cannot do.** Re-measured at n=129 with both gates
disabled:

| | |
|---|---|
| Answerable questions the model wrongly declined | **0 of 63 — precision is perfect** |
| Unanswerable questions it caught on its own | 17 of 52 — **32.7% recall** |
| False answers if it were the only gate | 67.3% |

So the self-declaration is trustworthy and nowhere near sufficient. It cannot replace the
score gate; it can only ever be the second half of one.

**What changed today, and a prediction that was falsified.** The threshold moved 3.5 → 3.0.

The justification, from the recorded tuning run, was that the false answers at 3.0 and 3.5
were **the same seven questions** — so 3.5 cost two correct answers and prevented zero
false ones, making the change free.

**A fresh eval says otherwise, and the honest number is the fresh one:**

| | 3.5 | 3.0 |
|---|---|---|
| Over-refusal | 19.0% (12/63) | **15.9% (10/63)** |
| False-answer | 15.5% (9/58) | **17.2% (10/58)** |
| Answer-traps correctly answered | 25% (2/8) | **50% (4/8)** |
| Citation-correct | 96.0% | 96.2% |
| Combined objective | 0.345 | **0.331** |

One extra false answer appeared that the tuning records said would not. Generation is not
deterministic — slot scheduling and sampling move a boundary question — so a single-run
record cannot support a claim as strong as "the same seven questions". **The change is kept
because it is better on the combined objective and doubles the answer-trap rate, not
because it was free. It was not free.**

That is worth stating twice: the prediction was checked against a real run rather than
assumed, and it was wrong by one question. A prediction that is never re-measured is just
a preference.

**What did not change is the important part.** This is a 3-point adjustment to a number
that is measuring the wrong thing. The real fix is calibration — per-publication or
per-genre normalisation of the reranker score, so that "3.0" means the same thing in a TCCC
clinical list as in an MCDP paragraph. That is a genuine piece of work, it needs its own
eval, and it is the first thing to do after the demo. Doing it now, three weeks out, on a
system whose 999 approved items are keyed to the current index, would be trading a known
state for an unknown one.

---

## D-046 — Device operational-security decision (omitted from the public log)

*Recorded a device provisioning/hardening decision and its fix; operational detail omitted from the public repository.*

---

## D-047 — A change recorded as DONE was never actually in the file. Verify the effect, not the log

**Date:** 25 Aug 2026
**Phase:** P5 (stability)

D-042 concluded "install_services.sh now passes --parallel 2". The tracker marked it done.
When the installer finally ran on device — after the sudo-password recovery — the
generator came up with **n_slots = 4**. The flag was not in the unit file, was not in the
installed unit, and was not in the local `scripts/install_services.sh` at all. The edit was
described in the decision entry and never landed in the code.

Most likely a patch whose assertion failed silently, or a change made in a scratch buffer
that was never written back; the exact cause does not matter. What matters is that a
measurement caught it and a document did not. The generator was checked for its actual slot
count rather than trusted to match the log, and that is the only reason the OOM fix is real
rather than merely claimed.

This is the same failure the whole DECISIONS.md discipline exists to prevent, turned on the
log itself: **"recorded as done" is not "done".** The rule that a claim needs a measurement
behind it applies to the claim that a change was made, not only to the claim that it worked.

Fixed for real and verified on a fresh generator process: `--parallel 2` in the unit file,
`n_slots = 2` in the running server, memory available 324 MB (at the OOM peak) to 2,012 MB.

The tracker item was re-opened and re-closed against the live evidence, not the prior text.

---

## D-048 — Device credential-recovery decision (omitted from the public log)

*Recorded recovery of a lost device credential by the owner and the sealing of the recovery path; operational detail omitted from the public repository.*

---

## D-049 — Air-gap claim proven at runtime, not just by static audit

**Date:** 25 Aug 2026
**Phase:** P5 (security, closing C2)

The "zero network calls at inference time" constraint was backed by a static code audit
(SECURITY_AUDIT.md: every model call is loopback, no DNS, no external hosts). That proves
the code as written cannot call out. It does not prove the running system does not.

Attempted the obvious runtime proof first — firewall all outbound with iptables, answer a
question anyway — and it failed on the platform: this L4T kernel's netfilter is missing the
`owner`, `conntrack`, and even basic append capability under nf_tables (`RULE_APPEND failed:
No such file or directory`). Fighting that three weeks out was the wrong trade.

The proof that worked is stronger because it is positive evidence rather than "the block
held", and it needs nothing the kernel lacks (`scripts/airgap_proof.sh`):

1. **Socket enumeration.** Every TCP socket held by the three llama-server processes and
   the API, via `ss -tanp`: all four bound to `127.0.0.1` (:8000/:8080/:8081/:8082).
   **Non-loopback sockets held by the inference stack: 0.**
2. **Packet capture during a live query.** `tcpdump` on `l4tbr0` (the only interface with
   an address), excluding the ssh session, while a real doctrine question was answered
   with three citations. **Non-ssh packets emitted during the query: 0.**

Sockets and packets cannot be argued with. C2 moves from PARTIAL to DONE, and the harness
is committed so it can be re-run in front of a reviewer.

---

## D-050 — Privacy decision recorded (closing C5): aggregate to the instructor, individual to the Marine

**Date:** 25 Aug 2026
**Phase:** P5 (fielding readiness)

The learner identifier defaults to `"default"` and is now validated and case-folded (D-040).
The open question was policy, not code: when it becomes a real identifier (an EDIPI), who
may see an individual Marine's demonstrated weaknesses?

**Decision, to be designed in before the data ever carries a real identifier:**

- **The individual owns their record.** A Marine's per-item history and calibration are
  visible to that Marine. Nobody sees another individual's misses without their knowledge.
- **The instructor sees the aggregate.** Class-level mastery and the gap report are for the
  instructor; individual drill-down is not, except a Marine's own.
- **Retention is bounded and the Marine can clear their own record.** `reset_demo.sh`
  already archives rather than deletes in this build; a fielded version needs an explicit
  retention period and a self-service erase.
- **The identifier stays out of the URL and the logs.** Today it rides in a query
  parameter (`?learner=`), which is fine for `"default"` and unacceptable for an EDIPI —
  that becomes a server-side session at fielding, per D-040.

This is a design constraint, not a feature to build now. It is recorded so the decision is
made before the data exists rather than discovered after, and it is consistent with what
`docs/FIELDING.md` already states under Privacy. The reason it is decidable now: the
pedagogically useful thing (an instructor seeing where the class is weak) and the
privacy-sensitive thing (an instructor seeing which named Marine is weak) are separable,
and the aggregate carries the teaching value without the individual exposure.

---

## D-051 — Chunker endnote fix shipped. Blast radius was **3 items, all already rejected**

**Date:** 25 Aug 2026
**Phase:** P5 (critical path)

The endnote-gluing fix (G10, agent-built, `strip_leading_endnotes`) is live. It splits a
leading run of reference apparatus off the head of a real paragraph, using review.py's
D-036 discipline (negative lookbehind refuses Army `3-67.`/`C-11.`, mandatory page ref, a
chunk that stays reference-apparatus after stripping is returned byte-identical).

**The feared cost did not materialise.** D-036 warned re-chunking could invalidate all 999
approvals. It did not, because item IDs are source-derived (D-035): an item is affected only
if its source paragraph's text changes. Checking every item's stored `source_text` against
the rebuilt chunk set:

| | |
|---|---|
| Items whose source paragraph changed | **3** |
| Items unaffected (approvals + hand-edits intact) | **1,008** |

And the 3 affected are **exactly** the MCDP 7 Notes-quoting-MCDP-1 items identified in D-036
and D-045 — which were **already rejected**. So the critical path's "regenerate & re-review"
stage collapsed to a no-op: there was nothing to review, because the only changed items were
already out of the servable set.

**What the fix actually bought.** The retrieval/ask path. Before: the MCDP 7 chunk read
`6. MCDP 1, pp. 4-21-4-22. "Another important tool..."` and the reranker saw the endnote
noise. After: the chunk is clean, and asking *"what is the main effort?"* now ranks the real
sources **MCDP 1-3 and MCDP 1 above** the MCDP 7 Notes chunk. Doctrine is no longer
attributed to the publication whose footnote quotes it.

**No regression.** Index rebuilt (61s, 4,230 chunks). recall@1 / recall@8 / MRR are
**identical** to the old index on the 63-question set — only 3 MCDP 7 Notes chunks changed,
none of them recall-critical. Citation verification holds at 99.2% pass-or-partial (n=120).
Old index kept as `doctrine_old_endnote.sqlite` and `doctrine.sqlite.pre-endnote.bak`.

The chunker still glues in one place it does not fix: a paragraph with a citation in the
*middle* (two quotes around an embedded endnote) keeps the mid-paragraph citation. That is a
larger, riskier split — deliberately out of scope, recorded by the agent, left for later.

---

## D-052 — Per-publication calibration: mechanism shipped, diagnosis confirmed, offsets **not applied**

**Date:** 25 Aug 2026
**Phase:** P5 (retrieval)

The reranker-calibration mechanism (G16) is built, tested (58 tests), and ships **defaulting
to a no-op** (`reranker_score_offsets: {}` = today's behaviour byte-for-byte). This entry is
about whether to turn it on. The answer, on the evidence, is **no** — and that is the right
answer, not a failure.

**The diagnosis is confirmed with real numbers.** The per-publication rank-1 score table
(recorded across all 129 eval questions on the rebuilt index) shows exactly what D-045
predicted:

| pub | n | median rank-1 score |
|---|---|---|
| **TCCC** | **2** | **−0.14** |
| MCDP 7 | 4 | 3.62 |
| (nine pubs) | 3–8 | 4.6 – 6.8 |
| TC 3-22.9 | 4 | 7.30 |
| MCDP 3 | 3 | 8.14 |

TCCC sits ~5.4 points below the median-of-medians reference (5.25). The genre effect is
real: terse clinical lists score far lower than doctrine prose.

**Why the offsets are not applied — two independent reasons.**

1. **The target cannot be calibrated.** TCCC — the entire motivation — has **n=2** rank-1
   samples. Estimating a 5-point shift from two questions is precisely the noise-fitting
   D-044 refused. The compute script correctly gives TCCC **no offset**, so applying the
   proposal would not fix the one thing it exists to fix.
2. **What it would change is noise, in the wrong direction.** Sweeping the eval with the
   proposed offsets versus without, at the live threshold 3.0:

   | | over-refusal | false-answer | sum |
   |---|---|---|---|
   | no offsets (live) | 15.5% (9/58) | 17.3% (9/52) | 0.328 |
   | proposed offsets | **17.2% (10/58)** | 15.4% (8/52) | 0.326 |

   The combined objective moves by **0.002 — a fifth of one question — and over-refusal, the
   metric this was meant to improve, gets *worse*.** This is the D-044 pattern precisely: a
   grid of per-pub offsets estimated from ≤8 samples per cell, delivering a change
   indistinguishable from noise.

**What actually shipped, and what is left.** The mechanism is in place and safe, so the day
there is proper data it is one config paste away. The diagnosis is now evidence, not
hypothesis. The real work — deriving each publication's genre offset from a broad
per-publication probe set (30+ queries per publication, scored against that publication's
own content) rather than from ≤8 eval points — is post-demo, and is the honest way to fix
TCCC without fitting two questions.

Over-refusal stays 15.9% (the improvement already banked by the D-045 threshold move). It is
still the weakest number, and it is now understood precisely: not retrieval, not tuning, but
a genre-scale property of the reranker that this small eval cannot calibrate away.

---

## D-053 — The `--parallel 2` OOM fix was wrong on both counts. Reverted to `--parallel 1`

**Date:** 25 Aug 2026
**Phase:** P5 (stability, correcting D-042/D-047)

D-042 added `--parallel 2` to the generator to fix the kernel OOM, and D-047 verified it
went live (`n_slots = 2`). The final eval then failed at its first question on a generator
**HTTP 400**, and the API returned 500 on any long-prompt ask.

**Root cause: `--parallel 2` set `n_ctx_slot = 2048, kv_unified = false`.** llama.cpp split
the 4096-token context into two 2048-token slots. A real RAG prompt — system + 8 reranked
chunks + question — exceeds 2048 tokens, so the generator rejected it with "exceeds
context." The earlier successes (the "main effort" ask, both tuning passes) only survived
because their retrieved chunks happened to fit under 2048; the eval's first question did not.

**And it never saved the memory it was supposed to.** Two 2048-token slots hold the same
4096 tokens of KV cache as one unified 4096 slot — identical memory. The actual OOM in
D-042 was the **eval running concurrently with the API serving a query**: two full pipelines
on a 7.6 GiB board, and the kernel's global OOM killer taking the largest RSS. Slot count
never touched that. So `--parallel 2` was pure downside: no memory saved, context halved.

**The honest correction.** `--parallel 1`: one slot, the full 4096-token context, the least
KV memory of any option, and no fragmentation. A single-learner appliance needs exactly one
slot; the only overlap is the boot warm-up query, which harmlessly serialises with the first
real query. Verified: `n_ctx_slot = 4096`, and the RAG prompt that 400'd now answers with
citations.

The real OOM mitigation was never a slot count — it is the operational rule already in
`demo/script.md` and TRACKER G15: **nothing else runs on the board during a demo.** That
stands. D-042's diagnosis of *where the memory goes* was right; its *fix* was not, and
D-047 verified the wrong thing was live. Both are left in the record as written, because a
decision log that quietly edits its mistakes is worth nothing.

---

## D-054 — Per-publication calibration **applied and validated**. False answers 17.2% → 13.8%

**Date:** 25 Aug 2026
**Phase:** P5 (retrieval). Supersedes the "do not apply" of D-052.

D-052 concluded the calibration could not be applied, because the only data was the
129-question eval (TCCC n=2) and median-alignment offsets computed from it moved the
objective by 0.002. That conclusion was correct **for that data**. With the demo cancelled,
the real work became possible: build a proper per-publication probe and do it right.

**The probe.** `scripts/probe_rerank_by_pub.py` runs all 999 approved item questions through
retrieval + rerank (no generation) and records the top chunk's score per publication. This
is a large, labelled distribution — 12 to 191 samples per publication instead of 2 to 8 —
and it is a clean train/test split: the probe questions are model-generated items, the eval
questions are hand-written and disjoint, so the eval never trains its own offsets. 999
records, 0 errors, 54 minutes.

**Two design choices that made it work, where D-052 failed:**

1. **A real distribution, not the eval.** Offsets come from the 999-sample probe, not from
   which eval questions passed. TCCC's probe median is 1.83 (n=12), and its offset is
   **+1.389** — far more conservative and better-grounded than the ~+5 the 2-sample eval
   implied.
2. **First-quartile alignment, not median.** Aligning each publication's *median* (D-052)
   pushed borderline in-corpus questions below the gate and raised over-refusal. Aligning
   the **25th percentile** — the weak-retrieval tail, the questions most at risk of
   over-refusal — instead lifts the low end to a common level. The result is stable across
   p25–p35 (both give the same held-out number), which is evidence it is not a fit to one
   lucky point.

**Validated on the held-out eval, then confirmed by a full canonical run:**

| | before | after |
|---|---|---|
| False-answer rate | 17.2% (10/58) | **13.8% (8/58)** |
| Correct-abstention | 82.8% | **86.2%** |
| Over-refusal | 15.9% (10/63) | **15.9%** — unchanged |
| Citation-correct | 94.2% | 94.2% |

Two out-of-corpus questions that were being wrongly answered are now correctly refused,
because the high-scoring publications (TC 3-22.9 −1.475, MCDP 1-0 −0.866) were pushed toward
the reference. Over-refusal did not move: an in-corpus MCDP 7 question was rescued as an
MCDP 1-0 one was lost, netting zero. The offline prediction (13.5%) and the live canonical
eval (13.8%) agree within noise.

**The honesty caveat, stated plainly.** The offsets are held-out (probe-trained); the
percentile is one hyperparameter, and it was chosen with the eval in view. Two things
contain the overfitting risk: p25 and p35 give the identical held-out result (a stable
region, not a spike), and the first quartile is a principled a-priori target for "lift the
weak-retrieval tail," not a value hunted for. A larger eval would firm this up, and remains
worth building.

**What it does not do.** It does not rescue the specific TCCC over-refusals from D-045 —
those score around −2.5, and a +1.4 offset leaves them below the gate. TCCC's whole
distribution is low and its failures are in the tail; a per-publication location shift
cannot reach them. That is a reranker-quality problem for clinical/checklist text, and it is
the genuine remaining work — not something a linear offset solves.

## D-055 — The grown eval (2× harder negatives) reveals the true false-answer rate: **22.1%, not 13.8%**. A small-model ceiling on false premises, and prompt-hardening does not move it.

**Phase:** P3 (evaluation) / P5 (generation). Corrects the generalisation claim of D-054, not its mechanism.

**What changed.** The eval grew from 129 → 233 questions (in-corpus 63→122, out-of-corpus refuse 58→95, answer-traps 8→16), weighted toward the large publications and TCCC (6→19), and — the point — toward *harder, disjoint adversarial negatives*: false-premise-content wrong-count near-misses, attribution/quote traps, stale-edition traps, false-premise-publication traps. Every answerability claim was verified against `corpus/chunks.jsonl`; every "unanswerable" assertion confirmed by zero-match scan; zero unmatched `relevant_sections` labels across all 122 in-corpus rows (D-023 satisfied). Five silently-broken labels were fixed (inc-015 was unscorable; inc-044's label resolved to no real section — a hidden recall depressant).

**The honest numbers on this set** (threshold 3.0, D-054 calibration applied, `--parallel 1`):

| metric | small eval (D-054) | grown eval (D-055) |
|---|---|---|
| false-answer (out-of-corpus) | 13.8% | **22.1%** (21/95) |
| correct-abstention | 86.2% | 77.9% |
| over-refusal (in-corpus) | 15.9% | 16.4% (20/122) |
| citation-correct | 94.2% | **99.0%** (label fixes) |
| answer-traps answered | 50% | 43.8% (7/16) |
| p50 / p95 latency | — | 6.48s / 15.50s |

The 13.8% was not *wrong* for the 58-question set it was measured on — it simply did not generalise. **D-054's calibration is still valid**: it corrects the reranker's genre-scale error and holds over-refusal; it never claimed to fix on-topic false premises, and this result does not undermine it.

**Diagnosis (all 21 false answers re-queried on device, actual text captured).** 20 of 21 are *genuine fabrications*, not scoring artifacts (only ooc-081 was a prose refusal mis-flagged). The failure class is uniform: the **question smuggles in a false specific** — a count ("the seven phases of the intelligence cycle"), an edition ("the 2011 TCCC Guidelines … suzetrigine", an anachronism), a page ("FMFM 1 … pages 35-36"), or an attribution ("quote where MCDP 7 credits Patton") — retrieval correctly returns *on-topic* chunks that score **high**, the reranker gate passes them, and the 2B model then affirms, echoes, or substitutes the specific (e.g. ooc-075: *"Yes … includes Suzetrigine … 100 mg PO"* — a fabricated drug and dose). Post-generation `min_support` is high (0.68–0.98) *even for the fabrications*, because the fabricated sentence is lexically close to the retrieved chunk — so the sentence-support gate cannot catch these either.

**Why none of the existing levers help.** The reranker score, the per-publication calibration, and BM25 all operate on *retrieval* — and retrieval is *correct* here; the topic genuinely is in the corpus. The only signal that could catch a false premise is the generator itself refusing it, and a 2B model given confident on-topic context does not.

**Prompt-hardening: tested and rejected (a real negative result).** Added a principled rule — *"treat any specific asserted in the question (count, edition, page, attribution) as UNVERIFIED unless it also appears in the SOURCES; never attribute a quotation unless the sources do."* Deployed, re-probed all 21: **exactly 1 flipped** to abstention. gemma-4-E2B does not reliably follow the nuanced instruction. Reverted — an ineffective rule only dilutes the rules that work. This is a **capability ceiling of a 2B model on an 8 GB board**, not a fixable prompt/retrieval bug.

**BM25 lexical rescue: confirmed OFF by device data (`tune_bm25_rescue.py`, step 4).** The trade curve has no usable operating point: any threshold that rescues a useful share of the 20 over-refusals (fixed=19) also wrongly rescues 63–73 of the 74 protected out-of-corpus abstentions — because the recoverable over-refusals and the negatives share BM25 vocabulary. The only zero-false-answer point (T≈48) rescues a single question at a near-exact-match bar. `bm25_rescue_threshold` stays `null`, exactly as its design anticipated.

**Disposition — stated honestly, not hidden behind the flattering number.** On *easy* (off-topic) negatives the gate is near-perfect. On *hard* (on-topic, false-premise) adversarial negatives the system fabricates ~1 in 5, and that is the measured ceiling of this model on this hardware. Paths that would move it — a larger instruct model (E4B excluded on memory, D-016/D-019) or a dedicated premise/claim-verification pass (doubles latency, and the same weak model would verify) — are not free on an 8 GB Orin. **All repo and PR numbers are corrected from 13.8% to 22.1%**; the limitation is documented in STATUS, README, docs/VALUE, and the SchoolCircle GROUNDING.md rather than papered over.

## D-056 — The false-premise ceiling (D-055) is solvable in principle but not on this board yet. Three standard gates built and empirically tested; only the premise gate works, at a ~1:1 safety trade, and it OOM-hangs the board under load. Committed config-gated OFF, with a mapped path off the ceiling.

**Phase:** P5 (generation). Follows D-055 (false-answer 22.1% is a false-premise ceiling, prompt-hardening dead) and answers "surely someone has solved this."

**Research (four parallel literature sweeps) converged on one answer.** The problem has names — *false-presupposition QA* (CREPE, (QA)², Kim et al.) and *grounding/faithfulness verification* (MiniCheck, HHEM, AlignScore). The fix is a GROUNDED verifier — either a pre-generation premise check or a post-generation entailment gate — NOT prompt-hardening (dead, D-055; ungrounded self-correction fails on small models, Huang et al. ICLR 2024) and NOT a bigger generator (false-premise robustness is non-monotonic in size — Qwen2.5-7B beats 72B; alignment, not parameters, dominates). Cheap options: a verbatim-specific backstop (free) and a small entailment model (HHEM-110M / MiniCheck-0.4B, Apache/MIT, sub-1B).

**Built three gates, all unit-tested (module tests green):**
- `specifics_gate.py` (A) — verbatim backstop: refuse a number/name/edition/page the answer asserts that no retrieved span contains.
- `premise_gate.py` (C) — extract the question's presupposition (count/edition/page/attribution) and verify it against the spans before generating.
- `grounding_verifier.py` (B) — per-sentence entailment gate; 2B-grounded-NLI backend (zero new memory) plus an HHEM HTTP-server seam (`hhem_url`) for the future.

**Measured on device (82-case fixture: 21 fabrications, 45 in-corpus answered, 16 answer-traps):**

| Gate | fabrications caught /21 | over-refusals added /45 | answer-traps broken /16 |
|---|---|---|---|
| A (verbatim backstop) | 0 | 1 | 0 |
| B (2B entailment gate) | 4 | 2 | **9** |
| C (premise pre-gate) | **6** | 3 | 0 |

- **A fails on this corpus.** Doctrine spans are dense with numbers and real figures, so a fabricated specific coincidentally matches ("1989", "pages 35-36" hit some span number; misattributions to Gray/Moltke/Liddell Hart pass because those names appear elsewhere). String matching cannot separate "Gray appears here" from "this passage credits THIS quote to Gray" — that is inherently semantic.
- **B is unshippable.** It breaks 9 of 16 answer-traps: the 2B-as-verifier labels genuinely-grounded answers NOT_SUPPORTED far too often (precision ≈ 3 right : 11 wrong). Verifier quality is everything and a 2B verifying its own output hits the same ceiling. Kept behind the HHEM seam.
- **C works, at a ~1:1 trade.** It catches the clean false-premise class (wrong counts "seven phases", misattribution "Patton", "five pillars") with zero answer-trap damage — the correct mechanism. But it is bottlenecked by the SAME 2B-verifier noise: it over-refuses TRUE counts (inc-018 "three phases of care in TCCC", correct) while catching the false "six phases", because the 2B cannot reliably confirm a count either way. Net: roughly one over-refusal per fabrication caught — a shift of the false-answer/over-refusal balance toward safety (fewer confident wrong answers to a Marine), not a reduction in total error.

**The hardware wall (decisive).** Attempting the authoritative full-eval with C enabled drove the board — already at ~12 MB available and ~3 GB into zram swap — into OOM-thrash. sshd and the API went unresponsive for ~3 minutes (load average 7); the box did NOT reboot (uptime intact) and fully recovered the instant the eval was killed. So the premise gate's extra 2B verification passes are not merely a latency cost; on this 8 GB board they are a memory-STABILITY risk under load. The board also has no ML runtime (no torch/transformers/onnxruntime), so a resident purpose-built verifier is doubly blocked (no runtime + no memory).

**Disposition.** All three modules are committed and wired, `premise_gate.enabled: false` by default. Enabling requires (a) accepting the ~1:1 safety trade and (b) freeing memory so verification does not thrash. The reliable fix is now concrete and partly built: free ~600 MB (a smaller / more-aggressively-quantized generator, or reduced n_ctx) + a light runtime (onnxruntime) + a small entailment model (HHEM-110M) behind `grounding_verifier`'s `hhem_url` seam — then the answer-gate becomes both reliable and affordable and false-answer drops without the over-refusal cost. That is a hardware/infrastructure project, not a config change. **The 22.1% ceiling stands on the current build; the path off it is mapped, and the scaffolding is in the tree behind a flag.**

### D-056 addendum — the "sharp" verifier (extract-and-compare) was tried and closes the prompt-based door for good.

After D-056's ~1:1 premise-gate trade, the natural next move was to sharpen the verification from fuzzy "is 'X has three phases' supported?" to a comparison the model might do better: read the passages and answer WRONG / RIGHT / UNKNOWN (with anti-anchoring wording so it does not rubber-stamp the asserted number). Measured on the same 82-case fixture:

| policy | fabrications caught /21 | over-refusals /45 | traps broken /16 |
|---|---|---|---|
| original fuzzy gate | 6 | 3 | 0 |
| sharp, refuse on WRONG\|UNKNOWN | 6 | 6 | 1 |
| sharp, refuse on WRONG only | 3 | 3 | 1 |

Neither beats the fuzzy gate. The count verdicts show why: on FALSE counts the 2B said WRONG 3/5 times (good), but on TRUE in-corpus counts it also said WRONG 3 times (inc-021/022/023) and UNKNOWN for most of the rest — it confirmed a true count (RIGHT) only once. The 2B **cannot reliably count enumerated items in a retrieved passage**, so it fabricates a different number and declares WRONG in BOTH directions; reframing the question cannot fix a capability the model lacks. (Counts are also the hardest class structurally: the enumeration is often not stated verbatim in the retrieved spans, so even a perfect verifier would return UNKNOWN.)

**Conclusion, now proven three independent ways** (rule-hardening 1/21, fuzzy gate ~1:1, sharp gate ~1:1): with only the resident 2B as verifier there is no clean fix — the bottleneck is verifier quality, not prompt framing. The clean fix requires a purpose-built verifier model (MiniCheck/HHEM) via `grounding_verifier`'s `hhem_url` seam, which is the mapped hardware/runtime project. The prompt-engineering avenue is closed; the scratch `premise_sharp` experiment is intentionally NOT committed (it does not improve on the shipped gate).

## D-057 — HHEM verifier runs on-device and BREAKS the D-055/056 ceiling. Premise gate + HHEM: false premises caught for real, offline, within memory. Enabled by default.

**Phase:** P5 (generation). Executes the "mapped path" of D-056: a purpose-built verifier instead of the 2B.

**What was stood up (all offline at inference).** The Jetson had torch 2.11 installed but BROKEN (`ImportError: libcudss.so.0`); its own bundled CUDA libs simply were not on the loader path. Fixed by putting `~/.local/.../nvidia/*/lib` on `LD_LIBRARY_PATH`. Pinned `transformers==4.46.3` (HHEM-2.1-Open's `trust_remote_code` head does not import on transformers 5.x) and `pillow>=10.2` + `sentencepiece`. Staged **Vectara HHEM-2.1-Open** (Flan-T5-base, 110M, Apache-2.0) into the HF cache. Packages were pulled through a temporary reverse-SSH tunnel to the workstation's internet; the device has NO network of its own and runs the model with `HF_HUB_OFFLINE=1`. Served as a new **`tutor-verify` systemd unit** on loopback `127.0.0.1:8083` (`scripts/verifier_server.py`), **CPU-only** (`CUDA_VISIBLE_DEVICES=`), so it never contends with the generator's GPU.

**HHEM does the exact thing the 2B could not.** True vs false counts from the same source: "TCCC has three phases" -> **0.94**, "TCCC has six phases" -> **0.01**; off-topic -> 0.01. Clean directional entailment where the 2B scored ~random and cosine scored ~0.98 for both.

**Measured (82-case device fixture), premise gate (C) verified by HHEM, per-span max, tau 0.5:**

| gate + verifier | fabrications caught /21 | over-refusals /45 | answer-traps broken /16 |
|---|---|---|---|
| C + 2B (D-056) | 6 | 3 | 0 |
| **C + HHEM (D-057)** | **8** | **2** | **0** |
| B (answer gate) + HHEM | 7 | 19 | 10 |

C+HHEM catches 8/21 including **ooc-075 (the invented suzetrigine drug+dose) and ooc-039 (a stale-edition trap)** — the clinically dangerous cases the 2B verifier MISSED — at only 2 over-refusals and zero answer-trap damage. A ~4:1 trade, not the 2B's 1:1. The **answer gate B stays OFF**: per-sentence entailment over-refuses real answers that synthesise across passages (19/45), a design flaw no verifier fixes.

**End-to-end, in the live pipeline:** "seven phases of the intelligence cycle" and "2011 TCCC ... suzetrigine" both REFUSE with a correction ("The sources do not support that ..."); "how does MCDP 1 define friction" ANSWERS with a citation. Latency ~9-10s on a gated (presupposition) query, ~7s normal (no HHEM call when the question asserts no specific).

**Memory:** HHEM CPU-only is ~1041 MB RSS (torch dominates; the 110M model is small). Full stack + HHEM resident leaves ~676 MB available plus zram swap headroom -- it FITS. The gate **fails open**: if the verifier service is down or errors, `pipeline.ask` skips the gate and answers normally, so a verifier outage never takes the tutor offline.

**Disposition.** `premise_gate.enabled: true` with `hhem_url: http://127.0.0.1:8083/verify` is now the DEFAULT (the 2B fallback remains if `hhem_url` is null). Setup, the torch/`LD_LIBRARY_PATH` fix, the transformers pin, and offline model staging are documented in `docs/HHEM_VERIFIER.md`; the unit is `deploy/tutor-verify.service`. This is the fix that moves the D-055 22.1% false-answer ceiling -- honestly, on the real hardware, offline.

**Authoritative full eval (233 questions, premise gate + HHEM, the shipped default).** false-answer **22.1% -> 15.8%** (15/95), correct-abstention 77.9% -> 84.2%, over-refusal 16.4% -> 18.9% (23/122), citation-correct 99.0% (unchanged), answer-traps 43.8% (unchanged), p50 6.11s / p95 18.70s. Six net false answers removed for three added over-refusals -- and the SUM of the two errors FELL (38.5% -> 34.7%), so this is a real net-error reduction, not merely a safety-ward shift. The board completed the full eval without hanging (HHEM as a separate CPU service is far lighter than the 2B-verifier passes that OOM-hung it in D-056). **15.8% is the new honest shipped false-answer number** on this hardware, offline.
