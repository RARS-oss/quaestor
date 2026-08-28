"""Live smoke for quaestor.zk against the real zkrisk binary (run inside WSL)."""
from quaestor.zk import ZkProver

p = ZkProver()
proof = p.prove_max_loss(9500.0)
assert proof is not None, "prove failed — is zkrisk built?"
print("commitment:", proof["commitment"][:32], "…")
print("prefix for client_order_id:", ZkProver.commitment_prefix(proof))
assert p.verify(proof), "verify failed on a good proof"
print("verify: OK")

tampered = dict(proof)
tampered["proof"] = ("0" if proof["proof"][0] != "0" else "1") + proof["proof"][1:]
assert not p.verify(tampered), "tampered proof must fail"
print("tampered: correctly rejected")

assert p.prove_max_loss(70000.0) is None, "over-cap must return None"
print("over-cap: correctly None")
print("ZK_SMOKE_OK")
