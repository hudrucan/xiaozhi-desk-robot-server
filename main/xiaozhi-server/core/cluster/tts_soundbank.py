"""Pinned, credential-free Soundbank playback before remote segment synthesis."""
import asyncio
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import wave
from pathlib import Path

from core.utils.soundbank_text import normalize_soundbank_text

PROTOCOL = 'xiaozhi-core-soundbank-v1'
MAX_ASSET_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_ASSETS = 4096
OUTPUT_RATE = 16000
MAX_SAMPLES = OUTPUT_RATE * 30
P3_RATES = (8000, 12000, 16000, 24000, 48000)


def fingerprint(entries):
    raw = json.dumps(entries, sort_keys=True, ensure_ascii=False,
                     separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def assets(value):
    for entry in value['entries']:
        yield entry['canonical']
        if entry['optimized'] is not None:
            yield entry['optimized']


def validate(value, revision):
    if (not isinstance(value, dict) or set(value) != {
            'protocol', 'revision', 'root', 'entries', 'fingerprint'}
            or value['protocol'] != PROTOCOL or type(value['revision']) is not int
            or value['revision'] != revision or not isinstance(value['root'], str)
            or not Path(value['root']).is_absolute() or not isinstance(value['entries'], list)
            or len(value['entries']) > MAX_ASSETS):
        raise ValueError('Invalid pinned Soundbank identity')
    for entry in value['entries']:
        if (not isinstance(entry, dict) or set(entry) != {'phrase', 'text', 'canonical', 'optimized'}
                or not isinstance(entry['phrase'], str) or not 0 < len(entry['phrase']) <= 512
                or not normalize_soundbank_text(entry['phrase'])
                or (entry['text'] is not None and (not isinstance(entry['text'], str)
                    or not entry['text'].strip() or len(entry['text']) > 512))):
            raise ValueError('Invalid pinned Soundbank entry')
        for asset, optimized in ((entry['canonical'], False), (entry['optimized'], True)):
            if asset is None and optimized:
                continue
            if (not isinstance(asset, dict) or set(asset) != {'name', 'sha256', 'size', 'sample_rate'}
                    or not isinstance(asset['sha256'], str)
                    or not re.fullmatch('[0-9a-f]{64}', asset['sha256'])
                    or type(asset['size']) is not int or not 0 < asset['size'] <= MAX_ASSET_BYTES
                    or not isinstance(asset['name'], str)
                    or asset['name'] not in {asset['sha256'] + suffix for suffix in ('.p3', '.wav', '.mp3')}
                    or (optimized and not asset['name'].endswith('.p3'))):
                raise ValueError('Invalid pinned Soundbank asset')
            if asset['name'].endswith('.p3'):
                if type(asset['sample_rate']) is not int or asset['sample_rate'] not in P3_RATES:
                    raise ValueError('Invalid pinned Soundbank audio contract')
            elif asset['sample_rate'] is not None:
                raise ValueError('Invalid canonical Soundbank audio contract')
    plan = list(assets(value))
    identities = {}
    for asset in plan:
        previous = identities.setdefault(asset['name'], asset)
        if previous != asset:
            raise ValueError('Conflicting pinned Soundbank asset identity')
    if (len(plan) > MAX_ASSETS or sum(asset['size'] for asset in plan) > MAX_TOTAL_BYTES
            or value['fingerprint'] != fingerprint(value['entries'])):
        raise ValueError('Pinned Soundbank identity exceeds limits or differs')
    return value


def read_asset(root, asset):
    root = Path(root)
    path = root / asset['name']
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('Soundbank cache cannot traverse symlinks')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o027
                or info.st_size != asset['size']):
            raise ValueError('Soundbank cache requires private verified files')
        content = stream.read(asset['size'] + 1)
    if len(content) != asset['size'] or hashlib.sha256(content).hexdigest() != asset['sha256']:
        raise ValueError('Soundbank cache checksum differs')
    return content


