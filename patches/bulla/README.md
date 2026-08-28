# bulla patch plan for quaestor v2 (SEAL-HELD live-trading cells)

Implementation brief against the local clone at `refs/bulla` (crates: `bulla-cli`,
`bulla-core`, vendored `hermit-core`). Line numbers are from the clone as read on
2026-08-29; anchor on the named structs/functions if they have drifted.

Why quaestor needs these three patches, in one sentence each:

1. **`--env`** — get `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` into the cell *without*
   `--nondeterministic` (v0's fixed env strips ALL host vars, and there is no other way in).
2. **`--cpu-s` / `--mem-mb`** — the decision step runs a real Python interpreter;
   the hardcoded 60 CPU-seconds / 2 GiB defaults need to be tunable per cell.
3. **TLS-passthrough egress broker** — today the broker only speaks plaintext
   HTTP/1.0 GET, so `https://paper-api.alpaca.markets` is unreachable from a sealed
   cell; a CONNECT-style byte tunnel keyed by the existing allowlist keeps
   `SEAL HELD` while the cell talks real TLS to Alpaca.

End state: quaestor v2 runs its decision+submission step inside

```
bulla run --work <cell> --out receipts/<id>.json --key receipts/signing.seed \
     --ledger receipts/ledger.jsonl --wall-ms 120000 --cpu-s 90 --mem-mb 1024 \
     --env ALPACA_API_KEY=... --env ALPACA_SECRET_KEY=... \
     --egress-allow paper-api.alpaca.markets:443 --egress-allow data.alpaca.markets:443 \
     -- python3 /work/decide_and_submit.py
```

and the receipt honestly reports SEAL HELD: empty netns, deterministic profile,
every byte in/out of Alpaca hashed into the egress chain.

---

## Patch 1 — repeatable `--env KEY=VAL` on `bulla run`

### Where

- `crates/bulla-cli/src/main.rs`
  - `struct RunArgs` (lines ~55–99): add the flag.
  - `fn cmd_run` (line ~215): parse + pass through.
  - `fn run_in_cell` (lines ~1129–1172): put the pairs into the hermit spec.
  - `fn cmd_eval` / `struct EvalArgs` (lines ~101–144, ~387): same flag, optional
    but cheap — both of its `run_in_cell` calls just forward the same slice.
- `crates/hermit-core/src/lib.rs`
  - `Spec.env: BTreeMap<String, String>` (line ~139) **already exists** — "Extra
    environment variables (layered over the hermetic env, or the inherited one)".
- `crates/hermit-core/src/child.rs`
  - Lines ~266–304: the fixed-env block builds `env`, then
    `for (k, v) in &spec.env { env.retain(|(ek, _)| ek != k); env.push(...) }`
    layers `Spec::env` ON TOP of the fixed profile. **No child change needed** —
    the mechanism is fully wired; only the CLI never exposes it.
  - `hermit-core/src/lib.rs` `fn run_direct` (lines ~482–519) also already applies
    `spec.env` after the optional `env_clear()` — the `--allow-no-sandbox` path is
    covered for free.

### Change

1. `RunArgs`:

   ```rust
   /// Inject KEY=VAL into the cell environment (repeatable). Layered over the
   /// deterministic profile, so the run stays deterministic apart from these
   /// declared inputs. Values are NEVER written into the receipt.
   #[arg(long = "env", value_name = "KEY=VAL")]
   env: Vec<String>,
   ```

2. Parse once in `cmd_run` (fail fast on a malformed pair):

   ```rust
   let env_pairs: Vec<(String, String)> = a.env.iter().map(|s| {
       s.split_once('=')
           .map(|(k, v)| (k.to_string(), v.to_string()))
           .ok_or_else(|| anyhow::anyhow!("--env wants KEY=VAL, got {s:?}"))
   }).collect::<Result<_>>()?;
   ```

3. `run_in_cell` gains a parameter `env: &[(String, String)]`; after
   `let mut spec = hc::Spec::new(argv.to_vec(), work);` insert:

   ```rust
   for (k, v) in env {
       spec.env.insert(k.clone(), v.clone());
   }
   ```

   Update the four call sites (`cmd_run` once, `cmd_eval` twice for solve/grade,
   and pass `&[]` anywhere env injection is not wanted).

### Receipt honesty (required)

Injected env is a *policy-relevant input*: the receipt must disclose that it
happened, without leaking values.

- Extend `bulla-core`'s `EvalPolicy` with `env_keys: Vec<String>` (sorted key
  names only, never values) and populate it in `cmd_run` where `bc::EvalPolicy`
  is built (main.rs lines ~237–247). This flows into `policy_digest`
  (`bc::sha256_hex(&serde_json::to_vec(&policy)?)`, line ~248) automatically, and
  `bulla verify` will show it.
- Do **not** relax `evaluate_seal`: `--env` does not touch `deterministic`,
  `fixed_env`, or the netns, so SEAL stays HELD — that is the whole point.
- Secrets hygiene: values live only in the parent's memory and the child's env;
  they must never appear in events, seal_notes, or the JSON summary. Grep the
  print paths (`print_run_summary`, `print_run_json`, lines ~581–698) when done.

