#!/usr/bin/env python3
"""OPTIONAL HHEM grounding-verifier server -- the upgrade path, not the default.

WHY THIS IS OPTIONAL, AND WHY IT IS A SERVER. The grounding gate needs a DIRECTIONAL
support check (does the source ENTAIL the claim?), because embedding cosine cannot tell a
faithful paraphrase from a confident contradiction -- "six phases" vs "seven phases" is
~0.98 similar and factually opposite (see src/generation/grounding_verifier.py). The best
small model for that job is Vectara's HHEM-2.1-Open (a Flan-T5-base, ~110M params,
Apache-2.0). But the Jetson Orin Nano serves the whole stack in 8GB of shared memory with
the 2B generator, the embedder, and the reranker already resident -- under 100MB free. HHEM
loaded is ~600MB with torch + transformers; it does NOT fit in-process.

So the pipeline's DEFAULT verifier reuses the resident 2B generator and spends zero new
memory (GroundingVerifier's 2B backend). This server is the opt-in upgrade: it runs HHEM in
its OWN process, so its memory is charged separately and it can be started, stopped, or
skipped entirely without touching the pipeline. The pipeline works fully WITHOUT it; set a
`hhem_url` in config to switch the gate over to it (the CONFIG SEAM in grounding_verifier).

REQUIREMENTS (device). Needs torch + transformers installed on the Jetson -- neither is in
requirements-device.txt, because the shipped path does not use them. Expect ~600MB resident
once the model loads; confirm the board has the headroom (and that nothing else you need is
about to be OOM-killed) before enabling this. First run downloads the weights, so the model
must be present locally on the air-gapped device (pre-stage the HF cache); there is no
network on the Jetson at serve time.

WIRE PROTOCOL.
    POST /verify   {"claim": "...", "spans": ["passage 1", "passage 2", ...]}
                -> {"support": 0.0..1.0}
    GET  /health -> {"status": "ok", "model": "..."}

Binds LOOPBACK ONLY (127.0.0.1:8083), never 0.0.0.0 -- same rule as the generator and the
api server in config/default.yaml. stdlib http.server; no web framework, matching the
"serving deps are minimal and pinned" discipline of requirements-device.txt.

    python3 verifier_server.py                # serve on 127.0.0.1:8083
    python3 verifier_server.py --self-test    # load HHEM, score one known pair, exit

The torch/transformers import is GUARDED: this file imports cleanly on a machine without
them (so the repo's tooling can read it), and raises a clear, actionable error only when you
actually try to load the model.
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = 8083
MODEL_NAME = "vectara/hallucination_evaluation_model"

# Loaded lazily by _load_model() so importing this file never pulls in torch/transformers.
_MODEL = None


def _load_model():
    """Load HHEM-2.1-Open once, guarding the heavy imports behind a clear error.

    transformers + torch are NOT device requirements (requirements-device.txt is serving
    only), so a missing import here is expected on any box that has not opted in. Turn it
    into an instruction, not a stack trace.
    """
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    try:
        from transformers import AutoModelForSequenceClassification  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "verifier_server needs torch + transformers, which are NOT installed by "
            "requirements-device.txt (they are ~600MB and the shipped pipeline does not "
            "use them -- it verifies grounding with the resident 2B instead). Install them "
            "on the device only if you are deliberately enabling the HHEM upgrade path, "
            "then pre-stage the model weights for offline use."
        ) from e
    # trust_remote_code: HHEM-2.1-Open ships a small custom head that computes the
    # entailment probability and exposes model.predict([(premise, hypothesis), ...]).
    _MODEL = AutoModelForSequenceClassification.from_pretrained(
        MODEL_NAME, trust_remote_code=True)
    _MODEL.eval()
    return _MODEL


def score(claim, spans):
    """Return HHEM support (probability the claim is grounded) in [0,1].

    HHEM judges one (premise, hypothesis) pair, so we take the STRONGEST support across the
    retrieved passages: a claim is grounded if ANY passage entails it. An empty passage list
    is vacuously ungrounded -> 0.0.
    """
    premises = [s for s in (spans or []) if s and s.strip()]
    if not premises:
        return 0.0
    model = _load_model()
    pairs = [(p, claim) for p in premises]
    scores = model.predict(pairs)
    # model.predict may return a torch tensor or a list; normalise to python floats.
    values = [float(x) for x in scores]
    return max(values) if values else 0.0


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            self._send(200, {"status": "ok", "model": MODEL_NAME})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/verify":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
            support = score(req.get("claim", ""), req.get("spans", []))
            self._send(200, {"support": support})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": str(e)})

    def log_message(self, *args):  # keep the console quiet; systemd captures stdout
        pass


def self_test():
    """Load the model and score one supported / one unsupported pair. Exit non-zero on fail.

    A smoke test that the weights are present and the wiring is right, without needing the
    rest of the stack. Uses the exact contradiction class this gate exists to catch.
    """
    span = "The Marine Corps Planning Process has six phases."
    supported = score("The planning process has six phases.", [span])
    contradicted = score("The planning process has seven phases.", [span])
    print(f"supported  claim -> support={supported:.4f}  (expect high)")
    print(f"contradicted claim -> support={contradicted:.4f}  (expect low)")
    ok = supported > contradicted
    print("SELF-TEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--self-test", action="store_true",
                    help="load the model, score one known pair, and exit")
    args = ap.parse_args()

    if args.self_test:
        raise SystemExit(self_test())

    _load_model()  # fail fast at startup if torch/transformers/weights are missing
    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"HHEM verifier serving on http://{args.host}:{args.port}  (model: {MODEL_NAME})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
