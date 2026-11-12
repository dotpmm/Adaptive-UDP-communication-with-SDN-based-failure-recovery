"""Application-level protocol for the Adaptive UDP service.

Every datagram starts with a fixed 16-byte header (network byte order):

    0       1       2       4               8                16
    +-------+-------+-------+---------------+----------------+
    | type  |  cid  |attempt|      seq      |   send_ts      |  (+ padding)
    +-------+-------+-------+---------------+----------------+
      u8      u8      u16     u32             f64 (epoch secs)

type     : MSG_DATA / MSG_ACK / MSG_PROBE / MSG_PROBE_ACK
cid      : client id (lets one server serve many clients)
attempt  : transmission attempt number of this seq (1 = first try)
seq      : per-client sequence number (DATA/ACK); 0 for probes
send_ts  : sender timestamp; the server echoes it back unchanged so the
           client can compute the RTT of the *specific attempt* that was ACKed
"""
import struct

MSG_DATA = 1
MSG_ACK = 2
MSG_PROBE = 3
MSG_PROBE_ACK = 4
_VALID = (MSG_DATA, MSG_ACK, MSG_PROBE, MSG_PROBE_ACK)

_HDR = struct.Struct('!BBHId')
HEADER_LEN = _HDR.size  # 16


def pack(msg_type, client_id, attempt, seq, ts, payload_len=0):
    return _HDR.pack(msg_type, client_id, attempt, seq, ts) + b'x' * payload_len


def unpack(data):
    """Return (type, client_id, attempt, seq, ts) or raise ValueError."""
    if len(data) < HEADER_LEN:
        raise ValueError('short datagram')
    t, cid, attempt, seq, ts = _HDR.unpack(data[:HEADER_LEN])
    if t not in _VALID:
        raise ValueError('bad message type %d' % t)
    return t, cid, attempt, seq, ts
