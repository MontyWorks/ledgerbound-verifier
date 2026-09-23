#!/usr/bin/env python3
"""
Ledgerbound verifier (v2).

A small, read-only re-walk tool for treasury claims on Robinhood Chain.
Given a claim file (transaction hash, block, destination wallet, expected
token movements), it replays the chain evidence and reports one verdict:

    MATCH             - the claim agrees with the chain on every point below
    MISMATCH          - the chain contradicts the claim
    CANNOT VERIFY YET - the evidence is missing or unreadable; not a no

A MATCH proves exactly this, and nothing more:

  1. the transaction exists and succeeded (receipt status 0x1);
  2. the receipt sits in the block the claim states;
  3. for every expected movement, Transfer events in that direction
     between the transaction and the destination sum to the claimed
     amount, at the claimed precision, with decimals read off the token
     contract's current state (never assumed);
  4. no other token movements into or out of the destination happened
     in that transaction beyond what the claim states;
  5. the claim itself is well formed: a transaction hash, a block number,
     a destination, at least one expected movement, no duplicate
     contract+direction pairs, and no unknown directions.

The verifier never holds keys, never signs, never moves funds, and never
posts anywhere. It only reads public RPC data.

This release is fixed to Robinhood Chain's public RPC. The verification
approach works on any EVM chain; other chains would be a later release.

Standard library only. Usage:

    python3 verifier.py claims/claim-004.json
    python3 -m unittest discover -s tests
"""

import json
import sys
import urllib.request
from decimal import Decimal, ROUND_HALF_UP

RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
# A browser-like user agent: this RPC endpoint answers 403 to the default
# Python user agent on plain GETs/POSTs.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)
DECIMALS_SELECTOR = "0x313ce567"  # decimals()
SYMBOL_SELECTOR = "0x95d89b41"    # symbol()


class RpcError(Exception):
    """The chain could not be read, or a contract call failed."""


def norm(addr):
    """Normalize an address for comparison (case-insensitive)."""
    return (addr or "").strip().lower()