---

## Patch 2 — `--cpu-s` / `--mem-mb` limit flags

### Where

- `crates/hermit-core/src/lib.rs`
  - `struct Limits` (lines ~51–71) with `impl Default` (lines ~73–86):
    `cpu_seconds: Some(60)`, `memory_bytes: Some(2 GiB)`, plus `pids`, `nofile`,
    `fsize_bytes` — all already plumbed, just not settable from the CLI.
  - Enforcement: `RLIMIT_CPU` in `child.rs` (line ~253, `rl(Resource::RLIMIT_CPU,
    l.cpu_seconds, "rlimit:cpu")`); memory via `cgroup::place(pid, &spec.limits)`
    (lib.rs line ~342, cgroup v2 `memory.max`, best-effort — falls back to a
    human-readable reason in `applied.cgroup` when no delegated cgroup exists).
    Note `RLIMIT_AS` stays off by design (ASan reserves ~20 TB VA; lib.rs ~58–60).
- `crates/bulla-cli/src/main.rs`
  - `struct RunArgs` next to `wall_ms` (line ~82); `run_in_cell` where
    `spec.limits.wall` is set (line ~1143).

### Change

1. `RunArgs` (and `EvalArgs` if desired):

   ```rust
   /// CPU-seconds ceiling for the cell (RLIMIT_CPU, soft=hard). Default 60.
   #[arg(long = "cpu-s")]
   cpu_s: Option<u64>,
   /// Memory ceiling in MiB (cgroup v2 memory.max, best-effort). Default 2048.
   #[arg(long = "mem-mb")]
   mem_mb: Option<u64>,
   ```

2. Thread both through `run_in_cell` (same route as patch 1) and apply next to
   the existing wall clamp:

   ```rust
   spec.limits.wall = Duration::from_millis(wall_ms);
   if let Some(s) = cpu_s { spec.limits.cpu_seconds = Some(s); }
   if let Some(mb) = mem_mb { spec.limits.memory_bytes = Some(mb * 1024 * 1024); }
   ```

3. Receipt: `wall_ms` is already inside `EvalPolicy`; add `cpu_s`/`mem_mb`
   (or the resolved `Limits`) alongside it so the digest covers resource policy
   too. The `applied.rlimits` vec + `applied.cgroup` string already record what
   was actually enforced (`ok rlimit:cpu ...` tokens parsed in lib.rs ~433–446).

---

## Patch 3 — TLS-passthrough egress broker (CONNECT-style byte tunnel)

### Current mechanism (what we keep)

- `start_egress_broker` (main.rs ~1393–1420): the broker runs as a **separate
  process** (`bulla egress-broker`, hidden subcommand) so the parent stays
  single-threaded for hermit's `clone` (safety comment at lib.rs ~318–321).
  Socket at `/work/.bulla/egress.sock`, chmod `0600` (finding L1, line ~1450);
  the call log lives in the **host-only state dir** (finding C1).
- `cmd_egress_broker` (main.rs ~1441–1534): serial accept loop; one request line
  `HOST PORT PATH\n`; `allowlist_ok` (line ~1538) pins ports (finding H2 — bare
  host ⇒ 80/443 only); `resolve_and_validate` (line ~1573) resolves ONCE (DNS-
  rebinding pin) and blocks internal IPs incl. v4-mapped (finding N2, `is_internal`
  ~1545–1569); `http_get` (~1593–1614) does plaintext HTTP/1.0 with `RESP_CAP`.
- `collect_egress` (~1423–1436) folds the log into `bc::egress_summary(allow,
  calls)` → allowlist digest + hash-chained `log_head` inside the signed receipt;
  `evaluate_seal` treats mediated egress with an **empty netns** as compatible
  with SEAL HELD (the run flag's doc, main.rs ~91–95: "The netns stays empty (no
  raw egress); every call is hashed into the receipt").

The limitation: `http_get` speaks plaintext only. Alpaca is HTTPS-only, so a
sealed quaestor cell cannot reach it. TLS must terminate **inside the cell**
(the client keeps end-to-end TLS; the broker never sees plaintext) and the
broker becomes a byte pipe with accounting.

### Protocol upgrade

Keep the socket + allowlist + log plumbing; add a second verb on the request line:

```
CONNECT <host> <port>\n        # new: raw byte tunnel after an "OK\n" reply
<host> <port> <path>\n         # legacy plaintext GET (unchanged, for back-compat)
```

Broker handling of `CONNECT` in `cmd_egress_broker`:

1. `allowlist_ok(&a.allow, &host, port)` — unchanged (H2 preserved; quaestor pins
   `paper-api.alpaca.markets:443`).
2. `resolve_and_validate(&host, port)` — unchanged (single resolve, pinned
   `SocketAddr`, internal-IP block). Connect with `TcpStream::connect_timeout`.
