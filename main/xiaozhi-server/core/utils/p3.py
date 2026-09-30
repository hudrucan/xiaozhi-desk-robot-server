import io
import struct
from collections.abc import Callable, Iterable


P3_HEADER = struct.Struct(">BBH")
P3_FRAME_DURATION_MS = 60


def _read_packets(stream, *, allow_empty=True):
    """Read every packet before exposing any data to a caller."""
    packets = []
    while True:
        header = stream.read(P3_HEADER.size)
        if not header:
            break
        if len(header) != P3_HEADER.size:
            raise ValueError("Incomplete p3 packet header")

        _, _, data_len = P3_HEADER.unpack(header)
        if data_len <= 0:
            raise ValueError("Invalid empty p3 Opus packet")

        opus_data = stream.read(data_len)
        if len(opus_data) != data_len:
            raise ValueError(
                f"P3 packet length mismatch: expected {data_len}, got {len(opus_data)}"
            )
        packets.append(opus_data)

    if not allow_empty and not packets:
        raise ValueError("Empty p3 file")
    return packets


def _duration_seconds(packets):
    return len(packets) * P3_FRAME_DURATION_MS / 1000.0


def decode_opus_from_file(input_file):
    """Return all packetized Opus frames and their nominal duration."""
    with open(input_file, "rb") as stream:
        packets = _read_packets(stream)
    return packets, _duration_seconds(packets)


def decode_opus_from_bytes(input_bytes):
    """Return all packetized Opus frames from an in-memory p3 payload."""
    packets = _read_packets(io.BytesIO(input_bytes))
    return packets, _duration_seconds(packets)


def decode_opus_from_file_stream(input_file, callback: Callable[[bytes], None]):
    """Validate p3 framing completely, then emit packets in file order."""
    packets, _ = decode_opus_from_file(input_file)
    for packet in packets:
        callback(packet)


def decode_opus_from_bytes_stream(
    input_bytes, callback: Callable[[bytes], None]
):
    """Validate in-memory p3 framing completely, then emit its packets."""
    packets, _ = decode_opus_from_bytes(input_bytes)
    for packet in packets:
        callback(packet)


def load_validated_opus_file(
    input_file,
    *,
    sample_rate: int,
    frame_duration_ms: int = P3_FRAME_DURATION_MS,
):
    """Load a non-empty p3 file and validate every Opus frame atomically."""
    with open(input_file, "rb") as stream:
        packets = _read_packets(stream, allow_empty=False)
    validate_opus_packets(
        packets,
        sample_rate=sample_rate,
        frame_duration_ms=frame_duration_ms,
    )
    return packets


def validate_opus_packets(
    packets: Iterable[bytes],
    *,
    sample_rate: int,
    frame_duration_ms: int = P3_FRAME_DURATION_MS,
):
    """Reject invalid Opus packets or packets with an unexpected duration."""
    import opuslib_next
    from opuslib_next.api.decoder import get_nb_samples

    expected_samples = sample_rate * frame_duration_ms // 1000
    if expected_samples <= 0:
        raise ValueError("Invalid Opus sample rate or frame duration")

    decoder = opuslib_next.Decoder(sample_rate, 1)
    for index, packet in enumerate(packets):
        try:
            samples = get_nb_samples(
                decoder.decoder_state,
                packet,
                len(packet),
            )
            if samples != expected_samples:
                raise ValueError(
                    f"Opus packet {index} is {samples} samples; expected {expected_samples}"
                )
            decoder.decode(packet, expected_samples)
        except Exception as error:
            if isinstance(error, ValueError):
                raise
            raise ValueError(f"Invalid Opus packet {index}: {error}") from error
