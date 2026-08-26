#!/usr/bin/env python3
"""Directional grounding gate: is each answer sentence ENTAILED by the sources?

WHY THIS EXISTS. Gate 2 in pipeline.py (check_grounding) scores each answer sentence by
EMBEDDING COSINE against the retrieved chunks. DECISIONS.md D-024 recorded why that fails
as an abstention signal, and it fails for a second, sharper reason this module fixes:
cosine measures SIMILARITY, not SUPPORT, and the two come apart exactly where it matters.
A fabricated sentence that says the planning process has "six phases" is ~0.98 cosine to a
source that says "seven phases" -- nearly identical strings, opposite facts. Similarity
cannot tell a faithful paraphrase from a confident contradiction because it never looks at
DIRECTION: does the source ENTAIL the claim? This module asks that question instead.

The nuance that a naive checker gets wrong (and the research is explicit about): there are
THREE outcomes, not two. A claim can be SUPPORTED, CONTRADICTED, or -- the common
fabrication -- one the source is simply SILENT about (neutral / not-supported). Silence is
NOT support. A sentence the passages never mention must FAIL the gate just as hard as one
they contradict; only "SUPPORTED" passes. Treating "not contradicted" as good enough is how
hallucinations slip through, so both NOT_SUPPORTED and CONTRADICTED map to 0.0 here.

HARD MEMORY REALITY -- why the DEFAULT backend carries no model. The Jetson Orin Nano runs
this whole stack in 8GB of shared memory, with the 2B generator, the embedding model, and
the reranker already resident; measured free headroom is under 100MB. A dedicated
entailment model (Vectara HHEM-2.1 / MiniCheck, ~110M params, ~600MB loaded) does NOT fit
alongside them. So the shipped default spends ZERO new memory: it reuses the resident 2B
generator as the entailment judge, asking it one strict yes/no question per claim. The HHEM
route exists as an OPTIONAL upgrade, run OUT OF PROCESS behind an HTTP server
(scripts/verifier_server.py) and selected by config -- see "CONFIG SEAM" below. When a
`hhem_url` is configured, verify() POSTs to that server; otherwise it uses the injected 2B.

DEPENDENCIES. Standard library only. torch/transformers live exclusively in the optional
server; this module never imports them, so it loads on the device with nothing extra. The
2B is reached through an INJECTED callable (ask_model), so this file has no knowledge of the
generator's URL or wire format -- the pipeline wires that in, and tests pass a stub.

CONFIG SEAM. This module is a leaf; nothing here reads config, and pipeline.py/config are
deliberately untouched. Wiring it in is a two-line seam under the existing `grounding:`
block in config/default.yaml, mirroring the abstention seams already there:

    grounding:
      # backend: "2b" (default, zero memory) or "hhem" (optional server upgrade)
      verifier_backend: "2b"
      # only consulted when verifier_backend == "hhem"; loopback, never 0.0.0.0
      hhem_url: http://127.0.0.1:8083/verify
      # support below this fails a sentence; exposed, never hardcoded, tuned on device
      support_tau: 0.5

    GroundingVerifier(ask_model=<2B call>, hhem_url=cfg.get("hhem_url"), tau=cfg["support_tau"])

The 2B backend is the default so the pipeline works with no new memory and no new service;
setting hhem_url (or verifier_backend: hhem) is the only change needed to switch, exactly
the "swap by config, no code change" discipline the rest of the stack follows.
"""
import json
import re
import urllib.request

# Sentence splitter: same shape as pipeline.SENT_SPLIT (split on sentence-final
# punctuation followed by a capital/digit), reproduced here rather than imported so this
# leaf module has no cross-module import to break when push.sh lays the tree out flat.
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")

# A "factual" sentence is one that would carry a citation -- a load-bearing claim, not a
# fragment or connective. pipeline.check_grounding uses the same >25-char heuristic to
# decide which sentences are claims worth grounding; we reuse it so the two gates agree on
# what counts as a claim.
_MIN_CLAIM_CHARS = 25

