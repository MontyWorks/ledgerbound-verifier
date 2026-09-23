# The Ledgerbound Standard

How the town re-walks a money claim. Plain language first; the verifier
script in this repository enforces exactly this, on Robinhood Chain.

## The promise

Six things. A claim the town can trust shows all of them:

1. **The wallet.** Which town wallet the money went to.
2. **The move.** What moved, how much, and in which direction.
3. **The size.** The amount, written out, at a stated precision.
4. **The transaction hash.** The one the money actually moved in.
5. **The block.** The block the transaction was mined in.
6. **The re-walk.** Someone independent replaying the evidence and
   publishing the result, win or lose.

## The walk

To re-walk a claim:

1. Read the claim's transaction hash, block, wallet, and expected
   movements.
2. Pull the receipt for the hash from a public RPC endpoint.
3. Confirm the transaction succeeded (receipt status is success).
4. Confirm the receipt sits in the block the claim states. A receipt in
   any other block is not the claimed transaction.
5. Replay every Transfer event in the receipt. For the claimed wallet,
   sum the amounts arriving (in) and the amounts leaving (out),
   per token contract. Show who sent to the wallet and who the wallet
   sent to.
6. Read `decimals()` off each token contract's current state. Never
   assume decimals; never trust the claim's decimals. The public RPC
   does not serve historical state, so the read is against the latest
   block, not the claim's block.
7. Round the summed amounts to the precision the claim states, and
   compare exactly. 21074053.60 at two decimals matches a sum of
   21074053.59811814423918164. It does not match at three decimals.
8. Check for movements the claim does not state. Any token arriving at
   or leaving the wallet in that transaction, beyond what the claim
   lists, is a contradiction.
9. Reject a malformed claim before it reaches the chain: no hash, no
   block, no wallet, no expected movements, an unknown direction, a zero
   or negative amount, or the same contract listed twice for the same
   direction.

## Claim format

```json
{
  "label": "Unshackled town chest, claim #4",
  "tx_hash": "0x28a5...",
  "block": 67708892,
  "destination": "0xd96c...",
  "expected": [
    {"contract": "0x91a2...", "direction": "in", "amount": "21074053.60"}
  ]
}
```

`direction` is `"in"` (tokens arriving at the destination) or `"out"`
(tokens leaving it). It defaults to `"in"` when omitted. `amount` must
be a finite, positive decimal string; zero, negative, or non-finite
amounts (`NaN`, `Infinity`) are malformed claims, not matches. A
contract may appear at most once per direction; appearing twice is a
malformed claim.

## What MATCH proves

A MATCH proves exactly this, and nothing more:

1. the transaction exists and succeeded;
2. the receipt sits in the block the claim states;
3. every expected movement happened in the stated direction, in the
   stated amount, at the stated precision, with decimals read off the
   token contract;
4. no other token movements into or out of the wallet happened in that
   transaction beyond what the claim states;
5. the claim itself was well formed.

## The three verdicts

- **MATCH.** The claim agrees with the chain on every point above.
- **MISMATCH.** The chain contradicts the claim: a failed transaction,
  a different block, a different amount, a missing token, or an
  unstated movement. Say exactly what differs.
- **CANNOT VERIFY YET.** The evidence is missing or unreadable: no
  hash, no block, no receipt yet, a contract that cannot be read, a
  malformed claim. This is not a no. It means: come back with the
  missing piece.

## Worked example: claim #4

The claim states 21,074,053.60 $musebook and 2.8325 META arrived at the
town wallet in transaction
`0x28a5cb26c8b6b4a6de21fa327343c95d96bc8f479b8442be8c85945f114c2f73`,
block 67708892.

The re-walk: the receipt exists and succeeded, and it sits in block
67708892, matching the claim. Replay of the Transfer events shows both
tokens arriving at the wallet from the fee-splitting hop
`0x4e3468951d49f2eea976ed0d6e75ffcb44a9a544`, and nothing leaving the
wallet in that transaction. Decimals read off both contracts are 18.
Summed and rounded to the claimed precision, the amounts are exactly
21,074,053.60 and 2.8325.

**MATCH**, at the filed precision.

## What this does not do

The verifier never holds keys, never signs, never moves funds, and
never posts anywhere. It reads public RPC data and reports what it
finds. A MATCH is a statement about one transaction, not a character
reference for anyone involved. Token decimals are read from each
contract's current state, not the state at the claim's block, because
the public RPC does not serve historical state.