3. Reply `OK\n` on the unix stream (or `DENIED ...\n` and log `allowed:false`).
4. Bidirectional pump until either side closes: two loops moving bytes between
   the `UnixStream` and the `TcpStream`, each direction feeding a running
   SHA-256 (`bc::sha256` streaming, or hash chunks into a `Sha256` context) and
   a byte counter. Enforce per-connection ceilings and an idle timeout
   (successors of `REQ_CAP`/`RESP_CAP`, finding M1) — suggested defaults:
   16 MiB per direction, 30 s idle, then shutdown both ends.
5. On close, append one `LoggedCall`:

   ```rust
   bc::LoggedCall {
       host, port,
       path: "(CONNECT)".into(),          // no URL is visible through TLS
       allowed, resolved_ip,
       req_sha256:  hex(client_to_server_digest),
       resp_sha256: hex(server_to_client_digest),
       resp_bytes:  server_to_client_count,
       // NEW field, #[serde(default)] for old receipts:
       // sent_bytes: client_to_server_count,
   }
   ```

   `bc::egress_summary` then chains it exactly like a GET call — the receipt
   carries **who was reached, when, and the digest + size of every byte in and
   out**, without ever seeing plaintext.

### Concurrency

The current loop is serial — fine for one-shot GETs, a deadlock for tunnels
(an open Alpaca keep-alive connection would block the next one). The broker is
already its own process, so the single-thread constraint of the bulla parent
does NOT apply here: `std::thread::spawn` per accepted connection (log writes
behind a `Mutex<File>`, one `writeln!` per completed call keeps lines atomic
enough; or a mpsc channel to a single writer thread).

### In-cell client story

Inside the cell the netns is empty but loopback is UP (`Hermetic.loopback`,
lib.rs ~108–110; `netlink::loopback_up()` in child.rs ~66–68), and
`/work/.bulla/egress.sock` is reachable via the rw work-dir bind. Two options:

- **Shim proxy (recommended):** a tiny static binary (or Python script using
  only stdlib) started inside the cell that listens on `127.0.0.1:3128` speaking
  standard HTTP CONNECT, and forwards each proxied connection through the unix
  socket with the broker's `CONNECT host port\n` preamble. Then any client just
  honors `HTTPS_PROXY=http://127.0.0.1:3128` (httpx, curl, reqwest, alpaca-py's
  urllib3 all do). Delivery: drop the shim into the work dir before the run, or
  bind it read-only via `Spec::ro_binds` (lib.rs ~135) with a new
  `--ro-bind PATH` CLI flag.
- **Direct dial:** clients that can dial a unix socket themselves (httpx
  `transport=httpx.HTTPTransport(uds=...)` cannot inject the preamble line, so
  this needs a per-client wrapper — the shim is less invasive).

quaestor's `decide_and_submit.py` would set `HTTPS_PROXY` and run completely
unmodified `httpx` code against `https://paper-api.alpaca.markets`.

### Security invariants to preserve (from the source's own findings)

| Finding | Where today | Rule for the tunnel |
|---|---|---|
| H2 port pinning | `allowlist_ok` ~1538 | unchanged check before connect |
| DNS rebinding | `resolve_and_validate` ~1573 | resolve once, connect only to the pinned addr |
| Internal-IP block (incl. v4-mapped, N2) | `is_internal` ~1545 | unchanged |
| M1 resource bounds | `REQ_CAP`/`RESP_CAP` ~1459 | per-direction byte ceilings + idle timeout |
| L1 socket perms | chmod 0600 ~1450 | unchanged |
| C1 log integrity | log in host state dir ~1396 | unchanged (never under /work) |

Optional hardening: peek the first client bytes for a TLS ClientHello and check
the SNI equals the allowlisted host before pumping (blocks domain-fronting an
allowed IP with a foreign Host); log `sni_checked: bool` in the call record.

### Seal semantics

No change needed: policy `network` stays `Deny`, netns stays empty, the broker
socket is the only path out, and every call is chained into the receipt —
`evaluate_seal` already accepts this shape as SEAL HELD (that is the design of
`--egress-allow`). The verify output (`cmd_verify` egress block, ~801–821)
renders `(CONNECT)` calls with digests and sizes as-is.

---

## Suggested order & test plan

1. Patch 2 (smallest, no new semantics) → `cargo test -p bulla-cli`, then a cell
   running `python3 -c "while True: pass"` must die at `--cpu-s 2`.
2. Patch 1 → unit-test KEY=VAL parsing; end-to-end: hermetic cell running
   `sh -c 'echo "$FOO"'` with `--env FOO=bar` → stdout hash of `bar\n`, receipt
   `seal_ok:true`, `env_keys:["FOO"]` visible in `bulla verify`, value nowhere.
3. Patch 3 → broker unit tests for the CONNECT line (reuse the existing
   `allowlist_enforces_port` / `is_internal_*` tests at ~1645–1694); integration:
   in-cell `curl --proxy http://127.0.0.1:3128 https://paper-api.alpaca.markets/v2/clock`
   with `--egress-allow paper-api.alpaca.markets:443` → 200, SEAL HELD, one
   `(CONNECT)` call in the receipt; same call WITHOUT the allowlist entry → DENIED
   and `allowed:false` logged.