# The three-way NLI verdict, collapsed to support in [0,1]. BOTH failure modes score 0.0:
# a contradiction and a source-is-silent "neutral" are equally unsupported (see header).
_LABEL_SCORES = {"SUPPORTED": 1.0, "NOT_SUPPORTED": 0.0, "CONTRADICTED": 0.0}

# The strict grounded-NLI prompt handed to the resident 2B. It is deliberately tiny -- one
# short generation, one word expected back -- because it runs once PER factual sentence on a
# memory- and latency-constrained board. It shows the numbered passages, states the claim,
# and forces a single label decided ONLY from the passages. The explicit "if the PASSAGES
# are silent, answer NOT_SUPPORTED" line is the whole point: it stops the model rewarding a
# plausible-but-unstated fabrication, which is precisely what cosine similarity could not do.
_GROUNDED_NLI_PROMPT = """You are a strict grounding checker. Decide whether the CLAIM is fully supported by the numbered PASSAGES, using ONLY the PASSAGES and no outside knowledge.

PASSAGES:
{passages}

CLAIM: {claim}

Answer with EXACTLY ONE of these words, and nothing else:
  SUPPORTED      - every part of the CLAIM is stated in the PASSAGES.
  NOT_SUPPORTED  - the PASSAGES do not state the CLAIM (even if it sounds plausible or is not directly contradicted).
  CONTRADICTED   - the PASSAGES state something that conflicts with the CLAIM.

If the PASSAGES are silent about the CLAIM, answer NOT_SUPPORTED.
ANSWER:"""


def split_sentences(text):
    """Split an answer into candidate sentences, pipeline.SENT_SPLIT style.

    Returns every non-empty trimmed sentence. Filtering down to load-bearing FACTUAL
    sentences (the ones that would carry a citation) is gate_answer's job, kept separate so
    a caller can see the raw split.
    """
    return [s.strip() for s in SENT_SPLIT.split(text or "") if s.strip()]


def is_factual(sentence):
    """A sentence long enough to carry a claim (and thus a citation).

    Same >25-char rule pipeline.check_grounding uses to pick which sentences to ground, so
    the entailment gate and the cosine gate agree on what a "claim" is.
    """
    return len(sentence.strip()) > _MIN_CLAIM_CHARS


def parse_label(text):
    """Map a raw model reply to one of SUPPORTED / NOT_SUPPORTED / CONTRADICTED.

    Robust by design: the 2B does not always emit a bare token. It may write
    "not_supported.", "The answer is CONTRADICTED", or add a trailing clause. So the parse
    is case-insensitive and scans for the FIRST recognisable label rather than trusting
    position -- and it checks the two FAILURE labels before SUPPORTED, because the string
    "SUPPORTED" is a substring of "NOT_SUPPORTED" and would otherwise mask it.

    Fails CLOSED: anything unrecognised is treated as NOT_SUPPORTED. For a grounding gate,
    an answer the judge could not clearly bless is not one to trust.
    """
    up = (text or "").upper()
    # NOT_SUPPORTED first: it contains "SUPPORTED", and "UNSUPPORTED" is a common variant.
    if re.search(r"NOT[\s_]*SUPPORTED", up) or "UNSUPPORTED" in up:
        return "NOT_SUPPORTED"
    if "CONTRADICT" in up:
        return "CONTRADICTED"
    if "SUPPORTED" in up:
        return "SUPPORTED"
    return "NOT_SUPPORTED"


