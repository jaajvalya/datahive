"""Envelope encryption.

    plaintext --AES-256-GCM--> ciphertext
                    ^
                 DEK (random, per batch, never reused)
                    ^ wrapped by
                 KEK held in the TENANT's KMS

Why per-batch DEKs with a tenant-held KEK:

  * Blast radius - one leaked DEK exposes one batch, not the corpus.
  * Crypto-shredding - revoke the KEK and every batch under it becomes
    permanently unreadable, INCLUDING BACKUPS. This is how "delete my data"
    is satisfied in an object store where real deletion is slow.
  * Separation of duties - you cannot decrypt tenant data without the tenant's
    KMS granting it. That is a sentence you can put in a contract.

Requires: cryptography  (pip install cryptography)
"""
from __future__ import annotations

import abc
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class WrappedKey:
    key_id: str          # KMS key ARN / URI - goes into the audit record
    wrapped_dek: bytes   # DEK encrypted under the KEK
    algorithm: str = "AES-256-GCM"


class KeyProvider(abc.ABC):
    """Wrap/unwrap a data key using the tenant's KMS.

    Implementations: AWS KMS GenerateDataKey/Decrypt, Azure Key Vault
    wrapKey/unwrapKey, GCP KMS encrypt/decrypt, PKCS#11 HSM.
    """

    @abc.abstractmethod
    def generate_dek(self, tenant_id: str) -> tuple[bytes, WrappedKey]:
        """Return (plaintext_dek, wrapped). Plaintext DEK must be zeroized
        by the caller after use and must never be written to disk."""

    @abc.abstractmethod
    def unwrap(self, tenant_id: str, wrapped: WrappedKey) -> bytes: ...


class LocalDevKeyProvider(KeyProvider):
    """FOR LOCAL DEVELOPMENT ONLY. Keeps the KEK in process memory.

    Do not ship this. It defeats the entire point of a tenant-held KEK: with
    the KEK in your own process, you can decrypt tenant data unilaterally, and
    crypto-shredding is not enforceable.
    """

    def __init__(self) -> None:
        self._keks: dict[str, bytes] = {}

    def _kek(self, tenant_id: str) -> bytes:
        return self._keks.setdefault(tenant_id, os.urandom(32))

    def generate_dek(self, tenant_id: str) -> tuple[bytes, WrappedKey]:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        dek = os.urandom(32)
        nonce = os.urandom(12)
        blob = nonce + AESGCM(self._kek(tenant_id)).encrypt(nonce, dek, None)
        return dek, WrappedKey(key_id=f"local://{tenant_id}", wrapped_dek=blob)

    def unwrap(self, tenant_id: str, wrapped: WrappedKey) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        blob = wrapped.wrapped_dek
        return AESGCM(self._kek(tenant_id)).decrypt(blob[:12], blob[12:], None)

    def revoke(self, tenant_id: str) -> None:
        """Crypto-shred: destroy the KEK. Every batch encrypted under it is now
        permanently unreadable, including anything already in backups."""
        self._keks.pop(tenant_id, None)


class AwsKmsKeyProvider(KeyProvider):
    """Sketch of the production shape. `key_arn` is the TENANT's CMK, and the
    grant is on their side - which is exactly the property that makes
    crypto-shredding meaningful."""

    def __init__(self, key_arn: str, client=None):
        self.key_arn = key_arn
        self._client = client   # boto3.client("kms")

    def generate_dek(self, tenant_id: str) -> tuple[bytes, WrappedKey]:
        resp = self._client.generate_data_key(
            KeyId=self.key_arn, KeySpec="AES_256",
            EncryptionContext={"tenant_id": tenant_id})   # bound to the tenant
        return resp["Plaintext"], WrappedKey(key_id=self.key_arn,
                                             wrapped_dek=resp["CiphertextBlob"])

    def unwrap(self, tenant_id: str, wrapped: WrappedKey) -> bytes:
        resp = self._client.decrypt(
            KeyId=self.key_arn, CiphertextBlob=wrapped.wrapped_dek,
            EncryptionContext={"tenant_id": tenant_id})
        return resp["Plaintext"]


def encrypt(plaintext: bytes, dek: bytes, *, aad: bytes = b"") -> bytes:
    """AES-256-GCM. `aad` should bind the ciphertext to its context (tenant,
    batch id) so a ciphertext cannot be replayed into a different batch."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    return nonce + AESGCM(dek).encrypt(nonce, plaintext, aad)


def decrypt(blob: bytes, dek: bytes, *, aad: bytes = b"") -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(dek).decrypt(blob[:12], blob[12:], aad)


def zeroize(buf: bytearray) -> None:
    """Best-effort key material wipe. Python's immutable bytes cannot be wiped,
    so hold DEKs in a bytearray if you intend to clear them."""
    for i in range(len(buf)):
        buf[i] = 0
