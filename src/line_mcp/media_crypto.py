"""LINE media HKDF/AES-CTR with mandatory HMAC verification.

Protocol reference: https://github.com/beeper/line/blob/main/pkg/connector/media.go
Independent implementation; unlike the reference receive helper, authenticates data.
"""
from __future__ import annotations

import base64
import hashlib
import hmac

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def decrypt_media(data: bytes, key_material: str, video: bool = False) -> bytes:
    material = base64.b64decode(key_material, validate=True)
    if len(material) != 32 or len(data) <= 32:
        raise ValueError("Invalid LINE media key or truncated ciphertext")
    derived = HKDF(algorithm=hashes.SHA256(), length=76, salt=None, info=b"FileEncryption").derive(material)
    encrypted, tag = data[:-32], data[-32:]
    authenticated = (b"".join(hashlib.sha256(encrypted[i:i+131072]).digest()
                            for i in range(0, len(encrypted), 131072)) if video else encrypted)
    expected = hmac.new(derived[32:64], authenticated, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("LINE media authentication failed; unsupported cache format or wrong key")
    decryptor = Cipher(algorithms.AES(derived[:32]), modes.CTR(derived[64:] + b"\0" * 4)).decryptor()
    return decryptor.update(encrypted) + decryptor.finalize()