class GroundingVerifier:
    """Directional support check for a single claim against retrieved passages.

    Two interchangeable backends, chosen at construction:

      * 2B (default, zero new memory): `ask_model(prompt) -> str` is an injected call to the
        resident generator. verify() builds the strict grounded-NLI prompt, asks once, and
        maps the label to support. This is what ships, because HHEM does not fit in the
        device's free memory alongside the models already resident.

      * HHEM (optional upgrade): if `hhem_url` is set, verify() POSTs the claim and passages
        to scripts/verifier_server.py and returns its `support` float instead. The 2B call
        is never made in this mode.

    tau is stored for reference/defaults; the pass/fail decision lives in gate_answer, which
    takes tau explicitly so the operator can tune it without reconstructing the verifier.
    """

    def __init__(self, ask_model=None, hhem_url=None, tau=0.5):
        self.ask_model = ask_model
        self.hhem_url = hhem_url
        self.tau = tau

    def verify(self, claim, spans):
        """Return support for `claim` given `spans` (retrieved passage texts), in [0,1].

        HHEM backend: POST and return its float. 2B backend: one strict grounded-NLI
        generation, label mapped to 1.0 (SUPPORTED) or 0.0 (NOT_SUPPORTED / CONTRADICTED).
        """
        if self.hhem_url:
            return self._verify_hhem(claim, spans)
        if self.ask_model is None:
            raise RuntimeError(
                "GroundingVerifier has neither an hhem_url nor an ask_model callable; one "
                "backend must be configured (the 2B ask_model is the shipped default).")
        prompt = self._build_prompt(claim, spans)
        reply = self.ask_model(prompt)
        return _LABEL_SCORES[parse_label(reply)]

    @staticmethod
    def _build_prompt(claim, spans):
        """The one strict grounded-NLI prompt, with passages numbered [1], [2], ...

        Numbering mirrors the SOURCES block the generator already answers from, so the judge
        sees the passages in the same shape the drafter did.
        """
        if spans:
            passages = "\n".join(f"[{i}] {s}" for i, s in enumerate(spans, 1))
        else:
            passages = "(none)"
        return _GROUNDED_NLI_PROMPT.format(passages=passages, claim=claim)

    def _verify_hhem(self, claim, spans):
        """POST to the optional HHEM verifier server; return its support float.

        Stdlib urllib only -- no dependency on the shared http_retry helper, so this leaf
        module stays importable wherever push.sh drops it. The server is a local, optional
        upgrade; a hard failure here should surface, not be silently swallowed.
        """
        body = json.dumps({"claim": claim, "spans": list(spans)}).encode()
        req = urllib.request.Request(
            self.hhem_url, data=body,
            headers={"Content-Type": "application/json", "Connection": "close"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        return float(data["support"])


def gate_answer(answer_sentences, spans, verifier, tau):
    """Decide whether an answer is grounded enough to show, sentence by sentence.

    Verifies every FACTUAL sentence (the ones that would carry a citation) against the
    passages and returns:

        {"refuse": bool, "min_support": float, "unsupported": [{"sentence", "support"}, ...]}

    refuse is True when the load-bearing sentence -- the one with the LOWEST support -- falls
    below tau. That is deliberately conservative: with the 2B backend, support is a hard
    1.0/0.0, so "below tau" fires only on a sentence the judge flatly could not ground
    (well below any reasonable tau), never on a merely lukewarm one. With the continuous
    HHEM backend, tau is the operator-tuned bar. Either way tau is EXPOSED, never hardcoded,
    matching the abstention thresholds in config.

    An answer with no factual sentences (a bare refusal token, fragments) is vacuously
    grounded: refuse False, so the gate never manufactures a refusal out of nothing.

    Note: NOT_SUPPORTED and CONTRADICTED both score 0.0, so a claim the source is SILENT
    about fails this gate exactly like a contradiction -- the point of a directional check.
    """
    factual = [s for s in answer_sentences if is_factual(s)]
    if not factual:
        return {"refuse": False, "min_support": 1.0, "unsupported": []}

    scored = [(s, verifier.verify(s, spans)) for s in factual]
    min_support = min(sup for _, sup in scored)
    unsupported = [{"sentence": s, "support": sup} for s, sup in scored if sup < tau]
    return {"refuse": min_support < tau,
            "min_support": min_support,
            "unsupported": unsupported}
