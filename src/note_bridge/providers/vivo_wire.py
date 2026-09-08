"""Independent implementation of the current vivo note web envelope.

Official suite app-modules-note_94b6ccdf.js (production module 3436), 2026-09-05.
Only the vendor's published RSA public key is embedded. Session AES keys are random.
"""

from __future__ import annotations

import base64
import secrets
import struct
import zlib

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad

from ..errors import BridgeError

PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAvDIrvLpx++MEO5xeVy6yUJrYi5zKLdq9MNNvbLGvLgF7YL9VI9gPWPJR2t5vIKVGhL83s/8Mp7/ZQ80TnNzDhaXfAkSOm6nplmar1HFuZryJ5CrY+aW45sLJP764h7i/CEMOj2CpxKgnGAxS8IMur90F2ffhkRLrSCJxolrSS2jFYi3B7NfX/DiOsh5refr6UFiO2emogzmuG2eyCal0P5gmgLZCXLmxeg9SIT+VG2r2wwFHQjittJZFVlDB4505SltBGzNv3aYI4V6JKALvVryxIgWS7KWAiBwxEOJVbEZgseyT44kCSop9wuDWPqxEjRFENROH5zEpp2x8i0sEqQIDAQAB
-----END PUBLIC KEY-----"""


class VivoWire:
    def __init__(self, public_key=PUBLIC_KEY):
        alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        self._key = "".join(secrets.choice(alphabet) for _ in range(16)).encode("ascii")
        self._iv = "".join(secrets.choice(alphabet) for _ in range(16)).encode("ascii")
        wrapped_key = PKCS1_v1_5.new(RSA.import_key(public_key)).encrypt(self._key)
        token = b"com.android.notes"
        fields = (
            struct.pack(">HHHBB", 32, 203, 1, 0x41, len(token))
            + token
            + self._iv
            + struct.pack(">H", len(wrapped_key))
            + wrapped_key
        )
        checksum = struct.pack(">Q", zlib.crc32(fields) & 0xFFFFFFFF)
        self._header = struct.pack(">H", 2 + len(checksum) + len(fields)) + checksum + fields

    def encrypt(self, text: str) -> str:
        payload = AES.new(self._key, AES.MODE_CBC, self._iv).encrypt(pad(text.encode("utf-8"), 16))
        return base64.urlsafe_b64encode(self._header + payload).decode("ascii").rstrip("=")

    def decrypt(self, encoded: str) -> str:
        try:
            raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
            if len(raw) < 36 or raw[12:14] != b"\x00\xcb":
                raise ValueError()
            header_size = struct.unpack(">H", raw[:2])[0]
            token_size = raw[17]
            key_size_offset = 34 + token_size
            key_size = struct.unpack(">H", raw[key_size_offset : key_size_offset + 2])[0]
            if header_size != key_size_offset + 2 + key_size or header_size >= len(raw):
                raise ValueError()
            if struct.unpack(">Q", raw[2:10])[0] != zlib.crc32(raw[10:header_size]) & 0xFFFFFFFF:
                raise ValueError()
            return unpad(AES.new(self._key, AES.MODE_CBC, self._iv).decrypt(raw[header_size:]), 16).decode(
                "utf-8"
            )
        except (ValueError, TypeError, IndexError, struct.error, UnicodeError):
            raise BridgeError("decode_failed", "vivo 正文响应无法正确解码，已停止处理。") from None

    def close(self):
        self._key = self._iv = self._header = b""