def selected(entry):
    optimized = entry['optimized']
    # Match the existing mono/60ms optimized contract for negotiated 16k output.
    if optimized is not None and optimized['sample_rate'] == OUTPUT_RATE:
        return optimized
    return entry['canonical']


def decode_audio(content, asset):
    suffix = Path(asset['name']).suffix
    if suffix == '.p3':
        from core.utils.p3 import decode_opus_from_bytes, load_validated_opus_bytes
        from opuslib_next import Decoder
        packets, _ = decode_opus_from_bytes(content)
        if not 0 < len(packets) <= 500:
            raise ValueError('Soundbank segment exceeds the audio limit')
        packets = load_validated_opus_bytes(content, sample_rate=asset['sample_rate'])
        decoder = Decoder(OUTPUT_RATE, 1)
        pcm = b''.join(decoder.decode(packet, 960) for packet in packets)
    elif suffix == '.wav':
        import audioop
        with wave.open(io.BytesIO(content), 'rb') as stream:
            channels, width, rate, frames = (stream.getnchannels(), stream.getsampwidth(),
                                           stream.getframerate(), stream.getnframes())
            if (channels not in (1, 2) or width not in (1, 2, 3, 4)
                    or not 8000 <= rate <= 48000 or not 0 < frames <= rate * 30
                    or stream.getcomptype() != 'NONE'):
                raise ValueError('Unsupported bounded Soundbank WAV')
            pcm = stream.readframes(frames)
            if len(pcm) != frames * channels * width:
                raise ValueError('Incomplete Soundbank WAV')
        if width == 1:
            pcm = audioop.bias(pcm, 1, -128)  # PCM WAV 8-bit samples are unsigned.
        if width != 2:
            pcm = audioop.lin2lin(pcm, width, 2)
        if channels == 2:
            pcm = audioop.tomono(pcm, 2, .5, .5)
        if rate != OUTPUT_RATE:
            pcm, _ = audioop.ratecv(pcm, 2, 1, rate, OUTPUT_RATE, None)
    else:
        # MP3-only native dependency. Explicit format/pipe protocols prevent
        # embedded playlist paths or URLs from granting file/network access.
        result = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error',
            '-protocol_whitelist', 'pipe', '-f', 'mp3', '-i', 'pipe:0',
            '-t', '30.06', '-ac', '1', '-ar', str(OUTPUT_RATE), '-f', 's16le', 'pipe:1'],
            input=content, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=15, check=True)
        pcm = result.stdout
    if not isinstance(pcm, bytes) or not 0 < len(pcm) <= MAX_SAMPLES * 2 or len(pcm) % 2:
        raise ValueError('Invalid bounded Soundbank PCM')
    return pcm


class SoundbankPlayback:
    def __init__(self, value):
        self.value, self.entries = value, {}
        if value is None:
            return
        for entry in value['entries']:
            # The existing runtime resolves normalized duplicates first-wins.
            self.entries.setdefault(normalize_soundbank_text(entry['phrase']), entry)

    def verify(self):
        try:
            if self.value is not None:
                for asset in assets(self.value):
                    read_asset(self.value['root'], asset)
                for entry in self.entries.values():
                    asset = selected(entry)
                    decode_audio(read_asset(self.value['root'], asset), asset)
        except Exception:
            raise ValueError('Pinned Soundbank cache is unavailable or invalid') from None

    def lookup(self, text):
        return self.entries.get(normalize_soundbank_text(text))

    def decode(self, entry):
        asset = selected(entry)
        return decode_audio(read_asset(self.value['root'], asset), asset)

    async def generate(self, entry):
        # Cancellation joins the owned codec/subprocess work before releasing
        # the turn. A late result can never enter playback after abort/reconnect.
        task = asyncio.create_task(asyncio.to_thread(self.decode, entry))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if task.done() and not task.cancelled():
                task.exception()
            raise