def rpc_call(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    req = urllib.request.Request(
        RPC_URL, data=body,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
    except Exception as exc:
        raise RpcError("rpc request failed: %s" % exc)
    if "error" in payload:
        raise RpcError("rpc error: %s" % payload["error"])
    return payload.get("result")


def read_token_info(contract):
    """Return (decimals, symbol) read off the token contract itself.

    Reads against the latest block: the public RPC does not serve
    historical state, so decimals always come from the contract's
    current state, not the state at the claim's block.
    """
    dec_raw = rpc_call("eth_call",
                       [{"to": contract, "data": DECIMALS_SELECTOR}, "latest"])
    if not dec_raw or len(dec_raw) < 66:
        raise RpcError("decimals() unreadable on %s" % contract)
    decimals = int(dec_raw, 16)
    sym_raw = rpc_call("eth_call",
                       [{"to": contract, "data": SYMBOL_SELECTOR}, "latest"])
    symbol = "?"
    if sym_raw and len(sym_raw) >= 130:
        try:
            ln = int(sym_raw[66:130], 16)
            symbol = bytes.fromhex(sym_raw[130:130 + ln * 2]).decode(
                "utf-8", "replace")
        except Exception:
            symbol = "?"
    return decimals, symbol


def fetch_receipt(tx_hash):
    receipt = rpc_call("eth_getTransactionReceipt", [tx_hash])
    if receipt is None:
        raise LookupError("no receipt found for %s" % tx_hash)
    return receipt


def quantize_to_claimed(amount, claimed_str):
    """Round a Decimal to the precision written in the claim string."""
    claimed = Decimal(claimed_str)
    dp = -claimed.as_tuple().exponent if claimed.as_tuple().exponent < 0 else 0
    quantum = Decimal(1).scaleb(-dp)
    return amount.quantize(quantum, rounding=ROUND_HALF_UP), dp


def scan_movements(receipt, destination):
    """Sum Transfer events touching the destination, split by direction.

    Returns (inflows, outflows, in_from, out_to) mapping contract -> value
    and contract -> set of counterparty addresses. A self-transfer
    (from == to == destination) counts in both directions; it is rare and
    nets to zero, so the literal reading is the honest one.
    """
    dest = norm(destination)
    inflows, outflows = {}, {}
    in_from, out_to = {}, {}
    for log in receipt.get("logs", []):
        topics = log.get("topics", [])
        if not topics or norm(topics[0]) != TRANSFER_TOPIC or len(topics) < 3:
            continue
        contract = norm(log.get("address", ""))
        frm = "0x" + topics[1][-40:]
        to = "0x" + topics[2][-40:]
        try:
            value = int(log.get("data", "0x0"), 16)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        if norm(to) == dest:
            inflows[contract] = inflows.get(contract, 0) + value
            in_from.setdefault(contract, set()).add(frm)
        if norm(frm) == dest:
            outflows[contract] = outflows.get(contract, 0) + value
            out_to.setdefault(contract, set()).add(to)
    return inflows, outflows, in_from, out_to


def result(verdict, claim, notes, tokens=None, block_actual=None):
    return {
        "label": claim.get("label"),
        "verdict": verdict,
        "tx_hash": claim.get("tx_hash"),
        "block_claimed": claim.get("block"),
        "block_actual": block_actual,
        "destination": claim.get("destination"),
        "tokens": tokens or [],
        "notes": notes,
    }


def check(claim, receipt, resolve_fn):
    """Pure verification logic over an already-fetched receipt.

    resolve_fn(contract) -> (decimals, symbol); raises RpcError when the
    contract cannot be read.
    """
    tx_hash = claim["tx_hash"]
    destination = claim["destination"]
    block_claimed = claim["block"]
    expected = claim["expected"]

    # --- claim shape -------------------------------------------------
    normalized = []
    seen = set()
    for entry in expected:
        direction = (entry.get("direction") or "in").strip().lower()
        contract = norm(entry.get("contract", ""))
        if direction not in ("in", "out"):
            return result("CANNOT VERIFY YET", claim,
                          ["claim uses unknown direction %r on %s; "
                           "allowed: 'in', 'out'"
                           % (entry.get("direction"), entry.get("contract"))])
        if not contract:
            return result("CANNOT VERIFY YET", claim,
                          ["claim lists a movement with no contract address"])
        try:
            amount = Decimal(entry.get("amount", ""))
        except Exception:
            return result("CANNOT VERIFY YET", claim,
                          ["claim amount %r is not a number"
                           % entry.get("amount")])
        if not amount.is_finite():
            return result("CANNOT VERIFY YET", claim,
                          ["claim amount %r is not a finite number; "
                           "a claim must state a finite positive movement"
                           % entry.get("amount")])
        if amount <= 0:
            return result(
                "CANNOT VERIFY YET", claim,
                ["claim states a zero or negative amount (%s) for %s; "
                 "a claim must state a positive movement, so this claim is "
                 "malformed" % (entry.get("amount"), entry.get("contract"))])
        key = (contract, direction)
        if key in seen:
            return result(
                "CANNOT VERIFY YET", claim,
                ["claim lists contract %s for direction %r more than once; "
                 "each contract may appear at most once per direction"
                 % (entry.get("contract"), direction)])
        seen.add(key)
        normalized.append((contract, direction, entry.get("amount", ""),
                           entry.get("symbol") or "?"))

    # --- receipt -----------------------------------------------------
    if receipt is None:
        return result("CANNOT VERIFY YET", claim,
                      ["no receipt found for %s; it may not be mined yet"
                       % tx_hash])
    if receipt.get("status") != "0x1":
        return result("MISMATCH", claim,
                      ["transaction failed (receipt status %s)"
                       % receipt.get("status")])
    try:
        block_actual = int(receipt.get("blockNumber", "0x0"), 16)
    except (TypeError, ValueError):
        return result("CANNOT VERIFY YET", claim,
                      ["receipt has no readable block number"])
    if block_actual != block_claimed:
        return result(
            "MISMATCH", claim,
            ["receipt sits in block %d but the claim states block %d"
             % (block_actual, block_claimed)],
            block_actual=block_actual)

    # --- movements ---------------------------------------------------
    inflows, outflows, in_from, out_to = scan_movements(receipt, destination)
    exp_in = {c for c, d, _a, _s in normalized if d == "in"}
    exp_out = {c for c, d, _a, _s in normalized if d == "out"}

    unclaimed = []
    for contract, value in sorted(inflows.items()):
        if contract not in exp_in:
            unclaimed.append("unclaimed inflow of %d wei from %s"
                             % (value, contract))
    for contract, value in sorted(outflows.items()):
        if contract not in exp_out:
            unclaimed.append("unclaimed outflow of %d wei from %s"
                             % (value, contract))
    if unclaimed:
        return result("MISMATCH", claim,
                      ["the destination moved tokens in this transaction "
                       "that the claim does not state:"] + unclaimed,
                      block_actual=block_actual)

    # --- amounts -----------------------------------------------------
    rows = []
    mismatches = []
    for contract, direction, claimed_str, _hint in normalized:
        moved_wei = (inflows if direction == "in" else outflows).get(
            contract, 0)
        try:
            decimals, symbol = resolve_fn(contract)
        except RpcError as exc:
            return result(
                "CANNOT VERIFY YET", claim,
                ["could not read the token contract %s: %s; refusing to "
                 "assume decimals" % (contract, exc)],
                block_actual=block_actual)
        moved = Decimal(moved_wei) / (Decimal(10) ** decimals)
        try:
            rounded, dp = quantize_to_claimed(moved, claimed_str)
        except Exception:
            return result("CANNOT VERIFY YET", claim,
                          ["claim amount %r is not a number" % claimed_str],
                          block_actual=block_actual)
        ok = rounded == Decimal(claimed_str)
        counterparties = sorted(
            (in_from if direction == "in" else out_to).get(contract, set()))
        rows.append({
            "contract": contract,
            "symbol": symbol,
            "direction": direction,
            "decimals": decimals,
            "moved_wei": moved_wei,
            "moved": str(moved),
            "claimed": claimed_str,
            "precision_dp": dp,
            "counterparties": counterparties,
            "match": ok,
        })
        if not ok:
            mismatches.append(
                "%s %s: moved %s but the claim states %s"
                % (direction.upper(), symbol, rounded, claimed_str))
    if mismatches:
        return result("MISMATCH", claim,
                      ["amounts disagree with the chain:"] + mismatches,
                      tokens=rows, block_actual=block_actual)

    notes = ["receipt is in block %d, matching the claim" % block_actual]
    if rows:
        parts = ["%s %s %s" % (r["direction"].upper(), r["moved"], r["symbol"])
                 for r in rows]
        notes.append("movements verified: " + "; ".join(parts))
    notes.append("no other token movements touched the destination "
                 "in this transaction")
    return result("MATCH", claim, notes, tokens=rows,
                  block_actual=block_actual)


def verify(claim):
    """Validate a claim, fetch its receipt, and check it against the chain."""
    tx_hash = (claim.get("tx_hash") or "").strip()
    if not tx_hash:
        return result("CANNOT VERIFY YET", claim,
                      ["the claim states no transaction hash; a claim "
                       "without a hash is not yet verifiable"])
    if not (tx_hash.startswith("0x") and len(tx_hash) == 66):
        return result("CANNOT VERIFY YET", claim,
                      ["the claim's transaction hash is malformed"])
    if not (claim.get("destination") or "").strip():
        return result("CANNOT VERIFY YET", claim,
                      ["the claim states no destination wallet"])
    if not isinstance(claim.get("block"), int):
        return result("CANNOT VERIFY YET", claim,
                      ["the claim states no block number; the receipt block "
                       "cannot be compared without one"])
    expected = claim.get("expected")
    if not expected:
        return result("CANNOT VERIFY YET", claim,
                      ["the claim lists no expected token movements"])
    try:
        receipt = fetch_receipt(tx_hash)
    except LookupError as exc:
        return result("CANNOT VERIFY YET", claim, [str(exc)])
    except RpcError as exc:
        return result("CANNOT VERIFY YET", claim,
                      ["could not read the chain: %s" % exc])
    return check(claim, receipt, read_token_info)


def render(res):
    lines = []
    lines.append("Ledgerbound re-walk: %s" % res["verdict"])
    if res.get("label"):
        lines.append("claim:        %s" % res["label"])
    lines.append("tx:           %s" % res.get("tx_hash"))
    lines.append("block:        claimed %s, receipt %s"
                 % (res.get("block_claimed"), res.get("block_actual")))
    lines.append("destination:  %s" % res.get("destination"))
    for row in res.get("tokens", []):
        arrow = "IN " if row["direction"] == "in" else "OUT"
        lines.append("  %s %-8s (%d dp): moved %s vs claim %s @ %ddp [%s]"
                     % (arrow, row["symbol"], row["decimals"], row["moved"],
                        row["claimed"], row["precision_dp"],
                        "ok" if row["match"] else "DIFFERS"))
        for party in row.get("counterparties", []):
            lines.append("      %s: %s"
                         % ("from" if row["direction"] == "in" else "to",
                            party))
    for note in res.get("notes", []):
        lines.append("note: %s" % note)
    return "\n".join(lines)


def main(argv):
    if len(argv) != 2:
        print("usage: python3 verifier.py <claim.json>", file=sys.stderr)
        return 2
    try:
        with open(argv[1], "r", encoding="utf-8") as fh:
            claim = json.load(fh)
    except Exception as exc:
        print("could not read claim file: %s" % exc, file=sys.stderr)
        return 2
    res = verify(claim)
    print(render(res))
    print(json.dumps(res, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
