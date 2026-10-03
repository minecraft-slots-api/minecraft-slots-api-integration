"""
--------------------

This is a *portfolio / demo* script. It shows the real shape of a
token-to-fiat conversion layer that a Minecraft server web-panel would
talk to when players want to spin slots (Pragmatic Play, Hacksaw, etc.).

It does NOT talk to any real casino provider.
It DOES implement every step you would need in production:
    - token balance stored per Minecraft UUID
    - exchange rate (tokens -> fiat credits)
    - house edge / RTP accounting
    - anti-fraud guards (min/max, cooldown, KYC threshold)
    - session token issued to the front-end
    - ledger of every conversion + every spin (audit trail)

Run:
    python mint_slots_bridge.py

Author: Mint Scripts Studio
Contact: https://t.me/mintscripts
Channel: https://t.me/mintscripts_studio
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List


# --------------------------------------------------------------------------- #
# 1. CONFIG — the knobs a server owner would tune in a real deployment.
# --------------------------------------------------------------------------- #

# Exchange rate: how many in-game tokens buy 1 unit of fiat credit.
# Example: 1000 tokens = 1.00 credit (like 1 USD).
TOKENS_PER_CREDIT = Decimal("1000")

# Minimum and maximum a player may convert in one request.
MIN_CONVERT_TOKENS = Decimal("1000")      # 1.00 credit
MAX_CONVERT_TOKENS = Decimal("500000")    # 500.00 credits

# Anti-fraud: one conversion per player every N seconds.
CONVERT_COOLDOWN_SECONDS = 5

# Compliance: conversions above this size require manual KYC review.
KYC_THRESHOLD_CREDITS = Decimal("300")

# House edge for the demo spin. Real slots use per-game RTP tables.
# 96% RTP  ->  4% edge  ->  expected loss per spin = stake * 0.04
DEMO_RTP = Decimal("0.96")


# --------------------------------------------------------------------------- #
# 2. DATA MODEL — everything a real bridge needs to persist.
# --------------------------------------------------------------------------- #

@dataclass
class Player:
    """One Minecraft player, keyed by their Mojang UUID."""
    uuid: str                      # e.g. "069a79f4-44e9-4726-a5be-fca90e38aaf5"
    username: str                  # e.g. "Notch"
    tokens: Decimal = Decimal("0") # in-game token balance
    credits: Decimal = Decimal("0")# fiat game balance available for slots
    last_convert_ts: float = 0.0   # unix timestamp of last conversion


@dataclass
class LedgerEntry:
    """Immutable audit record. Every money movement writes one row."""
    ts: float
    uuid: str
    kind: str                      # "convert" | "spin" | "payout"
    tokens_delta: Decimal
    credits_delta: Decimal
    note: str = ""


@dataclass
class Session:
    """Short-lived token the web front-end uses to open the slots game."""
    session_id: str
    uuid: str
    credits: Decimal
    issued_at: float
    expires_at: float


# --------------------------------------------------------------------------- #
# 3. STORAGE — in-memory for the demo. Swap for Postgres in production.
# --------------------------------------------------------------------------- #

class Store:
    def __init__(self) -> None:
        self.players: Dict[str, Player] = {}
        self.ledger: List[LedgerEntry] = []
        self.sessions: Dict[str, Session] = {}

    def get_or_create(self, uuid: str, username: str) -> Player:
        if uuid not in self.players:
            # New players start with a demo token grant so the script runs.
            self.players[uuid] = Player(
                uuid=uuid, username=username, tokens=Decimal("250000")
            )
        return self.players[uuid]

    def write(self, entry: LedgerEntry) -> None:
        self.ledger.append(entry)


# --------------------------------------------------------------------------- #
# 4. ERRORS — explicit, so the web-panel can map them to HTTP codes.
# --------------------------------------------------------------------------- #

class BridgeError(Exception):
    """Base error. The panel maps subclasses to HTTP 400/403/429."""

class AmountOutOfRange(BridgeError): pass
class CooldownActive(BridgeError): pass
class InsufficientTokens(BridgeError): pass
class KYCRequired(BridgeError): pass
class InsufficientCredits(BridgeError): pass


# --------------------------------------------------------------------------- #
# 5. BRIDGE — the actual token <-> fiat conversion logic.
# --------------------------------------------------------------------------- #

class SlotsBridge:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ---- 5a. tokens -> fiat credits ------------------------------------- #
    def convert_tokens_to_credits(
        self, uuid: str, username: str, tokens: Decimal
    ) -> Decimal:
        """
        Convert in-game tokens into fiat game credits.

        This is the core of the token economy bridge:
            tokens  --/ TOKENS_PER_CREDIT-->  credits
        """
        player = self.store.get_or_create(uuid, username)

        # Guard 1: sane amounts only.
        if tokens < MIN_CONVERT_TOKENS or tokens > MAX_CONVERT_TOKENS:
            raise AmountOutOfRange(
                f"amount must be between {MIN_CONVERT_TOKENS} and {MAX_CONVERT_TOKENS}"
            )

        # Guard 2: anti-spam cooldown.
        now = time.time()
        if now - player.last_convert_ts < CONVERT_COOLDOWN_SECONDS:
            raise CooldownActive("please wait before converting again")

        # Guard 3: player must actually own the tokens.
        if player.tokens < tokens:
            raise InsufficientTokens(
                f"have {player.tokens}, need {tokens}"
            )

        # Guard 4: compliance — large conversions need KYC.
        credits = (tokens / TOKENS_PER_CREDIT).quantize(
            Decimal("0.01"), rounding=ROUND_DOWN
        )
        if credits >= KYC_THRESHOLD_CREDITS:
            raise KYCRequired(
                f"conversion of {credits} credits requires KYC review"
            )

        # Apply the move atomically.
        player.tokens -= tokens
        player.credits += credits
        player.last_convert_ts = now

        self.store.write(LedgerEntry(
            ts=now, uuid=uuid, kind="convert",
            tokens_delta=-tokens, credits_delta=+credits,
            note=f"rate={TOKENS_PER_CREDIT}",
        ))
        return credits

    # ---- 5b. open a slots session --------------------------------------- #
    def open_slots_session(self, uuid: str, ttl_seconds: int = 300) -> Session:
        """
        Issue a short-lived session token the front-end hands to the slots iframe.
        Real providers expect something like this: a signed, expiring handle
        that maps to a player + balance, so the game never sees your DB.
        """
        player = self.store.players[uuid]
        now = time.time()
        session = Session(
            session_id=secrets.token_urlsafe(24),
            uuid=uuid,
            credits=player.credits,
            issued_at=now,
            expires_at=now + ttl_seconds,
        )
        self.store.sessions[session.session_id] = session
        return session

    # ---- 5c. demo spin (RTP simulation) --------------------------------- #
    def spin(self, uuid: str, stake: Decimal) -> Decimal:
        """
        Simulate one slot spin with a fixed RTP.

        Real flow: the provider returns win/loss; you settle the ledger.
        Demo flow: we roll a deterministic-ish outcome from a hash so the
        script is reproducible and clearly not gambling.
        """
        player = self.store.players[uuid]
        if stake <= 0:
            raise AmountOutOfRange("stake must be > 0")
        if player.credits < stake:
            raise InsufficientCredits(
                f"have {player.credits}, need {stake}"
            )

        # Deterministic pseudo-outcome derived from uuid + time + stake.
        seed = f"{uuid}:{time.time_ns()}:{stake}".encode()
        roll = int(hashlib.sha256(seed).hexdigest(), 16) % 10_000 / 10_000

        # 4% edge: player wins (1/RTP - 1) of the time on average.
        # This is a *demo* — real payouts come from the provider's math model.
        win_multiplier = Decimal("0") if roll > float(DEMO_RTP) else Decimal("2")

        payout = (stake * win_multiplier).quantize(Decimal("0.01"))
        net = payout - stake
        player.credits += net

        self.store.write(LedgerEntry(
            ts=time.time(), uuid=uuid, kind="spin",
            tokens_delta=Decimal("0"), credits_delta=net,
            note=f"stake={stake} payout={payout} roll={roll:.4f}",
        ))
        return payout


# --------------------------------------------------------------------------- #
# 6. DEMO — what a server owner sees when they run the script.
# --------------------------------------------------------------------------- #

def demo() -> None:
    store = Store()
    bridge = SlotsBridge(store)

    # A real Minecraft UUID + username.
    uuid = "069a79f4-44e9-4726-a5be-fca90e38aaf5"
    username = "Notch"

    print("=" * 60)
    print("Mint Scripts — Minecraft Slots API Bridge (demo)")
    print("=" * 60)

    player = store.get_or_create(uuid, username)
    print(f"\n[init] {player.username} ({player.uuid})")
    print(f"       tokens : {player.tokens}")
    print(f"       credits: {player.credits}")

    # Convert 50,000 tokens -> 50.00 credits.
    credits = bridge.convert_tokens_to_credits(uuid, username, Decimal("50000"))
    print(f"\n[convert] 50000 tokens -> {credits} credits")
    print(f"          tokens : {player.tokens}")
    print(f"          credits: {player.credits}")

    # Open a slots session for the front-end.
    session = bridge.open_slots_session(uuid)
    print(f"\n[session] id={session.session_id[:16]}... "
          f"expires_in={int(session.expires_at - session.issued_at)}s")

    # Spin a few times.
    print("\n[spins]")
    for i in range(5):
        payout = bridge.spin(uuid, Decimal("5.00"))
        print(f"  spin {i+1}: stake=5.00 payout={payout} "
              f"balance={player.credits}")

    # Show the audit trail — every row is a real ledger entry.
    print("\n[ledger]")
    for e in store.ledger:
        print(f"  {e.kind:8s} tokens={e.tokens_delta:+} "
              f"credits={e.credits_delta:+}  {e.note}")

    print("\nDone. In production this talks to your web-panel via HTTPS.")
    print("Contact: https://t.me/mintscripts")


if __name__ == "__main__":
    demo()
