"""Optional Vietnamese speech frontend with small technical-token rules."""

import re


_DIGITS = ("không", "một", "hai", "ba", "bốn", "năm", "sáu", "bảy", "tám", "chín")
_TECHNICAL_WORDS = {
    "ESP32": "e ét pê ba mươi hai",
    "ESP32-S3": "e ét pê ba mươi hai ét ba",
    "I2C": "ai hai xi",
    "I²C": "ai hai xi",
    "ONNX": "ô en en ích",
    "FP32": "ép pi ba mươi hai",
    "FP16": "ép pi mười sáu",
    "INT8": "ai en ti tám",
    "USB-C": "iu ét bi xi",
    "Wi-Fi": "oai fai",
}
_UNITS = {
    "V": "vôn",
    "mV": "mi li vôn",
    "A": "am pe",
    "mA": "mi li am pe",
    "kHz": "ki lô héc",
    "MHz": "mê ga héc",
    "GHz": "gi ga héc",
    "MB": "mê ga bai",
    "GB": "gi ga bai",
}
_PROTECTED = re.compile(
    # Keep internal URL punctuation, but leave sentence punctuation unprotected.
    r"(?P<url>(?<!\w)(?:https?://|www\.)[^\s<>\"']*[^\s<>\"'.,;:!?…)\]}])"
    r"|(?P<email>(?<![\w.+-])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)"
    r"|(?P<ip>(?<![\w.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?!\w|\.[0-9]))"
    r"|(?P<version>(?<![\w.])v?[0-9]+\.[0-9]+\.[0-9]+"
    r"(?:-[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?"
    r"(?:\+[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?(?!\w|\.[0-9]))"
    r"|(?P<technical>(?<!\w)(?:"
    + "|".join(
        re.escape(word) for word in sorted(_TECHNICAL_WORDS, key=len, reverse=True)
    )
    + r"|GPIO[0-9]+)(?![\w-]))"
    r"|(?P<quantity>(?<![\w.,])(?P<amount>-?[0-9]+(?:[.,][0-9]+)?)"
    r"\s*(?P<unit>mV|mA|kHz|MHz|GHz|MB|GB|V|A)(?!\w))"
)
_DOT_PERCENT = re.compile(r"(?<![\w.])([0-9]+)\.([0-9]+)(?=\s*%)")


def _read_digits(text: str) -> str:
    return " ".join(_DIGITS[int(char)] for char in text)


class VietnameseTTSNormalizer:
    def __init__(self):
        try:
            from vietnormalizer import VietnameseNormalizer
        except ImportError as error:
            raise RuntimeError(
                "Sherpa text_normalizer=vietnormalizer requires the optional "
                "vietnormalizer package; install vietnormalizer==0.2.3"
            ) from error
        # Avoid guessing pronunciations for unknown developer identifiers.
        self._normalizer = VietnameseNormalizer(enable_transliteration=False)

    def _normalize_plain_text(self, text: str) -> str:
        # Adapt dot-decimal percentages; leave Vietnamese money separators alone.
        text = _DOT_PERCENT.sub(r"\1,\2", text)
        normalized = self._normalizer.normalize(text)
        # The library strips fragments; retain their boundaries when joining.
        if text[:1].isspace():
            normalized = " " + normalized
        if text[-1:].isspace():
            normalized += " "
        return normalized

    def _spoken_token(self, match: re.Match) -> str:
        token = match.group(0)
        category = match.lastgroup
        if category in ("url", "email"):
            # Preserve addresses verbatim instead of deleting or guessing them.
            return token
        if category == "ip":
            parts = token.split(".")
            if any(int(part) > 255 for part in parts):
                return token
            return " chấm ".join(_read_digits(part) for part in parts)
        if category == "version":
            version = token.removeprefix("v")
            if "-" in version or "+" in version:
                return token
            prefix = "phiên bản " if token.startswith("v") else ""
            return prefix + " chấm ".join(
                _read_digits(part) for part in version.split(".")
            )
        if category == "technical":
            if token.startswith("GPIO"):
                return "gi pi ai ô " + self._normalizer.normalize(token[4:])
            return _TECHNICAL_WORDS[token]
        # Engineering units are case-sensitive (MB is not Mb).
        amount = match.group("amount")
        parts = re.split(r"[.,]", amount)
        if len(parts) == 2 and len(parts[1]) == 3:
            # Either separator could indicate a decimal or a thousands group.
            return token
        sign = "âm " if amount.startswith("-") else ""
        spoken = sign + self._normalizer.normalize(parts[0].removeprefix("-"))
        if len(parts) == 2:
            spoken += " phẩy " + _read_digits(parts[1])
        return spoken + " " + _UNITS[match.group("unit")]

    def normalize(self, text: str) -> str:
        # Normalize unprotected spans rather than relying on fragile placeholders.
        chunks = []
        offset = 0
        for match in _PROTECTED.finditer(text):
            chunks.append(self._normalize_plain_text(text[offset:match.start()]))
            chunks.append(self._spoken_token(match))
            offset = match.end()
        chunks.append(self._normalize_plain_text(text[offset:]))
        return re.sub(r"\s+", " ", "".join(chunks)).strip()
