"""A minimal DTLS 1.2 client that speaks only pre-shared keys.

Written because the library one cannot be made quiet enough. mbedtls offers,
in its ClientHello: the SCSV pseudo-suite alongside the real one, plus
signature_algorithms, encrypt_then_mac, extended_master_secret and
session_ticket. None of that is needed for PSK with AES-GCM, python-mbedtls
exposes no way to turn any of it off, and a bridge that answers a bare
ClientHello while ignoring that one leaves no other lever to pull.

So this offers exactly one cipher suite, TLS_PSK_WITH_AES_128_GCM_SHA256, and
no extensions at all — the smallest thing that can still complete a handshake.

Scope is deliberately narrow. One cipher suite, no renegotiation, no session
resumption, no fragmentation of outgoing flights, no certificate handling.
Entertainment streaming is a single short-lived session on a local network
carrying frames nobody minds losing, so the parts left out are parts that would
never run.
"""
import hashlib
import hmac
import os
import socket
import struct
import time

from mbedtls import cipher

# Content types
CHANGE_CIPHER_SPEC = 20
ALERT = 21
HANDSHAKE = 22
APPLICATION_DATA = 23

# Handshake types
CLIENT_HELLO = 1
SERVER_HELLO = 2
HELLO_VERIFY_REQUEST = 3
SERVER_KEY_EXCHANGE = 12
SERVER_HELLO_DONE = 14
CLIENT_KEY_EXCHANGE = 16
FINISHED = 20

DTLS_1_2 = b"\xfe\xfd"
PSK_AES_128_GCM_SHA256 = b"\x00\xa8"

VERIFY_DATA_LEN = 12
GCM_SALT_LEN = 4          # the "implicit" half of the nonce, from the key block
GCM_EXPLICIT_LEN = 8      # sent with every record


class DtlsError(Exception):
    pass


def prf(secret: bytes, label: bytes, seed: bytes, length: int) -> bytes:
    """TLS 1.2 PRF, which is P_SHA256 over label + seed (RFC 5246 §5)."""
    out, a = b"", label + seed
    while len(out) < length:
        a = hmac.new(secret, a, hashlib.sha256).digest()
        out += hmac.new(secret, a + label + seed, hashlib.sha256).digest()
    return out[:length]


def psk_premaster(psk: bytes) -> bytes:
    """RFC 4279 §2: N zero bytes, then the key, each with a 16-bit length."""
    n = len(psk)
    return struct.pack(">H", n) + bytes(n) + struct.pack(">H", n) + psk


