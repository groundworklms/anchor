# P0 benchmark table

**Jetson Orin Nano 8GB** (module `p3767-0003`, EEPROM `699-13767-0003-300`) —
JetPack 6.2.1 / L4T R36.4.3 / CUDA 12.6, llama.cpp `a94d563` built for `sm_87`.

All figures measured on device. GPU use confirmed via `tegrastats` (`GR3D_FREQ 99%`) on
every throughput row — throughput alone does not prove the GPU was used, and this build
silently accepts `-ngl -1` and runs on CPU (DECISIONS.md D-018).

---

## Generator throughput and power

| Model | Power mode | pp512 (t/s) | tg128 (t/s) | Peak RAM | Peak power |
|---|---|---|---|---|---|
| **gemma-4-E2B** QAT Q4_0 | **15W** | **717.57 ± 18.08** | **20.86 ± 0.02** | 5,027 / 7,620 MB | 11.4 W |
| gemma-4-E2B QAT Q4_0 | 7W | 260.37 ± 2.43 | 8.20 ± 0.02 | 4,923 / 7,620 MB | 7.2 W |
| gemma-4-E4B QAT Q4_0 | 15W | 396.72 ± 6.30 | 11.86 ± 0.01 | **6,945 / 7,620 MB** | 12.3 W |
| gemma-4-E4B QAT Q4_0 | 7W | 139.29 ± 1.13 | 4.49 ± 0.00 | 6,949 / 7,620 MB | 7.4 W |

Loaded sizes: E2B 3.10 GiB / 4.63 B params; E4B 4.79 GiB / 7.46 B params.

**E4B is excluded from the product on measured memory, not on preference.** At 15W it
leaves **675 MB** free against E2B's 2,593 MB — not enough for `bge-reranker-base` alone,
before the embedder, the API and a kiosk browser. E2B is also 1.76× faster at generation.

The 7W row is the deployability row: **8.20 tok/s at 7.2 W still exceeds reading speed.**

---

## Cold load time, per model

Measured with the page cache dropped (`echo 3 > /proc/sys/vm/drop_caches`) immediately
before each start, timed from `systemctl start` to the server's `/health` returning ok.

| Model | Cold load |
|---|---|
| `bge-small-en-v1.5` q8_0 (embeddings) | **0.7s** |
| `bge-reranker-base` q8_0 (reranker) | **4.1s** |
| `gemma-4-E2B` QAT Q4_0 (generator) | **8.6s** |

The three units start in parallel at boot, so the effective cost is the slowest, not the
sum.

---

## Resident memory, all three servers

| Server | Resident |
|---|---|
| generator (E2B) | 2,477 MB |
| reranker | 524 MB |
| embedder | 284 MB |
| **free for API + browser** | **~1,950 MB** |

The reranker figure is the interesting one: it was **1,242 MB** until the batch buffers
were right-sized. It had been started with `-b 2048 -ub 2048` while the model caps at 512
tokens, so those buffers were pure waste. Correcting all three servers freed roughly a
gigabyte — the headroom the kiosk browser needs. The flags in
`scripts/install_services.sh` are load-bearing; do not tidy them away.

---

## System

| Metric | Measured |
|---|---|
| **Cold boot to a cited answer** | **52.9s – 60.7s across 4 runs** (budget 60s) |
| — ssh reachable | 40.1s or 48.1s (bimodal) |
| — all four services healthy | +0.6s after ssh |
| Query latency, repeated | flat ~9.6s (no warm-up effect) |
| End-to-end p50 / p95 (full eval, incl. fast refusals) | 1.7s / 11.0s |
| Idle power | ~4.8 W |
| Idle temperature | ~49–50 °C |

### Cold boot: state this honestly

Measured four times after the optimisations: **52.9s, 53.3s, 60.7s, 60.7s.** It
**straddles the 60-second budget** and the distribution is bimodal, not noisy — ssh comes
back at either ~40.1s or ~48.1s, nothing between.

The cause is not in our control. `systemd-analyze` accounts for only **17.5s**
(10.8 kernel + 6.7 userspace), and the userspace critical chain reaches `multi-user.target`
in 6.6s. The remaining ~30–38s, and all of the variance, is **UEFI firmware before the
kernel starts**. Confirmed not to be a network-wait stall: `NetworkManager-wait-online` and
`systemd-networkd-wait-online` are both disabled, and NetworkManager itself costs 83ms.

Optimisations applied and kept, in order of effect:
- UEFI boot-menu `Timeout` 5s → 1s (`efibootmgr -t 1`)
- disabled `docker`, `containerd`, `snapd`, `lvm2-monitor`, `ModemManager`
- `BootOrder` reduced to the NVMe entry alone, dropping UEFI PXEv4/v6 and HTTPv4/v6
  attempts — pointless on an air-gapped device and a small attack surface besides
- the API self-warms at startup, absorbing ~2–3s of first-request initialisation

**Do not claim "under 60 seconds" as a flat fact.** The defensible statement is: *"about
55 seconds, measured between 53 and 61 across four cold boots, with the variance in
firmware rather than software."* In practice the relevant number is different
anyway — see the reset below.

| Operation | Measured | Use |
|---|---|---|
| **One-key reset to clean state** | **25s**, repeatable | what you actually use on stage |
| Cold boot to a cited answer | 52.9–60.7s | the fielding claim |

A reset is the operation a demo actually needs: it archives records, restarts all four
services, and does not return until a real doctrine question comes back answered.

---

## Corpus artifact

| Property | Value |
|---|---|
| Documents / pages / paragraphs | 13 / 1,780 / 4,234 |
| Index size | 15.9 MB, **one file** |
| Index build time (embedding on GPU) | ~94s for 5,046 windows |

The index ships as a **single** `doctrine.sqlite`. It was previously three files because
WAL mode leaves `-wal` and `-shm` sidecars; copying only the `.sqlite` would silently drop
whatever remained in the write-ahead log and make its checksum meaningless. The build now
checkpoints and switches to a rollback journal, so the artifact is hashable and portable.

---

## Reproduce

```
make bench        # throughput sweep across power modes
make boot-test    # cold boot to a cited answer, with a real reboot
make eval         # the trust table
```
