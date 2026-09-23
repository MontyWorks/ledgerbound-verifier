# ledgerbound-verifier

A small, read-only tool for re-walking treasury money claims on
Robinhood Chain. Given a claim file (transaction hash, block,
destination wallet, expected token movements), it replays the public
chain evidence and reports one verdict: **MATCH**, **MISMATCH**, or
**CANNOT VERIFY YET**.

It was built to serve as the town-chest verifier's instrument: the code
that enforces [the Ledgerbound standard](LEDGERBOUND-STANDARD.md). It is
an independent project, separate from Monty Works and from any token.

## What a MATCH proves

1. The transaction exists and succeeded.
2. The receipt sits in the block the claim states.
3. Every expected movement happened in the stated direction, in the
   stated amount, at the stated precision, with decimals read off the
   token contract's current state.
4. No other token movements into or out of the wallet happened in that
   transaction beyond what the claim states.
5. The claim itself was well formed: a hash, a block, a wallet, at
   least one expected movement, no duplicate contract per direction.

Anything else — a failed transaction, a different block, a different
amount, a missing token, an unstated inflow or outflow — is a
**MISMATCH**, with the exact difference named. Missing or unreadable
evidence (no hash, no block, no receipt yet, an unreadable contract, a
malformed claim) is **CANNOT VERIFY YET**: not a no, just not yet.

## Chain

This release verifies claims on Robinhood Chain only. The RPC endpoint
is fixed to `https://rpc.mainnet.chain.robinhood.com`. The verification
approach works on any EVM chain; other chains would be a later release.

Token decimals are read from each contract's current state (the latest
block), because the public RPC does not serve historical state.

## Quick start

Requirements: Python 3, standard library only. No installs.

```sh
python3 verifier.py claims/claim-004.json
```

The output is a human-readable re-walk followed by the full result as
JSON.

## Claim format

```json
{
  "label": "Unshackled town chest, claim #4",
  "tx_hash": "0x28a5cb26c8b6b4a6de21fa327343c95d96bc8f479b8442be8c85945f114c2f73",
  "block": 67708892,
  "destination": "0xd96c2ccac24d385e32baab3497641d0d6e065ec2",
  "expected": [
    {"contract": "0x91a2dae9699f0b82540b5886b0d8759c22820ba3",
     "direction": "in", "amount": "21074053.60"}
  ]
}
```

- `direction` is `"in"` (tokens arriving at the destination) or `"out"`
  (tokens leaving it); it defaults to `"in"`.
- `amount` is a finite, positive decimal string. The number of decimal
  places you write is the precision the comparison runs at. Zero,
  negative, or non-finite amounts (`NaN`, `Infinity`) are rejected as
  malformed claims.
- A contract may appear at most once per direction. The same contract
  with both `"in"` and `"out"` is allowed (a wallet can receive and
  send the same token in one transaction).

## Tests

```sh
python3 -m unittest discover -s tests
```

Most tests replay synthetic receipts and need no network. One
integration test replays the sample claim #4 against the live Robinhood
Chain RPC; it skips gracefully if the endpoint cannot be reached.

## Safety

The verifier never holds keys, never signs, never moves funds, and
never posts anywhere. It performs read-only JSON-RPC calls against a
public endpoint.

## License

MIT. See [LICENSE](LICENSE).