class DtlsPskClient:
    def __init__(self, host: str, port: int, identity: str, psk: bytes,
                 timeout: float = 5.0):
        self.host, self.port = host, port
        self.identity = identity.encode() if isinstance(identity, str) else identity
        self.psk = psk
        self.timeout = timeout
        self._sock = None
        self._epoch = 0
        self._send_seq = 0
        self._msg_seq = 0
        self._handshake = bytearray()   # every handshake message, for Finished
        self._write_key = b""
        self._write_salt = b""

    # ---------- records ----------

    def _record(self, content_type: int, payload: bytes) -> bytes:
        header = struct.pack(">BH", content_type, int.from_bytes(DTLS_1_2, "big"))
        header += struct.pack(">H", self._epoch)
        header += self._send_seq.to_bytes(6, "big")
        header += struct.pack(">H", len(payload))
        self._send_seq += 1
        return header + payload

    def _encrypted_record(self, content_type: int, plaintext: bytes) -> bytes:
        """AES-128-GCM as TLS 1.2 uses it: an 8-byte explicit nonce on the wire,
        prefixed to the ciphertext, and the sequence number in the AAD."""
        seq = self._send_seq
        explicit = seq.to_bytes(GCM_EXPLICIT_LEN, "big")
        nonce = self._write_salt + explicit
        aad = (struct.pack(">H", self._epoch) + seq.to_bytes(6, "big")
               + struct.pack(">B", content_type) + DTLS_1_2
               + struct.pack(">H", len(plaintext)))
        aes = cipher.AES.new(self._write_key, cipher.Mode.GCM, nonce, ad=aad)
        ciphertext, tag = aes.encrypt(plaintext)
        return self._record(content_type, explicit + ciphertext + tag)

    def _handshake_message(self, msg_type: int, body: bytes) -> bytes:
        head = struct.pack(">B", msg_type) + len(body).to_bytes(3, "big")
        head += struct.pack(">H", self._msg_seq)
        head += (0).to_bytes(3, "big") + len(body).to_bytes(3, "big")
        self._msg_seq += 1
        message = head + body
        self._handshake += message
        return message

    # ---------- handshake pieces ----------

    def _client_hello_body(self, cookie: bytes) -> bytes:
        body = DTLS_1_2 + self.client_random
        body += b"\x00"                                   # no session id
        body += bytes([len(cookie)]) + cookie
        body += struct.pack(">H", 2) + PSK_AES_128_GCM_SHA256
        body += b"\x01\x00"                               # null compression only
        body += b"\x00\x00"                               # and no extensions
        return body

    def _read(self) -> bytes:
        return self._sock.recv(4096)

    @staticmethod
    def _split_records(datagram: bytes):
        """A datagram can carry several records; a record can carry several
        handshake messages. Both happen in a normal server flight."""
        out, i = [], 0
        while i + 13 <= len(datagram):
            content_type = datagram[i]
            length = int.from_bytes(datagram[i + 11:i + 13], "big")
            out.append((content_type, datagram[i + 13:i + 13 + length]))
            i += 13 + length
        return out

    @staticmethod
    def _split_handshakes(payload: bytes):
        out, i = [], 0
        while i + 12 <= len(payload):
            msg_type = payload[i]
            length = int.from_bytes(payload[i + 1:i + 4], "big")
            out.append((msg_type, payload[i:i + 12 + length], payload[i + 12:i + 12 + length]))
            i += 12 + length
        return out

    def connect(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(self.timeout)
        self._sock.connect((self.host, self.port))
        self.client_random = struct.pack(">I", int(time.time())) + os.urandom(28)

        # Flight 1: ClientHello with no cookie. The reply is a
        # HelloVerifyRequest carrying one, which has to come back in flight 2.
        self._sock.send(self._record(HANDSHAKE,
                                     self._handshake_message(CLIENT_HELLO,
                                                             self._client_hello_body(b""))))
        cookie = None
        for _ in range(3):
            for content_type, payload in self._split_records(self._read()):
                if content_type != HANDSHAKE:
                    continue
                for msg_type, _raw, body in self._split_handshakes(payload):
                    if msg_type == HELLO_VERIFY_REQUEST:
                        cookie = body[3:3 + body[2]]
            if cookie is not None:
                break
        if cookie is None:
            raise DtlsError("the bridge never sent a HelloVerifyRequest")

        # The cookie exchange is excluded from the Finished hash: both hellos
        # are replaced by the second one alone (RFC 6347 4.2.1), and the second
        # ClientHello carries message_seq 1 (4.2.2).
        #
        # The record sequence number is emphatically NOT reset with them. It
        # counts records within an epoch, and the server keeps an anti-replay
        # window over it: a record arriving on a number already seen in epoch 0
        # is dropped as a replay, with no alert and no reply. Resending the
        # cookie at record 0 therefore looked exactly like a bridge that
        # answers the first ClientHello and then goes silent forever.
        self._handshake = bytearray()
        self._msg_seq = 1
        self._sock.send(self._record(
            HANDSHAKE, self._handshake_message(CLIENT_HELLO, self._client_hello_body(cookie))))

        server_random, done = None, False
        deadline = time.monotonic() + self.timeout
        while not done and time.monotonic() < deadline:
            try:
                datagram = self._read()
            except TimeoutError:
                # Falls through to the ServerHello check below, so the reason
                # this failed is stated rather than surfacing as a bare socket
                # timeout that names neither the flight nor the expectation.
                break
            for content_type, payload in self._split_records(datagram):
                if content_type == ALERT:
                    raise DtlsError(f"the bridge sent alert {payload[1] if len(payload) > 1 else '?'}")
                if content_type != HANDSHAKE:
                    continue
                for msg_type, raw, body in self._split_handshakes(payload):
                    if msg_type in (SERVER_HELLO, SERVER_KEY_EXCHANGE, SERVER_HELLO_DONE):
                        self._handshake += raw
                    if msg_type == SERVER_HELLO:
                        server_random = body[2:34]
                    elif msg_type == SERVER_HELLO_DONE:
                        done = True
        if server_random is None:
            raise DtlsError(
                "the bridge took our cookie and never sent a ServerHello")

        master = prf(psk_premaster(self.psk), b"master secret",
                     self.client_random + server_random, 48)
        # Only the client's half is ever used: nothing is read back from the
        # bridge once streaming starts.
        key_block = prf(master, b"key expansion", server_random + self.client_random, 40)
        self._write_key = key_block[0:16]
        self._write_salt = key_block[32:32 + GCM_SALT_LEN]

        self._sock.send(self._record(HANDSHAKE, self._handshake_message(
            CLIENT_KEY_EXCHANGE,
            struct.pack(">H", len(self.identity)) + self.identity)))

        self._sock.send(self._record(CHANGE_CIPHER_SPEC, b"\x01"))
        self._epoch += 1
        self._send_seq = 0

        verify = prf(master, b"client finished",
                     hashlib.sha256(bytes(self._handshake)).digest(), VERIFY_DATA_LEN)
        finished = self._handshake_message(FINISHED, verify)
        self._sock.send(self._encrypted_record(HANDSHAKE, finished))

        # Wait for the server to answer. Nothing is read back once streaming
        # starts, so this could be skipped — but skipping it makes a key the
        # bridge rejects look exactly like a successful connection, and the
        # first sign of trouble becomes lights that never change. One round
        # trip, once, buys an honest failure.
        try:
            reply = self._read()
        except OSError as e:
            raise DtlsError(
                "the bridge accepted the handshake but never confirmed it — "
                "usually a client key it does not recognise"
            ) from e
        for content_type, _payload in self._split_records(reply):
            if content_type == ALERT:
                raise DtlsError(
                    "the bridge rejected the handshake — the client key is not one "
                    "it recognises"
                )
        if not reply:
            raise DtlsError("the bridge closed without confirming the handshake")

    def send(self, payload: bytes):
        if self._sock is None:
            raise DtlsError("not connected")
        self._sock.send(self._encrypted_record(APPLICATION_DATA, payload))

    def close(self):
        if self._sock is None:
            return
        try:
            # close_notify, so the bridge frees the session instead of holding
            # it until its own timeout.
            self.send_alert_close()
        except Exception:
            pass
        try:
            self._sock.close()
        finally:
            self._sock = None

    def send_alert_close(self):
        self._sock.send(self._encrypted_record(ALERT, b"\x01\x00"))   # warning, close_notify

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()


def first_flight(identity: str = "lighthue") -> bytes:
    """The opening datagram connect() puts on the wire, built in isolation.

    For comparing against a packet capture. When tcpdump shows a datagram
    leaving and nothing coming back, the next question is whether what left was
    well-formed, and that is answerable only against the actual bytes. The
    cookie is empty because this is the first ClientHello of a handshake — the
    bridge answers it with a HelloVerifyRequest carrying one.
    """
    client = DtlsPskClient("0.0.0.0", 0, identity, b"\x00" * 16)
    client.client_random = struct.pack(">I", int(time.time())) + os.urandom(28)
    return client._record(
        HANDSHAKE,
        client._handshake_message(CLIENT_HELLO, client._client_hello_body(b"")),
    )
