# quaestor — сценарий видео / презентации

**Аудитория:** судьи Alpaca (Trading API lead, Chief Brokerage Officer, PM) + lablab.
**Язык озвучки:** английский. Пометки `[ЭКРАН]` — режиссёрские, вслух не читаются.
**Хронометраж:** ~3:41 — 523 слов озвучки при ~150 слов/мин плюс паузы на верификаторе. Ниже основной вариант и 60-секундная нарезка.

Правила, которые нельзя нарушать в озвучке:
- Никаких цифр из `scripts/sim_day.py` — это фейковый брокер, не доходность.
- Живой P&L подставляется 4 сентября в помеченный слот. До этого — не произносить.
- Всё, что заявлено вслух, должно проверяться на экране в этом же видео.

---

## Основной вариант (~3:41)

### 0:00–0:22 · Хук

`[ЭКРАН: чёрный, затем dashboard/index.html]`

> Every trading agent in this hackathon will tell you it had a good week.
> I can't ask you to believe mine — so I built one that doesn't need you to.
> Every single order this agent placed is cryptographically signed, and you can
> check it yourself, offline, in about thirty seconds.

### 0:22–0:51 · Проблема

`[ЭКРАН: alpaca-pitch.html, схема "TODAY — TRUST THE OPERATOR"]`

> Agents already trade on Alpaca's API. None of them can produce a record of what
> they actually did. A screenshot saying "plus forty percent" could be
> cherry-picked, backdated, or invented — the operator's word is the only thing
> tying that claim to the exchange.
>
> That missing piece blocks everything downstream: an agent marketplace,
> copy-trading, compliance-grade logs. You can't fund or audit a strategy on
> reputation alone.

### 0:51–1:18 · Что это

`[ЭКРАН: dashboard/console.html — позиции, циклы]`

> quaestor is a fully autonomous options agent. Every five minutes it reads SPY
> and QQQ chains and greeks, classifies the market regime, and forms an intent —
> defined-risk spreads at the core, catalyst plays for convexity.
>
> Deterministic risk gates then judge that intent before anything reaches the
> exchange: per-trade loss caps, concentration limits, and a daily halt that
> flattens the book.

### 1:18–2:05 · Примитив — сердце презентации

`[ЭКРАН: терминал, вывод scripts/demo_sealed_trade.sh — строки SEAL HELD и egress]`

> Here's what makes it different. The order isn't placed by the agent. It's placed
> from inside a hermetic, no-root sandbox — a sealed cell with no network, no
> filesystem, no ambient credentials.
>
> The cell reaches Alpaca through a mediated tunnel. T-L-S terminates *inside* the
> seal, so the broker never sees the keys, and every call is hash-chained into the
> record. When the cell exits it emits an Ed25519-signed receipt of the exact
> conversation with the exchange — which contract, which price, which response.
>
> Those receipts chain into a tamper-evident ledger, anchored externally. Change
> one number and the signature rejects it. Drop one losing day and the chain
> breaks.

### 2:05–2:41 · Доказательство на экране

`[ЭКРАН: терминал — make verify. Затем dashboard/verifier.html: Verify → зелёное, Tamper → красное]`

> You don't have to take that on faith. This is `make verify` — it re-checks every
> signature and the whole chain, offline, with no credentials.
>
> `[пауза, дать увидеть зелёный результат]`
>
> And the same check compiled to WebAssembly, running in your browser. Paste a
> receipt — verified. Now change a single character.
>
> `[пауза на красный результат]`
>
> Forgery rejected. No server, no trust, no API call.

### 2:41–3:02 · Реплей и риск

`[ЭКРАН: терминал — python -m quaestor replay]`

> The risk gates are provable too. Each decision seals the inputs it was judged on
> *and the policy it was judged under*, so every approve and reject re-derives from
> signed data. And that policy's hash is signed into every receipt — quietly
> loosening the rules mid-week is cryptographically visible.

### 3:02–3:28 · Зачем это Alpaca

`[ЭКРАН: dashboard/marketplace.html — "Alpaca Verified Agents"]`

> This is why it matters beyond one hackathon. Once execution is verifiable, an
> agent's track record becomes an asset instead of a claim — which is exactly what
> a verified-agent marketplace and copy-trading need. It's a trust primitive, and
> it sits on rails Alpaca already has.
>
> I packaged it as `attested-alpaca` — any agent adopts it in a few lines.

### 3:28–3:41 · Финал

`[ЭКРАН: dashboard/track-record.html]`

> `[СЛОТ 4 СЕНТЯБРЯ — одна фраза о фактическом результате недели. Например:
>   "Over the contest week the agent finished at ___ on a fresh hundred-thousand
>   dollar paper account." Ставить только реальную цифру со счёта PA3MOH6DEEEH.]`
>
> Don't trust that number. Verify it.

---

## 60-секундная нарезка (для соцсетей / короткого формата)

> Every agent here will claim a good week. Mine can prove one.
>
> quaestor is an autonomous options agent on Alpaca — but every order it places
> comes from inside a hermetic sealed cell, and comes back as an Ed25519-signed
> receipt of the exact exchange conversation, hash-chained into a tamper-evident
> ledger.
>
> `[ЭКРАН: verifier.html — Verify зелёное, затем Tamper красное]`
>
> Check it yourself: offline, or in your browser. Change one character and the
> forgery is rejected.
>
> Verifiable execution turns an agent's track record from a claim into an asset —
> and that's the primitive an agent marketplace runs on.
>
> Paper trading only. Don't trust the number. Verify it.

---

## Заметки по съёмке

- **Показывай терминал вживую, не скриншотами.** Весь смысл ролика в том, что
  проверка настоящая; заранее отрендеренная картинка убивает тезис.
- **Держи паузы на зелёном и красном результате верификатора** — это два кадра,
  ради которых снимается всё остальное. Не проговаривай их, дай посмотреть.
- **Скажи «paper trading only» вслух.** Это правда, это в правилах, и это снимает
  у судьи единственный неудобный вопрос.
- Не перечисляй технологии списком. Каждое утверждение — с подтверждением на
  экране, иначе оно звучит как маркетинг и работает против главного тезиса.
- Если время поджимает — режь блок 2:15–2:35 (реплей), он самый компактный по
  смыслу. Блок 1:05–1:45 не трогать ни при каких условиях.
