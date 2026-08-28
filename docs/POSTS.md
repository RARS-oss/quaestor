# Build-in-public посты (X + LinkedIn, теги: @lablabai @AlpacaHQ / lablab.ai + Alpaca)

До 5 постов идут в сабмишн; соц-приз $500 судят по качеству И охвату.
План: №1 сегодня (суббота), №2 в вс (архитектура), №3 пн (первый торговый день),
№4 ср (промежуточный P&L + квитанции), №5 чт-пт (итоги + NFP-финал).

---

## Пост №1 — "старт" (EN, X)

Building for the @AlpacaHQ AI Trading Agents Hackathon by @lablabai:

an autonomous options agent where every decision is a cryptographically signed receipt.

Most agents say "trust our risk management."
Mine says: verify it yourself — offline, one command, no credentials.

Stack: my own pre-existing OSS sandboxes (bulla: Ed25519-sealed hermetic cells,
sbx: typed-verdict executor) + Alpaca's Trading API, CLI and MCP server.
Options-native: multi-leg spreads, ZK Bulletproofs proving every order's
worst-case loss is under a hard cap — without revealing position size.

Day 1: 140 unit tests green, first mleg spread accepted by the paper API,
first SEAL HELD receipt signed and verified. Agent goes live Monday 9:30 ET.

#AITradingAgents #buildinpublic

---

## Пост №1 — вариант LinkedIn (чуть длиннее, добавить скрин)

Прикрепить: скрин `bulla verify` (SEAL HELD, signature ok) или `pytest: 140 passed`.

This week I'm competing in the Alpaca AI Trading Agents Hackathon (lablab.ai).

The idea: everyone will demo an AI that trades. Almost nobody can prove what
their agent actually did. So I'm building quaestor — an autonomous options
agent whose every decision cycle is sealed in a hermetic sandbox and emitted
as an Ed25519-signed receipt, hash-chained into a tamper-evident ledger.
Judges (or anyone) can verify the whole trading week offline with one command.

It stands on two research projects I open-sourced before the event — bulla
(verifiable execution receipts) and sbx (typed feedback sandbox for coding
agents) — now pointed at the highest-stakes agent use case I know: money.

Day 1 status: full pipeline working — market data → deterministic risk gates →
multi-leg options orders on Alpaca's paper API → signed SEAL HELD receipts →
zero-knowledge proofs that each order respects the risk cap.

Autonomous trading starts Monday at the open. Building in public all week.

@lablab.ai @Alpaca

---

## Заготовки фраз для следующих постов

- "The agent refused its own trade today: risk gate X fired, receipt #N shows
  the rejection — discipline you can cryptographically audit."
- "Tamper Playground is live: edit my receipt, watch the signature fail."
- "NFP morning playbook: straddle at 9:30, flat by 10:30, every step sealed."
- Скрины: equity curve из дашборда, order_log.csv, bulla log цепочка.
