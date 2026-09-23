#!/usr/bin/env python3
"""Tests for the Ledgerbound verifier.

Most tests run against synthetic receipts so they need no network. One
integration test replays claim #4 against the live chain; it skips
gracefully if the RPC endpoint cannot be reached.

Run:  python3 -m unittest discover -s tests
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from verifier import (  # noqa: E402
    TRANSFER_TOPIC,
    RpcError,
    check,
    norm,
    rpc_call,
    verify,
)

DEST = "0xd96c2ccac24d385e32baab3497641d0d6e065ec2"
TKA = "0x91a2dae9699f0b82540b5886b0d8759c22820ba3"   # 18 dp in tests
TKB = "0xc0d6457c16cc70d6790dd43521c899c87ce02f35"   # 18 dp in tests
TKC = "0x2222222222222222222222222222222222222222"   # 18 dp in tests
BLOCK = 67708892


def pad_addr(addr):
    return "0x" + addr[2:].rjust(64, "0")


def transfer_log(contract, frm, to, value):
    return {
        "address": contract,
        "topics": [TRANSFER_TOPIC, pad_addr(frm), pad_addr(to)],
        "data": hex(value),
    }


def receipt(logs, status="0x1", block=BLOCK):
    return {"status": status, "blockNumber": hex(block), "logs": logs}


def resolve18(contract):
    if norm(contract) in (TKA, TKB, TKC):
        return 18, "TKN"
    raise RpcError("unreadable contract in test")


def wei(amount_str, decimals=18):
    from decimal import Decimal
    return int(Decimal(amount_str) * (10 ** decimals))


def make_claim(**overrides):
    claim = {
        "label": "test",
        "tx_hash": "0x" + "ab" * 32,
        "block": BLOCK,
        "destination": DEST,
        "expected": [
            {"contract": TKA, "direction": "in", "amount": "100.00"},
        ],
    }
    claim.update(overrides)
    return claim


class VerifierTests(unittest.TestCase):
    # -- happy path -------------------------------------------------
    def test_inflow_match(self):
        rcpt = receipt([transfer_log(TKA, TKC, DEST, wei("100.004"))])
        res = check(make_claim(), rcpt, resolve18)
        self.assertEqual(res["verdict"], "MATCH")
        self.assertEqual(res["block_actual"], BLOCK)

    def test_inflow_match_exact(self):
        rcpt = receipt([transfer_log(TKA, TKC, DEST, wei("100.00"))])
        res = check(make_claim(), rcpt, resolve18)
        self.assertEqual(res["verdict"], "MATCH")

    # -- block comparison --------------------------------------------
    def test_block_mismatch(self):
        rcpt = receipt([transfer_log(TKA, TKC, DEST, wei("100.00"))],
                       block=BLOCK + 1)
        res = check(make_claim(), rcpt, resolve18)
        self.assertEqual(res["verdict"], "MISMATCH")
        self.assertIn(str(BLOCK + 1), " ".join(res["notes"]))

    def test_missing_block_in_claim(self):
        claim = make_claim()
        del claim["block"]
        res = verify(claim)
        self.assertEqual(res["verdict"], "CANNOT VERIFY YET")

    # -- outgoing movements -------------------------------------------
    def test_unclaimed_outflow_is_mismatch(self):
        rcpt = receipt([
            transfer_log(TKA, TKC, DEST, wei("100.00")),
            transfer_log(TKA, DEST, TKC, wei("40.00")),   # leaves again
        ])
        res = check(make_claim(), rcpt, resolve18)
        self.assertEqual(res["verdict"], "MISMATCH")
        self.assertTrue(any("outflow" in n for n in res["notes"]))

    def test_claimed_outflow_matches(self):
        claim = make_claim(expected=[
            {"contract": TKA, "direction": "in", "amount": "100.00"},
            {"contract": TKA, "direction": "out", "amount": "40.00"},
        ])
        rcpt = receipt([
            transfer_log(TKA, TKC, DEST, wei("100.00")),
            transfer_log(TKA, DEST, TKC, wei("40.00")),
        ])
        res = check(claim, rcpt, resolve18)
        self.assertEqual(res["verdict"], "MATCH")

    def test_outflow_amount_wrong_is_mismatch(self):
        claim = make_claim(expected=[
            {"contract": TKA, "direction": "in", "amount": "100.00"},
            {"contract": TKA, "direction": "out", "amount": "39.00"},
        ])
        rcpt = receipt([
            transfer_log(TKA, TKC, DEST, wei("100.00")),
            transfer_log(TKA, DEST, TKC, wei("40.00")),
        ])
        res = check(claim, rcpt, resolve18)
        self.assertEqual(res["verdict"], "MISMATCH")

    def test_claimed_outflow_with_nothing_sent_is_mismatch(self):
        claim = make_claim(expected=[
            {"contract": TKA, "direction": "out", "amount": "40.00"},
        ])
        rcpt = receipt([transfer_log(TKA, TKC, DEST, wei("100.00"))])
        res = check(claim, rcpt, resolve18)
        self.assertEqual(res["verdict"], "MISMATCH")

    # -- duplicates ----------------------------------------------------
    def test_duplicate_contract_rejected(self):
        claim = make_claim(expected=[
            {"contract": TKA, "direction": "in", "amount": "60.00"},
            {"contract": TKA, "direction": "in", "amount": "40.00"},
        ])
        rcpt = receipt([transfer_log(TKA, TKC, DEST, wei("100.00"))])
        res = check(claim, rcpt, resolve18)
        self.assertEqual(res["verdict"], "CANNOT VERIFY YET")
        self.assertTrue(any("more than once" in n for n in res["notes"]))

    def test_same_contract_in_and_out_is_allowed(self):
        claim = make_claim(expected=[
            {"contract": TKA, "direction": "in", "amount": "100.00"},
            {"contract": TKA, "direction": "out", "amount": "100.00"},
        ])
        rcpt = receipt([
            transfer_log(TKA, TKC, DEST, wei("100.00")),
            transfer_log(TKA, DEST, TKC, wei("100.00")),
        ])
        res = check(claim, rcpt, resolve18)
        self.assertEqual(res["verdict"], "MATCH")

    def test_unknown_direction_rejected(self):
        claim = make_claim(expected=[
            {"contract": TKA, "direction": "sideways", "amount": "1.00"},
        ])
        res = check(claim, receipt([]), resolve18)
        self.assertEqual(res["verdict"], "CANNOT VERIFY YET")

    def test_zero_amount_rejected(self):
        claim = make_claim(expected=[
            {"contract": TKA, "direction": "in", "amount": "0.00"},
        ])
        res = check(claim, receipt([]), resolve18)
        self.assertEqual(res["verdict"], "CANNOT VERIFY YET")
        self.assertTrue(any("zero or negative" in n for n in res["notes"]))

    def test_negative_amount_rejected(self):
        claim = make_claim(expected=[
            {"contract": TKA, "direction": "in", "amount": "-1.00"},
        ])
        res = check(claim, receipt([]), resolve18)
        self.assertEqual(res["verdict"], "CANNOT VERIFY YET")

    def test_nan_amount_rejected(self):
        claim = make_claim(expected=[
            {"contract": TKA, "direction": "in", "amount": "NaN"},
        ])
        rcpt = receipt([transfer_log(TKA, TKC, DEST, wei("1.00"))])
        res = check(claim, rcpt, resolve18)   # must not crash
        self.assertEqual(res["verdict"], "CANNOT VERIFY YET")
        self.assertTrue(any("finite" in n for n in res["notes"]))

    def test_nan_from_json_literal_rejected(self):
        raw = ('{"label":"t","tx_hash":"0x%s","block":%d,'
               '"destination":"%s","expected":['
               '{"contract":"%s","direction":"in","amount":NaN}]}'
               % ("ab" * 32, BLOCK, DEST, TKA))
        claim = json.loads(raw)   # parses NaN to float('nan')
        res = check(claim, receipt([]), resolve18)   # must not crash
        self.assertEqual(res["verdict"], "CANNOT VERIFY YET")

    def test_infinity_amount_rejected(self):
        claim = make_claim(expected=[
            {"contract": TKA, "direction": "in", "amount": "Infinity"},
        ])
        res = check(claim, receipt([]), resolve18)   # must not crash
        self.assertEqual(res["verdict"], "CANNOT VERIFY YET")

    def test_missing_movement_is_mismatch(self):
        claim = make_claim(expected=[
            {"contract": TKA, "direction": "in", "amount": "50.00"},
        ])
        res = check(claim, receipt([]), resolve18)
        self.assertEqual(res["verdict"], "MISMATCH")

    # -- existing edge cases -------------------------------------------
    def test_tampered_amount_is_mismatch(self):
        claim = make_claim(expected=[
            {"contract": TKA, "direction": "in", "amount": "999.00"},
        ])
        rcpt = receipt([transfer_log(TKA, TKC, DEST, wei("100.00"))])
        res = check(claim, rcpt, resolve18)
        self.assertEqual(res["verdict"], "MISMATCH")

    def test_unclaimed_inflow_is_mismatch(self):
        rcpt = receipt([
            transfer_log(TKA, TKC, DEST, wei("100.00")),
            transfer_log(TKB, TKC, DEST, wei("5.00")),
        ])
        res = check(make_claim(), rcpt, resolve18)
        self.assertEqual(res["verdict"], "MISMATCH")

    def test_failed_transaction_is_mismatch(self):
        rcpt = receipt([transfer_log(TKA, TKC, DEST, wei("100.00"))],
                       status="0x0")
        res = check(make_claim(), rcpt, resolve18)
        self.assertEqual(res["verdict"], "MISMATCH")

    def test_missing_hash_is_not_yet_verifiable(self):
        claim = make_claim(tx_hash="")
        res = verify(claim)
        self.assertEqual(res["verdict"], "CANNOT VERIFY YET")

    def test_unreadable_contract_is_not_yet_verifiable(self):
        claim = make_claim(expected=[
            {"contract": "0x9999999999999999999999999999999999999999",
             "direction": "in", "amount": "1.00"},
        ])
        rcpt = receipt([transfer_log(
            "0x9999999999999999999999999999999999999999",
            TKC, DEST, wei("1.00"))])
        res = check(claim, rcpt, resolve18)
        self.assertEqual(res["verdict"], "CANNOT VERIFY YET")

    def test_precision_follows_the_claim(self):
        # 100.004 rounds to 100.00 at 2dp -> MATCH; to 100.004 at 3dp.
        rcpt = receipt([transfer_log(TKA, TKC, DEST, wei("100.004"))])
        self.assertEqual(
            check(make_claim(), rcpt, resolve18)["verdict"], "MATCH")
        claim3 = make_claim(expected=[
            {"contract": TKA, "direction": "in", "amount": "100.005"},
        ])
        self.assertEqual(
            check(claim3, rcpt, resolve18)["verdict"], "MISMATCH")

    # -- integration: claim #4 against the live chain -------------------
    def test_claim_004_against_live_chain(self):
        try:
            rpc_call("eth_blockNumber", [])
        except RpcError as exc:
            self.skipTest("RPC unreachable: %s" % exc)
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "claims", "claim-004.json")
        with open(path, encoding="utf-8") as fh:
            claim = json.load(fh)
        res = verify(claim)
        self.assertEqual(res["verdict"], "MATCH",
                         "claim #4 did not MATCH: %s" % res["notes"])


if __name__ == "__main__":
    unittest.main()
